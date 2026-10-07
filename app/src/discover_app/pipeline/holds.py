"""Save for later: hold a story in the Bookmarks tab for ``hold_days``.

A hold is no signal. It writes nothing to ``feedback``, ``ratings`` or
``saves`` and never calls Linkwarden; profile and ranking never read the
``holds`` table. A held candidate is kept from the prune until its hold
expires, so the reader and its chat keep working. Saving a story releases
its hold: a story is never both held and saved.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..db import connection
from ..urls import norm_url

log = logging.getLogger(__name__)


def _cutoff(settings: Settings) -> str:
    # held_at is written by SQLite's datetime('now'): match that format
    return (datetime.now(UTC) - timedelta(days=settings.hold_days)).strftime("%Y-%m-%d %H:%M:%S")


def hold_candidate(settings: Settings, candidate_id: int) -> str:
    """Hold a story; "held", or "already_saved" for a story that is saved
    (nothing to hold then). Idempotent: a second hold keeps the first date.
    Raises LookupError for an unknown candidate."""
    with connection(settings) as conn:
        row = conn.execute("SELECT url FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise LookupError(f"unknown candidate {candidate_id}")
        if conn.execute(
            "SELECT 1 FROM feedback WHERE axis = 'saved' AND url = ?", (norm_url(row["url"]),)
        ).fetchone():
            return "already_saved"
        # an expired hold the GC has not dropped yet starts over
        conn.execute(
            "INSERT INTO holds(candidate_id) VALUES(?) ON CONFLICT(candidate_id) "
            "DO UPDATE SET held_at = datetime('now') WHERE held_at < ?",
            (candidate_id, _cutoff(settings)),
        )
    return "held"


def release_hold(settings: Settings, candidate_id: int) -> None:
    """Drop a hold, if there is one. Records nothing else."""
    with connection(settings) as conn:
        conn.execute("DELETE FROM holds WHERE candidate_id = ?", (candidate_id,))


def held_ids(conn, settings: Settings) -> set[int]:
    """Candidates held now (expired holds excluded, GC or not)."""
    return {
        row[0]
        for row in conn.execute(
            "SELECT candidate_id FROM holds WHERE held_at >= ?", (_cutoff(settings),)
        )
    }


def list_holds(conn, settings: Settings) -> list[dict]:
    """The Bookmarks tab: held stories, newest hold first, with the days
    left before each goes (started days count: 1 is the last day)."""
    now = datetime.now(UTC)
    items = []
    for row in conn.execute(
        "SELECT h.candidate_id, h.held_at, c.url, c.title, c.image_url, c.source "
        "FROM holds h JOIN candidates c ON c.id = h.candidate_id "
        "WHERE h.held_at >= ? ORDER BY h.held_at DESC, h.candidate_id DESC",
        (_cutoff(settings),),
    ):
        item = dict(row)
        held = datetime.strptime(item["held_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        left = held + timedelta(days=settings.hold_days) - now
        item["days_left"] = max(1, math.ceil(left.total_seconds() / 86400))
        items.append(item)
    return items


def gc_holds(conn, settings: Settings) -> int:
    """Drop holds older than ``hold_days``; their candidates fall back to the
    normal prune."""
    gone = conn.execute("DELETE FROM holds WHERE held_at < ?", (_cutoff(settings),)).rowcount
    if gone:
        log.info("gc_holds: dropped %d expired holds", gone)
    return gone
