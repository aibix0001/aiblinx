"""Mobile-first feed page: title-image cards with save / up / down.

One column, sized for a phone and centred on wider screens. Each card shows
the linked page's title image across its full width, the source and age, the
title, a two-to-three-sentence summary and the one-line reason it was picked. The only
actions are "save to Linkwarden" and more / less like this — preferences are
learned from those, so there is no rating scale, mood or score on the page.

Light and dark follow the system; the header toggle overrides it per device
(localStorage). Title images are hotlinked from the publisher with no
referrer; one that fails to load, or is a banner rather than a picture, is
dropped client-side and the card simply goes without.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

from .html_text import card_summary


def _age(iso_str: str | None) -> str:
    """Relative age ("35 min", "4 h", "2 d") or the date for older items."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    minutes = int((datetime.now(UTC) - dt).total_seconds() // 60)
    if minutes < 0:
        return dt.strftime("%d.%m.")
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    if minutes < 24 * 60:
        return f"{minutes // 60} h"
    if minutes < 7 * 24 * 60:
        return f"{minutes // (24 * 60)} d"
    return dt.strftime("%d.%m.")


def _plain(text: str | None) -> str:
    """Feed text for display. Some feeds (via Miniflux) deliver titles with
    HTML entities still encoded ("&#34;"); decode before escaping, so they
    show as characters rather than as the code."""
    return html.unescape(text or "")


def _alnum(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def repeats_title(summary: str, title: str) -> bool:
    """True when the summary says nothing beyond the headline: the same
    words, a cut-off start of it, or it plus a short trailer ("[ mehr ]")."""
    s, t = _alnum(_plain(summary)), _alnum(_plain(title))
    return bool(s and t) and (
        s == t or t.startswith(s) or (s.startswith(t) and len(s) - len(t) <= 12)
    )


def _http(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


_ICON_SAVE = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/><path d="M12 7v6M9 10h6"/>'
    "</svg>"
)
_ICON_REMOVE = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 6l12 12M18 6L6 18"/></svg>'
)
_ICON_UP = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 19V5M5 12l7-7 7 7"/></svg>'
)
_ICON_DOWN = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M12 5v14M19 12l-7 7-7-7"/></svg>'
)


def _meta(item: dict) -> str:
    """Source chip, "Hacker News" and age: the line above a title."""
    host = (urlsplit(item.get("url") or "").hostname or "").removeprefix("www.")
    meta = [html.escape(host)] if host else []
    if item.get("source") == "hackernews":
        meta.append("Hacker News")
    age = _age(item.get("published_at"))
    if age:
        meta.append(age)
    return "".join(
        f'<span class="chip">{m}</span>' if i == 0 and host else f"<span>{m}</span>"
        for i, m in enumerate(meta)
    )


_ICON_PROMOTE = (  # two roofs: lift this story up into the main interests
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M7 12l5-5 5 5M7 18l5-5 5 5"/></svg>'
)


_ICON_HOLD = (  # a sheet of paper: save for later, in the Bookmarks tab
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg>'
)


_ICON_EXPLORE = (  # a compass: keep it as a distraction, in Exploring
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="9"/><path d="M15.5 8.5l-2 5-5 2 2-5z"/></svg>'
)


def _actions(
    saved: bool,
    interest: str | None,
    promoted: bool | None = None,
    explored: bool | None = None,
    held: bool = False,
) -> str:
    """Five equal icon buttons: Save, one button that files the story on the
    other side, Save for later, more, less. Exploring items get Promote (make
    it a main interest, when ``promoted`` is not None), "For you" items get
    Save to Exploring (keep it as a distraction, less like it here, when
    ``explored`` is not None). Save for later holds the story in the
    Bookmarks tab without any signal."""
    other = ""
    if promoted is not None:
        other = f"""
        <button class="vote promote{" on" if promoted else ""}" onclick="promote(this)"
          aria-label="Promote to your main interests" title="Promote to your main interests"
          aria-pressed="{"true" if promoted else "false"}">{_ICON_PROMOTE}</button>"""
    elif explored is not None:
        other = f"""
        <button class="vote explore{" on" if explored else ""}" onclick="explore(this)"
          aria-label="Save to Exploring, less like this here"
          title="Save to Exploring, less like this here"
          aria-pressed="{"true" if explored else "false"}">{_ICON_EXPLORE}</button>"""
    save_label = "Saved" if saved else "Save"
    hold_label = "Saved for later" if held else "Save for later"
    return f"""\
<div class="actions">
        <button class="save{" on" if saved else ""}" onclick="save(this)"
          aria-label="{save_label}" title="{save_label}"
          aria-pressed="{"true" if saved else "false"}">{_ICON_SAVE}</button>{other}
        <button class="vote hold{" on" if held else ""}" onclick="hold(this)"
          aria-label="{hold_label}" title="{hold_label}"
          aria-pressed="{"true" if held else "false"}">{_ICON_HOLD}</button>
        <button class="vote{" on" if interest == "up" else ""}" onclick="vote(this, 'up')"
          aria-label="More like this" aria-pressed="{"true" if interest == "up" else "false"}">\
{_ICON_UP}</button>
        <button class="vote" onclick="vote(this, 'down')" aria-label="Less like this">\
{_ICON_DOWN}</button>
      </div>"""


def _card(item: dict, new_tab: bool, explore: bool = False) -> str:
    cid = int(item["candidate_id"])
    # The title opens the reader view, which falls back to the page itself.
    url = f"/read/{cid}" if _http(item.get("url")) else "#"
    meta_html = _meta(item)
    # noreferrer either way: publishers never learn where the reader came from
    link_attrs = 'rel="noopener noreferrer" target="_blank"' if new_tab else 'rel="noreferrer"'
    title_text = _plain(item.get("title")) or item.get("url") or ""
    title = html.escape(title_text)
    summary_text = _plain(card_summary(item.get("snippet"), item.get("description")))
    summary = "" if repeats_title(summary_text, title_text) else html.escape(summary_text)
    why = html.escape(item.get("reason") or "")
    image = _http(item.get("image_url"))
    figure = (
        f'<img class="hero" src="{html.escape(image, quote=True)}" alt="" loading="lazy" '
        'referrerpolicy="no-referrer" onload="checkImg(this)" onerror="dropImg(this)">'
        if image
        else ""
    )
    if figure and url != "#":
        # the picture opens the reader too; one link per card for screen readers
        figure = (
            f'<a class="hero-link" href="{html.escape(url, quote=True)}" {link_attrs} '
            f'tabindex="-1" aria-hidden="true">{figure}</a>'
        )
    saved = bool(item.get("saved"))
    interest = item.get("interest")
    return f"""\
<article class="card" id="c{cid}" data-id="{cid}"{' data-down=""' if interest == "down" else ""}>
  <div class="gone">Less like this, noted.</div>
  <div class="body">
    {figure}
    <div class="text">
      <div class="meta">{meta_html}</div>
      <h2><a href="{html.escape(url, quote=True)}" {link_attrs}>\
{title}</a></h2>
      {f'<p class="summary">{summary}</p>' if summary else ""}
      {f'<p class="why">{why}</p>' if why else ""}
      {
        _actions(
            saved,
            interest,
            promoted=bool(item.get("promoted")) if explore else None,
            explored=None if explore else bool(item.get("explored")),
            held=bool(item.get("held")),
        )
    }
    </div>
  </div>
</article>"""


