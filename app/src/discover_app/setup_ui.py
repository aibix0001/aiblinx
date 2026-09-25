"""The setup page (first run and later imports) and the optional login page.

Setup is three independent parts, none required: give the feed something to
learn from (browser bookmarks, pasted URLs, an OPML feed list, or topics), see
what is connected (configured through the environment, so the page shows the
variable to set rather than a form), and build the first feed. Uploads are
read in the browser and posted as JSON, so the app needs no multipart parser.
"""

from __future__ import annotations

import html

from .ui import HEAD_TAGS, THEME_CSS

_TOPIC = (
    '<label class="topic"><input type="checkbox" data-name="{name}" {checked} '
    'onchange="topic(this)"> {label}</label>'
)


def _status_row(name: str, on: bool, detail: str, labels=("Connected", "Not connected")) -> str:
    state = labels[0] if on else labels[1]
    return (
        f'<li><div><b>{html.escape(name)}</b><span class="state{" on" if on else ""}">'
        f"{state}</span></div><p>{detail}</p></li>"
    )


def render_setup(
    *,
    topics: list[dict],
    counts: dict[str, int],
    linkwarden: bool,
    miniflux: bool,
    chat_model: str,
    embed_model: str,
    password_on: bool,
    atom_url: str,
    has_feed: bool,
) -> str:
    """counts: bookmarks, imports, saves, upvotes (what the profile has to go on)."""
    topic_boxes = "\n".join(
        _TOPIC.format(
            name=html.escape(t["name"], quote=True),
            checked="checked" if t["selected"] else "",
            label=html.escape(t["name"]),
        )
        for t in topics
    )
    known = counts["bookmarks"] + counts["imports"] + counts["saves"] + counts["upvotes"]
    learned = (
        f"The feed learns from {known} page{'s' if known != 1 else ''} so far: "
        f"{counts['bookmarks']} Linkwarden bookmarks, {counts['imports']} imported, "
        f"{counts['saves']} saved, {counts['upvotes']} upvoted."
    )
    connections = "".join(
        [
            _status_row(
                "Linkwarden",
                linkwarden,
                "Your bookmarks shape the feed and saves go there too."
                if linkwarden
                else "Optional. Set <code>LINKWARDEN_BASE_URL</code> and "
                "<code>LINKWARDEN_TOKEN</code> to use your bookmarks. Saves made "
                "before then move over automatically.",
            ),
            _status_row(
                "Miniflux",
                miniflux,
                "Subscribes to the sites you keep and supplies their new articles."
                if miniflux
                else "Optional. Without it, aiblinx reads your sites' feeds itself. For a "
                "full reader app, start the <code>miniflux</code> profile; the API key "
                "is created for you.",
            ),
            _status_row(
                "AI model",
                bool(chat_model or embed_model),
                f"Ranking with <code>{html.escape(chat_model)}</code>, "
                f"embeddings with <code>{html.escape(embed_model)}</code>.",
                labels=("Configured", "Not configured"),
            ),
            _status_row(
                "Password",
                password_on,
                "This page and your feed need the password once per device."
                if password_on
                else "Set <code>APP_PASSWORD</code> if the feed is reachable "
                "from outside your home network.",
                labels=("On", "Off"),
            ),
        ]
    )
    atom = (
        f'<p class="hint">Reader app: <code class="copy">{html.escape(atom_url)}</code></p>'
        if atom_url
        else ""
    )
    build = (
        '<p>Your feed is built. It refreshes every morning.</p><a class="btn" href="/ui">'
        "Open the feed</a>"
        if has_feed
        else "<p>Builds your first feed now instead of waiting for the next morning. "
        'It takes a few minutes.</p><button class="btn" id="build" onclick="build()">'
        'Build my feed</button><p class="hint" id="build-status" role="status"></p>'
    )
    return (
        _SETUP.replace("__HEAD__", HEAD_TAGS)
        .replace("__THEME__", THEME_CSS)
        .replace("__LEARNED__", html.escape(learned))
        .replace("__OPML_BUTTON__", _OPML_BUTTON)
        .replace(
            "__OPML_NOTE__",
            "subscribes every feed in the file through Miniflux."
            if miniflux
            else "subscribes every feed in the file.",
        )
        .replace("__TOPICS__", topic_boxes)
        .replace("__CONNECTIONS__", connections)
        .replace("__ATOM__", atom)
        .replace("__BUILD__", build)
    )


