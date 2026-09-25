"""Network-free unit tests: storage, ingest walk, candidates, ranking, output."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import numpy as np
import pytest
import sqlite_vec

from discover_app.auth import feed_token
from discover_app.config import Settings
from discover_app.db import connection, get_meta, init_db
from discover_app.html_text import card_summary, first_image, page_meta, strip_html
from discover_app.importers import (
    opml_feed_count,
    parse_bookmarks_html,
    parse_url_list,
    store_imports,
)
from discover_app.pipeline import candidates as candidates_mod
from discover_app.pipeline import enrich as enrich_mod
from discover_app.pipeline import feeds as feeds_mod
from discover_app.pipeline import ingest
from discover_app.pipeline import miniflux_key as mf_key_mod
from discover_app.pipeline.candidates import gather_candidates, is_ad, prune_candidates
from discover_app.pipeline.cycle import run_cycle
from discover_app.pipeline.embedding import _embed_pending
from discover_app.pipeline.enrich import enrich_feed, fetch_meta
from discover_app.pipeline.feedback import (
    capture_candidate,
    detect_saved,
    facet_query,
    latest_facets,
    mirror_facet_tags,
    push_local_saves,
    record_feedback,
    record_rating,
)
from discover_app.pipeline.feeds import pending_domains, sync_feeds
from discover_app.pipeline.miniflux_key import ensure_miniflux_token
from discover_app.pipeline.output import render_markdown
from discover_app.pipeline.profile import rebuild_profile
from discover_app.pipeline.rank import build_feed, mmr_select
from discover_app.topics import IAB_TIER1, list_topics, selected_topics, set_topic
from discover_app.urls import norm_url


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        embed_dim=4,
        linkwarden_token="test-token",  # noqa: S106 - dummy value, tests never hit the network
        **overrides,
    )


def _add_candidate(
    conn: sqlite3.Connection, cid: int, url: str, title: str, vec: list[float]
) -> None:
    blob = sqlite_vec.serialize_float32(vec)
    conn.execute(
        "INSERT INTO candidates(id, source, url, title, snippet, embedded, embedding) "
        "VALUES(?, 'hackernews', ?, ?, '', 1, ?)",
        (cid, url, title, blob),
    )
    conn.execute("INSERT INTO vec_candidates(rowid, embedding) VALUES(?, ?)", (cid, blob))


def _add_centroid(conn: sqlite3.Connection, vec: list[float]) -> None:
    conn.execute(
        "INSERT INTO profile(kind, vector) VALUES('centroid', ?)",
        (sqlite_vec.serialize_float32(vec),),
    )


class _FakeRerankLLM:
    """LLM double for build_feed: returns a canned chat response."""

    def __init__(self, response: str) -> None:
        self.response = response

    async def chat(self, messages, temperature: float = 0.0) -> str:
        return self.response

    async def aclose(self) -> None:
        pass


def test_init_db_creates_tables(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert {"links", "candidates", "profile", "feed_items", "meta"} <= names
    assert "vec_links" in names
    assert "vec_candidates" in names


class _FakeLinkwarden:
    """Serves fixed newest-first pages keyed by cursor, like the real API."""

    def __init__(self, pages: dict[int | None, list[dict]]) -> None:
        self.pages = pages
        self.calls: list[int | None] = []

    async def fetch_links(self, cursor: int | None = None) -> list[dict]:
        self.calls.append(cursor)
        return self.pages.get(cursor, [])

    async def aclose(self) -> None:
        pass


def _link(link_id: int) -> dict:
    return {"id": link_id, "url": f"https://example.com/{link_id}", "tags": []}


class _FakeLLM:
    async def aclose(self) -> None:
        pass


async def _run_ingest(settings, fake, monkeypatch) -> int:
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: fake)
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())

    async def _noop_embed(settings, llm):
        return 0

    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)
    return await ingest.ingest_links(settings)


async def test_ingest_backfill_walks_all_pages(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    fake = _FakeLinkwarden(
        {None: [_link(6), _link(5)], 5: [_link(4), _link(3)], 3: [_link(2)], 2: []}
    )
    new = await _run_ingest(settings, fake, monkeypatch)
    assert new == 5
    assert fake.calls == [None, 5, 3, 2]  # walked to the empty page
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 5
        assert get_meta(conn, "linkwarden_backfill_done") == "1"


async def test_ingest_stops_early_once_backfilled(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    fake = _FakeLinkwarden({None: [_link(2), _link(1)], 1: []})
    await _run_ingest(settings, fake, monkeypatch)

    # Next poll: one new save on top; page 2 is all-known, so the walk stops there.
    fake2 = _FakeLinkwarden({None: [_link(3), _link(2)], 2: [_link(1)], 1: []})
    new = await _run_ingest(settings, fake2, monkeypatch)
    assert new == 1
    assert fake2.calls == [None, 2]  # no third request for the empty page

    # Quiet poll: all-known first page, single request.
    fake3 = _FakeLinkwarden({None: [_link(3), _link(2)], 2: [_link(1)], 1: []})
    assert await _run_ingest(settings, fake3, monkeypatch) == 0
    assert fake3.calls == [None]


async def test_ingest_skips_duplicate_urls(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    dup = _link(4)
    dup["url"] = "https://example.com/6"  # same URL saved twice, different ids
    fake = _FakeLinkwarden({None: [_link(6), _link(5)], 5: [dup], 4: []})
    new = await _run_ingest(settings, fake, monkeypatch)
    assert new == 2  # the duplicate is not mirrored and not counted
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
        assert get_meta(conn, "linkwarden_backfill_done") == "1"


async def test_ingest_stops_on_stuck_cursor(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    page = [_link(2), _link(1)]
    fake = _FakeLinkwarden({None: page, 1: page})  # cursor ignored: same page again
    new = await _run_ingest(settings, fake, monkeypatch)
    assert new == 2
    assert fake.calls == [None, 1]  # bailed instead of looping forever


class _FakeLinkwardenWriter:
    """Write-side double: records create/get/update calls, serves one link."""

    def __init__(self, link: dict | None = None) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.link = link or {"id": 4711, "url": "https://ex.com/a", "tags": []}

    async def create_link(self, url, name, collection_id, tags=None):
        self.created.append(
            {"url": url, "name": name, "collection_id": collection_id, "tags": tags or []}
        )
        return {"id": 4711, "url": url}

    async def get_link(self, link_id):
        return dict(self.link)

    async def update_link(self, link):
        self.updated.append(link)
        return link

    async def aclose(self) -> None:
        pass


def test_record_feedback_valid_and_invalid(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    record_feedback(settings, 1, "interest", "up")
    record_feedback(settings, 1, "mood", "sad")
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT candidate_id, url, axis, value, embedding FROM feedback ORDER BY id"
        ).fetchall()
    assert [(r["axis"], r["value"]) for r in rows] == [("interest", "up"), ("mood", "sad")]
    assert all(r["url"] == "ex.com/a" and r["embedding"] is not None for r in rows)
    with pytest.raises(ValueError):
        record_feedback(settings, 1, "interest", "happy")  # wrong value for axis
    with pytest.raises(ValueError):
        record_feedback(settings, 1, "saved", "explicit")  # saved is not an API axis
    with pytest.raises(LookupError):
        record_feedback(settings, 999, "interest", "up")


async def test_capture_creates_link_and_is_idempotent(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    fake = _FakeLinkwardenWriter()
    first = await capture_candidate(settings, 1, linkwarden=fake)
    assert first == {"status": "saved", "linkwarden_id": 4711}
    assert fake.created[0]["collection_id"] == settings.linkwarden_collection_id
    second = await capture_candidate(settings, 1, linkwarden=fake)
    assert second == {"status": "already_saved"}
    assert len(fake.created) == 1  # no duplicate create
    with pytest.raises(LookupError):
        await capture_candidate(settings, 999, linkwarden=fake)


async def test_capture_skips_already_bookmarked_url(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
        # the same page is already bookmarked under a URL variant
        conn.execute("INSERT INTO links(id, url, embedded) VALUES(1, 'http://www.ex.com/a/', 1)")
    fake = _FakeLinkwardenWriter()
    assert await capture_candidate(settings, 1, linkwarden=fake) == {"status": "already_saved"}
    assert fake.created == []


def test_detect_saved_matches_served_items_once(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/served", "served", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/unserved", "unserved", [0, 1.0, 0, 0])
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, '', 't1')"
        )
    # user saved the served item (URL variant) plus something never served
    assert detect_saved(settings, ["https://www.ex.com/served/", "https://other.com/x"]) == 1
    with connection(settings) as conn:
        row = conn.execute("SELECT candidate_id, value FROM feedback WHERE axis='saved'").fetchone()
    assert (row["candidate_id"], row["value"]) == (1, "implicit")
    # the next poll ingesting the same URL must not double-count
    assert detect_saved(settings, ["https://ex.com/served"]) == 0
    assert detect_saved(settings, []) == 0


def test_feedback_survives_candidate_prune(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/old", "old item", [1.0, 0, 0, 0])
        conn.execute("UPDATE candidates SET fetched_at = '2000-01-01 00:00:00' WHERE id = 1")
    record_feedback(settings, 1, "interest", "up")
    assert prune_candidates(settings) == 1
    with connection(settings) as conn:
        row = conn.execute("SELECT candidate_id, url, embedding FROM feedback").fetchone()
    assert row["candidate_id"] is None  # FK set null on prune
    assert row["url"] == "ex.com/old" and row["embedding"] is not None  # signal survives


async def test_capture_failure_writes_nothing_locally(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])

    class _DeadWriter(_FakeLinkwardenWriter):
        async def create_link(self, url, name, collection_id, tags=None):
            raise RuntimeError("linkwarden down")

    with pytest.raises(RuntimeError):
        await capture_candidate(settings, 1, linkwarden=_DeadWriter())
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0  # retry-safe


async def test_capture_race_creates_single_saved_event(tmp_path):
    import asyncio

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])

    class _SlowWriter(_FakeLinkwardenWriter):
        async def create_link(self, url, name, collection_id, tags=None):
            await asyncio.sleep(0.01)  # widen the race window across the await
            return await super().create_link(url, name, collection_id, tags)

    fake = _SlowWriter()
    results = await asyncio.gather(
        capture_candidate(settings, 1, linkwarden=fake),
        capture_candidate(settings, 1, linkwarden=fake),
    )
    assert sorted(r["status"] for r in results) == ["already_saved", "saved"]
    assert len(fake.created) == 1  # the capture lock stopped the double POST
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feedback WHERE axis='saved'").fetchone()[0] == 1


def test_feedback_endpoints_error_mapping(tmp_path, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())  # no context manager: lifespan (scheduler) not run

    assert client.post("/feed/1/interest", json={"value": "up"}).status_code == 200
    assert client.post("/feed/1/interest", json={"value": "sideways"}).status_code == 422
    assert client.post("/feed/999/mood", json={"value": "happy"}).status_code == 404

    async def _lw_down(settings, candidate_id):
        raise httpx.ConnectError("no route to linkwarden")

    monkeypatch.setattr(app_mod, "capture_candidate", _lw_down)
    resp = client.post("/feed/1/save")
    assert resp.status_code == 502
    assert "Linkwarden" in resp.json()["detail"]


def test_latest_facets_latest_click_wins(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    record_feedback(settings, 1, "interest", "up")
    record_feedback(settings, 1, "mood", "happy")
    record_feedback(settings, 1, "interest", "down")  # user changed their mind
    assert latest_facets(settings, "ex.com/a") == {"interest": "down", "mood": "happy"}


async def test_mirror_facet_tags_replaces_only_facets(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
        # the page is mirrored in Linkwarden as link 42 (URL variant)
        conn.execute("INSERT INTO links(id, url, embedded) VALUES(42, 'http://www.ex.com/a/', 1)")
    record_feedback(settings, 1, "interest", "up")
    record_feedback(settings, 1, "mood", "sad")
    fake = _FakeLinkwardenWriter(
        link={
            "id": 42,
            "url": "http://www.ex.com/a/",
            "tags": [
                {"name": "python"},
                {"name": "interest:low"},
                {"name": "mood:happy"},
            ],
        }
    )
    assert await mirror_facet_tags(settings, "ex.com/a", linkwarden=fake) is True
    tags = [t["name"] for t in fake.updated[0]["tags"]]
    assert "python" in tags  # non-facet tag untouched
    assert set(tags) & {"interest:high", "mood:sad"} == {"interest:high", "mood:sad"}
    assert "interest:low" not in tags and "mood:happy" not in tags  # stale facets replaced


async def test_mirror_facet_tags_without_link_or_on_failure(tmp_path):
    import httpx

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    record_feedback(settings, 1, "mood", "happy")
    # not in Linkwarden -> False, no client interaction needed
    assert await mirror_facet_tags(settings, "ex.com/a") is False

    class _DeadMirror(_FakeLinkwardenWriter):
        async def get_link(self, link_id):
            raise httpx.ConnectError("down")

    with connection(settings) as conn:
        conn.execute("INSERT INTO links(id, url, embedded) VALUES(42, 'https://ex.com/a', 1)")
    # Linkwarden down -> False (logged), never raised: SQLite already has the row
    assert await mirror_facet_tags(settings, "ex.com/a", linkwarden=_DeadMirror()) is False

    class _DeadUpdate(_FakeLinkwardenWriter):
        async def update_link(self, link):
            raise httpx.HTTPStatusError("400", request=None, response=None)

    # fetch ok but the PUT fails -> False, nothing recorded as updated
    dead_update = _DeadUpdate(link={"id": 42, "url": "https://ex.com/a", "tags": None})
    assert await mirror_facet_tags(settings, "ex.com/a", linkwarden=dead_update) is False
    assert dead_update.updated == []
    # "tags": null from the API must not crash the mirror (handled as empty)


async def test_capture_applies_prior_facets_as_tags(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    record_feedback(settings, 1, "interest", "up")
    record_feedback(settings, 1, "mood", "happy")
    fake = _FakeLinkwardenWriter()
    result = await capture_candidate(settings, 1, linkwarden=fake)
    assert result["status"] == "saved"
    assert sorted(fake.created[0]["tags"]) == ["interest:high", "mood:happy"]


def test_facet_query_window_and_latest_wins(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/happy", "happy item", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/flip", "flipped item", [0, 1.0, 0, 0])
        _add_candidate(conn, 3, "https://ex.com/old", "old item", [0, 0, 1.0, 0])
    record_feedback(settings, 1, "mood", "happy")
    record_feedback(settings, 2, "mood", "happy")
    record_feedback(settings, 2, "mood", "sad")  # latest wins: no longer happy
    record_feedback(settings, 3, "mood", "happy")
    with connection(settings) as conn:
        conn.execute(  # push item 3's event outside the window
            "UPDATE feedback SET created_at = '2000-01-01 00:00:00' WHERE candidate_id = 3"
        )
    found = facet_query(settings, "mood", "happy", days=7)
    assert [f["title"] for f in found] == ["happy item"]
    with pytest.raises(ValueError):
        facet_query(settings, "mood", "angry", days=7)


def test_ui_and_facet_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "Tricky <script> title", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "javascript:alert(1)", "hostile feed entry", [0, 1.0, 0, 0])
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, 'because & why', 't1'), "
            "(1, 'curated', 2, 0.5, '', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
    record_feedback(settings, 1, "mood", "happy")
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())

    page = client.get("/ui")
    assert page.status_code == 200
    assert "&lt;script&gt;" in page.text  # titles are escaped
    assert 'id="c1"' in page.text
    assert 'href="javascript:' not in page.text  # non-http(s) schemes never render live
    assert 'href="#"' in page.text  # candidate 2's javascript: URL was neutralized

    facet = client.get("/feed", params={"mood": "happy"}).json()
    assert facet["count"] == 1 and facet["items"][0]["section"] == "facet"
    assert client.get("/feed", params={"mood": "happy", "interest": "up"}).status_code == 422
    assert client.get("/feed", params={"interest": "sideways"}).status_code == 422
    assert client.get("/feed", params={"mood": ""}).status_code == 422  # empty is not a facet
    assert client.get("/feed", params={"mood": "happy", "days": 0}).status_code == 422

    # feedback on an item with no Linkwarden mirror reports mirrored: false
    resp = client.post("/feed/1/interest", json={"value": "up"}).json()
    assert resp["status"] == "recorded" and resp["mirrored"] is False


def test_digest_links_to_ui_page(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute(
            "INSERT INTO candidates(id, source, url, title, snippet) "
            "VALUES(1, 'hackernews', 'https://example.com', 'Hello World', 'snip')"
        )
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, '', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
    out = render_markdown(settings)
    assert "[rate](http://localhost:8000/ui#c1)" in out


def _add_link_vec(conn: sqlite3.Connection, link_id: int, url: str, vec: list[float]) -> None:
    blob = sqlite_vec.serialize_float32(vec)
    conn.execute(
        "INSERT INTO links(id, url, embedded, embedding) VALUES(?, ?, 1, ?)", (link_id, url, blob)
    )


def test_reweight_saved_signal_boosts_nearest_centroid(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        # two clearly separated interest clusters -> k-means finds both
        for i in range(3):
            _add_link_vec(conn, 100 + i, f"https://a.example/{i}", [1.0, 0.01 * i, 0, 0])
            _add_link_vec(conn, 200 + i, f"https://b.example/{i}", [0.01 * i, 1.0, 0, 0])
        # a saved suggestion and a thumbs-up, both in cluster A's region
        _add_candidate(conn, 1, "https://c.example/saved", "saved item", [0.99, 0.05, 0, 0])
        _add_candidate(conn, 2, "https://c.example/up", "liked item", [0.98, 0.06, 0, 0])
    settings2 = _settings(tmp_path, profile_clusters=2)
    with connection(settings2) as conn:
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding) "
            "SELECT id, url, 'saved', 'explicit', embedding FROM candidates WHERE id = 1"
        )
    record_feedback(settings2, 2, "interest", "up")
    assert rebuild_profile(settings2) == 2
    with connection(settings2) as conn:
        rows = conn.execute("SELECT weight, vector FROM profile ORDER BY weight DESC").fetchall()
    assert rows[0]["weight"] > rows[1]["weight"]  # cluster A got the signal
    # pin the calculus: base 1.0 + saved 2.0 + up 1.0, fresh events (decay ~ 1)
    assert abs(rows[0]["weight"] - 4.0) < 0.01
    assert rows[1]["weight"] == 1.0  # untouched cluster keeps base weight
    # the boosted centroid is the one pointing at cluster A's region
    top_vec = np.frombuffer(rows[0]["vector"], dtype=np.float32)
    assert top_vec[0] > top_vec[1]


def test_reweight_floor_decay_and_exclusions(tmp_path):
    settings = _settings(tmp_path, profile_clusters=1)
    init_db(settings)
    with connection(settings) as conn:
        _add_link_vec(conn, 1, "https://a.example/1", [1.0, 0, 0, 0])
        for i in range(9):  # nine distinct down-voted pages, same region
            _add_candidate(conn, 10 + i, f"https://d.example/{i}", f"d{i}", [0.9, 0.1, 0, 0])
    for i in range(9):
        record_feedback(settings, 10 + i, "interest", "down")
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    assert weight == 0.25  # 1 - 9 would go negative; the floor holds

    # mood events and NULL-embedding rows never contribute; ancient events decay to ~0
    with connection(settings) as conn:
        conn.execute("DELETE FROM feedback")
        _add_candidate(conn, 30, "https://m.example/x", "mood item", [0.9, 0, 0.1, 0])
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding) "
            "VALUES(NULL, 'no.embed/x', 'saved', 'implicit', NULL)"
        )
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding, created_at) "
            "SELECT id, url, 'saved', 'explicit', embedding, '2000-01-01 00:00:00' "
            "FROM candidates WHERE id = 30"
        )
    record_feedback(settings, 30, "mood", "happy")
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    assert abs(weight - 1.0) < 0.01  # 26-year-old save decayed away; mood/NULL ignored


def test_reweight_latest_interest_wins(tmp_path):
    settings = _settings(tmp_path, profile_clusters=1)
    init_db(settings)
    with connection(settings) as conn:
        _add_link_vec(conn, 1, "https://a.example/1", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://a.example/2", "item", [0.9, 0.1, 0, 0])
    record_feedback(settings, 2, "interest", "up")
    record_feedback(settings, 2, "interest", "down")  # changed their mind
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    assert weight == 0.25  # 1 - 1 = 0 -> floored; only the latest (down) event counted


def test_knn_pool_scales_by_centroid_weight(tmp_path):
    from discover_app.pipeline.rank import _knn_pool

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        # two orthogonal centroids, one twice the weight of the other
        conn.execute(
            "INSERT INTO profile(kind, weight, vector) VALUES('centroid', 2.0, ?)",
            (sqlite_vec.serialize_float32([1.0, 0, 0, 0]),),
        )
        conn.execute(
            "INSERT INTO profile(kind, weight, vector) VALUES('centroid', 1.0, ?)",
            (sqlite_vec.serialize_float32([0, 1.0, 0, 0]),),
        )
        # one candidate perfectly aligned with each centroid
        _add_candidate(conn, 1, "https://a.example/1", "strong cluster", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://b.example/1", "weak cluster", [0, 1.0, 0, 0])
        pool = _knn_pool(conn, settings)
    # both have raw cosine sim 1.0; weights split them: 1.0 vs 0.75
    assert pool[1] > pool[2]
    assert abs(pool[1] - 1.0) < 1e-5 and abs(pool[2] - 0.75) < 1e-5


def test_healthz_reports_saved_events(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(NULL, 'x/1', 'saved', 'implicit'), (NULL, 'x/2', 'mood', 'happy')"
        )
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())
    body = client.get("/healthz").json()
    assert body["saved_events"] == 1  # mood events don't count


def test_weight_column_migration_for_cleanup_era_dbs(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute("ALTER TABLE profile DROP COLUMN weight")  # simulate a post-#5 DB
    init_db(settings)  # migration re-adds it
    with connection(settings) as conn:
        conn.execute("INSERT INTO profile(kind, vector) VALUES('centroid', x'00000000')")
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    assert weight == 1.0


def test_choose_arm_explore_and_exploit():
    import random

    from discover_app.pipeline.broad import choose_arm

    stats = {"Science": (10, 8), "Travel": (10, 1)}
    exploit = random.Random(1)  # noqa: S311 - deterministic test rng
    # epsilon 0: always the best Laplace rate (Science: 9/12 vs Travel: 2/12)
    assert choose_arm(["Science", "Travel"], stats, 0.0, exploit) == "Science"
    # never-served arm gets the optimistic 1/2 prior and beats a proven loser
    assert choose_arm(["Travel", "Pets"], stats, 0.0, exploit) == "Pets"
    # epsilon 1: pure exploration — over many draws both arms appear
    explorer = random.Random(2)  # noqa: S311 - deterministic test rng
    seen = {choose_arm(["Science", "Travel"], stats, 1.0, explorer) for _ in range(50)}
    assert seen == {"Science", "Travel"}


async def test_build_feed_two_sections(tmp_path):
    settings = _settings(tmp_path, feed_size=10, broad_ratio=0.3)
    init_db(settings)
    set_topic(settings, "Science", True)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0, 0, 0])
        _add_candidate(conn, 1, "https://hn.example/a", "curated one", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://hn.example/b", "curated two", [0.9, 0.1, 0, 0])
        for i in range(4):
            _add_candidate(conn, 10 + i, f"https://sx.example/{i}", f"sx {i}", [0, 1.0, 0, 0])
            conn.execute(
                "UPDATE candidates SET source='searxng', topic='Science' WHERE id=?", (10 + i,)
            )
    wrote = await build_feed(settings, _FakeRerankLLM("garbage"))
    with connection(settings) as conn:
        curated = conn.execute(
            "SELECT candidate_id FROM feed_items WHERE section='curated' ORDER BY rank"
        ).fetchall()
        broad = conn.execute(
            "SELECT candidate_id, reason FROM feed_items WHERE section='broad' ORDER BY rank"
        ).fetchall()
    assert wrote == len(curated) + len(broad)
    assert len(broad) == 3  # round(10 * 0.3) slots, filled newest-first
    assert all(row["reason"] == "exploring: Science" for row in broad)
    assert {row[0] for row in curated} == {1, 2}  # searxng never curated
    assert {row[0] for row in broad} <= {10, 11, 12, 13}


async def test_broad_slots_skipped_without_material(tmp_path):
    settings = _settings(tmp_path, feed_size=10, broad_ratio=0.3)
    init_db(settings)
    set_topic(settings, "Science", True)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0, 0, 0])
        _add_candidate(conn, 1, "https://hn.example/a", "curated one", [1.0, 0, 0, 0])
        _add_candidate(conn, 10, "https://sx.example/0", "only sx item", [0, 1.0, 0, 0])
        conn.execute("UPDATE candidates SET source='searxng', topic='Science' WHERE id=10")
    await build_feed(settings, _FakeRerankLLM("garbage"))
    with connection(settings) as conn:
        broad_count = conn.execute(
            "SELECT COUNT(*) FROM feed_items WHERE section='broad'"
        ).fetchone()[0]
    assert broad_count == 1  # 3 slots, 1 candidate: short section, no filler


def test_arm_stats_one_reward_per_item_latest_interest_wins(tmp_path):
    from discover_app.pipeline.broad import arm_stats

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        for cid, topic in ((1, "Science"), (2, "Science"), (3, "Travel")):
            _add_candidate(conn, cid, f"https://sx.example/{cid}", f"sx {cid}", [0, 1.0, 0, 0])
            conn.execute("UPDATE candidates SET source='searxng', topic=? WHERE id=?", (topic, cid))
            conn.execute(
                "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
                "VALUES(0, 'broad', ?, 0.0, '', ?)",
                (cid, f"t{cid}"),
            )
        # candidate 1: saved AND two up-clicks — still exactly one reward
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(1, 'https://sx.example/1', 'saved', 'implicit')"
        )
        for _ in range(2):
            conn.execute(
                "INSERT INTO feedback(candidate_id, url, axis, value) "
                "VALUES(1, 'https://sx.example/1', 'interest', 'up')"
            )
        # candidate 2: up then down — only the latest opinion counts, no reward
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(2, 'https://sx.example/2', 'interest', 'up')"
        )
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(2, 'https://sx.example/2', 'interest', 'down')"
        )
        stats = arm_stats(conn)
    assert stats["Science"] == (2, 1)
    assert stats["Travel"] == (1, 0)


def test_pick_broad_items_excludes_served_and_saved(tmp_path):
    import random

    from discover_app.pipeline.broad import pick_broad_items

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        for cid in (1, 2, 3):
            _add_candidate(conn, cid, f"https://sx.example/{cid}", f"sx {cid}", [0, 1.0, 0, 0])
            conn.execute(
                "UPDATE candidates SET source='searxng', topic='Science' WHERE id=?", (cid,)
            )
        # 1 was served in a prior cycle (any section); 2 is an own bookmark
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'broad', 1, 0.0, '', 't1')"
        )
        conn.execute("INSERT INTO links(id, url) VALUES(1, 'https://sx.example/2')")
        rng = random.Random(1)  # noqa: S311 - deterministic test rng
        picks = pick_broad_items(conn, settings, ["Science"], slots=3, rng=rng)
    assert [p["id"] for p in picks] == [3]


async def test_broad_ratio_one_keeps_a_curated_slot(tmp_path):
    settings = _settings(tmp_path, feed_size=4, broad_ratio=1.0)
    init_db(settings)
    set_topic(settings, "Science", True)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0, 0, 0])
        _add_candidate(conn, 1, "https://hn.example/a", "curated one", [1.0, 0, 0, 0])
        for i in range(4):
            _add_candidate(conn, 10 + i, f"https://sx.example/{i}", f"sx {i}", [0, 1.0, 0, 0])
            conn.execute(
                "UPDATE candidates SET source='searxng', topic='Science' WHERE id=?", (10 + i,)
            )
    wrote = await build_feed(settings, _FakeRerankLLM("garbage"))
    with connection(settings) as conn:
        sections = dict(conn.execute("SELECT section, COUNT(*) FROM feed_items GROUP BY section"))
    assert sections["curated"] == 1  # the cap: curated never drops to zero slots
    assert sections["broad"] == 3  # feed_size - 1, not feed_size
    assert wrote == 4


def test_detect_saved_counts_broad_section(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://sx.example/served", "broad item", [0, 1.0, 0, 0])
        conn.execute("UPDATE candidates SET source='searxng', topic='Science' WHERE id=1")
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'broad', 1, 0.0, 'exploring: Science', 't1')"
        )
    # a broad-served item saved to Linkwarden is the bandit's reward signal
    assert detect_saved(settings, ["https://sx.example/served"]) == 1


def test_min_cosine_gate_keeps_offprofile_signals_out(tmp_path):
    settings = _settings(tmp_path, profile_clusters=1)
    init_db(settings)
    with connection(settings) as conn:
        blob = sqlite_vec.serialize_float32([1.0, 0, 0, 0])
        conn.execute(
            "INSERT INTO links(id, url, embedded, embedding) "
            "VALUES(1, 'https://a.example/1', 1, ?)",
            (blob,),
        )
        # off-profile broad feedback: orthogonal to the only interest cluster
        _add_candidate(conn, 2, "https://sx.example/x", "broad save", [0, 0, 1.0, 0])
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding) "
            "SELECT id, url, 'saved', 'implicit', embedding FROM candidates WHERE id = 2"
        )
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    assert weight == 1.0  # gated: the off-profile save never touched the cluster


def test_intra_list_diversity_bounds():
    from discover_app.pipeline.rank import _intra_list_diversity

    a = np.array([1.0, 0, 0, 0], dtype=np.float32)
    b = np.array([0, 1.0, 0, 0], dtype=np.float32)
    assert _intra_list_diversity([a, a]) == pytest.approx(0.0)  # identical: no diversity
    assert _intra_list_diversity([a, b]) == pytest.approx(1.0)  # orthogonal: max diversity
    assert _intra_list_diversity([a]) == 0.0  # degenerate list


def test_norm_url_identity_variants():
    canonical = norm_url("https://example.com/post/1")
    # scheme, www, trailing slash, fragment, and tracking params are identity-neutral
    assert norm_url("http://example.com/post/1") == canonical
    assert norm_url("https://www.example.com/post/1/") == canonical
    assert norm_url("https://example.com/post/1#section") == canonical
    assert norm_url("https://example.com/post/1?utm_source=rss&utm_medium=feed") == canonical
    assert norm_url("https://example.com/post/1?fbclid=abc123") == canonical
    # real query params still distinguish pages
    assert norm_url("https://example.com/post/1?page=2") != canonical
    assert norm_url("https://example.com/post/1?page=2&utm_source=x") == norm_url(
        "https://example.com/post/1?page=2"
    )


def test_mmr_prefers_relevance_then_diversity():
    a = np.array([1.0, 0.0], dtype=np.float32)
    a_dup = np.array([0.99, 0.01], dtype=np.float32)  # near-duplicate of a
    b = np.array([0.0, 1.0], dtype=np.float32)  # orthogonal, less relevant
    order = mmr_select([0.9, 0.85, 0.6], [a, a_dup, b], k=2, lam=0.5)
    assert order[0] == 0  # most relevant first
    assert order[1] == 2  # diversify away from the near-duplicate


def test_is_ad_matches_default_patterns():
    patterns = Settings(_env_file=None).ad_filter_patterns
    assert is_ad("Anzeige: DJI-Gimbal zum Bestpreis bei Amazon", patterns)
    assert is_ad("Neues Notebook im Test [Advertorial]", patterns)
    assert is_ad("Sponsored: The future of observability", patterns)
    assert not is_ad("Die Anzeigetafel der Bundesliga wird digital", patterns)
    assert not is_ad(None, patterns)
    assert not is_ad("Plain tech news", [])


async def test_build_feed_llm_rerank_and_exclusions(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0.0, 0.0, 0.0])
        _add_candidate(conn, 1, "https://new.example/a", "Fresh relevant piece", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://new.example/b", "Anzeige: some deal", [0.9, 0.1, 0, 0])
        _add_candidate(conn, 3, "https://saved.example/x", "Already bookmarked", [0.95, 0, 0.1, 0])
        # the user already saved candidate 3's URL (trailing slash must not matter)
        conn.execute(
            "INSERT INTO links(id, url, embedded) VALUES(1, 'https://saved.example/x/', 1)"
        )
    # items reach the LLM ordered by similarity: candidate 1 is index 0, the ad index 1
    llm = _FakeRerankLLM('[{"i": 0, "score": 0.9, "why": "matches"}, {"i": 1, "score": 0}]')
    wrote = await build_feed(settings, llm)
    assert wrote == 1  # ad zeroed out, bookmarked URL excluded
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT candidate_id, reason FROM feed_items WHERE section = 'curated'"
        ).fetchall()
    assert [(r["candidate_id"], r["reason"]) for r in rows] == [(1, "matches")]


async def test_build_feed_falls_back_to_similarity_on_bad_llm(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0.0, 0.0, 0.0])
        _add_candidate(conn, 1, "https://ex.com/a", "closest", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/b", "farther", [0.5, 0.5, 0, 0])
    wrote = await build_feed(settings, _FakeRerankLLM("I refuse to emit JSON"))
    # fallback ranks by similarity; the least-similar item must survive
    # normalization (the 0-score drop is reserved for LLM-zeroed ads)
    assert wrote == 2
    with connection(settings) as conn:
        top = conn.execute(
            "SELECT candidate_id FROM feed_items WHERE section = 'curated' ORDER BY rank"
        ).fetchone()
    assert top["candidate_id"] == 1


async def test_build_feed_excludes_previously_served(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0.0, 0.0, 0.0])
        _add_candidate(conn, 1, "https://ex.com/a", "day one item", [1.0, 0, 0, 0])
    assert await build_feed(settings, _FakeRerankLLM("garbage")) == 1
    with connection(settings) as conn:
        _add_candidate(conn, 2, "https://ex.com/b", "day two item", [0.9, 0.1, 0, 0])
    assert await build_feed(settings, _FakeRerankLLM("garbage")) == 1
    with connection(settings) as conn:
        latest = conn.execute(
            "SELECT candidate_id FROM feed_items WHERE cycle_ts = "
            "(SELECT value FROM meta WHERE key = 'last_cycle_ts')"
        ).fetchall()
    assert [r["candidate_id"] for r in latest] == [2]  # day-one item not repeated


async def test_gather_candidates_survives_failing_source(tmp_path, monkeypatch):
    settings = _settings(tmp_path, miniflux_token="mf")  # noqa: S106 - dummy
    init_db(settings)

    class _DeadSource:
        def __init__(self, s) -> None:
            pass

        async def fetch(self):
            raise RuntimeError("boom")

        async def aclose(self) -> None:
            pass

    class _GoodSource(_DeadSource):
        async def fetch(self):
            return [
                {"source": "miniflux", "url": "https://ok.example/1", "title": "fine"},
                {"source": "miniflux", "url": "https://ok.example/2", "title": "Anzeige: deal"},
            ]

    async def _noop_embed(settings, llm):
        return 0

    monkeypatch.setattr(candidates_mod, "HackerNewsClient", _DeadSource)
    monkeypatch.setattr(candidates_mod, "MinifluxClient", _GoodSource)
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)
    new = await gather_candidates(settings, _FakeRerankLLM(""))
    assert new == 1  # good source ingested minus the ad; dead source tolerated
    with connection(settings) as conn:
        urls = [r[0] for r in conn.execute("SELECT url FROM candidates")]
    assert urls == ["https://ok.example/1"]


def test_topics_seed_select_and_unknown(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    topics = list_topics(settings)
    assert len(topics) == len(IAB_TIER1)
    assert all(not t["selected"] for t in topics)  # nothing selected by default
    set_topic(settings, "Science", True)
    set_topic(settings, "Travel", True)
    assert selected_topics(settings) == ["Science", "Travel"]
    set_topic(settings, "Science", False)
    assert selected_topics(settings) == ["Travel"]
    with pytest.raises(LookupError):
        set_topic(settings, "Astrology", True)  # not in the taxonomy
    init_db(settings)  # re-seed is idempotent and keeps selections
    assert selected_topics(settings) == ["Travel"]


async def test_gather_searxng_gated_on_selected_topics(tmp_path, monkeypatch):
    settings = _settings(tmp_path, searxng_url="http://searxng:8080")
    init_db(settings)

    class _Quiet:
        def __init__(self, s) -> None:
            pass

        async def fetch(self):
            return []

        async def aclose(self) -> None:
            pass

    class _FakeSearxng(_Quiet):
        calls: list[list[str]] = []

        async def fetch(self, topics):
            _FakeSearxng.calls.append(topics)
            return [
                {
                    "source": "searxng",
                    "url": f"https://sx.example/{t}",
                    "title": f"about {t}",
                    "topic": t,
                }
                for t in topics
            ]

    async def _noop_embed(settings, llm):
        return 0

    monkeypatch.setattr(candidates_mod, "HackerNewsClient", _Quiet)
    monkeypatch.setattr(candidates_mod, "MinifluxClient", _Quiet)
    monkeypatch.setattr(candidates_mod, "SearxngClient", _FakeSearxng)
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)

    # no topics selected -> searxng never queried
    assert await gather_candidates(settings, _FakeRerankLLM("")) == 0
    assert _FakeSearxng.calls == []

    set_topic(settings, "Science", True)
    assert await gather_candidates(settings, _FakeRerankLLM("")) == 1
    assert _FakeSearxng.calls == [["Science"]]
    with connection(settings) as conn:
        row = conn.execute("SELECT source, topic FROM candidates").fetchone()
    assert (row["source"], row["topic"]) == ("searxng", "Science")


def test_topic_endpoints_and_ui_picker(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())

    listing = client.get("/topics").json()
    assert len(listing) == len(IAB_TIER1) and not any(t["selected"] for t in listing)
    assert client.post("/topics/Science", json={"selected": True}).json()["selected"] is True
    assert client.post("/topics/Astrology", json={"selected": True}).status_code == 404
    page = client.get("/ui").text
    assert "Exploring topics (1 selected)" in page
    assert 'data-name="Science" checked' in page


async def test_searxng_candidates_never_enter_curated_feed(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    set_topic(settings, "Science", True)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0, 0, 0])
        _add_candidate(conn, 1, "https://hn.example/a", "profile match", [1.0, 0, 0, 0])
        # a searxng candidate PERFECTLY aligned with the profile — the
        # strongest possible curated-leak bait
        _add_candidate(conn, 2, "https://sx.example/a", "searxng item", [1.0, 0, 0, 0])
        conn.execute("UPDATE candidates SET source = 'searxng', topic = 'Science' WHERE id = 2")
    assert await build_feed(settings, _FakeRerankLLM("garbage")) >= 1
    with connection(settings) as conn:
        curated = [
            row[0]
            for row in conn.execute("SELECT candidate_id FROM feed_items WHERE section = 'curated'")
        ]
    assert 1 in curated and 2 not in curated  # sources are per-section


async def test_searxng_client_per_topic_fault_tolerance(tmp_path):
    import httpx

    from discover_app.clients.searxng import SearxngClient

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        assert query["categories"] == "news" and query["format"] == "json"
        if query["q"] == "Science":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://sx.example/1", "title": "ok", "content": "c" * 900},
                        {"title": "no url — skipped"},
                    ]
                },
            )
        if query["q"] == "Travel":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(500)

    client = SearxngClient(_settings(tmp_path))
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://searxng.test"
    )
    items = await client.fetch(["Science", "Travel", "Sports"])
    await client.aclose()
    # one good topic survives two broken ones; url-less rows skipped
    assert [i["url"] for i in items] == ["https://sx.example/1"]
    assert items[0]["topic"] == "Science" and len(items[0]["snippet"]) == 500


def test_topic_name_with_ampersand_roundtrip(tmp_path, monkeypatch):
    from urllib.parse import quote

    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())
    name = "Law, Government & Politics"
    resp = client.post(f"/topics/{quote(name, safe='')}", json={"selected": True})
    assert resp.status_code == 200 and resp.json()["name"] == name
    assert selected_topics(settings) == [name]


def test_candidates_topic_column_migration(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute(
            "ALTER TABLE candidates DROP COLUMN topic"
        )  # simulate a DB from before the topic column
    init_db(settings)  # migration re-adds it
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
        assert conn.execute("SELECT topic FROM candidates").fetchone()[0] is None


async def test_ingest_skips_without_token(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, embed_dim=4, linkwarden_token="")
    init_db(settings)
    assert await ingest.ingest_links(settings) == 0
    with connection(settings) as conn:
        # a token-less poll must not mark the backfill as done
        assert get_meta(conn, "linkwarden_backfill_done") is None


def test_render_markdown_empty(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    assert "No items yet" in render_markdown(settings)


def test_render_markdown_with_item(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute(
            "INSERT INTO candidates(id, source, url, title, snippet) "
            "VALUES(1, 'hackernews', 'https://example.com', 'Hello World', 'snip')"
        )
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, 'relevant because', '2026-06-20T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_cycle_ts', '2026-06-20T00:00:00+00:00')"
        )
    out = render_markdown(settings)
    assert "Hello World" in out
    assert "https://example.com" in out
    assert "relevant because" in out


class _FakeEmbedLLM:
    """LLM double that returns deterministic vectors or raises."""

    def __init__(self, dim: int = 4, fail_batch: int | None = None) -> None:
        self.dim = dim
        self.fail_batch = fail_batch
        self.call_count = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_count += 1
        if self.fail_batch and self.call_count == self.fail_batch:
            raise RuntimeError("embed failed")
        return [[1.0 if i == j else 0.0 for j in range(self.dim)] for i, _ in enumerate(texts)]

    async def aclose(self) -> None:
        pass


def test_settings_rejects_batch_budget_below_document_cap(tmp_path) -> None:
    with pytest.raises(ValueError, match="embed_max_batch_chars"):
        _settings(tmp_path, embed_max_chars=101, embed_max_batch_chars=100)


async def test_embed_pending_truncates_oversized_documents(tmp_path) -> None:
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=65536)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute("INSERT INTO links(id, url, embedded) VALUES(1, 'http://ok.example/', 0)")
        conn.execute(
            "INSERT INTO links(id, url, text_content, embedded) "
            "VALUES(2, 'http://big.example/', ?, 0)",
            ("x" * 5000,),
        )
        conn.execute("INSERT INTO links(id, url, embedded) VALUES(3, 'http://ok2.example/', 0)")
    llm = _FakeEmbedLLM(4)
    done = await _embed_pending("links", "vec_links", "COALESCE(text_content,'')", settings, llm)
    assert done == 3
    with connection(settings) as conn:
        embedded = conn.execute("SELECT COUNT(*) FROM links WHERE embedded = 1").fetchone()[0]
    assert embedded == 3  # oversized doc was truncated and embedded successfully


async def test_embed_pending_uses_char_budget_for_batch_size(tmp_path) -> None:
    # Use a text_expr that produces 20-char text per row.
    # With batch_budget=35, only 1 row per batch (20+20=40 > 35).
    # 6 rows -> 6 batches.
    settings = _settings(tmp_path, embed_max_chars=35, embed_max_batch_chars=35)
    init_db(settings)
    with connection(settings) as conn:
        for i in range(6):
            conn.execute(
                "INSERT INTO links(id, url, text_content, embedded) VALUES(?, ?, ?, 0)",
                (i, f"http://e.example/{i}", f"text-{i}" * 3),
            )
    llm = _FakeEmbedLLM(4)
    await _embed_pending(
        "links",
        "vec_links",
        "COALESCE(text_content,'')",
        settings,
        llm,
    )
    assert llm.call_count == 6  # 18 chars each, budget 35 => 1 per batch


async def test_embed_pending_continues_after_batch_failure(tmp_path) -> None:
    # Use a text_expr that produces 20-char text per row.
    # With batch_budget=45, only 2 rows per batch (40 chars).
    # 6 rows -> 3 batches (2+2+2); fail_batch=2 skips rows 2-3.
    settings = _settings(tmp_path, embed_max_chars=45, embed_max_batch_chars=45)
    init_db(settings)
    with connection(settings) as conn:
        for i in range(6):
            conn.execute(
                "INSERT INTO links(id, url, text_content, embedded) VALUES(?, ?, ?, 0)",
                (i, f"http://e.example/{i}", f"text-{i}" * 3),
            )
    llm = _FakeEmbedLLM(4, fail_batch=2)
    done = await _embed_pending(
        "links",
        "vec_links",
        "COALESCE(text_content,'')",
        settings,
        llm,
    )
    assert done == 4  # batch 0 (rows 0-1) + batch 2 (rows 4-5) = 4 rows
    with connection(settings) as conn:
        embedded = conn.execute("SELECT COUNT(*) FROM links WHERE embedded = 1").fetchone()[0]
    assert embedded == 4


async def test_embed_pending_full_backfill_with_oversized_mixed(tmp_path) -> None:
    """Regression test: a full Linkwarden backfill with mixed normal + oversized docs."""
    settings = _settings(
        tmp_path,
        embed_max_chars=1024,
        embed_max_batch_chars=8192,
    )
    init_db(settings)
    with connection(settings) as conn:
        for i in range(666):
            text = ("x" * 20000) if i % 100 == 0 else f"article {i}"
            conn.execute(
                "INSERT INTO links(id, url, text_content, embedded) VALUES(?, ?, ?, 0)",
                (i, f"http://e.example/{i}", text),
            )
    llm = _FakeEmbedLLM(4)
    done = await _embed_pending("links", "vec_links", "COALESCE(text_content,'')", settings, llm)
    assert done == 666  # all 666 embedded despite some 20K-char docs


async def test_embed_pending_batch_budget_below_doc_length(tmp_path) -> None:
    """Regression: batch budget smaller than a single doc must not infinite-loop.

    ``embed_max_batch_chars``=100 with a 500-char row should still succeed
    because the per-doc cap truncated it to 20 chars at this point.
    """
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute(
            "INSERT INTO links(id, url, text_content, embedded) "
            "VALUES(1, 'http://e.example/', ?, 0)",
            ("x" * 5000,),
        )
        conn.execute(
            "INSERT INTO links(id, url, text_content, embedded) "
            "VALUES(2, 'http://e2.example/', 'short', 0)",
        )
    llm = _FakeEmbedLLM(4)
    done = await _embed_pending("links", "vec_links", "COALESCE(text_content,'')", settings, llm)
    assert done == 2


# ── run-cycle status tests ──────────────────────────────────────────────────


class _QuietSource:
    async def fetch(self):
        return []

    async def aclose(self) -> None:
        pass


async def _noop_embed(*a, **k):
    return 0


async def test_run_cycle_succeeds_with_clean_stages(tmp_path, monkeypatch):
    """All stages pass → status "cycle complete", empty errors."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: _FakeLinkwarden({}))
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())
    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)
    monkeypatch.setattr(candidates_mod, "HackerNewsClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "MinifluxClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)
    result = await run_cycle(settings)
    assert result["status"] == "cycle complete"
    assert result["errors"] == []


