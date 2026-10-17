"""Upgrade from Save for later (0.7.0): held stories become Archive saves.

Save for later kept stories in a local ``holds`` table, never in Linkwarden.
Archive replaced it: the same ``archive_candidate`` path the Archive
button takes files every row still held into the Archive collection, oldest
hold first. A hold row goes only after its story is filed, and the table is
dropped only once it is empty, so a Linkwarden outage leaves the rest for the
next start. A held story carries no discussion (chats lived in the browser).

A held story that is already a Linkwarden bookmark (mirrored in ``links``)
is moved to Archive, as the Archive button would do; the hold goes with it.
Until the table is gone, ``prune_candidates`` keeps the held candidates.
"""

from __future__ import annotations

import logging

from ..clients.linkwarden import LinkwardenClient
from ..config import Settings
from ..db import connection
from .feedback import archive_candidate

log = logging.getLogger(__name__)


async def migrate_holds(settings: Settings, linkwarden: LinkwardenClient | None = None) -> int:
    """File every held story into Archive; returns how many were filed. A
    no-op once the ``holds`` table is gone."""
    with connection(settings) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'holds'").fetchone():
            return 0
        held = [
            row[0]
            for row in conn.execute("SELECT candidate_id FROM holds ORDER BY held_at, candidate_id")
        ]
    owns_client = linkwarden is None and settings.linkwarden_enabled
    if owns_client:
        linkwarden = LinkwardenClient(settings)
    filed = 0
    try:
        for candidate_id in held:
            try:
                await archive_candidate(settings, candidate_id, linkwarden)
                filed += 1
            except Exception:
                # an outage must not keep the app from starting: the rest of
                # the holds wait for the next start
                log.exception(
                    "migrate_holds: %d of %d filed, the rest waits for the next start",
                    filed,
                    len(held),
                )
                break
            with connection(settings) as conn:
                conn.execute("DELETE FROM holds WHERE candidate_id = ?", (candidate_id,))
    finally:
        if owns_client:
            await linkwarden.aclose()
    with connection(settings) as conn:
        if conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0:
            conn.execute("DROP TABLE holds")
            log.info("migrate_holds: %d held stories filed under Archive, table dropped", filed)
    return filed
