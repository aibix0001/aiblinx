"""Push the operator's views of saved stories to transcribix (contract v1,
the "Push" half).

Every trigger (a highlight change, Save / Archive / Update PDF, a section
move, an interest vote, Unsave) upserts one outbox row per Linkwarden link,
so triggers coalesce: the row is sent once, with the state at send time. A
discussion exists on the server only here, from the request that carried it
until the row is delivered or fails for good (owner decision 2026-10-29).

The sender job retries network errors, 5xx and 429 with backoff; any other
4xx marks the row failed. A row changed while its request was in flight
(``version``) is never dropped by that request's answer.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from .config import Settings, get_settings
from .db import connection, get_meta, set_meta

log = logging.getLogger(__name__)

HIGHLIGHT_DELAY_S = 600.0  # a highlight change is sent 10 min after the last one
BATCH = 50
_MAX_BACKOFF_S = 3600.0
_LAST_TS_KEY = "link_views_last_updated_at"
_PATH = "/api/archive/v1/link-views/{}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_updated_at(conn, now: datetime | None) -> str:
    """The trigger time, strictly later than any issued before: the receiver
    ignores a record whose ``updated_at`` is not newer than the stored one."""
    moment = (now or datetime.now(UTC)).replace(microsecond=0)
    last = get_meta(conn, _LAST_TS_KEY)
    if last:
        floor = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        moment = max(moment, floor + timedelta(seconds=1))
    stamp = _iso(moment)
    set_meta(conn, _LAST_TS_KEY, stamp)
    return stamp


def _upsert(
    conn, linkwarden_id: int, op: str, discussion: str | None, delay_s: float, now, stamp=None
) -> None:
    conn.execute(
        "INSERT INTO link_view_outbox(linkwarden_id, op, discussion, updated_at, due_at) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(linkwarden_id) DO UPDATE SET "
        "  op = excluded.op, "
        "  discussion = CASE "
        "    WHEN excluded.op = 'delete' THEN NULL "
        "    WHEN excluded.discussion IS NOT NULL THEN excluded.discussion "
        "    WHEN link_view_outbox.op = 'delete' THEN NULL "
        "    ELSE link_view_outbox.discussion END, "
        "  updated_at = excluded.updated_at, due_at = excluded.due_at, "
        "  attempts = 0, last_error = NULL, state = 'pending', "
        "  version = link_view_outbox.version + 1",
        (
            linkwarden_id,
            op,
            discussion,
            stamp or _next_updated_at(conn, now),
            time.time() + delay_s,
        ),
    )


def queue_put(
    conn,
    settings: Settings,
    url_key: str,
    *,
    delay_s: float = 0.0,
    discussion: list[dict] | None = None,
    now: datetime | None = None,
    stamp: str | None = None,
) -> None:
    """Queue a record for the saved story behind ``url_key``. A story with no
    Linkwarden link of ours is out of scope and queues nothing. ``stamp``: one
    ``updated_at`` shared by a batch of different links (the backfill)."""
    if not settings.transcribix_push_enabled:
        return
    row = conn.execute(
        "SELECT linkwarden_id FROM saves WHERE url_key = ? AND linkwarden_id > 0", (url_key,)
    ).fetchone()
    if row is None:
        return
    text = json.dumps(discussion, ensure_ascii=False) if discussion else None
    _upsert(conn, int(row[0]), "put", text, delay_s, now, stamp)


def queue_delete(conn, settings: Settings, linkwarden_id: int, now: datetime | None = None) -> None:
    if settings.transcribix_push_enabled and linkwarden_id > 0:
        _upsert(conn, linkwarden_id, "delete", None, 0.0, now)


def build_payload(settings: Settings, row) -> dict | None:
    """The record for one outbox row, from the database as it is now. None
    when the save is gone (Unsave queues its own DELETE)."""
    # Function-level imports: pipeline.feedback and highlights import this module.
    from .highlights import load_marks
    from .pipeline.feedback import latest_facets
    from .signals import save_collection

    with connection(settings) as conn:
        save = conn.execute(
            "SELECT url, url_key, title, section, created_at FROM saves WHERE linkwarden_id = ?",
            (row["linkwarden_id"],),
        ).fetchone()
        if save is None:
            return None
        cand = conn.execute(
            "SELECT c.id, c.source, a.lang FROM candidates c "
            "LEFT JOIN articles a ON a.candidate_id = c.id WHERE c.url = ?",
            (save["url"],),
        ).fetchone()
        collection_id = save_collection(conn, settings, row["linkwarden_id"], save["section"])
    marks = load_marks(settings, cand["id"]) if cand is not None else {"orig": [], "tr": []}
    saved_at = datetime.strptime(save["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return {
        "v": 1,
        "url": save["url"],
        "url_key": save["url_key"],
        "title": save["title"],
        "lang": cand["lang"] if cand is not None else None,
        "collection_id": collection_id,
        "section": save["section"],
        "source": cand["source"] if cand is not None else None,
        "saved_at": _iso(saved_at),
        "updated_at": row["updated_at"],
        "interest": latest_facets(settings, save["url_key"]).get("interest"),
        "highlights": [
            {"variant": variant, **mark} for variant in ("orig", "tr") for mark in marks[variant]
        ],
        "discussion": json.loads(row["discussion"]) if row["discussion"] else None,
    }


def _retryable(status: int) -> bool:
    return status >= 500 or status == 429


async def send_link_views(
    settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> dict[str, int]:
    """Deliver the due outbox rows. Returns counts by outcome."""
    settings = settings or get_settings()
    counts = {"sent": 0, "retry": 0, "failed": 0}
    if not settings.transcribix_push_enabled:
        return counts
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT * FROM link_view_outbox WHERE state = 'pending' AND due_at <= ? "
            "ORDER BY due_at LIMIT ?",
            (time.time(), BATCH),
        ).fetchall()
    if not rows:
        return counts
    owns_client = client is None
    client = client or httpx.AsyncClient(
        base_url=settings.transcribix_url.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.transcribix_push_token}"},
        timeout=30.0,
    )
    try:
        for row in rows:
            key = (row["linkwarden_id"], row["version"])
            path = _PATH.format(row["linkwarden_id"])
            if row["op"] == "put":
                try:
                    payload = build_payload(settings, row)
                except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                    _fail(settings, *key, f"payload: {type(exc).__name__}")
                    counts["failed"] += 1
                    log.warning("link views: link %d has no valid record (%s)", key[0], exc)
                    continue
                if payload is None:
                    _drop(settings, *key)
                    continue
                request = client.put(path, json=payload)
            else:
                request = client.delete(path)
            try:
                resp = await request
                status, error = resp.status_code, f"HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                status, error = 0, type(exc).__name__
            if 200 <= status < 300:
                _drop(settings, *key)
                counts["sent"] += 1
            elif status == 0 or _retryable(status):
                _backoff(settings, *key, row["attempts"], error)
                counts["retry"] += 1
            else:
                _fail(settings, *key, error)
                counts["failed"] += 1
                log.warning("link views: link %d refused (%s), not retried", key[0], error)
    finally:
        if owns_client:
            await client.aclose()
    if any(counts.values()):
        log.info("link views: %(sent)d sent, %(retry)d to retry, %(failed)d failed", counts)
    return counts


def _drop(settings: Settings, linkwarden_id: int, version: int) -> None:
    with connection(settings) as conn:
        conn.execute(
            "DELETE FROM link_view_outbox WHERE linkwarden_id = ? AND version = ?",
            (linkwarden_id, version),
        )


def _backoff(settings: Settings, linkwarden_id: int, version: int, attempts: int, error) -> None:
    delay = min(60.0 * 2**attempts, _MAX_BACKOFF_S)
    with connection(settings) as conn:
        conn.execute(
            "UPDATE link_view_outbox SET attempts = attempts + 1, due_at = ?, last_error = ? "
            "WHERE linkwarden_id = ? AND version = ?",
            (time.time() + delay, error, linkwarden_id, version),
        )


def _fail(settings: Settings, linkwarden_id: int, version: int, error) -> None:
    with connection(settings) as conn:
        conn.execute(
            "UPDATE link_view_outbox SET state = 'failed', discussion = NULL, last_error = ? "
            "WHERE linkwarden_id = ? AND version = ?",
            (error, linkwarden_id, version),
        )


_BACKFILL_KEY = "link_views_backfill_done"


def backfill_link_views(settings: Settings, again: bool = False) -> int:
    """Queue a record for every save with a Linkwarden link, once. The sender
    job delivers them. ``discussion`` stays null: the server holds no past
    discussions (they are in the Linkwarden PDFs and on the device; Update
    the PDF sends one). Returns how many rows were queued."""
    if not settings.transcribix_push_enabled:
        return 0
    with connection(settings) as conn:
        if get_meta(conn, _BACKFILL_KEY) == "1" and not again:
            return 0
        keys = [
            row[0]
            for row in conn.execute(
                "SELECT url_key FROM saves WHERE linkwarden_id > 0 ORDER BY id"
            ).fetchall()
        ]
        stamp = _next_updated_at(conn, None)  # different links: one stamp for all
        for url_key in keys:
            queue_put(conn, settings, url_key, stamp=stamp)
        set_meta(conn, _BACKFILL_KEY, "1")
    return len(keys)


def main() -> None:
    """``python -m discover_app.linkviews [--again]``: the one-off backfill."""
    import sys

    settings = get_settings()
    if not settings.transcribix_push_enabled:
        print("link views: TRANSCRIBIX_URL / TRANSCRIBIX_PUSH_TOKEN not set, nothing queued")
        return
    queued = backfill_link_views(settings, again="--again" in sys.argv[1:])
    if queued:
        print(f"link views: {queued} saved stories queued; the sender delivers them within minutes")
    else:
        print("link views: backfill already done (pass --again to queue every save once more)")


if __name__ == "__main__":
    main()
