"""Feedback recording: explicit two-axis signals, "+" capture, save-detection.

Three signal kinds land in the ``feedback`` table:
- ``interest`` up|down — explicit relevance signal (reweights the profile),
- ``mood`` happy|sad — a filter facet, deliberately NOT a relevance signal,
- ``saved`` explicit|implicit — the strongest positive: explicit via the
  capture endpoint, implicit when a served suggestion later shows up in the
  Linkwarden poll. Every explicit save also lands in the local ``saves`` list;
  Linkwarden is an optional second destination.

The candidate's embedding is copied onto the feedback row at write time so the
signal survives the 14-day candidate prune. URLs are stored normalized
(``urls.norm_url``) — the same identity used by ranking exclusions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from ..clients.linkwarden import LinkwardenClient
from ..config import Settings
from ..db import connection
from ..urls import norm_url

log = logging.getLogger(__name__)

# Serializes captures so a double-click cannot fire two Linkwarden creates for
# the same URL (the feedback_saved_url unique index guards the DB side; this
# lock guards the external POST).
_capture_lock = asyncio.Lock()

VALID_VALUES: dict[str, set[str]] = {
    "interest": {"up", "down"},
    "mood": {"happy", "sad"},
}

VALID_RATINGS: set[float] = {-1.0, -0.5, 0.0, 0.5, 1.0}

# Facet tag names mirrored to Linkwarden (interest:high|low, mood:happy|sad).
FACET_TAG: dict[tuple[str, str], str] = {
    ("interest", "up"): "interest:high",
    ("interest", "down"): "interest:low",
    ("mood", "happy"): "mood:happy",
    ("mood", "sad"): "mood:sad",
}
_FACET_PREFIXES = ("interest:", "mood:")


def record_rating(settings: Settings, candidate_id: int, value: float) -> tuple[float, bool]:
    """Store one explicit rating for a known candidate.

    Idempotent per (url, value) — if the same URL + value was already rated,
    the row is not duplicated; the caller gets ``changed=False``.

    Returns ``(value, changed)`` where *changed* is True when this call produced
    a new row (the first time the user clicks that button) and False when the
    rating already existed (the row was already present from an earlier click).

    Raises LookupError for an unknown candidate and ValueError for an invalid
    value (the API layer maps these to 404/422).
    """
    if value not in VALID_RATINGS:
        raise ValueError(f"invalid rating value {value!r}")
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT url, embedding FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"unknown candidate {candidate_id}")
        url_key = norm_url(row["url"])
        # Check for existing rating (same URL + value) — idempotent
        existing = conn.execute(
            "SELECT 1 FROM ratings WHERE url = ? AND value = ?",
            (url_key, value),
        ).fetchone()
        if existing:
            return value, False
        conn.execute(
            "INSERT INTO ratings(candidate_id, url, value, embedding) VALUES(?, ?, ?, ?)",
            (candidate_id, url_key, value, row["embedding"]),
        )
    return value, True


def record_feedback(settings: Settings, candidate_id: int, axis: str, value: str) -> str:
    """Store one explicit interest/mood event for a known candidate.

    Returns the normalized page identity (callers pass it to the tag mirror —
    no second candidate lookup, so a concurrent prune cannot break the caller).
    Raises LookupError for an unknown candidate and ValueError for an invalid
    axis/value combination (the API layer maps these to 404/422).
    """
    allowed = VALID_VALUES.get(axis)
    if allowed is None or value not in allowed:
        raise ValueError(f"invalid feedback {axis}={value!r}")
    with connection(settings) as conn:
        row = conn.execute(
            "SELECT url, embedding FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"unknown candidate {candidate_id}")
        url_key = norm_url(row["url"])
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding) VALUES(?, ?, ?, ?, ?)",
            (candidate_id, url_key, axis, value, row["embedding"]),
        )
    return url_key


async def capture_candidate(
    settings: Settings,
    candidate_id: int,
    linkwarden: LinkwardenClient | None = None,
) -> dict:
    """Save: record the candidate in the local Saved list and the ``saved``
    signal, and — when Linkwarden is connected — create it there as a link.

    Idempotent per normalized URL: an item already saved (explicitly,
    implicitly, or as a pre-existing bookmark) is not saved again; the capture
    lock plus the unique indexes make this hold under concurrent clicks too.

    Raises LookupError for an unknown candidate. With Linkwarden connected, a
    Linkwarden/network failure propagates as an httpx error with no local
    state written (retry is safe).
    """
    async with _capture_lock:
        with connection(settings) as conn:
            row = conn.execute(
                "SELECT url, title, image_url, embedding FROM candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"unknown candidate {candidate_id}")
            url_key = norm_url(row["url"])
            already = conn.execute(
                "SELECT 1 FROM feedback WHERE axis = 'saved' AND url = ?", (url_key,)
            ).fetchone()
            bookmarked = already or any(
                norm_url(link_url) == url_key
                for (link_url,) in conn.execute("SELECT url FROM links")
            )
        if bookmarked:
            return {"status": "already_saved"}

        link_id = None
        if settings.linkwarden_enabled:
            owns_client = linkwarden is None
            linkwarden = linkwarden or LinkwardenClient(settings)
            try:
                # Feedback given before capture becomes tags at creation time
                # (tags are applied when the item is saved later).
                pre_facets = _facet_tag_names(latest_facets(settings, url_key))
                created = await linkwarden.create_link(
                    url=row["url"],
                    name=row["title"] or row["url"],
                    collection_id=settings.linkwarden_collection_id,
                    tags=pre_facets,
                )
            finally:
                if owns_client:
                    await linkwarden.aclose()
            link_id = created.get("id") if isinstance(created, dict) else None

        with connection(settings) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO feedback(candidate_id, url, axis, value, embedding) "
                "VALUES(?, ?, 'saved', 'explicit', ?)",
                (candidate_id, url_key, row["embedding"]),
            )
            conn.execute(
                "INSERT OR IGNORE INTO saves(url, url_key, title, image_url, embedding, "
                "linkwarden_id) VALUES(?, ?, ?, ?, ?, ?)",
                (row["url"], url_key, row["title"], row["image_url"], row["embedding"], link_id),
            )
    if settings.linkwarden_enabled:
        log.info("capture: candidate %d saved to Linkwarden as link %s", candidate_id, link_id)
        return {"status": "saved", "linkwarden_id": link_id}
    log.info("capture: candidate %d saved locally", candidate_id)
    return {"status": "saved", "linkwarden_id": None}


async def push_local_saves(settings: Settings, linkwarden: LinkwardenClient) -> int:
    """Linkwarden connected after the fact: create every local save it does
    not have yet, so switching from local saves to Linkwarden loses nothing.

    A save whose URL is already a mirrored Linkwarden link is only linked up,
    never re-created. Stops at the first Linkwarden error (the rest is retried
    on the next poll). Returns how many links were created.
    """
    with connection(settings) as conn:
        pending = conn.execute(
            "SELECT id, url, url_key, title FROM saves WHERE linkwarden_id IS NULL"
        ).fetchall()
        mirrored = {
            norm_url(url): link_id for link_id, url in conn.execute("SELECT id, url FROM links")
        }
    created = 0
    for save in pending:
        link_id = mirrored.get(save["url_key"])
        if link_id is None:
            link = await linkwarden.create_link(
                url=save["url"],
                name=save["title"] or save["url"],
                collection_id=settings.linkwarden_collection_id,
                tags=_facet_tag_names(latest_facets(settings, save["url_key"])),
            )
            link_id = link.get("id") if isinstance(link, dict) else None
            created += 1
        with connection(settings) as conn:
            # 0 marks "in Linkwarden, id unknown" so the save is not pushed twice
            conn.execute(
                "UPDATE saves SET linkwarden_id = ? WHERE id = ?", (link_id or 0, save["id"])
            )
    if created:
        log.info("push_local_saves: %d local save(s) created in Linkwarden", created)
    return created


def detect_saved(settings: Settings, new_link_urls: list[str]) -> int:
    """Implicit positive signal: a freshly polled Linkwarden save whose URL was
    previously served in the curated feed. Returns the number of new events.

    Deduped per normalized URL against ALL prior ``saved`` events, so an
    explicit capture (which the next poll re-ingests as a link) is not counted
    a second time.
    """
    if not new_link_urls:
        return 0
    new_keys = {norm_url(u) for u in new_link_urls if u}
    events = 0
    with connection(settings) as conn:
        # Any served section counts: a save from the broad section is exactly
        # the Exploring bandit's reward signal, not just curated saves.
        served = conn.execute(
            "SELECT DISTINCT c.id, c.url, c.embedding FROM feed_items f "
            "JOIN candidates c ON c.id = f.candidate_id"
        ).fetchall()
        recorded = {row[0] for row in conn.execute("SELECT url FROM feedback WHERE axis = 'saved'")}
        for row in served:
            url_key = norm_url(row["url"])
            if url_key in new_keys and url_key not in recorded:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO feedback(candidate_id, url, axis, value, embedding) "
                    "VALUES(?, ?, 'saved', 'implicit', ?)",
                    (row["id"], url_key, row["embedding"]),
                )
                recorded.add(url_key)
                events += cur.rowcount
    if events:
        log.info("detect_saved: %d served suggestion(s) were saved to Linkwarden", events)
    return events


def latest_facets(settings: Settings, url_key: str) -> dict[str, str]:
    """Latest value per explicit axis for one page identity (highest row id
    wins — repeated clicks are an event log; only the newest counts here)."""
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT f.axis, f.value FROM feedback f JOIN ("
            "  SELECT axis, MAX(id) AS mid FROM feedback"
            "  WHERE url = ? AND axis IN ('interest', 'mood') GROUP BY axis"
            ") m ON m.mid = f.id",
            (url_key,),
        ).fetchall()
    return {row["axis"]: row["value"] for row in rows}


def _link_id_for_url(settings: Settings, url_key: str) -> int | None:
    """The mirrored Linkwarden link id for a page identity, if it exists."""
    with connection(settings) as conn:
        for link_id, link_url in conn.execute("SELECT id, url FROM links"):
            if norm_url(link_url) == url_key:
                return int(link_id)
    return None


def _facet_tag_names(facets: dict[str, str]) -> list[str]:
    return [FACET_TAG[(a, v)] for a, v in facets.items() if (a, v) in FACET_TAG]


async def mirror_facet_tags(
    settings: Settings, url_key: str, linkwarden: LinkwardenClient | None = None
) -> bool:
    """Dual-write: replace the ``interest:*`` / ``mood:*`` tags on the mirrored
    Linkwarden link with the latest local facets (non-facet tags untouched).

    SQLite is the write-first store; a Linkwarden failure is logged and
    reported as False, never raised — the mirror is durable, not critical.
    Returns True only when the tags were actually written.
    """
    if not settings.linkwarden_enabled:
        return False  # disconnected: mirrored links may remain, but no token to write with
    link_id = _link_id_for_url(settings, url_key)
    if link_id is None:
        return False  # not (yet) in Linkwarden — tags are applied on capture
    desired = _facet_tag_names(latest_facets(settings, url_key))
    owns_client = linkwarden is None
    linkwarden = linkwarden or LinkwardenClient(settings)
    try:
        link = await linkwarden.get_link(link_id)
        kept = [
            t
            for t in (link.get("tags") or [])
            if not str(t.get("name", "")).startswith(_FACET_PREFIXES)
        ]
        link["tags"] = kept + [{"name": name} for name in desired]
        await linkwarden.update_link(link)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort mirror: report, never raise
        # httpx transport/status errors, JSON decode surprises, shape drift —
        # the SQLite write already succeeded; the caller only reports mirrored.
        log.warning("mirror_facet_tags: Linkwarden tag write failed for link %s: %s", link_id, exc)
        return False
    finally:
        if owns_client:
            await linkwarden.aclose()


def facet_query(settings: Settings, axis: str, value: str, days: int) -> list[dict]:
    """The benchmark query ("happy links from last week"), answered from local
    SQLite per the invariant. Latest event per page identity decides; the
    window is on that latest event's timestamp."""
    if value not in VALID_VALUES.get(axis, set()):
        raise ValueError(f"invalid facet {axis}={value!r}")
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT f.url, f.created_at, f.candidate_id, c.url AS raw_url, c.title, c.source "
            "FROM feedback f JOIN ("
            "  SELECT url, MAX(id) AS mid FROM feedback WHERE axis = ? GROUP BY url"
            ") m ON m.mid = f.id "
            "LEFT JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.value = ? AND f.created_at >= ? ORDER BY f.id DESC",
            (axis, value, cutoff),
        ).fetchall()
    return [
        {
            "candidate_id": row["candidate_id"] or 0,
            "url": row["raw_url"] or f"https://{row['url']}",
            "title": row["title"] or row["url"],
            "source": row["source"] or "",
            "created_at": row["created_at"],
        }
        for row in rows
    ]