_TOPIC = (
    '<label class="topic"><input type="checkbox" data-name="{name}" {checked} '
    'onchange="topic(this)"> {label}</label>'
)


def _saved_row(save: dict, new_tab: bool) -> str:
    url = _http(save.get("url")) or "#"
    host = (urlsplit(save.get("url") or "").hostname or "").removeprefix("www.")
    title = html.escape(_plain(save.get("title")) or save.get("url") or "")
    target = ' target="_blank"' if new_tab else ""
    return (
        f'<li><a href="{html.escape(url, quote=True)}" rel="noreferrer"{target}>{title}</a>'
        f"<span>{html.escape(host)}</span></li>"
    )


def _days_left(days: int) -> str:
    return "last day" if days <= 1 else f"{days} days left"


def _held_row(item: dict, new_tab: bool) -> str:
    """A held story in the Bookmarks tab: opens in the reader; Save files it
    for good, Remove lets it go."""
    cid = int(item["candidate_id"])
    url = f"/read/{cid}" if _http(item.get("url")) else "#"
    host = (urlsplit(item.get("url") or "").hostname or "").removeprefix("www.")
    title = html.escape(_plain(item.get("title")) or item.get("url") or "")
    target = ' target="_blank"' if new_tab else ""
    image = _http(item.get("image_url"))
    thumb = (
        f'<img class="thumb" src="{html.escape(image, quote=True)}" alt="" loading="lazy" '
        'referrerpolicy="no-referrer" onerror="this.remove()">'
        if image
        else ""
    )
    left = item.get("days_left", 1)
    return f"""\
<article class="card held-row" id="h{cid}" data-id="{cid}">
  <div class="held-top">{thumb}
    <div class="held-text">
      <a href="{html.escape(url, quote=True)}" rel="noreferrer"{target}>{title}</a>
      <span{' class="soon"' if left <= 1 else ""}>{html.escape(host)} · {_days_left(left)}</span>
    </div>
  </div>
  <div class="held-actions">
    <button onclick="heldSave(this)">{_ICON_SAVE}<span>Save</span></button>
    <button class="ghost" onclick="heldRemove(this)">{_ICON_REMOVE}<span>Remove</span></button>
  </div>
</article>"""


def render_page(
    items: list[dict],
    topics: list[dict] | None = None,
    broad: list[dict] | None = None,
    new_tab: bool = False,
    saves: list[dict] | None = None,
    linkwarden: bool = False,
    has_profile: bool = True,
    cycle: str = "",
    holds: list[dict] | None = None,
) -> str:
    """items/broad: current-cycle rows (candidate_id, url, title, snippet,
    description, image_url, reason, source, published_at) plus the viewer's
    ``saved`` flag and latest ``interest`` ("up" / "down" / None).
    topics: dicts with name, selected (the exploring-section picker).
    new_tab: open article links in a new tab instead of the feed's own.
    saves: the local Saved list, newest first (url, title).
    holds: the Bookmarks tab, stories held for later, newest first
    (candidate_id, url, title, image_url, days_left).
    linkwarden: whether saves also go to Linkwarden (labels and hints).
    has_profile: False until something is saved, upvoted or bookmarked — the
    "For you" section then explains how it starts instead of looking broken."""
    broad = broad or []
    topics = topics or []
    saves = saves or []
    holds = holds or []
    selected = sum(1 for t in topics if t["selected"])
    if has_profile:
        curated_empty = (
            '<p class="empty">Nothing new for you right now. '
            "Check back after the next daily update.</p>"
        )
    else:
        first_step = (
            "Tap <b>Save</b> or <b>↑</b> on stories you like under <b>Exploring</b>."
            if selected
            else "Start by picking a few topics under <b>Exploring</b>, then tap "
            "<b>Save</b> or <b>↑</b> on stories you like."
        )
        curated_empty = (
            '<div class="empty start"><h2>Your feed learns from what you keep</h2>'
            f"<p>{first_step} From the next daily update on, this page fills with "
            "stories like the ones you kept.</p>"
            + (
                ""
                if linkwarden
                else "<p>Using Linkwarden? Connect it and your bookmarks shape this "
                "feed from day one.</p>"
            )
            + '<a class="setup-link" href="/setup">Set up your feed</a></div>'
        )
    broad_empty = (
        '<p class="empty">Nothing here yet. The next cycle fills this feed.</p>'
        if selected
        else '<p class="empty">Pick a few topics below to fill this section.</p>'
    )
    # Open on Exploring while "For you" has nothing to show yet.
    start_broad = not items and bool(broad)
    topic_boxes = "\n".join(
        _TOPIC.format(
            name=html.escape(t["name"], quote=True),
            checked="checked" if t["selected"] else "",
            label=html.escape(t["name"]),
        )
        for t in topics
    )
    saved_note = (
        "Saves also go to your Linkwarden."
        if linkwarden
        else "Kept on this server. Connect Linkwarden and they move there too."
    )
    saved_list = (
        '<ul class="saved-list">' + "".join(_saved_row(x, new_tab) for x in saves) + "</ul>"
        if saves
        else '<p class="empty">Nothing saved yet. Tap Save on a story to keep it here.</p>'
    )
    held_list = "\n".join(_held_row(x, new_tab) for x in holds) or (
        '<p class="empty">Nothing held. Tap the paper on a story to keep it here for later.</p>'
    )
    return (
        # a JS string literal; entities mean nothing inside <script>
        _PAGE.replace("__CYCLE__", json.dumps(cycle).replace("</", "<\\/"))
        .replace("__HEAD__", HEAD_TAGS)
        .replace("__THEME__", THEME_CSS)
        .replace("__CARD__", CARD_CSS)
        .replace("__ACTIONS__", ACTIONS_JS)
        .replace("__THEMEBTN__", THEME_BUTTON)
        .replace("__SEL_CURATED__", "false" if start_broad else "true")
        .replace("__SEL_BROAD__", "true" if start_broad else "false")
        .replace("__HID_CURATED__", " hidden" if start_broad else "")
        .replace("__HID_BROAD__", "" if start_broad else " hidden")
        .replace("__SAVED_NOTE__", saved_note)
        .replace("__SAVED_COUNT__", str(len(saves)))
        .replace("__SAVED__", saved_list)
        .replace("__HELD_COUNT__", str(len(holds)))
        .replace("__HELD__", held_list)
        .replace(
            "__CURATED__",
            "\n".join(_card(i, new_tab) for i in items) or curated_empty,
        )
        .replace(
            "__BROAD__",
            "\n".join(_card(i, new_tab, explore=True) for i in broad) or broad_empty,
        )
        .replace("__TOPICS_SELECTED__", str(selected))
        .replace("__TOPICS__", topic_boxes or "<p>No topics available.</p>")
    )


