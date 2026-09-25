"""Fetch each served item's page once for its title image and description.

Runs after build_feed on the current cycle's items only (~30 pages a day),
never on the whole candidate pool. The page's own ``og:image`` wins over an
image the source supplied (the first ``<img>`` of a Miniflux entry, a SearXNG
thumbnail); the description backs the card summary when the feed snippet is
thin (Hacker News has none at all).

Every attempted item is marked ``enriched`` whatever the outcome — a site that
blocks bots (reuters.com) or hides its meta tags behind a consent wall
(golem.de) will do so tomorrow too.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import Settings, get_settings
from ..db import connection
from ..html_text import page_meta

log = logging.getLogger(__name__)

# Meta tags live in <head>; stop reading once it is closed or this much is in.
_MAX_BYTES = 512 * 1024
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148 aiblinx-preview"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


async def fetch_meta(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
    """``(image_url, description)`` of one page; ``(None, None)`` on any failure."""
    if not url.startswith(("http://", "https://")):
        return None, None
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                return None, None
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) >= _MAX_BYTES or b"</head>" in body.lower():
                    break
            page = body.decode(resp.encoding or "utf-8", errors="replace")
            return page_meta(page, str(resp.url))
    except httpx.HTTPError as exc:
        log.info("enrich: %s failed: %s", url, exc)
        return None, None


async def enrich_feed(settings: Settings | None = None) -> int:
    """Enrich the current cycle's not-yet-enriched items; return how many."""
    settings = settings or get_settings()
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT DISTINCT c.id, c.url FROM feed_items f "
            "JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.cycle_ts = (SELECT value FROM meta WHERE key = 'last_cycle_ts') "
            "AND c.enriched = 0"
        ).fetchall()
    if not rows:
        return 0
    semaphore = asyncio.Semaphore(settings.enrich_concurrency)

    async def one(client: httpx.AsyncClient, url: str) -> tuple[str | None, str | None]:
        async with semaphore:
            return await fetch_meta(client, url)

    async with httpx.AsyncClient(
        headers=PAGE_HEADERS, timeout=settings.enrich_timeout_s, follow_redirects=True
    ) as client:
        results = await asyncio.gather(*(one(client, row["url"]) for row in rows))
    with connection(settings) as conn:
        for row, (image, description) in zip(rows, results, strict=True):
            conn.execute(
                "UPDATE candidates SET image_url = COALESCE(?, image_url), "
                "description = ?, enriched = 1 WHERE id = ?",
                (image, description, row["id"]),
            )
    images = sum(1 for image, _ in results if image)
    log.info("enrich_feed: %d item(s), %d with a page image", len(rows), images)
    return len(rows)
