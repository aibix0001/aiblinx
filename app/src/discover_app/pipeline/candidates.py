"""Gather candidate items from Hacker News + Miniflux + SearXNG, dedupe, embed.

SearXNG (the Exploring source) is queried once per selected
onboarding topic per cycle and stays inert until topics are picked. Sources
are fetched concurrently and failures in one source don't abort the others.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..clients.hackernews import HackerNewsClient
from ..clients.llm import LLMClient
from ..clients.miniflux import MinifluxClient
from ..clients.rss import fetch_feeds
from ..clients.searxng import SearxngClient
from ..config import Settings, get_settings
from ..db import connection
from ..topics import selected_topics
from .embedding import embed_pending_candidates

log = logging.getLogger(__name__)


def prune_candidates(settings: Settings) -> int:
    """Drop candidates (and their vectors / served history) older than the
    freshness window so the ranking pool and the rerank prompt stay bounded."""
    # fetched_at is written by SQLite's datetime('now') — match that format.
    cutoff = (datetime.now(UTC) - timedelta(days=settings.candidate_max_age_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with connection(settings) as conn:
        # Never prune what the currently published cycle is serving — the live
        # feed must stay intact even if the next build_feed produces nothing.
        ids = [
            row[0]
            for row in conn.execute(
                "SELECT id FROM candidates WHERE fetched_at < ? AND id NOT IN ("
                "  SELECT candidate_id FROM feed_items WHERE cycle_ts = "
                "    (SELECT value FROM meta WHERE key = 'last_cycle_ts'))",
                (cutoff,),
            )
        ]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"DELETE FROM feed_items WHERE candidate_id IN ({placeholders})",  # noqa: S608
                ids,
            )
            conn.execute(
                f"DELETE FROM vec_candidates WHERE rowid IN ({placeholders})",  # noqa: S608
                ids,
            )
            conn.execute(f"DELETE FROM candidates WHERE id IN ({placeholders})", ids)  # noqa: S608
    if ids:
        log.info("prune_candidates: dropped %d candidates older than %s", len(ids), cutoff)
    return len(ids)


def is_ad(title: str | None, patterns: list[str]) -> bool:
    """True when the title marks promotional content (case-insensitive substring)."""
    lowered = (title or "").lower()
    return any(p.lower() in lowered for p in patterns)


async def gather_candidates(settings: Settings | None = None, llm: LLMClient | None = None) -> int:
    settings = settings or get_settings()
    owns_llm = llm is None
    llm = llm or LLMClient(settings)
    hackernews = HackerNewsClient(settings)
    miniflux = MinifluxClient(settings)
    searxng = SearxngClient(settings)
    new = 0
    try:
        fetchers = []
        if settings.hackernews_enabled:
            fetchers.append(hackernews.fetch())
        # Miniflux when connected, else the built-in reader's subscriptions
        fetchers.append(miniflux.fetch() if settings.miniflux_token else fetch_feeds(settings))
        # anti-bubble source: inert until the user selects onboarding topics
        topics = selected_topics(settings)
        if topics and settings.searxng_url:
            fetchers.append(searxng.fetch(topics))
        batches = await asyncio.gather(*fetchers, return_exceptions=True)

        items: list[dict] = []
        for batch in batches:
            if isinstance(batch, BaseException):
                log.warning("candidate source failed: %s", batch)
                continue
            items.extend(batch)

        ads = 0
        with connection(settings) as conn:
            seen = {row[0] for row in conn.execute("SELECT url FROM candidates")}
            for item in items:
                url = item.get("url")
                if not url or url in seen:
                    continue
                if is_ad(item.get("title"), settings.ad_filter_patterns):
                    ads += 1
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO candidates
                         (source, url, title, snippet, published_at, topic, image_url)
                       VALUES (:source, :url, :title, :snippet, :published_at, :topic,
                               :image_url)""",
                    {
                        "source": item["source"],
                        "url": url,
                        "title": item.get("title"),
                        "snippet": item.get("snippet"),
                        "published_at": str(item.get("published_at") or ""),
                        "topic": item.get("topic"),
                        "image_url": item.get("image_url"),
                    },
                )
                seen.add(url)
                new += 1
        # sync sqlite work — keep it off the event loop like KMeans in run_cycle
        await asyncio.to_thread(prune_candidates, settings)
        await embed_pending_candidates(settings, llm)
    finally:
        await hackernews.aclose()
        await miniflux.aclose()
        await searxng.aclose()
        if owns_llm:
            await llm.aclose()
    log.info("gather_candidates: %d new candidates, %d ads filtered", new, ads)
    return new
