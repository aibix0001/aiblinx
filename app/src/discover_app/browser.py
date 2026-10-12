"""Render pages in a remote headless browser for the reader.

Some pages hold their article only after a consent banner is accepted, or
build their text with scripts (inside shadow roots, where a plain fetch never
sees it). Those are opened in Chromium on a Playwright server
(``BROWSER_WS_URL``, the compose service ``browser``), so no browser runs in
the app itself. Each page gets a browser of its own (the server starts one
per connection and kills it on disconnect): no cookies carry over from one
page to the next, and a page busy with ads can't hold up closing it. Images,
media and fonts are never loaded: the extractor reads only the HTML.

The browser is the full Chromium build in its new headless mode, not the
stripped headless shell, and it names itself as the Chrome it is, without
"Headless": bot checks such as Cloudflare's stop either of those for good.

DuckDuckGo's autoconsent (https://github.com/duckduckgo/autoconsent, MPL-2.0)
runs in every page and frame, set to accept: it knows hundreds of consent
tools and looks for unknown ones by heuristics. A banner it misses can still
be clicked through ``Tab.buttons`` / ``Tab.click``, which the caller drives
(``article.py`` lets the chat model pick the button).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from .config import Settings

log = logging.getLogger(__name__)

__all__ = ["PlaywrightError", "Tab", "open_tab"]

# How long to wait for autoconsent to find a banner, then to finish with it
_DETECT_S = 4.0
_ACCEPT_S = 10.0
# After a click: time for the banner to go and the page to redraw
_SETTLE_S = 2.0
_MAX_BUTTONS_PER_FRAME = 100

_CONFIG = {
    "enabled": True,
    "autoAction": "optIn",
    "disabledCmps": [],
    "enablePrehide": False,
    "enableCosmeticRules": False,
    "enableGeneratedRules": True,
    "enableHeuristicDetection": True,
    "heuristicMode": "tier2",
    "detectRetries": 20,
    "isMainWorld": False,
    "prehideTimeout": 2000,
    "logs": dict.fromkeys(
        ("lifecycle", "rulesteps", "detectionsteps", "evals", "errors", "messages", "waits"),
        False,
    ),
}

# The page's HTML with every open shadow root written out in place, so text
# that web components render is there for the extractor.
_HTML = """() => {
    const roots = [];
    const walk = (node) => {
        for (const el of node.querySelectorAll('*')) {
            if (el.shadowRoot) { roots.push(el.shadowRoot); walk(el.shadowRoot); }
        }
    };
    walk(document);
    return '<!doctype html>' + document.documentElement.getHTML({shadowRoots: roots});
}"""

# The frame's visible clickable elements (shadow roots included), each marked
# with its number so it can be clicked again; returns their labels.
_BUTTONS = """([start, limit]) => {
    const out = [];
    const sel = 'button, a, [role="button"], input[type="button"], input[type="submit"]';
    const visit = (root) => {
        for (const el of root.querySelectorAll(sel)) {
            if (out.length >= limit) return;
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            if (box.width < 4 || box.height < 4 || style.visibility === 'hidden') continue;
            const label = (el.innerText || el.value || el.getAttribute('aria-label') || '')
                .replace(/\\s+/g, ' ').trim();
            if (!label || label.length > 80) continue;
            el.setAttribute('data-aiblinx-click', String(start + out.length));
            out.push(label);
        }
        for (const el of root.querySelectorAll('*')) if (el.shadowRoot) visit(el.shadowRoot);
    };
    visit(document);
    return out;
}"""

_SKIPPED = {"image", "media", "font"}
# read by ``playwright run-server`` for the browser it starts per connection
_LAUNCH = {"x-playwright-launch-options": json.dumps({"channel": "chromium"})}

_playwright: Playwright | None = None
_slots: asyncio.Semaphore | None = None


@cache
def _autoconsent(directory: str) -> tuple[str, dict] | None:
    """(content script, rules) from ``directory``, or None when not there."""
    root = Path(directory)
    try:
        script = (root / "dist" / "autoconsent.playwright.js").read_text()
        rules = json.loads((root / "rules" / "rules.json").read_text())
    except OSError:
        log.warning("browser: autoconsent not found in %s, banners go to the chat model", root)
        return None
    return script, rules


_CLOSED = ("has been closed", "connection closed")


def _quiet_closed(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Playwright answers a page's last requests and binding calls from
    tasks nobody awaits; once the browser is gone those fail ("Target page,
    context or browser has been closed", "Playwright connection closed"),
    which asyncio would log as an error. Nothing is lost."""
    exc = context.get("exception")
    if not (isinstance(exc, PlaywrightError) and any(m in str(exc) for m in _CLOSED)):
        loop.default_exception_handler(context)


