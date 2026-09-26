"""Ingest Linkwarden saves into local SQLite, then embed the new ones."""

from __future__ import annotations

import json
import logging

from ..clients.linkwarden import LinkwardenClient
from ..clients.llm import LLMClient
from ..config import Settings, get_settings
from ..db import connection, get_meta, set_meta
from .embedding import embed_pending_links
from .feedback import detect_saved, push_local_saves

log = logging.getLogger(__name__)

_BACKFILL_KEY = "linkwarden_backfill_done"

# Keyed on id; a changed name/description/text resets ``embedded`` so the
# vector is refreshed (Linkwarden archives textContent asynchronously, so a
# link's text often arrives after the first poll). ``url`` is never updated —
# it is UNIQUE and the row identity for vec bookkeeping.
_UPSERT_LINK = """
INSERT INTO links (id, url, name, description, text_content, tags, created_at, updated_at,
                   collection_id)
VALUES (:id, :url, :name, :description, :text_content, :tags, :created_at, :updated_at,
        :collection_id)
ON CONFLICT(id) DO UPDATE SET
    collection_id = excluded.collection_id,
    name = excluded.name,
    description = excluded.description,
    text_content = excluded.text_content,
    tags = excluded.tags,
    updated_at = excluded.updated_at,
    embedded = CASE
        WHEN links.name IS NOT excluded.name
          OR links.description IS NOT excluded.description
          OR links.text_content IS NOT excluded.text_content
        THEN 0 ELSE links.embedded
    END
"""


def _link_row(link: dict) -> dict:
    return {
        "id": int(link["id"]),
        "url": link.get("url") or "",
        "name": link.get("name"),
        "description": link.get("description"),
        "text_content": link.get("textContent"),
        "tags": json.dumps([t.get("name") for t in link.get("tags", [])]),
        "created_at": link.get("createdAt"),
        "updated_at": link.get("updatedAt"),
        "collection_id": link.get("collectionId"),
    }


async def ingest_links(settings: Settings | None = None, llm: LLMClient | None = None) -> int:
    """Poll Linkwarden, upsert links, embed any missing vectors.

    Pages are fetched with no DB connection open (never hold a write
    transaction across network I/O); each page commits in its own short
    transaction. Until one walk has reached the oldest page, every poll walks
    to the end — the backfill flag commits only when the walk actually
    finishes, so an interrupted backfill reruns (upserts are idempotent).
    Afterwards a page with no unknown ids means everything older is present.

    Returns the count of newly-stored links.
    """
    settings = settings or get_settings()
    if not settings.linkwarden_enabled:
        log.info("ingest_links: no Linkwarden token configured, skipping")
        return 0
    owns_llm = llm is None
    llm = llm or LLMClient(settings)
    linkwarden = LinkwardenClient(settings)
    new = 0
    seen = 0
    new_urls: list[str] = []
    try:
        with connection(settings) as conn:
            existing = {row[0] for row in conn.execute("SELECT id FROM links")}
            # links.url is UNIQUE; the same URL can be saved under several
            # Linkwarden ids — mirror only the first (newest) one we meet.
            existing_urls = {row[0] for row in conn.execute("SELECT url FROM links")}
            backfilled = get_meta(conn, _BACKFILL_KEY) == "1"
        cursor: int | None = None
        while True:
            links = await linkwarden.fetch_links(cursor)
            if not links:
                if not backfilled:
                    with connection(settings) as conn:
                        set_meta(conn, _BACKFILL_KEY, "1")
                break
            page_new = 0
            page_unknown = 0
            with connection(settings) as conn:
                for link in links:
                    link_id = int(link["id"])
                    if link_id not in existing:
                        page_unknown += 1
                        url = link.get("url") or ""
                        if url in existing_urls:
                            continue  # duplicate save of an already-mirrored URL
                        existing.add(link_id)
                        existing_urls.add(url)
                        new_urls.append(url)
                        page_new += 1
                    conn.execute(_UPSERT_LINK, _link_row(link))
            new += page_new
            seen += len(links)
            next_cursor = int(links[-1]["id"])
            if next_cursor == cursor:  # server ignored the cursor; don't loop forever
                log.warning("ingest_links: cursor %s not advancing, stopping walk", cursor)
                break
            cursor = next_cursor
            if backfilled and page_unknown == 0:
                break
        # Implicit positive signal: a new save whose URL we previously served.
        detect_saved(settings, new_urls)
        # Saves made while Linkwarden was not connected move over now; the
        # walk above has mirrored every existing link, so none is duplicated.
        try:
            await push_local_saves(settings, linkwarden)
        except Exception as exc:  # noqa: BLE001 - retried on the next poll
            log.warning("ingest_links: pushing local saves to Linkwarden failed: %s", exc)
        await embed_pending_links(settings, llm)
    finally:
        await linkwarden.aclose()
        if owns_llm:
            await llm.aclose()
    log.info("ingest_links: %d links seen, %d new", seen, new)
    return new
