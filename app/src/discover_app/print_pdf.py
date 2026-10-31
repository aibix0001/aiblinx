"""The PDF that Save puts into a story's Linkwarden link.

One document: the story (title, address, source, date, card summary and
why-line), the article as the reader shows it with the highlighter marks,
then the discussion turn by turn with its searches. It is printed in the
remote browser (``browser.print_pdf``) from a page built here, which runs the
reader's own mark and Markdown scripts. Too big for Linkwarden, it is printed
again without pictures. Without the browser (or when it fails) only a
discussion can still go, as fpdf2's text PDF (``discussion_pdf``).
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from .browser import PlaywrightError, print_pdf
from .config import Settings
from .discussion_pdf import render_discussion
from .ui import FORMAT_JS, MARK_CSS, MARKS_JS, json_script

log = logging.getLogger(__name__)

_CSS = """\
@page { size: A4; }
body {
  margin: 0; color: #1d1d1f; background: #fff;
  font: 11pt/1.55 "Noto Sans", "Liberation Sans", "DejaVu Sans", Arial, sans-serif;
}
a { color: #1a7a82; overflow-wrap: anywhere; }
.meta { font-size: 9pt; color: #6b6b70; }
h1 { font-size: 20pt; line-height: 1.2; margin: 4pt 0 6pt; }
h2 { font-size: 14pt; margin: 18pt 0 6pt; }
h3 { font-size: 12pt; margin: 14pt 0 4pt; }
.about { margin: 8pt 0 0; padding: 8pt 10pt; border-radius: 6pt; background: #f2f2f4;
  font-size: 9.5pt; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
.about p { margin: 0 0 4pt; }
.about p:last-child { margin: 0; }
img { max-width: 100%; height: auto; }
.hero { display: block; margin: 10pt 0 0; border-radius: 6pt; }
.article { margin-top: 12pt; overflow-wrap: break-word; }
.article p, .article ul, .article ol, .article blockquote, .article pre { margin: 0 0 9pt; }
.article figure { margin: 12pt 0; break-inside: avoid; }
.article figure img { display: block; margin: 0 auto; }
.article figcaption { margin-top: 4pt; font-size: 9pt; color: #6b6b70; }
.article blockquote { padding-left: 10pt; border-left: 2pt solid #d2d2d7; color: #555; }
.article pre, .article .plain {
  font: 8.5pt/1.45 "Liberation Mono", "DejaVu Sans Mono", monospace; white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.article .plain span { display: block; padding-left: calc(var(--h, 0) * 1ch);
  text-indent: calc(var(--h, 0) * -1ch); }
.article .plain span:empty { height: 1.45em; }
.article .diagram svg { display: block; width: 100%; max-width: 420px; margin: 0 auto; }
.article .player { display: none; }
.discussion { break-before: page; }
.turn { margin: 0 0 10pt; }
.who { font-size: 9pt; font-weight: 700; color: #6b6b70; margin-bottom: 2pt; }
.turn.user .text { white-space: pre-wrap; }
.turn p, .turn ul, .turn ol { margin: 0 0 5pt; }
.searches { font-size: 8.5pt; color: #6b6b70; margin-bottom: 4pt; }
.searches a { display: block; color: #6b6b70; }
"""

_NO_IMAGES = "img, figure { display: none !important; }\n"

_SCRIPT = r"""
const kept = JSON.parse(document.getElementById('hl-data').textContent);
const root = document.querySelector('.article');
if (root) hlDraw(root, hlFind(hlText(root), kept)[0]);
const box = document.getElementById('turns');
for (const turn of JSON.parse(document.getElementById('turns-data').textContent)) {
  const el = document.createElement('div');
  const mine = turn.role === 'user';
  el.className = 'turn ' + (mine ? 'user' : 'bot');
  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = mine ? 'You' : turn.by ? 'Second opinion · ' + turn.by : 'aiblinx';
  el.append(who);
  for (const search of turn.searches || []) {
    const row = document.createElement('div');
    row.className = 'searches'; row.textContent = 'Searched: ' + search.query;
    for (const r of search.results || []) {
      const a = document.createElement('a');
      a.href = r.url; a.textContent = r.title || r.url;
      row.append(a);
    }
    el.append(row);
  }
  const text = document.createElement('div');
  text.className = 'text';
  if (mine) text.textContent = turn.content; else text.innerHTML = fmt(turn.content);
  el.append(text);
  box.append(el);
}
"""


def _http(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


def print_html(
    item: dict,
    body: str | None,
    marks: list[dict],
    turns: list[dict],
    images: bool = True,
    saved_at: datetime | None = None,
) -> str:
    """The page to print. ``item``: the story with ``summary`` and ``why``
    (``chat.story``); ``body``: safe reader HTML, or None for a story the
    reader can't show; ``marks``: the highlights of that text."""
    saved_at = saved_at or datetime.now(UTC)
    title = html.escape(item.get("title") or item.get("url") or "")
    url = _http(item.get("url"))
    info = " · ".join(
        html.escape(part)
        for part in (item.get("source"), (item.get("published_at") or "")[:10])
        if part
    )
    head = f'<div class="meta">{info}</div>\n<h1>{title}</h1>\n'
    if url:
        quoted = html.escape(url, quote=True)
        head += f'<div class="meta"><a href="{quoted}">{html.escape(url)}</a></div>\n'
    about = ""
    if item.get("summary"):
        about += f"<p>{html.escape(item['summary'])}</p>"
    if item.get("why"):
        about += f"<p><b>Why aiblinx picked it:</b> {html.escape(item['why'])}</p>"
    if about:
        head += f'<div class="about">{about}</div>\n'
    image = _http(item.get("image_url"))
    if image and body is not None:
        head += (
            f'<img class="hero" src="{html.escape(image, quote=True)}" alt="" '
            'onerror="this.remove()">\n'
        )
    article = f'<div class="article">\n{body}\n</div>\n' if body is not None else ""
    discussion = (
        '<section class="discussion">\n'
        f"<h2>Discussion, saved {saved_at:%Y-%m-%d %H:%M} UTC</h2>\n"
        '<div id="turns"></div>\n</section>\n'
        if turns
        else ""
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{title}</title>\n"
        # the reader's figures call these on load and on error
        "<script>function dropFig(img) { img.closest('figure').remove(); }\n"
        "function checkFig(img) { if (img.naturalWidth < 200) dropFig(img); }</script>\n"
        f"<style>\n{_CSS}{MARK_CSS}{'' if images else _NO_IMAGES}</style>\n</head>\n<body>\n"
        f"{head}{article}{discussion}"
        f"{json_script('hl-data', marks)}{json_script('turns-data', turns)}"
        f"<script>{MARKS_JS}{FORMAT_JS}{_SCRIPT}</script>\n</body>\n</html>\n"
    )


async def make_pdf(
    settings: Settings, item: dict, body: str | None, marks: list[dict], turns: list[dict]
) -> tuple[bytes | None, bool]:
    """(the PDF, whether it holds the marks). None when there is nothing
    that could go: no browser and no discussion, or a PDF still too big."""
    limit = settings.pdf_max_mb * 1024 * 1024
    if settings.browser_ws_url:
        try:
            for images in (True, False):
                pdf = await print_pdf(settings, print_html(item, body, marks, turns, images))
                if len(pdf) <= limit:
                    return pdf, True
                log.info("pdf: %d bytes for candidate %s, too big", len(pdf), item.get("id"))
            log.warning("pdf: candidate %s is too big even without pictures", item.get("id"))
            return None, False
        except (PlaywrightError, TimeoutError) as exc:
            log.warning("pdf: printing candidate %s failed: %s", item.get("id"), exc)
    if not turns:
        return None, False
    pdf = await asyncio.to_thread(render_discussion, item, item["summary"], item["why"], turns)
    return pdf, not marks
