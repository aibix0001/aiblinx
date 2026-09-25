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
from ..clients.rss import discover_feed, subscribe
from ..config import Settings, get_settings
from ..db import connection
from ..urls import norm_url
from .enrich import PAGE_HEADERS

log = logging.getLogger(__name__)


def pending_domains(settings: Settings) -> list[str]:
    """Sites worth a feed lookup, most-kept first."""
    exclude = {d.lower() for d in settings.feed_sync_exclude}
    with connection(settings) as conn:
        tried = {row[0] for row in conn.execute("SELECT domain FROM feed_domains")}
        # Every page the user keeps counts once: bookmarks, local saves not
        # (yet) in Linkwarden, and upvoted pages. Feedback URLs are stored
        # normalized ("host/path"), so they are read as scheme-less.
        pages = {norm_url(url): url for (url,) in conn.execute("SELECT url FROM links")}
        for (url,) in conn.execute("SELECT url FROM saves UNION ALL SELECT url FROM imports"):
            pages.setdefault(norm_url(url), url)
        for (key,) in conn.execute(
            "SELECT url FROM feedback WHERE value = 'up' AND id IN ("
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