async def test_run_cycle_reports_ingest_failure(tmp_path, monkeypatch):
    """Ingest raises → status "cycle degraded" with errors=["ingest"]."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)

    async def _fail_ingest(*a, **k):
        raise RuntimeError("linkwarden unreachable")

    monkeypatch.setattr("discover_app.pipeline.cycle.ingest_links", _fail_ingest)
    monkeypatch.setattr(candidates_mod, "HackerNewsClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "MinifluxClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)
    result = await run_cycle(settings)
    assert result["status"] == "cycle degraded"
    assert result["errors"] == ["ingest"]


async def test_run_cycle_reports_gather_candidates_failure(tmp_path, monkeypatch):
    """gather_candidates raises → status "cycle degraded" with errors=["gather_candidates"]."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: _FakeLinkwarden({}))
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())
    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)

    async def _fail_gather(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr("discover_app.pipeline.cycle.gather_candidates", _fail_gather)
    result = await run_cycle(settings)
    assert result["status"] == "cycle degraded"
    assert result["errors"] == ["gather_candidates"]


async def test_run_cycle_reports_multiple_failures(tmp_path, monkeypatch):
    """Both ingest and gather fail → errors list contains both."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)

    async def _fail_ingest(*a, **k):
        raise RuntimeError("ingest boom")

    async def _fail_candidates(*a, **k):
        raise RuntimeError("gather boom")

    monkeypatch.setattr("discover_app.pipeline.cycle.ingest_links", _fail_ingest)
    monkeypatch.setattr("discover_app.pipeline.cycle.gather_candidates", _fail_candidates)
    result = await run_cycle(settings)
    assert result["status"] == "cycle degraded"
    assert set(result["errors"]) == {"ingest", "gather_candidates"}


async def test_run_cycle_reports_profile_rebuild_failure(tmp_path, monkeypatch):
    """rebuild_profile raises → status "cycle degraded" with errors=["rebuild_profile"]."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: _FakeLinkwarden({}))
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())
    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)
    monkeypatch.setattr(candidates_mod, "HackerNewsClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "MinifluxClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)

    def _bad_rebuild(s):
        raise RuntimeError("k-means failed")

    monkeypatch.setattr("discover_app.pipeline.cycle.rebuild_profile", _bad_rebuild)
    result = await run_cycle(settings)
    assert result["status"] == "cycle degraded"
    assert result["errors"] == ["rebuild_profile"]


