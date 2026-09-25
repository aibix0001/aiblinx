"""Top-level jobs wired to the scheduler.

``run_poll`` is the lightweight frequent job (pull new saves). ``run_cycle`` is
the full daily digest pass. Both share one LLM client so the concurrency
semaphore is honored across all steps, and both serialize on a module lock so
the scheduler jobs and ``POST /admin/run-cycle`` never run the pipeline
concurrently (concurrent runs would contend for the SQLite write lock and
double-pay for the same pending embeddings).
"""

from __future__ import annotations

import asyncio
import logging

from ..clients.llm import LLMClient
from ..config import Settings, get_settings
from ..db import connection
from .candidates import gather_candidates
from .embedding import embed_pending_imports
from .enrich import enrich_feed
from .feeds import sync_feeds
from .ingest import ingest_links
from .miniflux_key import ensure_miniflux_token
from .profile import rebuild_profile
from .rank import build_feed

log = logging.getLogger(__name__)

_pipeline_lock = asyncio.Lock()


async def run_poll(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    async with _pipeline_lock:
        llm = LLMClient(settings)
        try:
            await ingest_links(settings, llm)
            # pages imported on the setup page join the profile within minutes
            await embed_pending_imports(settings, llm)
        finally:
            await llm.aclose()


async def run_cycle(settings: Settings | None = None) -> dict:
    """Full digest pass. Source steps are isolated: a transient Linkwarden or
    candidate-source failure is logged and the cycle still ranks and publishes
    a feed from the data it has — a day with a stale profile beats a day with
    no digest.

    Returns a dict with ``status`` ("cycle complete" / "cycle degraded") and
    an ``errors`` list of stage names that failed.
    """
    settings = settings or get_settings()
    errors: list[str] = []
    async with _pipeline_lock:
        llm = LLMClient(settings)
        try:
            try:
                await ingest_links(settings, llm)
            except Exception:
                log.exception("run_cycle: ingest failed, continuing with existing links")
                errors.append("ingest")
            try:
                await embed_pending_imports(settings, llm)
            except Exception:
                log.exception("run_cycle: embedding imported pages failed")
                errors.append("embed_imports")
            try:
                settings = await ensure_miniflux_token(settings)
            except Exception:
                log.exception("run_cycle: could not create a Miniflux API key")
                errors.append("miniflux_key")
            # New subscriptions are fetched by Miniflux on creation, so their
            # entries are already available to the gather that follows.
            try:
                await sync_feeds(settings)
            except Exception:
                log.exception("run_cycle: feed sync failed, continuing")
                errors.append("sync_feeds")
            try:
                await gather_candidates(settings, llm)
            except Exception:
                log.exception("run_cycle: candidate gather failed, continuing")
                errors.append("gather_candidates")
            # KMeans.fit + the sqlite loops are synchronous; keep them off the
            # event loop so HTTP endpoints stay responsive during the cycle.
            try:
                await asyncio.to_thread(rebuild_profile, settings)
            except Exception:
                log.exception("run_cycle: profile rebuild failed")
                errors.append("rebuild_profile")
            try:
                await build_feed(settings, llm)
            except Exception:
                log.exception("run_cycle: feed build failed")
                errors.append("build_feed")
            try:
                await enrich_feed(settings)
            except Exception:
                log.exception("run_cycle: feed enrichment failed")
                errors.append("enrich_feed")
            # Quality signal: suggestions saved back to
            # Linkwarden (explicit captures + poll-detected saves).
            with connection(settings) as conn:
                saved = conn.execute(
                    "SELECT COUNT(*) FROM feedback WHERE axis = 'saved'"
                ).fetchone()[0]
            log.info("benchmark: %d suggestion(s) saved to Linkwarden so far", saved)
        finally:
            await llm.aclose()
    status = "cycle degraded" if errors else "cycle complete"
    log.info("run_cycle %s", status)
    return {"status": status, "errors": errors}