def render_reader(
    item: dict,
    body: str,
    saved: bool,
    interest: str | None,
    linkwarden: bool,
    promoted: bool | None = None,
    explored: bool | None = None,
    translation: dict | None = None,
    chat: bool = False,
    held: bool = False,
) -> str:
    """The reader page: an article's extracted text, then save / more / less.

    item: the candidate row (id, url, title, image_url, source, published_at).
    body: safe HTML from ``reader.extract_article``, inserted as is.
    promoted: None for "For you" items; for Exploring items whether the story
    is already a main interest (adds the Promote button).
    explored: None for Exploring items; for "For you" items whether the story
    is already saved to Exploring (adds the Save to Exploring button).
    translation: {"lang", "title", "body"} with ``lang`` the article's own
    language ("de" / "en"): both versions go into the page and a DE / EN
    toggle swaps them. The article always opens in its own language.
    chat: whether the chat about the story sits under the text; with
    ``linkwarden`` a discussion goes to the saved link as a PDF."""
    url = _http(item.get("url")) or "#"
    image = _http(item.get("image_url"))
    figure = (
        f'<a class="hero-link" href="{html.escape(url, quote=True)}" rel="noreferrer" '
        'tabindex="-1" aria-hidden="true">'
        f'<img class="hero" src="{html.escape(image, quote=True)}" alt="" '
        'referrerpolicy="no-referrer" onload="checkImg(this)" onerror="dropImg(this)"></a>'
        if image and url != "#"
        else ""
    )
    title = html.escape(_plain(item.get("title")) or item.get("url") or "")
    headline, article, lang_button = (
        f"<h1>{title}</h1>",
        f'<div class="article">\n{body}\n</div>',
        "",
    )
    if translation:
        src = translation["lang"]
        dst = "en" if src == "de" else "de"
        headline = (
            f'<h1 lang="{src}" data-lang="{src}">{title}</h1>'
            f'<h1 lang="{dst}" data-lang="{dst}" hidden>{html.escape(translation["title"])}</h1>'
        )
        article = (
            f'<div class="article" lang="{src}" data-lang="{src}">\n{body}\n</div>\n'
            f'<div class="article" lang="{dst}" data-lang="{dst}" hidden>\n'
            f"{translation['body']}\n</div>"
        )
        lang_button = (
            f'<button class="icon-btn size" onclick="switchLang(this)" '
            f'aria-label="{_READ_IN[dst]}">{dst.upper()}</button>'
        )
    return (
        _READER.replace("__HEAD__", HEAD_TAGS)
        .replace("__THEME__", THEME_CSS)
        .replace("__CARD__", CARD_CSS)
        .replace("__ACTIONS__", ACTIONS_JS)
        .replace("__THEMEBTN__", THEME_BUTTON)
        .replace("__ID__", str(int(item["id"])))
        .replace("__ACTIONBAR__", _actions(saved, interest, promoted, explored, held))
        .replace("__URL__", html.escape(url, quote=True))
        .replace("__META__", _meta(item))
        .replace("__FIGURE__", figure)
        .replace("__LANGBTN__", lang_button)
        .replace("__HEADLINE__", headline)
        .replace("__TITLE__", title)
        .replace("__CHAT__", _chat(linkwarden) if chat else "")
        .replace("__CHATJS__", CHAT_JS if chat else "")
        .replace("__BODY__", article)  # last: article text is never scanned for placeholders
    )


_READ_IN = {"de": "Read in German", "en": "Read in English"}

_ICON_SEND = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M5 12h14M13 6l6 6-6 6"/></svg>'
)


def _chat(linkwarden: bool) -> str:
    """The chat under the article. The conversation stays on the device for
    30 days; with Linkwarden, Save also attaches it to the link as a PDF."""
    note = (
        "The conversation stays on this device for 30 days. Save attaches it to the "
        "Linkwarden link as a PDF, in place of the page PDF."
        if linkwarden
        else "The conversation stays on this device for 30 days."
    )
    attach = (
        '\n  <button class="attach" id="attach" hidden onclick="attach(this)">'
        "Attach this discussion to Linkwarden</button>"
        if linkwarden
        else ""
    )
    return f"""\
<section class="chat" id="chat" data-linkwarden="{"1" if linkwarden else ""}">
  <h2>Talk about this story</h2>
  <div class="turns" id="turns" aria-live="polite"></div>
  <form class="ask" onsubmit="ask(event)">
    <textarea id="question" rows="2" maxlength="4000" required
      placeholder="Ask about this story" aria-label="Your question"
      onkeydown="askKey(event)"></textarea>
    <button type="submit" id="send" aria-label="Send"
      onmousedown="event.preventDefault()">{_ICON_SEND}</button>
  </form>{attach}
  <p class="chat-note">{note}</p>
</section>"""


# Shared by every page: theme tokens (light by default, dark by system setting
# or the per-device toggle) and the head tags for the home-screen app.
THEME_CSS = """\
:root {
  /* AIBIX CI: navy, teal / deep teal, slate neutrals; Trebuchet MS + Calibri */
  --bg: #D5DAE3; --bar: rgba(213, 218, 227, 0.88); --card: #FFFFFF; --ink: #1B2A4A;
  --muted: #64748B; --line: #D1D5DB; --chip: #D5DAE3; --btn: #D5DAE3; --btn-ink: #1B2A4A;
  --on: #1B2A4A; --on-ink: #FFFFFF; --save-on: #1A7A82; --save-on-ink: #FFFFFF;
  --img: #D1D5DB; --accent: #2B9EA5; --soon: #B45309;
  --font-body: Calibri, Carlito, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-head: "Trebuchet MS", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #1B2A4A; --bar: rgba(27, 42, 74, 0.88); --card: #1E293B; --ink: #FFFFFF;
    --muted: #D1D5DB; --line: #334155; --chip: #334155; --btn: #334155; --btn-ink: #FFFFFF;
    --on: #FFFFFF; --on-ink: #1B2A4A; --img: #334155; --soon: #FBBF24;
  }
}
:root[data-theme="dark"] {
  --bg: #1B2A4A; --bar: rgba(27, 42, 74, 0.88); --card: #1E293B; --ink: #FFFFFF;
  --muted: #D1D5DB; --line: #334155; --chip: #334155; --btn: #334155; --btn-ink: #FFFFFF;
  --on: #FFFFFF; --on-ink: #1B2A4A; --img: #334155; --soon: #FBBF24;
}
h1, h2, h3, .wordmark { font-family: var(--font-head); }
/* the wordmark: "aib" (the maker's prefix) + "linx" on the teal highlight */
.wordmark { font-weight: 700; letter-spacing: -0.02em; white-space: nowrap; }
.wordmark b {
  font-weight: inherit; background: var(--accent); color: #FFFFFF;
  border-radius: 5px; padding: 0 3px; margin-left: 1px;
}
"""

HEAD_TAGS = """\
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#D5DAE3" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#1B2A4A" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icons/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="aiblinx">
<script>
try { const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; }
catch (e) {}
</script>"""