def render_login() -> str:
    return _LOGIN.replace("__HEAD__", HEAD_TAGS).replace("__THEME__", THEME_CSS)


_OPML_BUTTON = """\
    <label class="btn soft file">Import OPML feeds
      <input type="file" accept=".opml,.xml,text/xml" onchange="upload(this, 'opml')">
    </label>"""

_BASE_CSS = """
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%;
}
main {
  max-width: 640px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px;
  padding: calc(env(safe-area-inset-top) + 16px) 12px calc(env(safe-area-inset-bottom) + 40px);
}
h1 { margin: 4px 6px 0; font-size: 28px; letter-spacing: -0.02em; }
h1 .wordmark b { font-size: 0.92em; }
h2 { margin: 0; font-size: 19px; }
p { margin: 0; line-height: 1.5; }
code { font-size: 0.9em; background: var(--chip); padding: 1px 5px; border-radius: 5px; }
.lede { margin: 0 6px; color: var(--muted); font-size: 16px; }
section { background: var(--card); border-radius: 24px; padding: 18px; display: flex;
  flex-direction: column; gap: 12px; }
section > p { color: var(--muted); font-size: 15px; }
.hint { color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; min-height: 48px;
  padding: 0 18px; border: 0; border-radius: 16px; font: inherit; font-size: 15px;
  background: var(--on); color: var(--on-ink); text-decoration: none; cursor: pointer;
  align-self: flex-start;
}
.btn.soft { background: var(--btn); color: var(--btn-ink); }
.btn:disabled { opacity: 0.5; cursor: default; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
textarea {
  width: 100%; min-height: 96px; border-radius: 14px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: inherit; font-size: 15px; padding: 10px 12px;
}
input[type="password"] {
  width: 100%; height: 48px; border-radius: 14px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); font: inherit; font-size: 16px; padding: 0 12px;
}
label.file { position: relative; }
label.file input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
label.topic { display: inline-flex; align-items: center; gap: 6px; margin: 4px 14px 4px 0;
  font-size: 15px; }
ul.status { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
ul.status li { padding: 10px 0; border-top: 1px solid var(--line); display: flex;
  flex-direction: column; gap: 4px; }
ul.status li:first-child { border-top: 0; padding-top: 0; }
ul.status li div { display: flex; justify-content: space-between; gap: 8px; }
ul.status p { color: var(--muted); font-size: 14px; }
.state { font-size: 13px; color: var(--muted); }
.state.on { color: var(--ink); font-weight: 600; }
a { color: var(--ink); }
:focus-visible { outline: 2px solid #8DB600; outline-offset: 2px; }
"""

