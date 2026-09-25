"""Plain text, meta tags and images out of feed-entry and page HTML.

Feed contents arrive as HTML fragments (Miniflux keeps the publisher's markup,
tagesschau even wraps it in a literal CDATA section), and cards need plain
text. Regex parsing is deliberate: the inputs are small, only a few well-known
constructs are needed, and a best-effort miss just means no image/summary.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

_CDATA = re.compile(r"<!\[CDATA\[|\]\]>")
_TAG = re.compile(r"<[^>]+>")
_OPEN_TAG_AT_END = re.compile(r"<[^>]*$")  # a snippet cut off inside a tag
_INVISIBLE = re.compile("[​-‍⁠﻿]")
_WS = re.compile(r"\s+")
_IMG = re.compile(r"<img\b[^>]*?\bsrc\s*=\s*[\"']([^\"']+)", re.IGNORECASE)
_META = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"([a-zA-Z_:][-\w:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")

# Card summary: whole sentences until about two phone lines are filled, at
# most three; the card grows rather than cutting a sentence off.
_SUMMARY_MIN_CHARS = 80
_SUMMARY_MAX_SENTENCES = 3
_SUMMARY_HARD_CHARS = 240  # a single run-on sentence beyond this is cut at a word
_SENTENCE_BREAK = re.compile(r"(?:(?<=[.!?…])|(?<=[.!?…][\"“”»)]))\s+(?=[\"„“»(]?[A-ZÄÖÜ0-9])")
_TRAILING_ELLIPSIS = re.compile(r"\s*(\.\.\.|…)[\"“”»)]?$")
_SENTENCE_END = re.compile(r"[.!?…][\"“”»)]?$")


def _decode(fragment: str | None) -> str:
    """Entity-decode until stable: some feeds (tagesschau) deliver their HTML
    escaped a second time, so the markup only appears after decoding."""
    text = fragment or ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return _CDATA.sub("", text)


def strip_html(fragment: str | None) -> str:
    """Plain text of an HTML fragment: entities decoded, tags dropped,
    zero-width characters removed, whitespace collapsed."""
    text = _OPEN_TAG_AT_END.sub("", _TAG.sub(" ", _decode(fragment)))
    return _WS.sub(" ", _INVISIBLE.sub("", text)).strip()


def _http_url(url: str | None, base: str | None = None) -> str | None:
    if not url:
        return None
    url = urljoin(base, html.unescape(url).strip()) if base else html.unescape(url).strip()
    return url if url.startswith(("http://", "https://")) else None


def first_image(fragment: str | None) -> str | None:
    """The first absolute http(s) ``<img src>`` in a fragment, if any."""
    match = _IMG.search(_decode(fragment))
    return _http_url(match.group(1)) if match else None


def page_meta(page: str, base_url: str) -> tuple[str | None, str | None]:
    """``(image_url, description)`` from a page's meta tags.

    Image: ``og:image`` then ``twitter:image``, resolved against ``base_url``.
    Description: ``og:description`` then ``description``.
    """
    meta: dict[str, str] = {}
    for tag in _META.findall(page):
        attrs = {k.lower(): a or b for k, a, b in _ATTR.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs:
            meta.setdefault(key, attrs["content"])
    image = _http_url(meta.get("og:image") or meta.get("twitter:image"), base_url)
    description = strip_html(meta.get("og:description") or meta.get("description")) or None
    return image, description


def _word_cut(text: str, limit: int) -> str:
    """Cut at a word boundary and end with a single ellipsis."""
    text = _TRAILING_ELLIPSIS.sub("", text)
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:–-")
    return text + "…"


def _sentence_summary(text: str, may_be_cut: bool) -> str:
    """Up to three sentences, stopping once the minimum length is reached.

    A trailing ellipsis ("its ...") is a publisher's or search engine's cut
    mark in any text, so that sentence is dropped. A missing end mark only
    counts as a cut in feed snippets; page descriptions are often written
    headline-style without a final period.
    """
    sentences = [s for s in _SENTENCE_BREAK.split(text) if s]
    if sentences and (
        _TRAILING_ELLIPSIS.search(sentences[-1])
        or (may_be_cut and not _SENTENCE_END.search(sentences[-1]))
    ):
        sentences.pop()
    picked: list[str] = []
    for sentence in sentences[:_SUMMARY_MAX_SENTENCES]:
        picked.append(sentence)
        if len(" ".join(picked)) >= _SUMMARY_MIN_CHARS:
            break
    summary = " ".join(picked)
    return (
        summary if len(summary) <= _SUMMARY_HARD_CHARS else _word_cut(summary, _SUMMARY_HARD_CHARS)
    )


def card_summary(snippet: str | None, description: str | None) -> str:
    """The card's summary, built from whole sentences of the feed snippet or
    the page's own description — whichever first fills about two lines, else
    the longer. When neither has a complete sentence, the longer text is cut
    at a word and ends in an ellipsis."""
    texts = [(strip_html(snippet), True), (strip_html(description), False)]
    texts = [(t, cut) for t, cut in texts if t]
    summaries = [_sentence_summary(t, cut) for t, cut in texts]
    for summary in summaries:
        if len(summary) >= _SUMMARY_MIN_CHARS:
            return summary
    best = max(summaries, key=len, default="")
    if best or not texts:
        return best
    return _word_cut(max((t for t, _ in texts), key=len), _SUMMARY_HARD_CHARS)
