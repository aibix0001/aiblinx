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


def _http(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


_ICON_SAVE = (
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/><path d="M12 7v6M9 10h6"/>'
    "</svg>"
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


def _card(item: dict, new_tab: bool, saved_label: str) -> str:
    cid = int(item["candidate_id"])
    url = _http(item.get("url")) or "#"
    host = (urlsplit(item.get("url") or "").hostname or "").removeprefix("www.")
    meta = [html.escape(host)] if host else []
    if item.get("source") == "hackernews":
        meta.append("Hacker News")
    age = _age(item.get("published_at"))
    if age:
        meta.append(age)
    meta_html = "".join(
        f'<span class="chip">{m}</span>' if i == 0 and host else f"<span>{m}</span>"
        for i, m in enumerate(meta)
    )
    # noreferrer either way: publishers never learn where the reader came from
    link_attrs = 'rel="noopener noreferrer" target="_blank"' if new_tab else 'rel="noreferrer"'
    title = html.escape(item.get("title") or item.get("url") or "")
    summary = html.escape(card_summary(item.get("snippet"), item.get("description")))
    why = html.escape(item.get("reason") or "")
    image = _http(item.get("image_url"))
    figure = (
        f'<img class="hero" src="{html.escape(image, quote=True)}" alt="" loading="lazy" '
        'referrerpolicy="no-referrer" onload="checkImg(this)" onerror="dropImg(this)">'
        if image
        else ""
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
      <div class="actions">
        <button class="save{" on" if saved else ""}" onclick="save(this)"
          aria-pressed="{"true" if saved else "false"}">{_ICON_SAVE}\
<span>{saved_label if saved else "Save"}</span></button>
        <button class="vote{" on" if interest == "up" else ""}" onclick="vote(this, 'up')"
          aria-label="More like this" aria-pressed="{"true" if interest == "up" else "false"}">\
{_ICON_UP}</button>
        <button class="vote" onclick="vote(this, 'down')" aria-label="Less like this">\
{_ICON_DOWN}</button>
      </div>
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
    title = html.escape(save.get("title") or save.get("url") or "")
    target = ' target="_blank"' if new_tab else ""
    return (
        f'<li><a href="{html.escape(url, quote=True)}" rel="noreferrer"{target}>{title}</a>'
        f"<span>{html.escape(host)}</span></li>"
    )


def render_page(
    items: list[dict],
    topics: list[dict] | None = None,
    broad: list[dict] | None = None,
    new_tab: bool = False,
    saves: list[dict] | None = None,
    linkwarden: bool = False,
    has_profile: bool = True,
) -> str:
    """items/broad: current-cycle rows (candidate_id, url, title, snippet,
    description, image_url, reason, source, published_at) plus the viewer's
    ``saved`` flag and latest ``interest`` ("up" / "down" / None).
    topics: dicts with name, selected (the exploring-section picker).
    new_tab: open article links in a new tab instead of the feed's own.
    saves: the local Saved list, newest first (url, title).
    linkwarden: whether saves also go to Linkwarden (labels and hints).
    has_profile: False until something is saved, upvoted or bookmarked — the
    "For you" section then explains how it starts instead of looking broken."""
    broad = broad or []
    topics = topics or []
    saves = saves or []
    selected = sum(1 for t in topics if t["selected"])
    saved_label = "Saved to Linkwarden" if linkwarden else "Saved"
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
    return (
        _PAGE.replace("__HEAD__", HEAD_TAGS)
        .replace("__THEME__", THEME_CSS)
        .replace("__SEL_CURATED__", "false" if start_broad else "true")
        .replace("__SEL_BROAD__", "true" if start_broad else "false")
        .replace("__HID_CURATED__", " hidden" if start_broad else "")
        .replace("__HID_BROAD__", "" if start_broad else " hidden")
        .replace("__SAVED_LABEL__", saved_label)
        .replace("__SAVED_NOTE__", saved_note)
        .replace("__SAVED_COUNT__", str(len(saves)))
        .replace("__SAVED__", saved_list)
        .replace(
            "__CURATED__",
            "\n".join(_card(i, new_tab, saved_label) for i in items) or curated_empty,
        )
        .replace(
            "__BROAD__", "\n".join(_card(i, new_tab, saved_label) for i in broad) or broad_empty
        )
        .replace("__TOPICS_SELECTED__", str(selected))
        .replace("__TOPICS__", topic_boxes or "<p>No topics available.</p>")
    )


# Shared by every page: theme tokens (light by default, dark by system setting
# or the per-device toggle) and the head tags for the home-screen app.
THEME_CSS = """\
:root {
  --bg: #F4F4EF; --bar: rgba(244, 244, 239, 0.86); --card: #FFFFFF; --ink: #15170F;
  --muted: #545849; --line: #E2E3DA; --chip: #ECEDE5; --btn: #ECEDE5; --btn-ink: #15170F;
  --on: #15170F; --on-ink: #FFFFFF; --save-on: #C6F432; --save-on-ink: #0E0F0C;
  --img: #DEDFD6;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0E0F0C; --bar: rgba(14, 15, 12, 0.84); --card: #1A1C17; --ink: #F2F2EC;
    --muted: #B4B7AB; --line: #2A2D25; --chip: #262923; --btn: #262923; --btn-ink: #E4E6DC;
    --on: #F2F2EC; --on-ink: #0E0F0C; --img: #2B2E26;
  }
}
:root[data-theme="dark"] {
  --bg: #0E0F0C; --bar: rgba(14, 15, 12, 0.84); --card: #1A1C17; --ink: #F2F2EC;
  --muted: #B4B7AB; --line: #2A2D25; --chip: #262923; --btn: #262923; --btn-ink: #E4E6DC;
  --on: #F2F2EC; --on-ink: #0E0F0C; --img: #2B2E26;
}
/* the wordmark: "aib" (the maker's prefix) + "linx" on the lime highlight */
.wordmark { font-weight: 750; letter-spacing: -0.02em; white-space: nowrap; }
.wordmark b {
  font-weight: inherit; background: #C6F432; color: #0E0F0C;
  border-radius: 5px; padding: 0 3px; margin-left: 1px;
}
"""

HEAD_TAGS = """\
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#F4F4EF" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E0F0C" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icons/icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="aiblinx">
<script>
try { const t = localStorage.getItem('theme'); if (t) document.documentElement.dataset.theme = t; }
catch (e) {}
</script>"""


_PAGE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
<title>aiblinx</title>
<script>
// in <head>: a cached image can fire onload/onerror before the body script runs
function dropImg(img) { img.remove(); }
function checkImg(img) {
  // logos and banners are not title pictures
  if (img.naturalWidth < 200 || img.naturalWidth / img.naturalHeight > 3) img.remove();
}
</script>
<style>
__THEME__* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%;
}
header {
  position: sticky; top: 0; z-index: 2; background: var(--bar);
  -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px);
  padding: calc(env(safe-area-inset-top) + 12px) 12px 12px;
}
.bar { max-width: 640px; margin: 0 auto; display: flex; align-items: center; gap: 6px; }
.logo { font-size: 22px; margin-right: auto; }
.tabs { display: flex; gap: 4px; padding: 3px; border-radius: 999px; background: var(--chip); }
.tabs button {
  height: 38px; padding: 0 11px; border: 0; border-radius: 999px; font: inherit;
  white-space: nowrap;
  font-size: 14px; background: transparent; color: var(--muted); cursor: pointer;
}
.tabs button[aria-selected="true"] { background: var(--on); color: var(--on-ink); }
.icon-btn {
  width: 42px; height: 42px; flex-shrink: 0; border: 0; border-radius: 999px;
  background: var(--chip);
  color: var(--ink); display: grid; place-items: center; cursor: pointer;
}
main {
  max-width: 640px; margin: 0 auto;
  padding: 6px 12px calc(env(safe-area-inset-bottom) + 40px);
}
.panel { display: flex; flex-direction: column; gap: 14px; }
.panel[hidden] { display: none; }
.card { border-radius: 24px; background: var(--card); overflow: hidden; }
.card .gone { display: none; }
.card[data-down] .body { display: none; }
.card[data-down] .gone {
  display: block; padding: 14px 18px; font-size: 15px; color: var(--muted);
}
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
.actions .save { flex-grow: 1; }
.actions .vote { width: 56px; flex-shrink: 0; }
.actions .save.on { background: var(--save-on); color: var(--save-on-ink); }
.actions .vote.on { background: var(--on); color: var(--on-ink); }
.actions button:disabled { opacity: 0.6; }
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
#toast {
  position: fixed; left: 50%; bottom: calc(env(safe-area-inset-bottom) + 20px);
  transform: translateX(-50%); padding: 12px 18px; border-radius: 14px;
  background: var(--on); color: var(--on-ink); font-size: 15px; opacity: 0;
  transition: opacity 160ms; pointer-events: none;
}
#toast.show { opacity: 1; }
@media (min-width: 700px) { main { padding-top: 16px; } .panel { gap: 20px; } }
</style>
</head>
<body>
<header>
  <div class="bar">
    <div class="logo wordmark" aria-label="aiblinx">aib<b>linx</b></div>
    <div class="tabs" role="tablist">
      <button role="tab" aria-selected="__SEL_CURATED__" data-panel="curated" onclick="tab(this)">\
