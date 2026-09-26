"""Miniflux REST client — fetch unread entries as discovery candidates."""

from __future__ import annotations

import html
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..html_text import first_image, strip_html


class MinifluxClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.miniflux_url.rstrip("/"),
            headers={"X-Auth-Token": self.settings.miniflux_token},
            timeout=30.0,
        )

    async def fetch(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.settings.miniflux_token:
            return []
        resp = await self._client.get(
            "/v1/entries",
            params={"status": "unread", "limit": limit, "direction": "desc"},
        )
        resp.raise_for_status()
        entries = resp.json().get("entries", [])
        out: list[dict[str, Any]] = []
        for entry in entries:
            url = entry.get("url")
            if not url:
                continue
            content = entry.get("content") or ""
            out.append(
                {
                    "source": "miniflux",
                    "url": url,
                    # Miniflux keeps entities in titles ("&#34;"): decode once here
                    "title": html.unescape(entry.get("title") or ""),
                    "snippet": strip_html(content)[:500],
                    "image_url": first_image(content),
                    "published_at": entry.get("published_at"),
                }
            )
        return out

    async def first_category_id(self) -> int:
        """Id of the user's first category — feed creation requires one."""
        resp = await self._client.get("/v1/categories")
        resp.raise_for_status()
        return int(resp.json()[0]["id"])

    async def discover(self, url: str) -> list[dict[str, Any]]:
        """Feeds Miniflux finds on a page (``[{"url", "title", "type"}]``)."""
        resp = await self._client.post("/v1/discover", json={"url": url})
        resp.raise_for_status()
        return resp.json()

    async def create_feed(self, feed_url: str, category_id: int) -> int:
        resp = await self._client.post(
            "/v1/feeds", json={"feed_url": feed_url, "category_id": category_id}
        )
        resp.raise_for_status()
        return int(resp.json()["feed_id"])

    async def list_feeds(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/v1/feeds")
        resp.raise_for_status()
        return resp.json()

    async def get_feed(self, feed_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/v1/feeds/{feed_id}")
        resp.raise_for_status()
        return resp.json()

    async def delete_feed(self, feed_id: int) -> None:
        resp = await self._client.delete(f"/v1/feeds/{feed_id}")
        resp.raise_for_status()

    async def import_opml(self, opml: str) -> None:
        """Subscribe every feed in an OPML document (Miniflux's own importer)."""
        resp = await self._client.post(
            "/v1/import", content=opml.encode(), headers={"Content-Type": "application/xml"}
        )
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


async def create_api_key(settings: Settings, description: str) -> str:
    """Create a Miniflux API key with the admin login (basic auth)."""
    async with httpx.AsyncClient(
        base_url=settings.miniflux_url.rstrip("/"),
        auth=(settings.miniflux_admin_user, settings.miniflux_admin_password),
        timeout=30.0,
    ) as client:
        resp = await client.post("/v1/api-keys", json={"description": description})
        resp.raise_for_status()
        return str(resp.json()["token"])
