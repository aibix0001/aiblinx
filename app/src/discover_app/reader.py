"""Reader view: an article's main text on an aiblinx page, opened in place of
the publisher's page.

The server fetches the page and trafilatura extracts the main text. Its XML
output is rebuilt through a small tag allowlist with every string escaped, so
no publisher markup or script reaches the reader's browser. The article's
pictures are shown where they stand in the text, with their captions; like
the title image they load from the publisher without a referrer.

Nothing about the visit is kept: no read event, no log line naming the
article — a record of what was opened would be a reading log, and aiblinx
promises no tracking. The extracted text of every served item is prepared at
cycle time (``pipeline.articles``), read or not, so storing it reveals nothing.
"""

from __future__ import annotations

import html
import json
import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from lxml import etree
from lxml import html as lxml_html

from .pipeline.enrich import PAGE_HEADERS

# Whole articles, not just <head> as in enrich — but still bounded.
_MAX_BYTES = 4 * 1024 * 1024
# Less text than this is a teaser, paywall stub or link page: open the original.
MIN_TEXT_CHARS = 400
# Prose has sentences. A page with no article (a video page, say) makes
# trafilatura fall back to the site navigation: hundreds of words and not one
# full stop. Real articles measured 12-28 words per sentence.
MIN_SENTENCES = 3
MAX_WORDS_PER_SENTENCE = 60
_SENTENCE_END = re.compile(r"[.!?…][\"'»«“”)\]]*(?:\s|$)")

_BLOCK = {"p": "p", "quote": "blockquote"}
_HEADS = {"h1": "h2", "h2": "h2", "h3": "h3"}  # the page title is the only <h1>
_HI = {"#b": "strong", "#i": "em", "#u": "em", "#t": "code"}


# A picture's place in the text: it goes through the extraction as a word
# and comes back as a <figure>.
_TOKEN = re.compile(r"\s*aiblinxfigure(\d+)x\s*")
# Pictures narrower than this are icons, avatars and tracking pixels.
_MIN_IMAGE_PX = 200
_SIZE = re.compile(r"-\d+x\d+(?=\.\w+$)")
_IMAGE_FILE = re.compile(r"\.(jpe?g|png|webp|gif|avif)$", re.I)
# A teaser is a picture, a link and a few lines; an article is longer.
_TEASER_CHARS = 1000
_ASIDE = {"nav", "aside", "footer"}
_WHOLE = {"html", "body", "main", "article"}
# Wrappers that hold a picture and nothing else
_WRAPPERS = {"a", "picture", "span", "noscript"}


class _NoReads(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access records: (client, method, path, http version, status)
        args = record.args
        return not (isinstance(args, tuple) and len(args) > 2 and str(args[2]).startswith("/read/"))


def hide_reads_from_access_log() -> None:
    """Keep reader visits out of the access log: a list of opened articles
    is exactly the reading log aiblinx promises not to keep."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _NoReads) for f in access.filters):
        access.addFilter(_NoReads())


async def fetch_html(url: str, timeout_s: float) -> bytes | None:
    """The page's raw HTML, or None if it is not an HTML page or fails to load.
    Bytes, not text: many pages declare their charset only in a <meta> tag,
    which trafilatura's own encoding detection reads."""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with (
            httpx.AsyncClient(
                headers=PAGE_HEADERS, timeout=timeout_s, follow_redirects=True
            ) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                return None
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) >= _MAX_BYTES:
                    break
            return body
    except httpx.HTTPError:
        return None


def _inline(el: etree._Element) -> str:
    """Escaped text of ``el`` with emphasis kept and every other tag unwrapped."""
    out = [html.escape(el.text or "")]
    for child in el:
        inner = _inline(child)
        if child.tag == "hi" and child.get("rend") in _HI:
            tag = _HI[child.get("rend")]
            inner = f"<{tag}>{inner}</{tag}>"
        elif child.tag == "lb":
            inner = "<br>"
        out.append(inner + html.escape(child.tail or ""))
    return "".join(out)


def _text(el: etree._Element, figures: list[str], out: list[str]) -> str:
    """``_inline`` of ``el``, its pictures moved out in front of the block."""
    text = _inline(el)
    out.extend(figures[int(n)] for n in _TOKEN.findall(text) if int(n) < len(figures))
    return _TOKEN.sub(" ", text).strip()


