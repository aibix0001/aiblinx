"""SearXNG client — per-topic candidate fetches for the Exploring section.

One `GET /search?q=<topic>&categories=news&time_range=week&format=json` per
selected topic per cycle. JSON output must be enabled in the instance's
settings.yml (`formats: [html, json]`) — it is OFF upstream by default; the
in-stack instance ships with it on.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..html_text import strip_html

log = logging.getLogger(__name__)

_RESULTS_PER_TOPIC = 20


def _http(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


class SearxngClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.searxng_url.rstrip("/"), timeout=30.0
        )

    async def fetch(self, topics: list[str]) -> list[dict[str, Any]]:
        """Fetch candidates for the given topics. A failing topic query is
        logged and skipped — one broken engine must not kill the source."""
        out: list[dict[str, Any]] = []
        for topic in topics:
            try:
                resp = await self._client.get(
                    "/search",
                    params={
                        "q": topic,
                        "categories": "news",
                        "time_range": "week",
                        "format": "json",
                    },
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
            except Exception as exc:  # noqa: BLE001 - per-topic fault tolerance
                log.warning("searxng: query for topic %r failed: %s", topic, exc)
                continue
            for result in results[:_RESULTS_PER_TOPIC]:
                url = result.get("url")
                if not url:
                    continue
                out.append(
                    {
                        "source": "searxng",
                        "url": url,
                        "title": result.get("title", ""),
                        "snippet": strip_html(result.get("content"))[:500],
                        "image_url": _http(result.get("thumbnail") or result.get("img_src")),
                        "published_at": result.get("publishedDate") or "",
                        "topic": topic,
                    }
                )
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
