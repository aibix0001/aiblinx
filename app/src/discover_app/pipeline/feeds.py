"""Subscribe to the site feeds of domains the user keeps pages from.

Domains come from the pages the user keeps — the ``links`` mirror of
Linkwarden (when connected), the local Saved list and upvoted pages — ranked
by how often each site appears. Each domain is tried once and remembered in
``feed_domains``; a bounded number of new domains is tried per cycle because
every discovery makes Miniflux fetch the remote site.

Miniflux answers per-site problems (unreachable site, unparsable feed) with
4xx/5xx, so an HTTP status error is recorded against that domain, and so is a
timeout: discovery probes several well-known feed paths in turn and a slow
site can outlast the client timeout — left unrecorded, that domain would head
the list and stall the sync every cycle. Any other transport error means
Miniflux itself is unreachable — that aborts the sync without recording
anything, so the domains are retried next cycle.
"""

from __future__ import annotations

import logging
from collections import Counter
from urllib.parse import urlsplit

import httpx

from ..clients.miniflux import MinifluxClient
from ..clients.rss import discover_feed, looks_like_feed, subscribe
from ..config import Settings, get_settings
from ..db import connection
from ..urls import norm_url
from .enrich import PAGE_HEADERS
from .feedback import known_explore_collection

log = logging.getLogger(__name__)


def pending_domains(settings: Settings) -> list[str]:
    """Sites worth a feed lookup, most-kept first."""
    exclude = {d.lower() for d in settings.feed_sync_exclude}
    with connection(settings) as conn:
        tried = {row[0] for row in conn.execute("SELECT domain FROM feed_domains")}
        # Every page the user keeps counts once: bookmarks, local saves not
        # (yet) in Linkwarden, and upvoted pages. Feedback URLs are stored
        # normalized ("host/path"), so they are read as scheme-less.
        # Exploring saves and votes are distractions, not sites to follow.
        pages = {
            norm_url(url): url
            for (url,) in conn.execute(
                "SELECT url FROM links WHERE :explore IS NULL OR collection_id IS NOT :explore",
                {"explore": known_explore_collection(conn, settings)},
            )
        }
        for (url,) in conn.execute(
            "SELECT url FROM saves WHERE section = 'curated' UNION ALL SELECT url FROM imports"
        ):
            pages.setdefault(norm_url(url), url)
        for (key,) in conn.execute(
            "SELECT url FROM feedback WHERE value = 'up' AND section = 'curated' AND id IN ("
            "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url)"
        ):
            pages.setdefault(key, f"https://{key}")
        counts = Counter(
            (urlsplit(url).hostname or "").removeprefix("www.") for url in pages.values()
        )
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        domain
        for domain, n in ranked
        if domain
        and n >= settings.feed_sync_min_links
        and domain not in exclude
        and domain not in tried
    ][: settings.feed_sync_per_cycle]


def _record(settings: Settings, domain: str, status: str, feed_url=None, detail=None) -> None:
    with connection(settings) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feed_domains(domain, status, feed_url, detail) "
            "VALUES(?, ?, ?, ?)",
            (domain, status, feed_url, detail),
        )


async def _sync_builtin(settings: Settings, domains: list[str]) -> int:
    """No Miniflux: find each site's feed ourselves and subscribe it in the
    built-in reader. A site with no feed is recorded like Miniflux's 404."""
    subscribed = 0
    async with httpx.AsyncClient(
        headers=PAGE_HEADERS, timeout=settings.enrich_timeout_s, follow_redirects=True
    ) as client:
        for domain in domains:
            feed_url = await discover_feed(client, f"https://{domain}")
            if not feed_url:
                _record(settings, domain, "no_feed")
                continue
            subscribe(settings, feed_url, domain)
            _record(settings, domain, "subscribed", feed_url)
            subscribed += 1
    log.info("sync_feeds: %d of %d domain(s) subscribed (built-in)", subscribed, len(domains))
    return subscribed