def _blocks(parent: etree._Element, figures: list[str]) -> list[str]:
    out = []
    for el in parent:
        if el.tag in _BLOCK:
            text = _text(el, figures, out)
            if text:
                out.append(f"<{_BLOCK[el.tag]}>{text}</{_BLOCK[el.tag]}>")
        elif el.tag == "head":
            tag = _HEADS.get(el.get("rend") or "", "h3")
            text = _text(el, figures, out)
            if text:
                out.append(f"<{tag}>{text}</{tag}>")
        elif el.tag == "list":
            tag = "ol" if el.get("rend") == "ol" else "ul"
            items = [_text(i, figures, out) for i in el if i.tag == "item"]
            items = "".join(f"<li>{item}</li>" for item in items if item)
            if items:
                out.append(f"<{tag}>{items}</{tag}>")
        elif el.tag == "code":
            out.append(f"<pre><code>{html.escape(''.join(el.itertext()))}</code></pre>")
        elif el.tag == "div":
            out.extend(_blocks(el, figures))
        # text the page left outside a paragraph, behind a picture
        tail = html.escape((el.tail or "").strip())
        if tail and _TOKEN.fullmatch(el.text or "") and not len(el):
            out.append(f"<p>{tail}</p>")
    return out


def _px(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _file(url: str) -> str:
    """The picture's file name without a size ("a-500x333.jpg" is "a.jpg"):
    the same picture has one name at every size and host."""
    return _SIZE.sub("", urlsplit(url).path.rsplit("/", 1)[-1])


def _image_url(img: etree._Element, base: str) -> str | None:
    """The picture's address; a lazy-loading page keeps it in data-src or
    srcset and puts a placeholder in src."""
    for name in ("src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"):
        value = (img.get(name) or "").strip()
        if "srcset" in name:
            value = (value.split(",")[0].split() or [""])[0]
        if value and not value.startswith("data:"):
            url = urljoin(base, value)
            if url.startswith(("http://", "https://")):
                return url
    return None


def _around(el: etree._Element, levels: int):
    """The containers around ``el``, innermost first, that hold less than
    the whole article."""
    for node in list(el.iterancestors())[:levels]:
        if node.tag in _WHOLE:
            return
        yield node


def _is_small(img: etree._Element) -> bool:
    """An icon, an avatar or a tracking pixel, by its declared size."""
    width, height = _px(img.get("width")), _px(img.get("height"))
    return min(width or _MIN_IMAGE_PX, height or _MIN_IMAGE_PX) < _MIN_IMAGE_PX


def _alone(node: etree._Element) -> bool:
    """Whether ``node`` holds one picture at most (small ones and a
    <noscript> copy aside)."""
    images = (i for i in node.iter("img") if i.getparent().tag != "noscript")
    return sum(1 for i in images if not _is_small(i)) <= 1


def _captions(tree: etree._Element) -> list:
    """The elements that describe a picture: a <figcaption> in a figure with
    a picture, or a class named "caption" next to one. Without its picture
    a caption reads as a sentence about something that is not there, and
    trafilatura repeats it for each copy the page holds."""
    found = []
    for el in tree.xpath(
        "//figcaption | //*[contains(translate(@class, 'CAPTION', 'caption'), 'caption')]"
    ):
        if el.find(".//img") is not None:
            continue
        if el.tag == "figcaption":
            # a quote's figure names its speaker there: that is text
            around = list(el.iterancestors("figure"))[:1]
        else:
            around = _around(el, 3)
        if any(node.xpath(".//img | .//picture | .//video") for node in around):
            found.append(el)
    ids = {id(el) for el in found}
    return [el for el in found if not any(id(a) in ids for a in el.iterancestors())]


def _caption_of(img: etree._Element, captions: list) -> etree._Element | None:
    """The caption ``aria-labelledby`` names, else the one that shares a
    container with this picture alone."""
    label = img.get("aria-labelledby")
    for caption in captions:
        if label and caption.get("id") == label:
            return caption
    for node in _around(img, 4):
        if not _alone(node):
            break
        for caption in captions:
            if any(a is node for a in caption.iterancestors()):
                return caption
    return None


def _is_teaser(img: etree._Element, base: str, captions: list) -> bool:
    """Whether the picture stands for another page: a related story, a logo
    or an advertisement. Such a picture is a link to a page, or shares a
    small container with one; an article's own picture links to its larger
    file, if at all."""
    if any(a.tag in _ASIDE for a in img.iterancestors()):
        return True
    ids = {id(el) for el in captions}
    for node in _around(img, 3):
        if (
            node.tag == "p"
            or not _alone(node)
            or len(" ".join(node.text_content().split())) > _TEASER_CHARS
        ):
            break
        for a in node.iter("a"):
            if any(id(c) in ids for c in a.iterancestors()):
                continue  # a caption links to the photographer
            path = urlsplit(urljoin(base, a.get("href") or "")).path
            if path.strip("/") and not _IMAGE_FILE.search(path):
                return True
    return False


def _take_figures(tree: etree._Element, url: str, hero: str | None) -> list[str]:
    """Take the pictures and their captions out of the page before the text
    is extracted: each picture leaves a token where it stood, which comes
    back as a <figure> in ``_blocks``."""
    captions = _captions(tree)
    figures: list[str] = []
    # the title image stands above the article already
    seen = {_file(hero)} if hero else set()
    for img in list(tree.iter("img")):
        if not any(a is tree for a in img.iterancestors()):
            continue  # went with a wrapper that held two copies of a picture
        src = _image_url(img, url)
        name = _file(src) if src else ""
        caption = _caption_of(img, captions)
        alt = " ".join((img.get("alt") or "").split())
        if (
            not src
            or name in seen
            or name.lower().endswith(".svg")
            or _is_small(img)
            # a picture says what it is: by a caption, a description or its size
            or not (caption is not None or alt or _px(img.get("width")))
            or _is_teaser(img, url, captions)
        ):
            continue
        seen.add(name)
        # its parts joined by a space: the credit is often a block of its own
        text = " ".join(" ".join(caption.itertext()).split()) if caption is not None else ""
        figures.append(
            f'<figure><img src="{html.escape(src, quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}" loading="lazy" '
            'referrerpolicy="no-referrer" onload="checkFig(this)" onerror="dropFig(this)">'
            + (f"<figcaption>{html.escape(text)}</figcaption>" if text else "")
            + "</figure>"
        )
        target = img
        while target.getparent().tag in _WRAPPERS:
            target = target.getparent()
        figure = next(target.iterancestors("figure"), None)
        if figure is not None and _alone(figure):
            target = figure  # trafilatura takes a <figure> for a picture and drops it
        # a <p> may not hold another <p>
        inside_p = any(a.tag == "p" for a in target.iterancestors())
        token = etree.Element("span" if inside_p else "p")
        token.text = f" aiblinxfigure{len(figures) - 1}x "
        token.tail = target.tail
        target.getparent().replace(target, token)
    for caption in captions:
        caption.drop_tree()
    return figures


def _is_prose(text: str) -> bool:
    text = text.strip()
    sentences = len(_SENTENCE_END.findall(text))
    return (
        len(text) >= MIN_TEXT_CHARS
        and sentences >= MIN_SENTENCES
        and len(text.split()) <= MAX_WORDS_PER_SENTENCE * sentences
    )


def extract_article(
    page_html: bytes | str, url: str, title: str | None = None, image: str | None = None
) -> str | None:
    """Safe HTML body of the page's main text, or None when there is too
    little of it to be worth a reader page. ``image`` is the title image the
    reader shows above it, left out of the text."""
    blocks = extract_blocks(page_html, url, title, image)
    return "\n".join(blocks) if blocks is not None else None


def extract_blocks(
    page_html: bytes | str, url: str, title: str | None = None, image: str | None = None
) -> list[str] | None:
    """``extract_article`` as a list of block elements (<p>, <h2>, <ul> ...)."""
    tree = trafilatura.load_html(page_html)
    if tree is None:
        return None
    figures = _take_figures(tree, url, image)
    xml = trafilatura.extract(
        tree,
        url=url,
        output_format="xml",
        include_images=False,
        include_links=False,
        include_tables=False,
        include_comments=False,
        include_formatting=True,
    )
    if not xml:
        return None
    main = etree.fromstring(xml.encode()).find("main")
    if main is None or not _is_prose(_TOKEN.sub(" ", " ".join(main.itertext()))):
        return None
    # Most articles repeat their headline as the first heading.
    first = main[0] if len(main) else None
    if (
        first is not None
        and first.tag == "head"
        and title
        and "".join(first.itertext()).strip().casefold() == title.strip().casefold()
    ):
        main.remove(first)
    return _blocks(main, figures)


# Files a browser plays natively everywhere. HLS (.m3u8) is not among them,
# and iframe players (YouTube & co.) would load their tracking: both open
# the original page instead.
_PLAYABLE = re.compile(r"\.(mp4|m4v|webm)(\?|$)", re.I)
_PLAYABLE_TYPES = {"video/mp4", "video/webm"}


def _first_url(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    return value if isinstance(value, str) and value.startswith(("http://", "https://")) else None


def _video_objects(node):
    if isinstance(node, list):
        for item in node:
            yield from _video_objects(item)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "VideoObject" or (isinstance(kind, list) and "VideoObject" in kind):
            yield node
        for key in ("@graph", "video", "mainEntity", "hasPart"):
            if key in node:
                yield from _video_objects(node[key])


_ISO_DURATION = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


def _duration(value) -> str | None:
    """ISO 8601 "PT2M14S" as "2:14" (or "1:02:03")."""
    match = _ISO_DURATION.match(value) if isinstance(value, str) else None
    if not match or not any(match.groups()):
        return None
    h, m, s = (int(g or 0) for g in match.groups())
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _author(value) -> str | None:
    """Author name, trimmed to name and first affiliation ("Oliver Sallet,
    ARD Berlin, tagesschau, Das Erste, <date>" → "Oliver Sallet, ARD Berlin")."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("name")
    if not isinstance(value, str) or not value.strip():
        return None
    return ", ".join(part.strip() for part in value.split(",")[:2])


def find_video(page_html: bytes | str) -> dict | None:
    """``{"src", "poster", "description", "duration", "author"}`` of the
    page's directly playable video (schema.org VideoObject.contentUrl, else
    og:video of a video type), or None."""
    if isinstance(page_html, bytes):
        # lxml falls back to Latin-1 without a charset tag; most pages are UTF-8
        try:
            page_html = page_html.decode("utf-8")
        except UnicodeDecodeError:
            pass
    try:
        doc = lxml_html.fromstring(page_html)
    except (etree.ParserError, ValueError):
        return None
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content())
        except ValueError:
            continue
        for video in _video_objects(data):
            src = _first_url(video.get("contentUrl"))
            if src and (_PLAYABLE.search(src) or video.get("encodingFormat") in _PLAYABLE_TYPES):
                description = video.get("description")
                return {
                    "src": src,
                    "poster": _first_url(video.get("thumbnailUrl")),
                    "description": description if isinstance(description, str) else None,
                    "duration": _duration(video.get("duration")),
                    "author": _author(video.get("author")),
                }
    meta = {
        el.get("property") or el.get("name"): el.get("content")
        for el in doc.xpath("//meta[@content]")
    }
    src = _first_url(meta.get("og:video:secure_url")) or _first_url(meta.get("og:video"))
    if src and (_PLAYABLE.search(src) or meta.get("og:video:type") in _PLAYABLE_TYPES):
        return {
            "src": src,
            "poster": _first_url(meta.get("og:image")),
            "description": meta.get("og:description"),
            "duration": None,
            "author": None,
        }
    return None


def video_body(video: dict, title: str | None) -> str:
    """Reader body for a video page, under the headline: a short description
    (only when it says more than the headline), the player, then a
    "Video · 2:14 min · author" line."""
    parts = []
    description = (video.get("description") or "").strip()
    if description and description.casefold() != html.unescape(title or "").strip().casefold():
        parts.append(f'<p class="lead">{html.escape(description)}</p>')
    info = ["Video"]
    if video.get("duration"):
        info.append(f"{video['duration']} min")
    if video.get("author"):
        info.append(video["author"])
    poster = f' poster="{html.escape(video["poster"], quote=True)}"' if video["poster"] else ""
    parts.append(
        f'<video class="player" controls preload="none" playsinline{poster} '
        f'src="{html.escape(video["src"], quote=True)}"></video>'
    )
    parts.append(f'<p class="video-info">{html.escape(" · ".join(info))}</p>')
    return "\n".join(parts)
