"""Reader view: an article's main text on an aiblinx page, opened in place of
the publisher's page.

The server fetches the page and trafilatura extracts the main text. Its XML
output is rebuilt through a small tag allowlist with every string escaped, so
no publisher markup, script or image reaches the reader's browser.

Nothing about the visit is kept: no read event, no cache of the extracted
text, no log line naming the article — a record of what was opened would be a
reading log, and aiblinx promises no tracking.
"""

from __future__ import annotations

import html
import json
import logging
import re

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


def _blocks(parent: etree._Element) -> list[str]:
    out = []
    for el in parent:
        if el.tag in _BLOCK:
            text = _inline(el).strip()
            if text:
                out.append(f"<{_BLOCK[el.tag]}>{text}</{_BLOCK[el.tag]}>")
        elif el.tag == "head":
            tag = _HEADS.get(el.get("rend") or "", "h3")
            out.append(f"<{tag}>{_inline(el).strip()}</{tag}>")
        elif el.tag == "list":
            tag = "ol" if el.get("rend") == "ol" else "ul"
            items = "".join(f"<li>{_inline(i).strip()}</li>" for i in el if i.tag == "item")
            out.append(f"<{tag}>{items}</{tag}>")
        elif el.tag == "code":
            out.append(f"<pre><code>{html.escape(''.join(el.itertext()))}</code></pre>")
        elif el.tag == "div":
            out.extend(_blocks(el))
    return out


def _is_prose(text: str) -> bool:
    text = text.strip()
    sentences = len(_SENTENCE_END.findall(text))
    return (
        len(text) >= MIN_TEXT_CHARS
        and sentences >= MIN_SENTENCES
        and len(text.split()) <= MAX_WORDS_PER_SENTENCE * sentences
    )


def extract_article(page_html: bytes | str, url: str, title: str | None = None) -> str | None:
    """Safe HTML body of the page's main text, or None when there is too
    little of it to be worth a reader page."""
    xml = trafilatura.extract(
        page_html,
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
    if main is None or not _is_prose(" ".join(main.itertext())):
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
    return "\n".join(_blocks(main))


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