async def sync_feeds(settings: Settings | None = None) -> int:
    """Try up to ``feed_sync_per_cycle`` new domains; return feeds subscribed.
    Subscribes in Miniflux when it is connected, else in the built-in reader."""
    settings = settings or get_settings()
    domains = pending_domains(settings)
    if not domains:
        return 0
    if not settings.miniflux_token:
        return await _sync_builtin(settings, domains)
    miniflux = MinifluxClient(settings)
    subscribed = 0
    try:
        category_id = await miniflux.first_category_id()
        for domain in domains:
            try:
                feeds = await miniflux.discover(f"https://{domain}")
            except httpx.HTTPStatusError as exc:
                _record(settings, domain, "no_feed", detail=exc.response.text[:200])
                continue
            except httpx.TimeoutException:
                _record(settings, domain, "failed", detail="discovery timed out")
                continue
            if not feeds:
                _record(settings, domain, "no_feed")
                continue
            feed_url = feeds[0]["url"]
            try:
                await miniflux.create_feed(feed_url, category_id)
            except httpx.TimeoutException:
                _record(settings, domain, "failed", feed_url, "subscription timed out")
                continue
            except httpx.HTTPStatusError as exc:
                if "already exists" not in exc.response.text:
                    _record(settings, domain, "failed", feed_url, exc.response.text[:200])
                    continue
            _record(settings, domain, "subscribed", feed_url)
            subscribed += 1
    finally:
        await miniflux.aclose()
    log.info("sync_feeds: %d of %d domain(s) subscribed", subscribed, len(domains))
    return subscribed


# ── Managing subscriptions from the setup page ─────────────────
# One interface over both readers: Miniflux when connected (ids are
# Miniflux feed ids), else the built-in ``feeds`` table (ids are its rows).


def _domain(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").removeprefix("www.")


async def list_feeds(settings: Settings) -> list[dict]:
    """Subscribed feeds, ``{"id", "title", "site", "url"}``, by title."""
    if settings.miniflux_token:
        miniflux = MinifluxClient(settings)
        try:
            raw = await miniflux.list_feeds()
        finally:
            await miniflux.aclose()
        feeds = [
            {
                "id": int(f["id"]),
                "title": f.get("title") or _domain(f.get("site_url") or f.get("feed_url")),
                "site": _domain(f.get("site_url") or f.get("feed_url")),
                "url": f.get("feed_url") or "",
            }
            for f in raw
        ]
    else:
        with connection(settings) as conn:
            rows = conn.execute("SELECT id, url, site FROM feeds").fetchall()
        feeds = [
            {
                "id": row["id"],
                "title": row["site"] or _domain(row["url"]),
                "site": row["site"] or _domain(row["url"]),
                "url": row["url"],
            }
            for row in rows
        ]
    return sorted(feeds, key=lambda f: f["title"].casefold())


async def add_feed(settings: Settings, url: str) -> str:
    """Subscribe to a site or feed URL; returns the feed URL subscribed.
    Raises LookupError when no feed can be found there."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if settings.miniflux_token:
        miniflux = MinifluxClient(settings)
        try:
            try:
                found = await miniflux.discover(url)
            except httpx.HTTPStatusError:
                found = []
            if not found:
                raise LookupError(f"No feed found at {url}")
            feed_url = found[0]["url"]
            try:
                await miniflux.create_feed(feed_url, await miniflux.first_category_id())
            except httpx.HTTPStatusError as exc:
                if "already exists" not in exc.response.text:
                    raise
        finally:
            await miniflux.aclose()
    else:
        async with httpx.AsyncClient(
            headers=PAGE_HEADERS, timeout=settings.enrich_timeout_s, follow_redirects=True
        ) as client:
            feed_url = None
            try:  # the URL may be the feed itself
                probe = await client.get(url)
                if probe.status_code == 200 and looks_like_feed(probe.content):
                    feed_url = str(probe.url)
            except httpx.HTTPError:
                pass
            feed_url = feed_url or await discover_feed(client, url)
        if not feed_url:
            raise LookupError(f"No feed found at {url}")
        subscribe(settings, feed_url, _domain(url))
    # a manual subscription also ends an earlier "unsubscribed" block
    _record(settings, _domain(url), "subscribed", feed_url)
    log.info("feeds: subscribed %s", feed_url)
    return feed_url


async def remove_feed(settings: Settings, feed_id: int) -> None:
    """Unsubscribe, and keep automatic discovery from adding the site back.
    Raises LookupError for an unknown id."""
    if settings.miniflux_token:
        miniflux = MinifluxClient(settings)
        try:
            try:
                feed = await miniflux.get_feed(feed_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise LookupError(f"no feed {feed_id}") from exc
                raise
            await miniflux.delete_feed(feed_id)
        finally:
            await miniflux.aclose()
        domain = _domain(feed.get("site_url") or feed.get("feed_url"))
        feed_url = feed.get("feed_url")
    else:
        with connection(settings) as conn:
            row = conn.execute("SELECT url, site FROM feeds WHERE id = ?", (feed_id,)).fetchone()
            if row is None:
                raise LookupError(f"no feed {feed_id}")
            conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        domain = row["site"] or _domain(row["url"])
        feed_url = row["url"]
    if domain:
        _record(settings, domain, "unsubscribed", feed_url)
    log.info("feeds: unsubscribed %s", feed_url)