# The card look and its save / more / less actions, shared by the feed and the
# reader page (whose actions sit in a .card of their own).
CARD_CSS = """\
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: var(--font-body);
  -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%;
}
.card { border-radius: 24px; background: var(--card); overflow: hidden; }
.card .gone { display: none; }
.card[data-down] .body { display: none; }
.card[data-down] .gone {
  display: block; padding: 14px 18px; font-size: 15px; color: var(--muted);
}
.hero-link { display: block; }
.hero {
  display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover; background: var(--img);
}
.text { padding: 16px 18px 14px; display: flex; flex-direction: column; gap: 8px; }
.meta {
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
  font-size: 13px; color: var(--muted);
}
.meta span + span::before { content: "·"; margin-right: 8px; }
.meta .chip + span::before { content: none; margin: 0; }
.chip { padding: 3px 8px; border-radius: 7px; background: var(--chip); color: var(--ink); }
h2 { margin: 0; font-size: 22px; line-height: 1.18; font-weight: 680; letter-spacing: -0.01em; }
h2 a { color: inherit; text-decoration: none; }
.summary {
  margin: 0; font-size: 17px; line-height: 1.45; color: var(--ink); opacity: 0.86;
}
.why { margin: 0; font-size: 13px; line-height: 1.4; color: var(--muted); }
.actions { display: flex; gap: 8px; padding-top: 6px; }
.actions button {
  height: 48px; border: 0; border-radius: 16px; font: inherit; font-size: 15px;
  display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer;
  background: var(--btn); color: var(--btn-ink); transition: background 120ms, color 120ms;
}
.actions button { flex: 1 1 0; min-width: 0; }
.actions .save.on { background: var(--save-on); color: var(--save-on-ink); }
.actions .vote.on { background: var(--on); color: var(--on-ink); }
.actions button:disabled { opacity: 0.6; }
.theme-btn .sun { display: none; }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .theme-btn .moon { display: none; }
  :root:not([data-theme="light"]) .theme-btn .sun { display: block; }
}
:root[data-theme="dark"] .theme-btn .moon { display: none; }
:root[data-theme="dark"] .theme-btn .sun { display: block; }
#ptr {
  position: fixed; left: 50%; top: calc(env(safe-area-inset-top) + 8px); z-index: 3;
  transform: translate(-50%, -60px); opacity: 0; padding: 8px 14px; border-radius: 999px;
  background: var(--on); color: var(--on-ink); font-size: 14px; pointer-events: none;
}
#toast {
  position: fixed; left: 50%; bottom: calc(env(safe-area-inset-bottom) + 20px);
  transform: translateX(-50%); padding: 12px 18px; border-radius: 14px;
  background: var(--on); color: var(--on-ink); font-size: 15px; opacity: 0;
  transition: opacity 160ms; pointer-events: none;
}
#toast.show { opacity: 1; }
"""

# The light/dark toggle for the feed and reader headers. It holds both icons;
# CARD_CSS shows the mode it switches to (moon in light, sun in dark).
THEME_BUTTON = """\
<button class="icon-btn theme-btn" onclick="toggleTheme()" \
aria-label="Switch light or dark mode">\
<svg class="moon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z"/></svg>\
<svg class="sun" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4\
M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg></button>"""

ACTIONS_JS = """\
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
}
function toggleTheme() {
  const root = document.documentElement;
  const dark = root.dataset.theme
    ? root.dataset.theme === 'dark'
    : matchMedia('(prefers-color-scheme: dark)').matches;
  root.dataset.theme = dark ? 'light' : 'dark';
  try { localStorage.setItem('theme', root.dataset.theme); } catch (e) {}
}
async function post(id, path, body) {
  const resp = await fetch(`/feed/${id}/${path}`, body === undefined ? {method: 'POST'} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (!resp.ok) {
    let detail = resp.status;
    try { detail = (await resp.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return resp.json();
}
function press(btn, on, label) {
  btn.classList.toggle('on', on); btn.setAttribute('aria-pressed', on);
  if (label) { btn.setAttribute('aria-label', label); btn.title = label; }
}
// a saved story is no longer held: every copy of its card shows that
function markSaved(id) {
  document.querySelectorAll(`[data-id="${id}"] .save`).forEach(b => press(b, true, 'Saved'));
  document.querySelectorAll(`[data-id="${id}"] .hold`)
    .forEach(b => press(b, false, 'Save for later'));
}
async function hold(btn) {
  const id = btn.closest('.card').dataset.id;
  const on = !btn.classList.contains('on');
  btn.disabled = true;
  try {
    const resp = await fetch(`/feed/${id}/hold`, {method: on ? 'POST' : 'DELETE'});
    if (!resp.ok) throw new Error(resp.status);
    if ((await resp.json()).status === 'already_saved') { toast('Already saved'); return; }
    document.querySelectorAll(`[data-id="${id}"] .hold`)
      .forEach(b => press(b, on, on ? 'Saved for later' : 'Save for later'));
    window.holdsChanged = true;
    toast(on ? 'Kept in Bookmarks for later' : 'Removed from Bookmarks');
  } catch (e) { toast('Could not update: ' + e.message); }
  finally { btn.disabled = false; }
}
async function save(btn) {
  if (btn.classList.contains('on')) return;
  const card = btn.closest('.card');
  btn.disabled = true;
  try {
    // the reader chat's discussion goes along, as a PDF for Linkwarden
    const body = typeof saveBody === 'function' ? saveBody() : undefined;
    const result = await post(card.dataset.id, 'save', body);
    markSaved(card.dataset.id);
    if (typeof saved === 'function') saved(result);
  } catch (e) { toast('Could not save: ' + e.message); }
  finally { btn.disabled = false; }
}
async function promote(btn) {
  if (btn.classList.contains('on')) return;
  const card = btn.closest('.card');
  btn.disabled = true;
  try {
    await post(card.dataset.id, 'promote');
    press(btn, true);
    markSaved(card.dataset.id);
    if (typeof saved === 'function') saved({});
    toast('Now one of your main interests');
  } catch (e) { toast('Could not promote: ' + e.message); }
  finally { btn.disabled = false; }
}
async function explore(btn) {
  if (btn.classList.contains('on')) return;
  const card = btn.closest('.card');
  btn.disabled = true;
  try {
    await post(card.dataset.id, 'explore');
    press(btn, true);
    markSaved(card.dataset.id);
    if (typeof saved === 'function') saved({});
    toast('Saved to Exploring, less like this here');
  } catch (e) { toast('Could not save to Exploring: ' + e.message); }
  finally { btn.disabled = false; }
}
async function vote(btn, value) {
  const card = btn.closest('.card');
  btn.disabled = true;
  try {
    await post(card.dataset.id, 'interest', {value});
    if (value === 'down') { card.setAttribute('data-down', ''); }
    else {
      btn.classList.add('on'); btn.setAttribute('aria-pressed', 'true');
      toast('More like this');
    }
  } catch (e) { toast('Could not record: ' + e.message); }
  finally { btn.disabled = false; }
}
"""


