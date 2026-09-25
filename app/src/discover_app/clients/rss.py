"""Built-in feed reader: the minimal install's replacement for Miniflux.

Without Miniflux, feed sync subscribes discovered site feeds into the local
``feeds`` table, and each cycle reads them directly: newest entries per feed,
within the candidate freshness window. Miniflux stays the better choice for
people who also want to read feeds in a reader app; this covers the
"one container" start.
"""

from __future__ import annotations

import asyncio
import logging
import re
from calendar import timegm
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx

from ..config import Settings
from ..db import connection
from ..html_text import first_image, strip_html
from ..pipeline.enrich import PAGE_HEADERS

log = logging.getLogger(__name__)

_LINK_TAG = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"([a-zA-Z_:][-\w:.]*)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
# Where sites without a <link rel="alternate"> usually keep their feed.
_COMMON_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")
_MAX_FEED_BYTES = 2 * 1024 * 1024
_ENTRIES_PER_FEED = 20


def _looks_like_feed(body: bytes) -> bool:
    head = body[:1000].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head


async def discover_feed(client: httpx.AsyncClient, site_url: str) -> str | None:
    """The feed URL a site advertises, else the first common feed path that
    serves a feed. None when the site has none (or is unreachable)."""
    try:
        resp = await client.get(site_url)
        if resp.status_code == 200:
            for tag in _LINK_TAG.findall(resp.text[:300_000]):
                attrs = {k.lower(): a or b for k, a, b in _ATTR.findall(tag)}
                if "alternate" in attrs.get("rel", "").lower().split() and (
                    attrs.get("type", "").lower() in _FEED_TYPES
                ):
                    href = attrs.get("href")
                    if href:
                        return urljoin(str(resp.url), href)
        for path in _COMMON_PATHS:
            candidate = urljoin(site_url, path)
            probe = await client.get(candidate)
            if probe.status_code == 200 and _looks_like_feed(probe.content):
                return str(probe.url)
    except httpx.HTTPError as exc:
        log.info("rss: discovery on %s failed: %s", site_url, exc)
    return None


def subscribe(settings: Settings, feed_url: str, site: str | None = None) -> bool:
    """Add a feed to the built-in reader; False when it was there already."""
    with connection(settings) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO feeds(url, site) VALUES(?, ?)", (feed_url, site))
        return cur.rowcount == 1


def _entry(entry: Any, cutoff: datetime) -> dict | None:
    url = entry.get("link")
    if not url or not url.startswith(("http://", "https://")):
        return None
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    published = datetime.fromtimestamp(timegm(stamp), UTC) if stamp else None
    if published and published < cutoff:
        return None
    body = entry.get("summary") or ""
    if entry.get("content"):
        body = entry["content"][0].get("value") or body
    image = None
    for media in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if str(media.get("url", "")).startswith(("http://", "https://")):
            image = media["url"]
            break
    for link in entry.get("links", []):
        if not image and link.get("rel") == "enclosure" and "image" in link.get("type", ""):
            image = link.get("href")
    return {
        "source": "rss",
        "url": url,
        "title": strip_html(entry.get("title")),
        "snippet": strip_html(body)[:500],
        "image_url": image or first_image(body),
        "published_at": published.isoformat() if published else "",
    }


async def fetch_feeds(settings: Settings) -> list[dict]:
    """New entries from every subscribed feed, as candidate dicts."""
    with connection(settings) as conn:
        feeds = [row[0] for row in conn.execute("SELECT url FROM feeds")]
    if not feeds:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=settings.candidate_max_age_days)
    semaphore = asyncio.Semaphore(settings.enrich_concurrency)

    async def one(client: httpx.AsyncClient, feed_url: str) -> list[dict]:
        async with semaphore:
            try:
                resp = await client.get(feed_url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.info("rss: %s failed: %s", feed_url, exc)
                return []
        parsed = feedparser.parse(resp.content[:_MAX_FEED_BYTES])
        items = [_entry(e, cutoff) for e in parsed.entries[:_ENTRIES_PER_FEED]]
        return [item for item in items if item]

    async with httpx.AsyncClient(
        headers=PAGE_HEADERS, timeout=settings.enrich_timeout_s, follow_redirects=True
    ) as client:
        batches = await asyncio.gather(*(one(client, url) for url in feeds))
    return [item for batch in batches for item in batch]
