"""Linkwarden client: poll for saved links + write saves back.

Linkwarden has no webhook, so new saves are detected by polling and diffing
against the last-seen id. The list endpoint has no date params — any date
filtering must be done client-side. Pages are newest-first; passing the last
id of a page as ``cursor`` returns the next (older) page, so the full history
is reached by walking cursors until an empty page (see ingest_links).

Write contract (verified against the installed version, 2026-07-27):
``POST /links`` accepts ``{"url", "name", "collection": {"id": N}, "tags":
[{"name": ...}]}`` — the collection MUST be addressed by id (a mismatched name
silently creates a duplicate collection), and tags are created on the fly by
name. Tag replacement needs the full-object PUT: fetch the link,
mutate ``tags``, PUT the whole object back — a partial PUT body returns 400.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings, get_settings


class LinkwardenClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client = httpx.AsyncClient(
            base_url=self.settings.linkwarden_base_url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {self.settings.linkwarden_token}"},
            timeout=30.0,
        )

    async def fetch_links(self, cursor: int | None = None) -> list[dict[str, Any]]:
        if not self.settings.linkwarden_token:
            return []
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await self._client.get("/links", params=params)
        resp.raise_for_status()
        data = resp.json()
        # Linkwarden wraps list payloads as {"response": [...]}.
        if isinstance(data, dict):
            return data.get("response", [])
        return data  # type: ignore[return-value]

    async def create_link(
        self, url: str, name: str, collection_id: int, tags: list[str] | None = None
    ) -> dict[str, Any]:
        """Create a link ("+" capture). Collection is addressed by id — invariant."""
        payload = {
            "url": url,
            "name": name or "",
            "collection": {"id": int(collection_id)},
            "tags": [{"name": t} for t in (tags or [])],
        }
        resp = await self._client.post("/links", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", data) if isinstance(data, dict) else data

    async def get_link(self, link_id: int) -> dict[str, Any]:
        resp = await self._client.get(f"/links/{link_id}")
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", data) if isinstance(data, dict) else data

    async def update_link(self, link: dict[str, Any]) -> dict[str, Any]:
        """PUT the full link object back (partial bodies are rejected with 400).

        Callers fetch via get_link, mutate (typically ``tags``), and pass the
        whole object here — the verified tag-replacement contract.
        """
        resp = await self._client.put(f"/links/{link['id']}", json=link)
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", data) if isinstance(data, dict) else data

    async def aclose(self) -> None:
        await self._client.aclose()
