"""Hacker News Firebase API client — no auth, no rate limit.

Fetches a story listing (best/top/new) and resolves items concurrently. Ask/Show
self-posts without a target URL are skipped. For date-filtered queries, prefer
the Algolia HN Search API's ``numericFilters`` (not needed so far).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Settings, get_settings

FIREBASE = "https://hacker-news.firebaseio.com/v0"

log = logging.getLogger(__name__)


class HackerNewsClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(timeout=20.0)
        self._sem = asyncio.Semaphore(10)

    async def _item(self, item_id: int) -> dict[str, Any] | None:
        async with self._sem:
            resp = await self._client.get(f"{FIREBASE}/item/{item_id}.json")
        resp.raise_for_status()
        return resp.json()

    async def fetch(self) -> list[dict[str, Any]]:
        resp = await self._client.get(f"{FIREBASE}/{self.settings.hackernews_list}.json")
        resp.raise_for_status()
        ids = (resp.json() or [])[: self.settings.hackernews_limit]
        # One failed item request must not discard the whole batch.
        items = await asyncio.gather(*(self._item(i) for i in ids), return_exceptions=True)
        failed = sum(1 for item in items if isinstance(item, BaseException))
        if failed:
            log.warning("hackernews: %d of %d item fetches failed, skipping them", failed, len(ids))
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, BaseException) or not item or not item.get("url"):
                continue
            published = item.get("time")
            out.append(
                {
                    "source": "hackernews",
                    "url": item["url"],
                    "title": item.get("title", ""),
                    "snippet": "",
                    # normalize to ISO-8601 UTC like the Miniflux source
                    "published_at": (
                        datetime.fromtimestamp(published, UTC).isoformat() if published else ""
                    ),
                }
            )
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