async def test_run_cycle_reports_build_feed_failure(tmp_path, monkeypatch):
    """build_feed raises → status "cycle degraded" with errors=["build_feed"]."""
    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: _FakeLinkwarden({}))
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())
    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)
    monkeypatch.setattr(candidates_mod, "HackerNewsClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "MinifluxClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)

    async def _fail_build_feed(*a, **k):
        raise RuntimeError("rerank failed")

    monkeypatch.setattr("discover_app.pipeline.cycle.build_feed", _fail_build_feed)
    result = await run_cycle(settings)
    assert result["status"] == "cycle degraded"
    assert result["errors"] == ["build_feed"]


async def test_run_cycle_via_http_endpoint(tmp_path, monkeypatch):
    """POST /admin/run-cycle returns the proper response model."""
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path, embed_max_chars=20, embed_max_batch_chars=100)
    init_db(settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.pipeline.cycle.get_settings", lambda: settings)
    monkeypatch.setattr(ingest, "LinkwardenClient", lambda s: _FakeLinkwarden({}))
    monkeypatch.setattr(ingest, "LLMClient", lambda s: _FakeLLM())
    monkeypatch.setattr(ingest, "embed_pending_links", _noop_embed)
    monkeypatch.setattr(candidates_mod, "HackerNewsClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "MinifluxClient", lambda s: _QuietSource())
    monkeypatch.setattr(candidates_mod, "embed_pending_candidates", _noop_embed)

    client = TestClient(app_mod.create_app())
    resp = client.post("/admin/run-cycle")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("cycle complete", "cycle degraded")
    assert isinstance(body["errors"], list)


# ── Rating tests ────────────────────────────────────────────────────────────


def test_record_rating_valid_values(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    # First rating is "changed"
    value, changed = record_rating(settings, 1, 0.5)
    assert value == 0.5 and changed is True
    with connection(settings) as conn:
        row = conn.execute("SELECT value FROM ratings").fetchone()
    assert row["value"] == 0.5

    # Same value again is idempotent ("existing")
    value, changed = record_rating(settings, 1, 0.5)
    assert value == 0.5 and changed is False

    # Different value is a new row (idempotent on URL+value, not on candidate_id)
    value, changed = record_rating(settings, 1, 1.0)
    assert value == 1.0 and changed is True
    with connection(settings) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ratings WHERE candidate_id = 1").fetchone()[0]
    assert count == 2  # two distinct values


def test_record_rating_rejects_invalid_values(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    for bad_val in [0.3, 0.7, -0.1, 1.1, -1.5]:
        with pytest.raises(ValueError):
            record_rating(settings, 1, bad_val)


def test_record_rating_unknown_candidate(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with pytest.raises(LookupError):
        record_rating(settings, 999, 0.5)


async def test_rating_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())

    # Valid rating
    resp = client.post("/feed/1/rating", json={"value": "0.5"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "recorded"
    assert body["value"] == 0.5 and body["changed"] is True

    # Same rating again → existing
    resp = client.post("/feed/1/rating", json={"value": "0.5"})
    assert resp.json()["status"] == "existing" and resp.json()["changed"] is False

    # Different value → recorded
    resp = client.post("/feed/1/rating", json={"value": "1.0"})
    assert resp.json()["changed"] is True

    # Invalid value → 422
    assert client.post("/feed/1/rating", json={"value": "0.3"}).status_code == 422

    # Unknown candidate → 404
    assert client.post("/feed/999/rating", json={"value": "0.5"}).status_code == 404


def test_rating_in_profile_recompute(tmp_path):
    settings = _settings(tmp_path, profile_clusters=1)
    init_db(settings)
    with connection(settings) as conn:
        _add_link_vec(conn, 1, "https://a.example/1", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://b.example/1", "rated item", [0.9, 0.1, 0, 0])
    record_rating(settings, 2, 1.0)  # strong positive
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    # base 1.0 + rating 2.0 (1.0 maps to base 2.0) = 3.0
    assert abs(weight - 3.0) < 0.01


def test_negative_rating_downweights_centroid(tmp_path):
    settings = _settings(tmp_path, profile_clusters=1)
    init_db(settings)
    with connection(settings) as conn:
        _add_link_vec(conn, 1, "https://a.example/1", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://b.example/1", "neg rated", [0.9, 0.1, 0, 0])
    record_rating(settings, 2, -1.0)  # strongly negative → base -2.0
    rebuild_profile(settings)
    with connection(settings) as conn:
        weight = conn.execute("SELECT weight FROM profile").fetchone()[0]
    # base 1.0 + rating -2.0 = -1.0 → floored to 0.25
    assert weight == 0.25


def test_healthz_reports_ratings_count(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "an item", [1.0, 0, 0, 0])
    record_rating(settings, 1, 0.5)
    record_rating(settings, 1, 1.0)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())
    body = client.get("/healthz").json()
    assert body["ratings_count"] == 2


def test_ui_cards_image_summary_and_restored_state(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "Saved and upvoted", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/b", "Downvoted", [0, 1.0, 0, 0])
        _add_candidate(conn, 3, "https://ex.com/c", "Hostile image", [0, 0, 1.0, 0])
        conn.execute(
            "UPDATE candidates SET image_url = 'https://img.ex.com/a.jpg', "
            "snippet = '<![CDATA[<p>Short</p>]]>', description = 'A page description "
            "that is long enough to beat the thin feed snippet on the card.' WHERE id = 1"
        )
        conn.execute("UPDATE candidates SET image_url = 'javascript:alert(1)' WHERE id = 3")
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, 'because & why', 't1'), "
            "(1, 'curated', 2, 0.5, '', 't1'), (2, 'curated', 3, 0.4, '', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(1, 'ex.com/a', 'saved', 'true')"
        )
    record_feedback(settings, 1, "interest", "down")
    record_feedback(settings, 1, "interest", "up")  # latest vote wins
    record_feedback(settings, 2, "interest", "down")
    record_rating(settings, 1, 0.5)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())

    page = client.get("/ui").text
    # only save / up / down remain
    assert "rating" not in page and "mood" not in page and "score" not in page
    assert 'src="https://img.ex.com/a.jpg"' in page and 'referrerpolicy="no-referrer"' in page
    assert "javascript:" not in page  # non-http image URLs never render
    assert "A page description that is long enough" in page  # thin snippet replaced
    assert "because &amp; why" in page
    card1 = page[page.index('id="c1"') : page.index('id="c2"')]
    assert "Saved to Linkwarden" in card1 and 'class="vote on"' in card1
    card2 = page[page.index('id="c2"') : page.index('id="c3"')]
    assert "data-down" in card2  # downvoted card stays collapsed after reload

    item = client.get("/feed").json()["items"][0]
    assert item["image_url"] == "https://img.ex.com/a.jpg"
    assert item["summary"].startswith("A page description")
    assert item["snippet"] == "Short"  # CDATA/markup stripped from the API too


# ── feed sync ───────────────────────────────────────────────────


def _add_links(settings, urls: list[str]) -> None:
    with connection(settings) as conn:
        for i, url in enumerate(urls, start=1):
            conn.execute("INSERT INTO links(id, url) VALUES(?, ?)", (i, url))


def _status_error(status: int, text: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://miniflux.test/")
    return httpx.HTTPStatusError(
        text, request=request, response=httpx.Response(status, text=text, request=request)
    )


class _FakeMiniflux:
    """Miniflux double: per-site discovery results and a create_feed log."""

    def __init__(self, discovered: dict, create_errors: dict | None = None) -> None:
        self.discovered = discovered
        self.create_errors = create_errors or {}
        self.created: list[tuple[str, int]] = []

    async def first_category_id(self) -> int:
        return 7

    async def discover(self, url: str):
        result = self.discovered[url]
        if isinstance(result, Exception):
            raise result
        return result

    async def create_feed(self, feed_url: str, category_id: int) -> int:
        if feed_url in self.create_errors:
            raise self.create_errors[feed_url]
        self.created.append((feed_url, category_id))
        return len(self.created)

    async def aclose(self) -> None:
        pass


def test_pending_domains_threshold_exclusion_and_tried(tmp_path):
    settings = _settings(tmp_path, feed_sync_exclude=["github.com"])
    init_db(settings)
    _add_links(
        settings,
        [
            "https://www.golem.de/a",
            "https://golem.de/b",
            "https://GOLEM.de/c",
            "https://heise.de/a",
            "https://heise.de/b",
            "https://github.com/x",
            "https://github.com/y",
            "https://once.example/a",
            "https://tried.example/a",
            "https://tried.example/b",
        ],
    )
    with connection(settings) as conn:
        conn.execute("INSERT INTO feed_domains(domain, status) VALUES('tried.example', 'no_feed')")
    # www./case-insensitive merge, ranked by count, excluded/single/tried skipped
    assert pending_domains(settings) == ["golem.de", "heise.de"]
    assert pending_domains(_settings(tmp_path, feed_sync_per_cycle=1)) == ["golem.de"]


async def test_sync_feeds_records_each_outcome(tmp_path, monkeypatch):
    settings = _settings(tmp_path, miniflux_token="mf-token")  # noqa: S106 - dummy
    init_db(settings)
    _add_links(settings, [f"https://{d}/{i}" for d in "abcde" for i in range(2)])
    fake = _FakeMiniflux(
        {
            "https://a": [{"url": "https://a/feed", "title": "A", "type": "rss"}],
            "https://b": [],
            "https://c": _status_error(500, "site unreachable"),
            "https://d": [{"url": "https://d/feed", "title": "D", "type": "atom"}],
            "https://e": [{"url": "https://e/feed", "title": "E", "type": "rss"}],
        },
        create_errors={
            "https://d/feed": _status_error(400, '{"error_message":"This feed already exists."}'),
            "https://e/feed": _status_error(500, "unparsable feed"),
        },
    )
    monkeypatch.setattr(feeds_mod, "MinifluxClient", lambda s: fake)
    assert await sync_feeds(settings) == 2
    assert fake.created == [("https://a/feed", 7)]
    with connection(settings) as conn:
        rows = dict(conn.execute("SELECT domain, status FROM feed_domains").fetchall())
    assert rows == {
        "a": "subscribed",
        "b": "no_feed",
        "c": "no_feed",
        "d": "subscribed",
        "e": "failed",
    }
    # every domain is remembered, so the next cycle does no discovery at all
    assert pending_domains(settings) == []


async def test_sync_feeds_transport_error_records_nothing(tmp_path, monkeypatch):
    settings = _settings(tmp_path, miniflux_token="mf-token")  # noqa: S106 - dummy
    init_db(settings)
    _add_links(settings, ["https://a/1", "https://a/2"])
    fake = _FakeMiniflux({"https://a": httpx.ConnectError("miniflux down")})
    monkeypatch.setattr(feeds_mod, "MinifluxClient", lambda s: fake)
    with pytest.raises(httpx.ConnectError):
        await sync_feeds(settings)
    assert pending_domains(settings) == ["a"]  # retried next cycle


async def test_sync_feeds_timeout_is_recorded_not_fatal(tmp_path, monkeypatch):
    """A slow site must not abort the sync and head the list every cycle."""
    settings = _settings(tmp_path, miniflux_token="mf-token")  # noqa: S106 - dummy
    init_db(settings)
    _add_links(
        settings, ["https://a/1", "https://a/2", "https://a/3", "https://b/1", "https://b/2"]
    )
    fake = _FakeMiniflux(
        {
            "https://a": httpx.ReadTimeout("slow site"),
            "https://b": [{"url": "https://b/feed", "title": "B", "type": "rss"}],
        }
    )
    monkeypatch.setattr(feeds_mod, "MinifluxClient", lambda s: fake)
    assert await sync_feeds(settings) == 1
    with connection(settings) as conn:
        rows = dict(conn.execute("SELECT domain, status FROM feed_domains").fetchall())
    assert rows == {"a": "failed", "b": "subscribed"}


async def test_sync_feeds_uses_builtin_reader_without_miniflux(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    _add_links(settings, ["https://a/1", "https://a/2", "https://b/1", "https://b/2"])

    def _boom(s):
        raise AssertionError("no Miniflux client without a token")

    async def _discover(client, site_url):
        return "https://a/feed.xml" if site_url == "https://a" else None

    monkeypatch.setattr(feeds_mod, "MinifluxClient", _boom)
    monkeypatch.setattr(feeds_mod, "discover_feed", _discover)
    assert await sync_feeds(settings) == 1
    with connection(settings) as conn:
        feeds = [tuple(r) for r in conn.execute("SELECT url, site FROM feeds")]
        status = dict(conn.execute("SELECT domain, status FROM feed_domains").fetchall())
    assert feeds == [("https://a/feed.xml", "a")]
    assert status == {"a": "subscribed", "b": "no_feed"}


# ── card enrichment ─────────────────────────────────────────────


def test_strip_html_and_first_image():
    raw = (
        '<![CDATA[<p> <a href="https://t.de/x"><img src="https://img.t.de/1.jpg" alt="">'
        "</a> Der Text &amp; mehr\u200b</p>]]>"
    )
    assert strip_html(raw) == "Der Text & mehr"
    assert first_image(raw) == "https://img.t.de/1.jpg"
    assert first_image('<img src="/relative.jpg">') is None
    assert strip_html(None) == "" and first_image(None) is None


def test_strip_html_entity_escaped_feed_content():
    """Some feeds deliver their content HTML-escaped a second time."""
    raw = (
        "&lt;![CDATA[&lt;p&gt; &lt;a href=&#34;https://www.tagesschau.de/x.html&#34;&gt;"
        "&lt;img src=&#34;https://images.tagesschau.de/a.jpg?width=1920&#34; alt=&#34;T&#34;"
        "&gt;&lt;/a&gt; Bedroht KI die Menschheit? &amp;quot;Ja&amp;quot;&lt;/p&gt;]]&gt;"
    )
    assert strip_html(raw) == 'Bedroht KI die Menschheit? "Ja"'
    assert first_image(raw) == "https://images.tagesschau.de/a.jpg?width=1920"
    # a 500-char cut inside a tag leaves no half tag behind
    assert strip_html('Text davor <img src="https://ex.com/a.jpg" al') == "Text davor"
    # stored rows from before the fix: the markup-only snippet yields to the description
    assert card_summary(raw[:300], "Was hinter den Warnungen steckt") == (
        "Was hinter den Warnungen steckt"
    )


def test_page_meta_and_card_summary():
    page = (
        "<head><meta content='/img/og.jpg' property='og:image'>"
        '<meta name="twitter:image" content="https://cdn.ex/tw.jpg">'
        '<meta name="description" content="Plain &amp; simple"></head>'
    )
    assert page_meta(page, "https://ex.com/news/1") == (
        "https://ex.com/img/og.jpg",  # relative og:image resolved against the page
        "Plain & simple",
    )
    assert page_meta("<head></head>", "https://ex.com/") == (None, None)
    assert card_summary("", None) == ""


def test_card_summary_whole_sentences():
    """1-3 complete sentences, never cut mid-word."""
    three = (
        "Der erste Satz ist kurz. Der zweite Satz bringt den Text über zwei Zeilen hinaus. "
        "Ein dritter Satz wird nicht mehr gebraucht."
    )
    assert card_summary(three, None) == (
        "Der erste Satz ist kurz. Der zweite Satz bringt den Text über zwei Zeilen hinaus."
    )
    # a source cut mid-sentence loses the unfinished tail ...
    cut = "Die KI schreibt mit. Die Möglichkeiten künstlicher Intelligenz sind für Sc"
    # ... and a description with a fuller complete sentence wins over the stub
    desc = "In Singapur sollen Schüler lernen, KI zu nutzen, ohne ihr das Denken zu überlassen."
    assert card_summary(cut, desc) == desc
    assert card_summary(cut, None) == "Die KI schreibt mit."
    # a period inside closing quotes still ends the sentence
    assert (
        card_summary(
            "Messages containing “Microslop.” were blocked. People then started testing…", None
        )
        == "Messages containing “Microslop.” were blocked."
    )
    assert card_summary(
        "Sie sagte „Nein.“ Danach war Ruhe im Saal und im ganzen Haus der …", None
    ) == ("Sie sagte „Nein.“")
    # search-engine snippets mark their cut with "..."
    assert card_summary("Radio waves were found. It reveals its ...", None) == (
        "Radio waves were found."
    )
    # abbreviations followed by lowercase do not split a sentence
    assert card_summary("Das gilt z.B. für Schulen und Hochschulen im ganzen Land.", None) == (
        "Das gilt z.B. für Schulen und Hochschulen im ganzen Land."
    )
    # a publisher-cut description loses its cut sentence too
    assert card_summary(None, "Er kam. Und dann sah er die …") == "Er kam."
    # only a cut sentence exists: shown once, ending in a single ellipsis
    assert card_summary("GM and Ford lost share, according to a ...", None) == (
        "GM and Ford lost share, according to a…"
    )
    # no sentence end anywhere: cut at a word boundary with an ellipsis
    words = "wort " * 80
    out = card_summary(words, None)
    assert out.endswith("…") and len(out) <= 241 and " wor…" not in out


async def test_fetch_meta_reads_head_and_tolerates_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ok":
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text='<head><meta property="og:image" content="https://i.ex/a.jpg">'
                '<meta property="og:description" content="Desc"></head><body>x</body>',
            )
        if request.url.path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, text="%PDF")
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await fetch_meta(client, "https://ex.com/ok") == ("https://i.ex/a.jpg", "Desc")
        assert await fetch_meta(client, "https://ex.com/pdf") == (None, None)
        assert await fetch_meta(client, "https://ex.com/blocked") == (None, None)
        assert await fetch_meta(client, "javascript:alert(1)") == (None, None)


async def test_enrich_feed_marks_items_and_keeps_source_image(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/page", "has og", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/none", "no og", [0, 1.0, 0, 0])
        _add_candidate(conn, 3, "https://ex.com/old", "not served now", [0, 0, 1.0, 0])
        conn.execute("UPDATE candidates SET image_url = 'https://feed.ex/img.jpg' WHERE id = 2")
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, '', 't2'), (1, 'broad', 2, 0.5, '', 't2'), "
            "(0, 'curated', 3, 0.9, '', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't2')")
    fetched: list[str] = []

    async def _fake_fetch(client, url):
        fetched.append(url)
        return ("https://ex.com/og.jpg", "Desc") if url.endswith("/page") else (None, None)

    monkeypatch.setattr(enrich_mod, "fetch_meta", _fake_fetch)
    assert await enrich_feed(settings) == 2
    assert sorted(fetched) == ["https://ex.com/none", "https://ex.com/page"]
    with connection(settings) as conn:
        rows = {
            r["id"]: (r["image_url"], r["description"], r["enriched"])
            for r in conn.execute("SELECT id, image_url, description, enriched FROM candidates")
        }
    assert rows[1] == ("https://ex.com/og.jpg", "Desc", 1)
    assert rows[2] == ("https://feed.ex/img.jpg", None, 1)  # source image kept
    assert rows[3] == (None, None, 0)  # only the current cycle is fetched
    assert await enrich_feed(settings) == 0  # each page is fetched once


def test_ui_link_target_same_tab_by_default(tmp_path, monkeypatch):
    """Articles open in the feed's tab unless LINK_TARGET=new."""
    from fastapi.testclient import TestClient
    from pydantic import ValidationError

    from discover_app import app as app_mod

    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "An article", [1.0, 0, 0, 0])
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'curated', 1, 0.9, '', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    page = TestClient(app_mod.create_app()).get("/ui").text
    assert 'href="https://ex.com/a" rel="noreferrer">' in page
    assert 'target="_blank"' not in page

    new_tab = _settings(tmp_path, link_target="new")
    monkeypatch.setattr(app_mod, "get_settings", lambda: new_tab)
    page = TestClient(app_mod.create_app()).get("/ui").text
    assert 'rel="noopener noreferrer" target="_blank"' in page

    with pytest.raises(ValidationError):
        _settings(tmp_path, link_target="popup")


# ── Linkwarden optional ─────────────────────────────────────────


def _no_linkwarden(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, embed_dim=4, **overrides)


async def test_capture_without_linkwarden_saves_locally(tmp_path):
    settings = _no_linkwarden(tmp_path)
    assert not settings.linkwarden_enabled
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a?utm_source=x", "Kept story", [1.0, 0, 0, 0])
        conn.execute("UPDATE candidates SET image_url = 'https://img.ex/a.jpg' WHERE id = 1")

    class _Boom:
        async def create_link(self, *a, **k):
            raise AssertionError("Linkwarden must not be called when not connected")

    assert await capture_candidate(settings, 1, _Boom()) == {
        "status": "saved",
        "linkwarden_id": None,
    }
    assert (await capture_candidate(settings, 1, _Boom()))["status"] == "already_saved"
    with connection(settings) as conn:
        save = conn.execute("SELECT * FROM saves").fetchone()
        events = conn.execute("SELECT COUNT(*) FROM feedback WHERE axis = 'saved'").fetchone()[0]
    assert save["url_key"] == "ex.com/a" and save["title"] == "Kept story"
    assert save["image_url"] == "https://img.ex/a.jpg" and save["embedding"] is not None
    assert save["linkwarden_id"] is None and events == 1


async def test_push_local_saves_when_linkwarden_connected_later(tmp_path):
    local = _no_linkwarden(tmp_path)
    init_db(local)
    with connection(local) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "Only local", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/b", "Also bookmarked", [0, 1.0, 0, 0])
    await capture_candidate(local, 1)
    await capture_candidate(local, 2)

    connected = _settings(tmp_path)  # same DB, now with a Linkwarden token
    with connection(connected) as conn:
        conn.execute("INSERT INTO links(id, url) VALUES(77, 'https://www.ex.com/b/')")
    writer = _FakeLinkwardenWriter()
    assert await push_local_saves(connected, writer) == 1
    assert [c["url"] for c in writer.created] == ["https://ex.com/a"]  # b already there
    with connection(connected) as conn:
        ids = dict(conn.execute("SELECT url_key, linkwarden_id FROM saves").fetchall())
    assert ids == {"ex.com/a": 4711, "ex.com/b": 77}
    assert await push_local_saves(connected, writer) == 0  # never pushed twice


def test_profile_from_saves_and_upvotes_without_links(tmp_path):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    assert rebuild_profile(settings) == 0  # nothing kept yet: no profile
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "saved", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://ex.com/b", "upvoted", [0, 1.0, 0, 0])
        _add_candidate(conn, 3, "https://ex.com/c", "up then down", [0, 0, 1.0, 0])
        conn.execute(
            "INSERT INTO saves(url, url_key, title, embedding) "
            "SELECT url, 'ex.com/a', title, embedding FROM candidates WHERE id = 1"
        )
    record_feedback(settings, 2, "interest", "up")
    record_feedback(settings, 3, "interest", "up")
    record_feedback(settings, 3, "interest", "down")  # latest vote wins: not a profile point
    assert rebuild_profile(settings) == 2
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0] == 2


async def test_build_feed_publishes_exploring_alone_without_profile(tmp_path):
    settings = _no_linkwarden(tmp_path, feed_size=10, broad_ratio=0.3)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://hn.example/a", "hn story", [1.0, 0, 0, 0])
        for i in range(12):
            _add_candidate(conn, 10 + i, f"https://sx.example/{i}", f"sx {i}", [0, 1.0, 0, 0])
            conn.execute(
                "UPDATE candidates SET source='searxng', topic='Science' WHERE id=?", (10 + i,)
            )
    # no profile and no topics: nothing to publish, no cycle marker
    assert await build_feed(settings, _FakeRerankLLM("garbage")) == 0
    set_topic(settings, "Science", True)
    assert await build_feed(settings, _FakeRerankLLM("garbage")) == 10
    with connection(settings) as conn:
        sections = [r[0] for r in conn.execute("SELECT section FROM feed_items")]
        assert get_meta(conn, "last_cycle_ts") is not None
    assert sections == ["broad"] * 10  # Exploring takes every slot until there's a profile


def test_pending_domains_include_saves_and_upvotes(tmp_path):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        for url in ["https://kept.example/1", "https://kept.example/2"]:
            conn.execute(
                "INSERT INTO saves(url, url_key, title) VALUES(?, ?, 't')", (url, norm_url(url))
            )
        _add_candidate(conn, 1, "https://voted.example/a", "a", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://www.voted.example/b", "b", [1.0, 0, 0, 0])
    record_feedback(settings, 1, "interest", "up")
    record_feedback(settings, 2, "interest", "up")
    assert sorted(pending_domains(settings)) == ["kept.example", "voted.example"]


def test_ui_start_state_and_saved_list_without_linkwarden(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    client = TestClient(app_mod.create_app())

    page = client.get("/ui").text  # brand-new install: nothing at all
    assert "Your feed learns from what you keep" in page
    assert "Start by picking a few topics" in page
    assert "Connect it and your bookmarks shape this feed" in page
    assert "Nothing saved yet" in page

    with connection(settings) as conn:
        _add_candidate(conn, 5, "https://sx.example/x", "Exploring story", [0, 1.0, 0, 0])
        conn.execute("UPDATE candidates SET source='searxng', topic='Science' WHERE id=5")
        conn.execute(
            "INSERT INTO feed_items(rank, section, candidate_id, score, reason, cycle_ts) "
            "VALUES(0, 'broad', 5, 0.0, 'exploring: Science', 't1')"
        )
        conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
    set_topic(settings, "Science", True)
    assert client.post("/feed/5/save").json()["status"] == "saved"
    page = client.get("/ui").text
    # Exploring opens first while "For you" is empty
    assert 'aria-selected="true" data-panel="broad"' in page
    assert '<section class="panel" id="curated" hidden>' in page
    assert "Saved to Linkwarden" not in page and ">Saved<" in page
    assert "Saved · 1" in page and ">Exploring story</a>" in page
    assert "Kept on this server" in page


def test_init_db_backfills_saved_list_from_earlier_saves(tmp_path):
    settings = _settings(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        _add_candidate(conn, 1, "https://ex.com/a", "Saved before #42", [1.0, 0, 0, 0])
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value, embedding) "
            "SELECT 1, 'ex.com/a', 'saved', 'explicit', embedding FROM candidates WHERE id = 1"
        )
        conn.execute(
            "INSERT INTO feedback(candidate_id, url, axis, value) "
            "VALUES(NULL, 'gone.example/x', 'saved', 'implicit')"  # candidate already pruned
        )
    init_db(settings)
    init_db(settings)  # idempotent
    with connection(settings) as conn:
        rows = conn.execute("SELECT url, title, linkwarden_id FROM saves ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("https://ex.com/a", "Saved before #42", 0),
        ("https://gone.example/x", None, 0),
    ]


async def test_mirror_tags_skipped_when_linkwarden_disconnected(tmp_path):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    with connection(settings) as conn:
        conn.execute("INSERT INTO links(id, url) VALUES(9, 'https://ex.com/a')")  # left over

    class _Boom:
        async def get_link(self, *a):
            raise AssertionError("no Linkwarden calls without a token")

    assert await mirror_facet_tags(settings, "ex.com/a", _Boom()) is False


# ── onboarding: imports, setup, login, home screen ──────────────

_BOOKMARKS_HTML = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<DL><p>
  <DT><H3>Tech</H3>
  <DL><p>
    <DT><A HREF="https://www.heise.de/news/ki-1.html" ADD_DATE="1">KI &amp; Recht</A>
    <DT><A HREF="https://example.org/a" ICON="data:x">Example <b>A</b></A>
  </DL><p>
  <DT><A HREF="javascript:alert(1)">Bookmarklet</A>
  <DT><A HREF="place:sort=8">Recent</A>
  <DT><a href='http://example.org/a'>Same page again</a>
</DL>"""


def _app_client(monkeypatch, settings):
    from fastapi.testclient import TestClient

    from discover_app import app as app_mod

    monkeypatch.setattr("discover_app.db.get_settings", lambda: settings)
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.auth.get_settings", lambda: settings)
    monkeypatch.setattr("discover_app.pipeline.output.get_settings", lambda: settings)
    return app_mod, TestClient(app_mod.create_app())


def test_parse_imports():
    assert parse_bookmarks_html(_BOOKMARKS_HTML) == [
        ("https://www.heise.de/news/ki-1.html", "KI & Recht"),
        ("https://example.org/a", "Example A"),
        ("http://example.org/a", "Same page again"),
    ]
    assert parse_url_list("see https://a.example/x, and (https://b.example/y).\nftp://no") == [
        "https://a.example/x",
        "https://b.example/y",
    ]
    assert opml_feed_count('<outline type="rss" xmlUrl="https://x/feed"/><outline/>') == 1


def test_setup_import_bookmarks_and_urls(tmp_path, monkeypatch):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    _, client = _app_client(monkeypatch, settings)
    resp = client.post("/setup/import", json={"kind": "bookmarks", "content": _BOOKMARKS_HTML})
    assert resp.status_code == 200
    assert resp.json()["added"] == 2 and resp.json()["skipped"] == 1  # http/https same page
    assert (
        client.post(
            "/setup/import", json={"kind": "bookmarks", "content": "<p>no links</p>"}
        ).status_code
        == 422
    )

    async def _fake_meta(client, url):
        return None, f"About {url}"

    monkeypatch.setattr("discover_app.importers.fetch_meta", _fake_meta)
    resp = client.post(
        "/setup/import",
        json={"kind": "urls", "content": "https://c.example/1\nhttps://example.org/a"},
    ).json()
    assert (resp["added"], resp["skipped"]) == (1, 1)
    with connection(settings) as conn:
        rows = conn.execute("SELECT url_key, title, description, source FROM imports").fetchall()
    assert sorted(tuple(r) for r in rows) == [
        ("c.example/1", None, "About https://c.example/1", "urls"),
        ("example.org/a", "Example A", None, "bookmarks"),
        ("heise.de/news/ki-1.html", "KI & Recht", None, "bookmarks"),
    ]


def test_setup_import_opml_via_miniflux(tmp_path, monkeypatch):
    opml = '<opml><body><outline xmlUrl="https://x.example/feed"/></body></opml>'
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    app_mod, client = _app_client(monkeypatch, settings)
    with_mf = _no_linkwarden(tmp_path, miniflux_token="mf")  # noqa: S106 - dummy
    monkeypatch.setattr(app_mod, "get_settings", lambda: with_mf)
    imported: list[str] = []

    class _FakeMf:
        def __init__(self, settings):
            pass

        async def import_opml(self, text):
            imported.append(text)

        async def aclose(self):
            pass

    monkeypatch.setattr(app_mod, "MinifluxClient", _FakeMf)
    resp = client.post("/setup/import", json={"kind": "opml", "content": opml})
    assert resp.json()["message"] == "Subscribed to 1 feeds in Miniflux." and imported == [opml]


def test_setup_page_build_and_status(tmp_path, monkeypatch):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    app_mod, client = _app_client(monkeypatch, settings)
    page = client.get("/setup").text
    assert "The feed learns from 0 pages so far" in page
    assert "Import OPML feeds" in page  # the built-in reader takes OPML too
    assert "Build my feed" in page and "Not connected" in page
    ran: list[bool] = []

    async def _fake_cycle():
        ran.append(True)
        with connection(settings) as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES('last_cycle_ts', 't1')")
        return {"status": "cycle complete", "errors": []}

    monkeypatch.setattr(app_mod, "run_cycle", _fake_cycle)
    assert client.post("/setup/build").status_code == 202
    status = client.get("/setup/status").json()
    assert ran == [True] and status["has_feed"] is True and status["running"] is False
    # once a feed exists, the open endpoint no longer starts model runs
    assert client.post("/setup/build").status_code == 409
    assert "Open the feed" in client.get("/setup").text


def test_optional_password(tmp_path, monkeypatch):
    open_settings = _no_linkwarden(tmp_path)
    init_db(open_settings)
    _, client = _app_client(monkeypatch, open_settings)
    assert client.get("/ui").status_code == 200  # no password: open, as before
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui"

    locked = _no_linkwarden(tmp_path, app_password="hunter2")  # noqa: S106 - test value
    _, client = _app_client(monkeypatch, locked)
    page = client.get("/ui", headers={"accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/ui"
    assert client.post("/feed/1/save").status_code == 401
    for path in ("/healthz", "/manifest.webmanifest", "/icons/icon-192.png", "/login"):
        assert client.get(path).status_code == 200, path
    assert client.get("/feed.atom").status_code == 401
    token = feed_token(locked)
    assert client.get(f"/feed.atom?token={token}").status_code == 200
    assert client.post("/login", json={"password": "nope"}).status_code == 401
    assert client.post("/login", json={"password": "hunter2"}).status_code == 200
    assert client.get("/ui").status_code == 200  # session cookie now set


def test_manifest_and_icons_are_real(tmp_path, monkeypatch):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    _, client = _app_client(monkeypatch, settings)
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/ui"
    for icon in manifest["icons"]:
        png = client.get(icon["src"])
        assert png.headers["content-type"] == "image/png"
        assert png.content.startswith(b"\x89PNG")
    assert 'rel="manifest"' in client.get("/ui").text
    assert client.get("/icons/nope.png").status_code == 404


async def test_ensure_miniflux_token_creates_key_once(tmp_path, monkeypatch):
    settings = _no_linkwarden(
        tmp_path,
        miniflux_admin_user="admin",
        miniflux_admin_password="pw",  # noqa: S106
    )
    init_db(settings)
    created: list[str] = []

    async def _fake_create(settings, description):
        created.append(description)
        return "new-key"

    monkeypatch.setattr(mf_key_mod, "create_api_key", _fake_create)
    first = await ensure_miniflux_token(settings)
    again = await ensure_miniflux_token(settings)
    assert first.miniflux_token == again.miniflux_token == "new-key"  # noqa: S105 - dummy
    assert len(created) == 1
    explicit = _no_linkwarden(tmp_path, miniflux_token="mine")  # noqa: S106
    assert (await ensure_miniflux_token(explicit)).miniflux_token == "mine"  # noqa: S105
    assert (await ensure_miniflux_token(_no_linkwarden(tmp_path))).miniflux_token == ""


def test_imports_join_profile(tmp_path):
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    store_imports(settings, [{"url": "https://ex.com/a", "title": "A"}], "bookmarks")
    assert rebuild_profile(settings) == 0  # not embedded yet
    with connection(settings) as conn:
        conn.execute(
            "UPDATE imports SET embedded = 1, embedding = ?",
            (sqlite_vec.serialize_float32([1.0, 0, 0, 0]),),
        )
    assert rebuild_profile(settings) == 1


# ── inference presets ───────────────────────────────────────────


def test_openrouter_preset_fills_only_unset_llm_settings(tmp_path):
    preset = Settings(_env_file=None, data_dir=tmp_path, openrouter_api_key="or-key")
    assert preset.llm_base_url == "https://openrouter.ai/api/v1"
    assert preset.llm_token == "or-key"  # noqa: S105 - dummy
    assert preset.llm_chat_model == "google/gemini-2.5-flash-lite"
    assert preset.llm_embed_model == "nvidia/nemotron-3-embed-1b:free"
    assert preset.embed_dim == 2048 and preset.llm_embed_input_type == ""
    assert preset.rerank_enabled
    mine = Settings(
        _env_file=None,
        data_dir=tmp_path,
        openrouter_api_key="or-key",
        llm_embed_model="baai/bge-m3",
        embed_dim=1024,
        llm_chat_model="",
    )
    assert mine.llm_embed_model == "baai/bge-m3" and mine.embed_dim == 1024
    assert mine.llm_chat_model == "" and not mine.rerank_enabled  # free: no chat model
    assert mine.llm_base_url == "https://openrouter.ai/api/v1"
    plain = Settings(_env_file=None, data_dir=tmp_path)
    assert plain.llm_base_url == "https://integrate.api.nvidia.com/v1"  # untouched


async def test_build_feed_without_chat_model_ranks_by_similarity(tmp_path):
    settings = _settings(tmp_path, llm_chat_model="")
    init_db(settings)
    with connection(settings) as conn:
        _add_centroid(conn, [1.0, 0, 0, 0])
        _add_candidate(conn, 1, "https://hn.example/a", "close", [1.0, 0, 0, 0])
        _add_candidate(conn, 2, "https://hn.example/b", "farther", [0.6, 0.8, 0, 0])

    class _NoChat(_FakeRerankLLM):
        async def chat(self, *a, **k):
            raise AssertionError("no chat call without a chat model")

    assert await build_feed(settings, _NoChat("")) == 2
    with connection(settings) as conn:
        rows = conn.execute("SELECT candidate_id, reason FROM feed_items ORDER BY rank").fetchall()
    assert [tuple(r) for r in rows] == [(1, ""), (2, "")]


def test_empty_env_vars_fall_back_to_defaults_and_presets(tmp_path, monkeypatch):
    """Compose passes every setting as ${VAR:-}: empty must mean "not set"."""
    for name in ("LLM_BASE_URL", "LLM_CHAT_MODEL", "EMBED_DIM", "SEARXNG_URL", "LLM_RERANK"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert settings.embed_dim == 2048 and settings.llm_rerank is True
    assert settings.searxng_url == ""  # Exploring stays off until SearXNG is set up
    monkeypatch.setenv("LLM_RERANK", "false")  # the free preset
    assert not Settings(_env_file=None, data_dir=tmp_path).rerank_enabled


def test_embed_input_type_derived_from_endpoint(tmp_path, monkeypatch):
    """An empty LLM_EMBED_INPUT_TYPE keeps meaning "send none" off NVIDIA."""
    monkeypatch.setenv("LLM_EMBED_INPUT_TYPE", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://gpu-box:8082/v1")
    assert Settings(_env_file=None, data_dir=tmp_path).llm_embed_input_type == ""
    monkeypatch.delenv("LLM_BASE_URL")
    nim = Settings(_env_file=None, data_dir=tmp_path)  # default endpoint: hosted NIM
    assert nim.llm_embed_input_type == "passage"
    monkeypatch.setenv("LLM_EMBED_INPUT_TYPE", "query")
    assert Settings(_env_file=None, data_dir=tmp_path).llm_embed_input_type == "query"


# ── built-in feed reader ────────────────────────────────────────

_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Ex</title>
<item><title>Fresh &amp; new</title><link>https://ex.com/fresh</link>
<description>&lt;p&gt;&lt;img src="https://img.ex/f.jpg"&gt;Body text here.&lt;/p&gt;</description>
<pubDate>__NOW__</pubDate></item>
<item><title>Old</title><link>https://ex.com/old</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 +0000</pubDate></item>
<item><title>No link</title></item>
</channel></rss>"""


async def test_builtin_reader_discovers_and_reads_feeds(tmp_path, monkeypatch):
    from email.utils import format_datetime

    from discover_app.clients import rss as rss_mod

    now = format_datetime(datetime.now(UTC))
    pages = {
        "https://site.example": (
            200,
            "text/html",
            '<head><link rel="alternate" type="application/rss+xml" href="/rss"></head>',
        ),
        "https://site.example/rss": (200, "application/rss+xml", _RSS.replace("__NOW__", now)),
        "https://bare.example": (200, "text/html", "<head></head>"),
        "https://bare.example/feed.xml": (200, "text/xml", '<?xml version="1.0"?><feed>'),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        status, ctype, body = pages.get(url, (404, "text/plain", ""))
        return httpx.Response(status, headers={"content-type": ctype}, text=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await rss_mod.discover_feed(client, "https://site.example") == (
            "https://site.example/rss"
        )
        assert await rss_mod.discover_feed(client, "https://bare.example") == (
            "https://bare.example/feed.xml"
        )  # common path fallback
        assert await rss_mod.discover_feed(client, "https://none.example") is None

    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    assert rss_mod.subscribe(settings, "https://site.example/rss", "site.example")
    assert not rss_mod.subscribe(settings, "https://site.example/rss")  # once
    real_client = httpx.AsyncClient
    monkeypatch.setattr(rss_mod.httpx, "AsyncClient", lambda **kw: real_client(transport=transport))
    items = await rss_mod.fetch_feeds(settings)
    assert [i["url"] for i in items] == ["https://ex.com/fresh"]  # old + linkless dropped
    fresh = items[0]
    assert fresh["source"] == "rss" and fresh["title"] == "Fresh & new"
    assert fresh["snippet"] == "Body text here."
    assert fresh["image_url"] == "https://img.ex/f.jpg"


def test_opml_import_without_miniflux_goes_to_builtin_reader(tmp_path, monkeypatch):
    opml = (
        '<opml><body><outline xmlUrl="https://x.example/feed"/>'
        '<outline xmlUrl="https://y.example/rss"/></body></opml>'
    )
    settings = _no_linkwarden(tmp_path)
    init_db(settings)
    _, client = _app_client(monkeypatch, settings)
    resp = client.post("/setup/import", json={"kind": "opml", "content": opml})
    assert resp.status_code == 200 and resp.json()["added"] == 2
    with connection(settings) as conn:
        assert conn.execute("SELECT COUNT(*) FROM feeds").fetchone()[0] == 2