_SETUP = (
    """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
<title>aiblinx setup</title>
<style>
__THEME__"""
    + _BASE_CSS
    + """</style>
</head>
<body>
<main>
<h1><span class="wordmark" aria-label="aiblinx">aib<b>linx</b></span> setup</h1>
<p class="lede">__LEARNED__</p>

<section>
  <h2>Tell it what you like</h2>
  <p>Anything you import becomes part of your interest profile. Nothing is shared.</p>
  <div class="row">
    <label class="btn soft file">Import browser bookmarks
      <input type="file" accept=".html,.htm,text/html" onchange="upload(this, 'bookmarks')">
    </label>
__OPML_BUTTON__
  </div>
  <p class="hint">Bookmarks: in your browser, use Export bookmarks (an HTML file).
  OPML: __OPML_NOTE__</p>
  <label for="urls" class="hint">Or paste links to articles you liked, one or many:</label>
  <textarea id="urls" placeholder="https://…"></textarea>
  <button class="btn soft" onclick="pasteUrls()">Add links</button>
  <p class="hint" id="import-status" role="status"></p>
</section>

<section>
  <h2>Topics to explore</h2>
  <p>Stories on these topics fill the Exploring section, and give a new install
  something to react to on day one.</p>
  <div>__TOPICS__</div>
</section>

<section>
  <h2>Connections</h2>
  <ul class="status">__CONNECTIONS__</ul>
  __ATOM__
</section>

<section>
  <h2>Your feed</h2>
  __BUILD__
</section>
<p class="hint" style="margin: 0 6px;"><a href="/ui">Back to the feed</a></p>
</main>
<script>
function say(id, msg) { document.getElementById(id).textContent = msg; }
async function send(kind, content) {
  const resp = await fetch('/setup/import', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind, content})});
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.status);
  return data;
}
function report(data) {
  say('import-status', data.message);
}
async function upload(input, kind) {
  const file = input.files[0];
  if (!file) return;
  say('import-status', 'Importing ' + file.name + '…');
  try { report(await send(kind, await file.text())); }
  catch (e) { say('import-status', 'Import failed: ' + e.message); }
  input.value = '';
}
async function pasteUrls() {
  const box = document.getElementById('urls');
  if (!box.value.trim()) return;
  say('import-status', 'Reading the pages…');
  try { report(await send('urls', box.value)); box.value = ''; }
  catch (e) { say('import-status', 'Import failed: ' + e.message); }
}
async function topic(box) {
  box.disabled = true;
  try {
    const resp = await fetch(`/topics/${encodeURIComponent(box.dataset.name)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected: box.checked})});
    if (!resp.ok) throw new Error(resp.status);
  } catch (e) { box.checked = !box.checked; }
  finally { box.disabled = false; }
}
async function build() {
  const btn = document.getElementById('build');
  btn.disabled = true;
  say('build-status', 'Building your feed. This takes a few minutes; you can leave this page.');
  const resp = await fetch('/setup/build', {method: 'POST'});
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    say('build-status', data.detail || 'Could not start the build.');
    btn.disabled = false;
    return;
  }
  const poll = setInterval(async () => {
    const s = await (await fetch('/setup/status')).json().catch(() => ({}));
    if (s.running) return;
    clearInterval(poll);
    if (s.has_feed) { location.href = '/ui'; }
    else { say('build-status', s.message || 'The build finished without stories.');
      btn.disabled = false; }
  }, 4000);
}
</script>
</body>
</html>
"""
)

_LOGIN = (
    """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
__HEAD__
<title>aiblinx</title>
<style>
__THEME__"""
    + _BASE_CSS
    + """</style>
</head>
<body>
<main>
<h1><span class="wordmark" aria-label="aiblinx">aib<b>linx</b></span></h1>
<section>
  <form id="login" style="display: flex; flex-direction: column; gap: 12px;">
    <label for="password"><b>Password</b></label>
    <input type="password" id="password" autocomplete="current-password" required autofocus>
    <button class="btn" type="submit">Open my feed</button>
    <p class="hint" id="login-status" role="status"></p>
  </form>
</section>
</main>
<script>
document.getElementById('login').addEventListener('submit', async (e) => {
  e.preventDefault();
  const resp = await fetch('/login', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: document.getElementById('password').value})});
  if (resp.ok) {
    const next = new URLSearchParams(location.search).get('next') || '/ui';
    location.href = next.startsWith('/') && !next.startsWith('//') ? next : '/ui';
  } else {
    document.getElementById('login-status').textContent = 'That password is not right.';
  }
});
</script>
</body>
</html>
"""
)