# The reader chat. The conversation lives in ``turns``, kept on the device
# (localStorage, per story, for CHAT_DAYS); each question sends it whole and
# the answer streams back as server-sent events. The server writes the
# answer as a job that outlives the page: an answer still pending when the
# page went away is fetched again by its job id when the story opens. Model
# text is escaped before the little Markdown it may use (paragraphs, lists,
# bold, http/https links) becomes markup.
CHAT_JS = r"""
const turns = [];
let attachedTurns = 0;
const chatId = document.querySelector('.end .card').dataset.id;
const chatLinkwarden = document.getElementById('chat').dataset.linkwarden === '1';
const CHAT_KEY = 'chat:' + chatId, CHAT_DAYS = 30, CHAT_KEEP = 50;
let pending = null;  // {question, job}: the answer the server is writing
function store() {
  try {
    if (turns.length || pending) {
      localStorage.setItem(CHAT_KEY,
        JSON.stringify({turns, attachedTurns, pending, at: Date.now()}));
    } else localStorage.removeItem(CHAT_KEY);
  } catch (e) {}
}
// discussions older than CHAT_DAYS go, and all but the newest CHAT_KEEP
function pruneChats() {
  try {
    const chats = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key.startsWith('chat:')) continue;
      let at = 0;
      try { at = JSON.parse(localStorage.getItem(key)).at || 0; } catch (e) {}
      chats.push([at, key]);
    }
    chats.sort((a, b) => b[0] - a[0]);
    const old = Date.now() - CHAT_DAYS * 864e5;
    chats.forEach(([at, key], i) => {
      if (i >= CHAT_KEEP || at < old) localStorage.removeItem(key);
    });
  } catch (e) {}
}
function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" rel="noreferrer" target="_blank">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]*[^\s<).,;:!?])/g,
      '$1<a href="$2" rel="noreferrer" target="_blank">$2</a>');
}
function fmt(text) {
  return text.trim().split(/\n{2,}/).map(block => {
    const lines = block.split('\n');
    if (lines.every(l => /^\s*([-*]|\d+\.)\s+/.test(l))) {
      const tag = /^\s*\d+\./.test(lines[0]) ? 'ol' : 'ul';
      return `<${tag}>` + lines.map(l =>
        '<li>' + inline(l.replace(/^\s*([-*]|\d+\.)\s+/, '')) + '</li>').join('') + `</${tag}>`;
    }
    return '<p>' + lines.map(l => inline(l.replace(/^#{1,6}\s+/, ''))).join('<br>') + '</p>';
  }).join('');
}
function addTurn(cls) {
  const el = document.createElement('div');
  el.className = 'turn ' + cls;
  document.getElementById('turns').append(el);
  return el;
}
function showSearch(el, search) {
  let box = el.querySelector('.searches');
  if (!box) {
    box = document.createElement('div'); box.className = 'searches';
    el.prepend(box);
  }
  const row = document.createElement('div');
  row.textContent = 'Searched: ' + search.query;
  for (const r of search.results) {
    const a = document.createElement('a');
    a.href = r.url; a.rel = 'noreferrer'; a.target = '_blank';
    a.textContent = r.title || r.url;
    row.append(a);
  }
  box.append(row);
}
function chatLang() {
  const shown = document.querySelector('.article[data-lang]:not([hidden])');
  return shown ? shown.dataset.lang : null;
}
function isSaved() { return document.querySelector('.end .save').classList.contains('on'); }
function updateAttach() {
  const btn = document.getElementById('attach');
  if (btn) btn.hidden = !(isSaved() && turns.length > attachedTurns);
}
// Save sends the discussion along; after it, only a changed one is offered again
function saveBody() { return chatLinkwarden && turns.length ? {discussion: turns} : undefined; }
function saved(result) {
  if (result.attached === true) {
    attachedTurns = turns.length; store(); toast('Saved, with the discussion as PDF');
  } else if (result.attached === false) {
    toast('Saved, but the discussion could not be attached');
  }
  updateAttach();
}
async function attach(btn) {
  btn.disabled = true;
  try {
    const result = await post(chatId, 'save', {discussion: turns});
    if (result.attached) {
      attachedTurns = turns.length; store(); toast('Discussion attached to Linkwarden');
    } else toast('Could not attach the discussion');
  } catch (e) { toast('Could not attach: ' + e.message); }
  finally { btn.disabled = false; updateAttach(); }
}
function askKey(ev) {
  // Enter sends on a keyboard; on a phone it is a new line
  const keyboard = matchMedia('(pointer: fine)').matches;
  if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing && keyboard) {
    ev.preventDefault(); ev.target.form.requestSubmit();
  }
}
async function ask(ev) {
  ev.preventDefault();
  const box = document.getElementById('question'), send = document.getElementById('send');
  const question = box.value.trim();
  if (!question || send.disabled) return;
  // the question field keeps the focus, so a phone keeps its keyboard open
  box.focus({preventScroll: true});
  const mine = addTurn('user'); mine.textContent = question;
  box.value = '';
  const history = turns.map(t => ({role: t.role, content: t.content}));
  history.push({role: 'user', content: question});
  pending = {question, job: null}; store();
  await answer(fetch(`/read/${chatId}/chat`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({messages: history, lang: chatLang()})}));
}
function follow() { return fetch(`/read/${chatId}/chat/${pending.job}`, {cache: 'no-store'}); }
// One answer, from the POST that asks or the GET that fetches it again.
// When only the line breaks, the server goes on writing: follow it once
// more, and if that fails too keep it pending for the next visit.
async function answer(request, retry = true) {
  const box = document.getElementById('question'), send = document.getElementById('send');
  send.disabled = true;
  const bot = addTurn('bot'), text = document.createElement('div');
  text.className = 'text'; text.innerHTML = '<p class="wait">Thinking…</p>'; bot.append(text);
  const reply = {role: 'assistant', content: '', searches: []};
  let failed = false, done = false, gone = false;
  try {
    const resp = await request;
    gone = resp.status === 404;
    if (!resp.ok || !resp.body) throw new Error(resp.status);
    const reader = resp.body.getReader(), decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const {value, done: end} = await reader.read();
      if (end) break;
      buffer += decoder.decode(value, {stream: true});
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const line = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
        if (!line.startsWith('data: ')) continue;
        const event = JSON.parse(line.slice(6));
        if (event.type === 'job') {
          pending.job = event.id; store();
        } else if (event.type === 'text') {
          reply.content += event.text; text.innerHTML = fmt(reply.content);
        } else if (event.type === 'search') {
          reply.searches.push({query: event.query, results: event.results});
          showSearch(bot, event);
          if (!reply.content) text.innerHTML = '<p class="wait">Reading the results…</p>';
        } else if (event.type === 'error') { failed = true; }
        else if (event.type === 'done') { done = true; }
      }
    }
  } catch (e) {}
  if (!done && !gone && pending.job && retry) {
    bot.remove();
    return answer(follow(), false);
  }
  const question = pending.question;
  if (done && !failed && reply.content.trim()) {
    turns.push({role: 'user', content: question}, reply);
    pending = null;
    updateAttach();
  } else {
    text.innerHTML = '<p class="failed">No answer came back. Ask again?</p>';
    if (!box.value) box.value = question;
    if (done || gone || !pending.job) pending = null;
  }
  store();
  send.disabled = false;
}
// the discussion kept on this device, and an answer still pending
function restoreChat() {
  pruneChats();
  let kept = null;
  try { kept = JSON.parse(localStorage.getItem(CHAT_KEY)); } catch (e) {}
  if (!kept) return;
  attachedTurns = kept.attachedTurns || 0;
  for (const turn of kept.turns || []) {
    turns.push(turn);
    const el = addTurn(turn.role === 'user' ? 'user' : 'bot');
    if (turn.role === 'user') { el.textContent = turn.content; continue; }
    const text = document.createElement('div');
    text.className = 'text'; text.innerHTML = fmt(turn.content); el.append(text);
    for (const search of turn.searches || []) showSearch(el, search);
  }
  updateAttach();
  pending = kept.pending || null;
  if (pending && pending.job) {
    addTurn('user').textContent = pending.question;
    answer(follow());
  } else if (pending) {
    // asked, but the page went before the server took it on
    document.getElementById('question').value = pending.question;
    pending = null; store();
  }
}
restoreChat();
// a page brought back from the back/forward cache has a stale chat
addEventListener('pageshow', e => { if (e.persisted && pending) location.reload(); });
"""