For you</button>
      <button role="tab" aria-selected="__SEL_BROAD__" data-panel="broad" onclick="tab(this)">\
Exploring</button>
    </div>
    <button class="icon-btn" data-panel="saved" aria-selected="false" onclick="tab(this)"
      aria-label="Saved stories">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"/></svg></button>
    <button class="icon-btn" onclick="toggleTheme()" aria-label="Switch light or dark mode">\
<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" \
stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">\
<path d="M12 3a9 9 0 1 0 9 9 7 7 0 0 1-9-9z"/></svg></button>
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
<section class="panel" id="saved" hidden>
<div class="saved-head"><h2>Saved · __SAVED_COUNT__</h2><p>__SAVED_NOTE__</p></div>
__SAVED__
<p class="panel-foot"><a href="/setup">Setup: imports, topics and connections</a></p>
</section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2200);
}
function tab(btn) {
  document.querySelectorAll('[data-panel]').forEach(b => {
    const on = b === btn;
    b.setAttribute('aria-selected', on);
    document.getElementById(b.dataset.panel).hidden = !on;
  });
  window.scrollTo(0, 0);
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
async function save(btn) {
  if (btn.classList.contains('on')) return;
  const card = btn.closest('.card');
  btn.disabled = true;
  try {
    await post(card.dataset.id, 'save');
    btn.classList.add('on'); btn.setAttribute('aria-pressed', 'true');
    btn.querySelector('span').textContent = '__SAVED_LABEL__';
  } catch (e) { toast('Could not save: ' + e.message); }
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