async def _skip_heavy(route: Route) -> None:
    try:
        if route.request.resource_type in _SKIPPED:
            await route.abort()
        else:
            await route.continue_()
    except PlaywrightError:
        pass  # the page or browser closed meanwhile


class Tab:
    """One page in its own browser context."""

    def __init__(self, context: BrowserContext, page: Page, timeout_s: float) -> None:
        self._context = context
        self._page = page
        self._timeout_ms = timeout_s * 1000
        self._frames: dict[int, Frame] = {}
        self._detected = asyncio.Event()
        self._done = asyncio.Event()
        self._replies: set[asyncio.Task] = set()

    async def _autoconsent(self, directory: str) -> None:
        assets = _autoconsent(directory)
        if assets is None:
            return
        script, rules = assets

        async def reply(frame: Frame, message: dict) -> None:
            try:
                await frame.evaluate(
                    "m => window.autoconsentReceiveMessage && window.autoconsentReceiveMessage(m)",
                    message,
                )
            except PlaywrightError:
                pass  # the frame went away

        async def receive(source: dict, message: dict) -> None:
            frame, kind = source["frame"], message.get("type")
            if kind == "init":
                answer = {"type": "initResp", "config": _CONFIG, "rules": rules}
            elif kind == "eval":
                try:
                    result = await frame.evaluate(message["code"])
                except PlaywrightError:
                    result = False
                answer = {"type": "evalResp", "id": message["id"], "result": result}
            else:
                if kind in ("cmpDetected", "popupFound"):
                    self._detected.set()
                elif kind == "autoconsentDone":
                    self._done.set()
                return
            # answered from a task: the page waits for this binding to return
            task = asyncio.get_running_loop().create_task(reply(frame, answer))
            self._replies.add(task)
            task.add_done_callback(self._replies.discard)

        await self._page.expose_binding("autoconsentSendMessage", receive)
        await self._context.add_init_script(script)

    async def _load(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        try:
            await asyncio.wait_for(self._detected.wait(), _DETECT_S)
            await asyncio.wait_for(self._done.wait(), _ACCEPT_S)
        except TimeoutError:
            pass  # no banner, or one autoconsent could not handle
        await self._settle()

    async def _settle(self) -> None:
        try:
            await self._page.wait_for_load_state("load", timeout=_SETTLE_S * 1000)
        except PlaywrightError:
            pass  # still loading ads and such: the text is there by now
        await asyncio.sleep(_SETTLE_S)

    async def html(self) -> bytes:
        """The page as it stands, shadow roots written out. Raises
        ``TimeoutError`` when the page's scripts keep it too busy to answer."""
        await self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        page = await asyncio.wait_for(self._page.evaluate(_HTML), self._timeout_ms / 1000)
        return page.encode()

    async def buttons(self) -> list[str]:
        """Labels of the visible clickable elements in all frames, in order;
        ``click(n)`` clicks the n-th."""
        labels: list[str] = []
        self._frames = {}
        for frame in self._page.frames:
            try:
                found = await asyncio.wait_for(
                    frame.evaluate(_BUTTONS, [len(labels), _MAX_BUTTONS_PER_FRAME]), _SETTLE_S * 2
                )
            except (PlaywrightError, TimeoutError):
                continue  # detached, cross-origin trouble or busy: skip that frame
            for n in range(len(labels), len(labels) + len(found)):
                self._frames[n] = frame
            labels += found
        return labels

    async def click(self, n: int) -> None:
        frame = self._frames[n]
        await frame.locator(f'[data-aiblinx-click="{n}"]').first.click(timeout=5000)
        await self._settle()


def _user_agent(version: str) -> str:
    """What Chrome on Linux sends: its major version only, and no "Headless"."""
    major = version.split(".")[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


@asynccontextmanager
async def open_tab(settings: Settings, url: str) -> AsyncIterator[Tab]:
    """``url`` loaded in a browser of its own, its consent banner accepted
    when autoconsent knows how. Raises ``PlaywrightError`` when the browser
    or the page can't be reached."""
    global _playwright, _slots
    if _slots is None:
        _slots = asyncio.Semaphore(settings.browser_concurrency)
    async with _slots:
        if _playwright is None:
            _playwright = await async_playwright().start()
            asyncio.get_running_loop().set_exception_handler(_quiet_closed)
        browser = await _playwright.chromium.connect(
            settings.browser_ws_url, timeout=settings.browser_timeout_s * 1000, headers=_LAUNCH
        )
        try:
            context = await browser.new_context(user_agent=_user_agent(browser.version))
            await context.route("**/*", _skip_heavy)
            tab = Tab(context, await context.new_page(), settings.browser_timeout_s)
            await tab._autoconsent(settings.autoconsent_dir)
            await tab._load(url)
            yield tab
        finally:
            # disconnecting makes the server kill that browser, busy or not
            await browser.close()