_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
<title>aiblinx</title>
<script>
// before first paint: a tab remembered in the fragment is shown straight
// away (CSS below), instead of flashing "For you" until the body script runs
if (['broad', 'held', 'saved'].includes(location.hash.slice(1))) {
  document.documentElement.dataset.tab = location.hash.slice(1);
}
// in <head>: a cached image can fire onload/onerror before the body script runs
function dropImg(img) { (img.closest('.hero-link') || img).remove(); }
function checkImg(img) {
  // logos and banners are not title pictures
  if (img.naturalWidth < 200 || img.naturalWidth / img.naturalHeight > 3) dropImg(img);
}
</script>
<style>
__THEME__* { box-sizing: border-box; }
__CARD__header {
  position: sticky; top: 0; z-index: 2; background: var(--bar);
  -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  padding: calc(env(safe-area-inset-top) + 12px) 12px 12px;
}
.bar { max-width: 640px; margin: 0 auto; display: flex; align-items: center; gap: 6px; }
.logo { font-size: 22px; margin-right: auto; }
.tabs { display: flex; gap: 4px; padding: 3px; border-radius: 999px; background: var(--card); }
.tabs button {
  width: 44px; height: 38px; padding: 0; border: 0; border-radius: 999px;
  display: grid; place-items: center;
  background: transparent; color: var(--muted); cursor: pointer;
}
.tabs button[aria-selected="true"] { background: var(--on); color: var(--on-ink); }
.icon-btn {
  width: 42px; height: 42px; flex-shrink: 0; border: 0; border-radius: 999px;
  background: var(--card);
  color: var(--ink); display: grid; place-items: center; cursor: pointer;
}
main {
  max-width: 640px; margin: 0 auto;
  padding: 6px 12px calc(env(safe-area-inset-bottom) + 40px);
}
.panel { display: flex; flex-direction: column; gap: 14px; }
.panel[hidden] { display: none; }
:root[data-tab] #curated { display: none; }
:root[data-tab="broad"] #broad, :root[data-tab="held"] #held,
:root[data-tab="saved"] #saved { display: flex; }
:root[data-tab] .tabs button[data-panel="curated"] { background: transparent; color: var(--muted); }
:root[data-tab="broad"] .tabs button[data-panel="broad"],
:root[data-tab="held"] .tabs button[data-panel="held"],
:root[data-tab="saved"] .icon-btn[data-panel="saved"] {
  background: var(--on); color: var(--on-ink);
}
details.topics { border-radius: 24px; background: var(--card); padding: 16px 18px; }
details.topics summary { cursor: pointer; font-weight: 600; min-height: 28px; }
.topics p { color: var(--muted); font-size: 14px; }
label.topic {
  display: inline-flex; align-items: center; gap: 6px; margin: 6px 14px 6px 0; font-size: 15px;
}
.empty { text-align: center; color: var(--muted); padding: 48px 16px; }
.empty.start {
  text-align: left; padding: 22px 20px; border-radius: 24px; background: var(--card);
  color: var(--ink); display: flex; flex-direction: column; gap: 10px;
}
.empty.start h2 { font-size: 22px; }
.empty.start p { margin: 0; font-size: 17px; line-height: 1.45; color: var(--muted); }
.setup-link {
  align-self: flex-start; display: inline-flex; align-items: center; min-height: 48px;
  padding: 0 18px; border-radius: 16px; background: var(--on); color: var(--on-ink);
  text-decoration: none; font-size: 15px; margin-top: 4px;
}
.panel-foot { margin: 0 6px; font-size: 14px; }
.panel-foot a { color: var(--muted); }
.icon-btn[aria-selected="true"] { background: var(--on); color: var(--on-ink); }
.saved-head { padding: 8px 6px 0; display: flex; flex-direction: column; gap: 4px; }
.saved-head p { margin: 0; color: var(--muted); font-size: 14px; }
.saved-list {
  list-style: none; margin: 0; padding: 0; border-radius: 24px; background: var(--card);
}
.saved-list li {
  padding: 14px 18px; border-bottom: 1px solid var(--line); display: flex;
  flex-direction: column; gap: 3px;
}
.saved-list li:last-child { border-bottom: 0; }
.saved-list a {
  color: var(--ink); font-weight: 600; font-size: 16px; line-height: 1.3; text-decoration: none;
}
.saved-list span { color: var(--muted); font-size: 13px; }
.held-row { padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.held-top { display: flex; gap: 12px; align-items: flex-start; }
.held-row .thumb {
  width: 72px; height: 72px; flex-shrink: 0; border-radius: 14px; object-fit: cover;
  background: var(--img);
}
.held-text { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.held-text a {
  color: var(--ink); font-family: var(--font-head); font-weight: 700; font-size: 17px;
  line-height: 1.2; text-decoration: none; overflow-wrap: anywhere;
}
.held-text span { color: var(--muted); font-size: 13px; }
.held-text span.soon { color: var(--soon); font-weight: 700; }
.held-actions { display: flex; gap: 8px; }
.held-actions button {
  flex: 1 1 0; height: 44px; border: 0; border-radius: 14px; font: inherit; font-size: 15px;
  display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer;
  background: var(--btn); color: var(--btn-ink);
}
.held-actions button.ghost { background: transparent; border: 1.5px solid var(--line); }
.held-actions button:disabled { opacity: 0.6; }
@media (min-width: 700px) { main { padding-top: 16px; } .panel { gap: 20px; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <div class="logo wordmark" aria-label="aiblinx">aib<b>linx</b></div>
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="__SEL_CURATED__" data-panel="curated" onclick="tab(this)"
        aria-label="For you" title="For you">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.2a4.3 4.3 0 0 1 7.5 2.6\
C19.5 15.4 12 20 12 20z"/></svg></button>
      <button role="tab" aria-selected="__SEL_BROAD__" data-panel="broad" onclick="tab(this)"
        aria-label="Exploring" title="Exploring">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 2.6 3.8 5.6 3.8 9s-1.2 6.4-3.8 9\
M12 3c-2.6 2.6-3.8 5.6-3.8 9s1.2 6.4 3.8 9"/></svg></button>
      <button role="tab" aria-selected="false" data-panel="held" onclick="tab(this)"
        aria-label="Bookmarks" title="Bookmarks">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/></svg></button>
    </div>
    <button class="icon-btn" data-panel="saved" aria-selected="false" onclick="tab(this)"
      aria-label="Saved stories">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/></svg></button>
    __THEMEBTN__
  </div>
</header>
<main>
<section class="panel" id="curated"__HID_CURATED__>
__CURATED__
</section>
<section class="panel" id="broad"__HID_BROAD__>
__BROAD__
<details class="topics">
  <summary>Exploring topics (__TOPICS_SELECTED__ selected)</summary>
  <p>Selected topics feed this section. Pick a few outside your usual interests.</p>
  __TOPICS__
</details>
</section>
<section class="panel" id="held" hidden>
<div class="saved-head"><h2>Bookmarks · __HELD_COUNT__</h2>\
<p>Kept for later, not ranked and not sent to Linkwarden.</p></div>
__HELD__
</section>
<section class="panel" id="saved" hidden>
<div class="saved-head"><h2>Saved · __SAVED_COUNT__</h2><p>__SAVED_NOTE__</p></div>
__SAVED__
<p class="panel-foot"><a href="/setup">Setup: imports, topics and connections</a></p>
</section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<div id="ptr" aria-hidden="true">Pull to refresh</div>
<script>
__ACTIONS__// The home-screen app resumes this page instead of loading it again, so it
// checks for a newer daily feed whenever it comes back, and reloads only
// then (a reload keeps the tab: it is in the fragment).
const CYCLE = __CYCLE__;
async function checkFresh() {
  try {
    const resp = await fetch('/feed/version', {cache: 'no-store'});
    if (resp.ok && (await resp.json()).cycle !== CYCLE) location.reload();
  } catch (e) {}
}
addEventListener('pageshow', e => { if (e.persisted) checkFresh(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') checkFresh();
});
// Pull to refresh: standalone web apps on iOS have none of their own.
if (navigator.standalone || matchMedia('(display-mode: standalone)').matches) {
  const ptr = document.getElementById('ptr');
  const PULL = 80;
  let startY = null, pulled = 0;
  addEventListener('touchstart', e => {
    startY = scrollY <= 0 ? e.touches[0].clientY : null;
  }, {passive: true});
  addEventListener('touchmove', e => {
    if (startY === null) return;
    pulled = Math.max(0, e.touches[0].clientY - startY);
    ptr.textContent = pulled > PULL ? 'Release to refresh' : 'Pull to refresh';
    ptr.style.transform = `translate(-50%, ${Math.min(pulled, PULL * 1.5) - 60}px)`;
    ptr.style.opacity = Math.min(1, pulled / PULL);
  }, {passive: true});
  addEventListener('touchend', () => {
    if (startY !== null && pulled > PULL) {
      ptr.textContent = 'Refreshing…';
      location.reload();
      return;
    }
    startY = null; pulled = 0;
    ptr.style.transform = ''; ptr.style.opacity = '';
  });
}
function tab(btn, restoring) {
  // the Bookmarks tab is rendered by the server: after a hold changed, load
  // it fresh (the fragment keeps the tab across the reload)
  if (btn.dataset.panel === 'held' && window.holdsChanged && !restoring) {
    history.replaceState(null, '', '#held');
    location.reload();
    return;
  }
  document.querySelectorAll('[data-panel]').forEach(b => {
    const on = b === btn;
    b.setAttribute('aria-selected', on);
    document.getElementById(b.dataset.panel).hidden = !on;
  });
  // the open tab lives in the URL fragment, so a reload keeps it; the
  // fragment never reaches the server
  delete document.documentElement.dataset.tab;  // hidden attributes rule from here
  const panel = btn.dataset.panel;
  history.replaceState(null, '', panel === 'curated' ? location.pathname : '#' + panel);
  if (!restoring) window.scrollTo(0, 0);
}
if (['broad', 'held', 'saved'].includes(location.hash.slice(1))) {
  tab(document.querySelector(`[data-panel="${location.hash.slice(1)}"]`), true);
}
function heldDone(row, msg) {
  row.remove();
  const head = document.querySelector('#held h2');
  head.textContent = 'Bookmarks · ' + document.querySelectorAll('#held .held-row').length;
  window.holdsChanged = true;
  toast(msg);
}
async function heldSave(btn) {
  const row = btn.closest('.card');
  btn.disabled = true;
  try {
    await post(row.dataset.id, 'save');  // also releases the hold
    markSaved(row.dataset.id);
    heldDone(row, 'Saved');
  } catch (e) { toast('Could not save: ' + e.message); btn.disabled = false; }
}
async function heldRemove(btn) {
  const row = btn.closest('.card');
  btn.disabled = true;
  try {
    const resp = await fetch(`/feed/${row.dataset.id}/hold`, {method: 'DELETE'});
    if (!resp.ok) throw new Error(resp.status);
    document.querySelectorAll(`[data-id="${row.dataset.id}"] .hold`)
      .forEach(b => press(b, false, 'Save for later'));
    heldDone(row, 'Removed from Bookmarks');
  } catch (e) { toast('Could not remove: ' + e.message); btn.disabled = false; }
}
async function topic(box) {
  box.disabled = true;
  try {
    const resp = await fetch(`/topics/${encodeURIComponent(box.dataset.name)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected: box.checked})});
    if (!resp.ok) throw new Error(resp.status);
  } catch (e) { box.checked = !box.checked; toast('Could not update topic'); }
  finally { box.disabled = false; }
}
</script>
</body>
</html>
"""


_READER = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="referrer" content="no-referrer">
__HEAD__
<title>__TITLE__ · aiblinx</title>
<script>
function dropImg(img) { (img.closest('.hero-link') || img).remove(); }
function checkImg(img) {
  if (img.naturalWidth < 200 || img.naturalWidth / img.naturalHeight > 3) dropImg(img);
}
// a picture in the text goes with its caption when it fails to load or is
// an icon
function dropFig(img) { img.closest('figure').remove(); }
function checkFig(img) { if (img.naturalWidth < 200) dropFig(img); }
// the article text size (A- / A+) is a per-device preference, applied before
// first paint so the text never jumps
const SIZES = [15, 17, 19, 21, 23, 25, 27];
let size = 19;
try { const s = +localStorage.getItem('readerSize'); if (SIZES.includes(s)) size = s; }
catch (e) {}
document.documentElement.style.setProperty('--reader-size', size + 'px');
function fontSize(step) {
  size = SIZES[Math.min(SIZES.length - 1, Math.max(0, SIZES.indexOf(size) + step))];
  document.documentElement.style.setProperty('--reader-size', size + 'px');
  try { localStorage.setItem('readerSize', size); } catch (e) {}
  sizeButtons();
}
function sizeButtons() {
  document.getElementById('smaller').disabled = size === SIZES[0];
  document.getElementById('larger').disabled = size === SIZES[SIZES.length - 1];
}
// DE / EN: swap the article and its translation in place; nothing is saved,
// every article opens in its own language
function switchLang(btn) {
  const show = btn.textContent.toLowerCase();
  document.querySelectorAll('[data-lang]').forEach(el => { el.hidden = el.dataset.lang !== show; });
  const next = show === 'de' ? 'en' : 'de';
  btn.textContent = next.toUpperCase();
  btn.setAttribute('aria-label', next === 'de' ? 'Read in German' : 'Read in English');
}
function back() {
  // back to the feed in history (keeps its scroll position); /ui if opened directly
  if (history.length > 1) history.back(); else location.href = '/ui';
}
</script>
<style>
__THEME__* { box-sizing: border-box; }
__CARD__header {
  position: sticky; top: 0; z-index: 2; background: var(--bar);
  -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  padding: calc(env(safe-area-inset-top) + 12px) 12px 12px;
}
.bar { max-width: 640px; margin: 0 auto; display: flex; align-items: center; gap: 6px; }
.logo { font-size: 22px; margin-right: auto; }
.icon-btn {
  width: 42px; height: 42px; flex-shrink: 0; border: 0; border-radius: 999px;
  background: var(--card); color: var(--ink); display: grid; place-items: center;
  cursor: pointer;
}
main {
  max-width: 640px; margin: 0 auto;
  padding: 10px 18px calc(env(safe-area-inset-bottom) + 40px);
}
.hero { border-radius: 18px; margin: 16px 0 2px; }
h1 { margin: 12px 0 0; font-size: 30px; line-height: 1.15; font-weight: 720;
  letter-spacing: -0.015em; }
.icon-btn:disabled { opacity: 0.4; cursor: default; }
.icon-btn.size { width: 38px; font: 700 15px var(--font-head); }
.icon-btn.size.up { font-size: 19px; }
.article {
  margin-top: 16px; font-size: var(--reader-size, 19px); line-height: 1.62;
  overflow-wrap: break-word;
}
.article p, .article ul, .article ol, .article blockquote, .article pre { margin: 0 0 1em; }
.article h2 { font-size: 1.21em; margin: 1.4em 0 0.5em; }
.article h3 { font-size: 1.05em; margin: 1.3em 0 0.4em; }
.article figure { margin: 1.4em 0; }
.article figure img {
  display: block; max-width: 100%; height: auto; margin: 0 auto; border-radius: 12px;
  background: var(--img);
}
.article figcaption {
  margin-top: 0.5em; font-size: 0.79em; line-height: 1.45; color: var(--muted);
}
.article blockquote { padding-left: 16px; border-left: 3px solid var(--line); color: var(--muted); }
.article pre {
  padding: 12px 14px; border-radius: 12px; background: var(--chip); overflow-x: auto;
  font-size: 0.79em; line-height: 1.45;
}
main > .meta .chip { background: var(--card); }  /* on the page, not a card */
.article .lead { font-size: 1.05em; line-height: 1.5; }
.article .video-info { margin: -4px 0 0; font-size: 0.79em; color: var(--muted); }
.article .player {
  display: block; width: 100%; aspect-ratio: 16 / 9; border-radius: 18px; background: #000;
  margin: 0 0 12px;
}
.chat {
  margin-top: 28px; padding: 16px; border-radius: 24px; background: var(--card);
  display: flex; flex-direction: column; gap: 12px;
}
.chat h2 { margin: 0; font-size: 19px; }
.turns { display: flex; flex-direction: column; gap: 10px; }
.turns:empty { display: none; }
/* the chat follows the article's A- / A+ size */
.turn { font-size: var(--reader-size, 19px); line-height: 1.55; overflow-wrap: break-word; }
.turn p, .turn ul, .turn ol { margin: 0 0 0.6em; }
.turn > :last-child, .turn .text > :last-child { margin-bottom: 0; }
.turn.user {
  align-self: flex-end; max-width: 88%; padding: 10px 14px; border-radius: 18px;
  background: var(--chip); white-space: pre-wrap;
}
.turn.bot a { color: var(--accent); }
.turn.bot .wait, .turn.bot .failed { color: var(--muted); }
.searches { margin: 0 0 8px; font-size: 0.72em; color: var(--muted); }
.searches div { margin-bottom: 4px; }
.searches a { color: var(--muted); display: block; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.ask { display: flex; gap: 8px; align-items: flex-end; }
.ask textarea {
  flex: 1; min-height: 48px; max-height: 40vh; resize: vertical; padding: 12px 14px;
  border: 1px solid var(--line); border-radius: 16px; background: var(--bg); color: var(--ink);
  /* iOS zooms into a field with text under 16 px */
  font: inherit; font-size: max(16px, var(--reader-size, 19px)); line-height: 1.4;
}
.ask button, .chat .attach {
  height: 48px; border: 0; border-radius: 16px; background: var(--btn); color: var(--btn-ink);
  cursor: pointer; font: inherit; font-size: 15px;
}
.ask button { width: 56px; flex-shrink: 0; display: grid; place-items: center; }
.ask button:disabled, .chat .attach:disabled { opacity: 0.6; }
.chat .attach { background: var(--save-on); color: var(--save-on-ink); padding: 0 16px; }
.chat-note { margin: 0; font-size: 13px; color: var(--muted); }
.end { margin-top: 28px; display: flex; flex-direction: column; gap: 14px; }
.end .card .actions { padding: 12px; }
.original { color: var(--muted); font-size: 15px; margin: 0 6px; }
</style>
</head>
<body>
<header>
  <div class="bar">
    <button class="icon-btn" onclick="back()" aria-label="Back to the feed">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M15 5l-7 7 7 7"/></svg></button>
    <div class="logo wordmark" aria-label="aiblinx">aib<b>linx</b></div>
    <button class="icon-btn size" id="smaller" onclick="fontSize(-1)" \
aria-label="Smaller text">A−</button>
    <button class="icon-btn size up" id="larger" onclick="fontSize(1)" \
aria-label="Larger text">A+</button>
    __LANGBTN__
    __THEMEBTN__
  </div>
</header>
<main>
<div class="meta">__META__</div>
__HEADLINE__
__FIGURE__
__BODY__
__CHAT__
<div class="end">
  <article class="card" data-id="__ID__">
    <div class="gone">Less like this, noted.</div>
    <div class="body">__ACTIONBAR__</div>
  </article>
  <a class="original" href="__URL__" rel="noreferrer">Open the original page ↗</a>
</div>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
__ACTIONS__sizeButtons();
__CHATJS__
</script>
</body>
</html>
"""
