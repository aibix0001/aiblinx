"""FastAPI application: serves the feed and runs the scheduler in its lifespan."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .auth import feed_token, password_ok, require_login, set_session
from .clients.miniflux import MinifluxClient
from .clients.rss import subscribe
from .config import get_settings
from .db import connection, get_meta, init_db
from .html_text import card_summary, strip_html
from .icons import ICONS, MANIFEST
from .importers import import_bookmarks, import_urls, opml_feed_count, opml_feed_urls
from .models import (
    CaptureResponse,
    FeedAdd,
    FeedbackRequest,
    FeedbackResponse,
    FeedInfo,
    FeedItem,
    FeedResponse,
    HealthResponse,
    ImportRequest,
    ImportResponse,
    LoginRequest,
    RatingResponse,
    RunCycleResponse,
    SetupStatus,
    Topic,
    TopicUpdate,
)
from .pipeline.cycle import run_cycle
from .pipeline.feedback import (
    capture_candidate,
    explore_candidate,
    facet_query,
    mirror_facet_tags,
    promote_candidate,
    record_feedback,
    record_rating,
    served_section,
)
from .pipeline.feeds import add_feed, list_feeds, remove_feed
from .pipeline.miniflux_key import ensure_miniflux_token, miniflux_configured
from .pipeline.output import current_items, render_atom, render_markdown
from .reader import (
    extract_article,
    fetch_html,
    find_video,
    hide_reads_from_access_log,
    video_body,
)
from .scheduler import build_scheduler
from .setup_ui import render_login, render_setup
from .topics import list_topics, set_topic
from .ui import render_page, render_reader
from .urls import norm_url

log = logging.getLogger("discover_app")

# The Saved panel shows the newest saves; older ones stay in the database.
_SAVED_LIST_MAX = 200


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(settings)
    hide_reads_from_access_log()
    scheduler = build_scheduler(settings)
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("discover-app started (db=%s)", settings.db_path)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


def create_app() -> FastAPI:
    # basicConfig here (not at import time) so plain library imports don't
    # mutate global logging; it is a no-op when uvicorn already configured it.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app = FastAPI(title="aiblinx discover-app", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        with connection() as conn:
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                for table in ("links", "candidates", "feed_items")
            }
            saved = conn.execute("SELECT COUNT(*) FROM feedback WHERE axis = 'saved'").fetchone()[0]
            ratings = conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
        return HealthResponse(status="ok", saved_events=saved, ratings_count=ratings, **counts)

    @app.get("/feed", response_model=FeedResponse)
    def feed(
        mood: str | None = None,
        interest: str | None = None,
        days: Annotated[int, Query(ge=1)] = 7,
    ) -> FeedResponse:
        """Current curated feed — or, with a facet param, the benchmark query:
        items whose latest mood/interest feedback matches, within `days`
        (answered from local SQLite per the invariant)."""
        if mood is not None and interest is not None:
            raise HTTPException(status_code=422, detail="use one facet per query")
        if mood is not None or interest is not None:
            axis, value = ("mood", mood) if mood is not None else ("interest", interest)
            try:
                found = facet_query(get_settings(), axis, value or "", days)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            items = [
                FeedItem(
                    rank=idx,
                    section="facet",
                    candidate_id=entry["candidate_id"],
                    source=entry["source"],
                    url=entry["url"],
                    title=entry["title"],
                    score=0.0,
                    reason=f"{axis}:{value} @ {entry['created_at']}",
                )
                for idx, entry in enumerate(found)
            ]
            return FeedResponse(count=len(items), items=items)
        with connection() as conn:
            rows = conn.execute(
                "SELECT f.rank, f.section, f.candidate_id, f.score, f.reason, "
                "f.cycle_ts, c.source, c.url, c.title, c.snippet, c.published_at, "
                "c.image_url, c.description "
                "FROM feed_items f JOIN candidates c ON c.id = f.candidate_id "
                "WHERE f.cycle_ts = (SELECT value FROM meta WHERE key = 'last_cycle_ts') "
                "ORDER BY CASE f.section WHEN 'curated' THEN 0 ELSE 1 END, f.rank"
            ).fetchall()
        items: list[FeedItem] = []
        for row in rows:
            url = row["url"] or ""
            domain = url.split("//")[-1].split("/")[0].replace("www.", "")
            items.append(
                FeedItem(
                    rank=row["rank"],
                    section=row["section"],
                    candidate_id=row["candidate_id"],
                    source=row["source"],
                    url=url,
                    title=row["title"] or row["url"],
                    snippet=strip_html(row["snippet"]),
                    score=row["score"],
                    reason=row["reason"] or "",
                    published_at=row["published_at"],
                    discovered_at=row["cycle_ts"],
                    favicon=(f"https://www.google.com/s2/favicons?domain={domain}&sz=128"),
                    image_url=row["image_url"],
                    summary=card_summary(row["snippet"], row["description"]),
                )
            )
        return FeedResponse(count=len(items), items=items)

    @app.get("/ui", response_class=HTMLResponse)
    def ui_page() -> str:
        """Server-rendered mobile-first feed: title-image cards with save and
        up/down, restored from the feedback log, plus the topic picker."""
        with connection() as conn:
            curated = [dict(row) for row in current_items(conn)]
            broad = [dict(row) for row in current_items(conn, section="broad")]
            saved = {
                row[0] for row in conn.execute("SELECT url FROM feedback WHERE axis = 'saved'")
            }
            promoted = {
                row[0]
                for row in conn.execute("SELECT url_key FROM saves WHERE section = 'curated'")
            }
            explored = {
                row[0] for row in conn.execute("SELECT url_key FROM saves WHERE section = 'broad'")
            }
            interest = {
                row["url"]: row["value"]
                for row in conn.execute(
                    "SELECT url, value FROM feedback WHERE id IN ("
                    "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url)"
                )
            }
            saves = [
                dict(row)
                for row in conn.execute(
                    "SELECT url, title FROM saves ORDER BY id DESC LIMIT ?", (_SAVED_LIST_MAX,)
                )
            ]
            has_profile = conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] > 0
        for item in curated + broad:
            key = norm_url(item["url"] or "")
            item["saved"] = key in saved
            item["interest"] = interest.get(key)
            item["promoted"] = key in promoted
            item["explored"] = key in explored
        settings = get_settings()
        return render_page(
            curated,
            list_topics(settings),
            broad=broad,
            new_tab=settings.link_target == "new",
            saves=saves,
            linkwarden=settings.linkwarden_enabled,
            has_profile=has_profile,
        )

    app.middleware("http")(require_login)

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui")

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page() -> str:
        return render_login()

    @app.post("/login")
    async def login(body: LoginRequest) -> JSONResponse:
        """Exchange the optional APP_PASSWORD for a long-lived session cookie."""
        settings = get_settings()
        if not settings.app_password:
            return JSONResponse({"status": "open"})
        if not password_ok(settings, body.password):
            await asyncio.sleep(1.0)  # slows guessing without any lockout state
            raise HTTPException(status_code=401, detail="wrong password")
        response = JSONResponse({"status": "ok"})
        set_session(response, settings)
        return response

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> JSONResponse:
        return JSONResponse(MANIFEST, media_type="application/manifest+json")

    @app.get("/icons/{name}", include_in_schema=False)
    def icon(name: str) -> Response:
        data = ICONS.get(name)
        if data is None:
            raise HTTPException(status_code=404, detail="no such icon")
        return Response(data, media_type="image/png", headers={"Cache-Control": "max-age=604800"})

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page() -> str:
        """First-run and import page: what the feed learns from, topics,
        connections, and building the first feed."""
        settings = get_settings()
        with connection() as conn:
            counts = {
                "bookmarks": conn.execute("SELECT COUNT(*) FROM links").fetchone()[0],
                "imports": conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0],
                "saves": conn.execute("SELECT COUNT(*) FROM saves").fetchone()[0],
                "upvotes": conn.execute(
                    "SELECT COUNT(*) FROM feedback WHERE value = 'up' AND id IN ("
                    "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url)"
                ).fetchone()[0],
            }
            has_feed = get_meta(conn, "last_cycle_ts") is not None
        token = feed_token(settings)
        atom_url = settings.public_base_url.rstrip("/") + "/feed.atom"
        return render_setup(
            topics=list_topics(settings),
            counts=counts,
            linkwarden=settings.linkwarden_enabled,
            miniflux=miniflux_configured(settings),
            chat_model=settings.llm_chat_model if settings.rerank_enabled else "",
            embed_model=settings.llm_embed_model,
            password_on=bool(settings.app_password),
            atom_url=f"{atom_url}?token={token}" if token else atom_url,
            has_feed=has_feed,
        )

    @app.post("/setup/import", response_model=ImportResponse)
    async def setup_import(body: ImportRequest) -> ImportResponse:
        settings = get_settings()
        if body.kind == "opml":
            feeds = opml_feed_count(body.content)
            if not feeds:
                raise HTTPException(status_code=422, detail="No feeds found in that file.")
            if not miniflux_configured(settings):
                # no Miniflux: the built-in reader takes the subscriptions
                added = sum(subscribe(settings, url) for url in opml_feed_urls(body.content))
                return ImportResponse(
                    added=added,
                    skipped=feeds - added,
                    message=f"Subscribed to {added} feeds"
                    + (f" ({feeds - added} already there)." if feeds - added else "."),
                )
            settings = await ensure_miniflux_token(settings)
            miniflux = MinifluxClient(settings)
            try:
                await miniflux.import_opml(body.content)
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502, detail=f"Miniflux rejected the file: {exc}"
                ) from exc
            finally:
                await miniflux.aclose()
            return ImportResponse(
                added=feeds, skipped=0, message=f"Subscribed to {feeds} feeds in Miniflux."
            )
        if body.kind == "bookmarks":
            added, skipped = import_bookmarks(settings, body.content)
            what = "bookmarks"
        else:
            added, skipped = await import_urls(settings, body.content)
            what = "links"
        if added + skipped == 0:
            raise HTTPException(
                status_code=422,
                detail="No links found. Is it a browser bookmarks export (an HTML file)?"
                if body.kind == "bookmarks"
                else "No links found in that text.",
            )
        note = f" ({skipped} already known)" if skipped else ""
        return ImportResponse(
            added=added,
            skipped=skipped,
            message=f"Added {added} {what}{note}. They shape your feed from the next update.",
        )

    @app.post("/setup/build", status_code=202)
    async def setup_build() -> dict:
        """Build the very first feed now instead of the next morning. Only
        while no feed exists: later builds are the daily cycle's job (and
        POST /admin/run-cycle), so this open endpoint can't be used to run
        paid model calls repeatedly."""
        with connection() as conn:
            if get_meta(conn, "last_cycle_ts") is not None:
                raise HTTPException(
                    status_code=409, detail="Your feed exists already. It refreshes every morning."
                )
        task = getattr(app.state, "build_task", None)
        if task is None or task.done():
            app.state.build_task = asyncio.create_task(run_cycle())
        return {"status": "building"}

    @app.get("/setup/status", response_model=SetupStatus)
    def setup_status() -> SetupStatus:
        task = getattr(app.state, "build_task", None)
        running = task is not None and not task.done()
        with connection() as conn:
            has_feed = get_meta(conn, "last_cycle_ts") is not None
        message = (
            ""
            if running or has_feed
            else "Nothing to show yet. Import bookmarks or pick a few topics, then try again."
        )
        return SetupStatus(running=running, has_feed=has_feed, message=message)

    async def _feed_settings():
        """Settings for feed management: Miniflux (with its API key made on
        demand) when it is set up, else the built-in reader."""
        settings = get_settings()
        if miniflux_configured(settings):
            settings = await ensure_miniflux_token(settings)
        return settings

    @app.get("/feeds", response_model=list[FeedInfo])
    async def feeds_list() -> list[FeedInfo]:
        try:
            feeds = await list_feeds(await _feed_settings())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Miniflux: {exc}") from exc
        return [FeedInfo(**f) for f in feeds]

    @app.post("/feeds", status_code=201)
    async def feeds_add(body: FeedAdd) -> dict:
        try:
            feed_url = await add_feed(await _feed_settings(), body.url)
        except LookupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Miniflux: {exc}") from exc
        return {"url": feed_url}

    @app.delete("/feeds/{feed_id}", status_code=204)
    async def feeds_remove(feed_id: int) -> Response:
        try:
            await remove_feed(await _feed_settings(), feed_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Miniflux: {exc}") from exc
        return Response(status_code=204)

    @app.get("/topics", response_model=list[Topic])
    def topics_list() -> list[Topic]:
        return [Topic(**t) for t in list_topics(get_settings())]

    @app.post("/topics/{name}", response_model=Topic)
    def topics_set(name: str, body: TopicUpdate) -> Topic:
        """Toggle an onboarding topic; selected topics arm the anti-bubble
        section (SearXNG fetch + bandit)."""
        try:
            set_topic(get_settings(), name, body.selected)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Topic(name=name, selected=body.selected)

    @app.get("/feed.atom")
    def feed_atom() -> Response:
        return Response(content=render_atom(), media_type="application/atom+xml")

    @app.get("/digest", response_class=PlainTextResponse)
    def digest() -> str:
        return render_markdown()

    @app.post("/admin/run-cycle", response_model=RunCycleResponse)
    async def admin_run_cycle(
        x_admin_token: Annotated[str, Header()] = "",
    ) -> RunCycleResponse:
        """Trigger a full pipeline cycle on demand.

        Each call spends real LLM/embedding credit, so when ADMIN_TOKEN is
        configured the X-Admin-Token header must match. An empty ADMIN_TOKEN
        leaves the route open (local/dev use).
        """
        settings = get_settings()
        if settings.admin_token and not secrets.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(status_code=403, detail="X-Admin-Token header required")
        result = await run_cycle()
        return RunCycleResponse(**result)

    # Feedback endpoints share the read endpoints' exposure model (personal
    # deployment): they mutate only local feedback state / the user's own
    # Linkwarden, and the PR-2 HTML page must be able to call them without a
    # token dance. ADMIN_TOKEN stays reserved for credit-spending admin routes.

    @app.get("/read/{candidate_id}", response_class=HTMLResponse, include_in_schema=False)
    async def read(candidate_id: int) -> Response:
        """Reader view of one feed item; the original page when there is no
        article text to show. Records nothing about the visit (no tracking)."""
        settings = get_settings()
        with connection() as conn:
            row = conn.execute(
                "SELECT id, url, title, image_url, source, published_at FROM candidates "
                "WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None or not row["url"].startswith(("http://", "https://")):
                raise HTTPException(status_code=404, detail="no such item")
            key = norm_url(row["url"])
            saved = conn.execute(
                "SELECT 1 FROM feedback WHERE axis = 'saved' AND url = ?", (key,)
            ).fetchone()
            interest = conn.execute(
                "SELECT value FROM feedback WHERE axis = 'interest' AND url = ? "
                "ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
            # the one button that files the story on the other side
            filed = conn.execute("SELECT section FROM saves WHERE url_key = ?", (key,)).fetchone()
            if served_section(conn, candidate_id) == "broad":
                promoted, explored = bool(filed and filed[0] == "curated"), None
            else:
                promoted, explored = None, bool(filed and filed[0] == "broad")
        item = dict(row)
        page = await fetch_html(item["url"], settings.enrich_timeout_s)
        body = (
            await asyncio.to_thread(extract_article, page, item["url"], item["title"])
            if page
            else None
        )
        if body is None and page:
            # no article, but maybe a video we can play without its page
            video = await asyncio.to_thread(find_video, page)
            if video:
                body = video_body(video, item["title"])
                item["image_url"] = None  # the poster is the picture
        if body is None:
            return RedirectResponse(item["url"], status_code=302)
        return HTMLResponse(
            render_reader(
                item,
                body,
                saved=saved is not None,
                interest=interest[0] if interest else None,
                linkwarden=settings.linkwarden_enabled,
                promoted=promoted,
                explored=explored,
            ),
            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"},
        )

    @app.post("/feed/{candidate_id}/save", response_model=CaptureResponse)
    async def feed_save(candidate_id: int) -> CaptureResponse:
        """ "+" capture: save this suggestion into Linkwarden (idempotent)."""
        try:
            result = await capture_candidate(get_settings(), candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            # Most common cause: wrong LINKWARDEN_COLLECTION_ID or expired token.
            raise HTTPException(
                status_code=502, detail=f"Linkwarden rejected the save: {exc}"
            ) from exc
        return CaptureResponse(**result)

    @app.post("/feed/{candidate_id}/promote", response_model=CaptureResponse)
    async def feed_promote(candidate_id: int) -> CaptureResponse:
        """Make an Exploring story a main interest: saved into (or moved to)
        the main collection, where it shapes "For you"."""
        try:
            result = await promote_candidate(get_settings(), candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Linkwarden rejected the promotion: {exc}"
            ) from exc
        return CaptureResponse(**result)

    @app.post("/feed/{candidate_id}/explore", response_model=CaptureResponse)
    async def feed_explore(candidate_id: int) -> CaptureResponse:
        """Keep a "For you" story as a distraction: saved into (or moved to)
        the Exploring collection, and "less like this" for "For you"."""
        try:
            result = await explore_candidate(get_settings(), candidate_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail=f"Linkwarden rejected the save: {exc}"
            ) from exc
        return CaptureResponse(**result)

    @app.post("/feed/{candidate_id}/interest", response_model=FeedbackResponse)
    async def feed_interest(candidate_id: int, body: FeedbackRequest) -> FeedbackResponse:
        return await _record(candidate_id, "interest", body.value)

    @app.post("/feed/{candidate_id}/mood", response_model=FeedbackResponse)
    async def feed_mood(candidate_id: int, body: FeedbackRequest) -> FeedbackResponse:
        return await _record(candidate_id, "mood", body.value)

    @app.post("/feed/{candidate_id}/rating", response_model=RatingResponse)
    async def feed_rating(candidate_id: int, body: FeedbackRequest) -> RatingResponse:
        settings = get_settings()
        try:
            value_float = float(body.value)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=422,
                detail=f"invalid rating value {body.value!r}",
            ) from None
        try:
            value, changed = record_rating(settings, candidate_id, value_float)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return RatingResponse(
            status="recorded" if changed else "existing",
            candidate_id=candidate_id,
            value=value,
            changed=changed,
        )

    async def _record(candidate_id: int, axis: str, value: str) -> FeedbackResponse:
        settings = get_settings()
        try:
            url_key = record_feedback(settings, candidate_id, axis, value)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # SQLite is written; the Linkwarden tag mirror is best-effort and its
        # outcome is reported, not raised (durable mirror, not critical path).
        # record_feedback returned the page identity, so a concurrent prune of
        # the candidate row cannot fail the request after the write succeeded.
        mirrored = await mirror_facet_tags(settings, url_key)
        return FeedbackResponse(
            status="recorded",
            candidate_id=candidate_id,
            axis=axis,
            value=value,
            mirrored=mirrored,
        )

    return app
