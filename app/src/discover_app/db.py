"""SQLite + sqlite-vec storage layer.

A fresh connection is opened per unit of work (SQLite connections are cheap and
this sidesteps cross-thread/event-loop sharing issues). The ``vec_*`` virtual
tables hold embeddings for brute-force KNN; the raw float32 blob is *also* stored
on the base row so the profile/clustering code can read vectors back reliably
without depending on how vec0 round-trips a SELECT.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import sqlite_vec

from .config import Settings, get_settings


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def connection(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = _connect(str(settings.db_path))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id           INTEGER PRIMARY KEY,           -- Linkwarden link id
    url          TEXT NOT NULL UNIQUE,
    name         TEXT,
    description  TEXT,
    text_content TEXT,
    tags         TEXT,                          -- JSON array of tag names
    created_at   TEXT,
    updated_at   TEXT,
    embedded     INTEGER NOT NULL DEFAULT 0,
    embedding    BLOB,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,                 -- "hackernews" | "miniflux" | "searxng"
    url          TEXT NOT NULL UNIQUE,
    title        TEXT,
    snippet      TEXT,
    published_at TEXT,
    embedded     INTEGER NOT NULL DEFAULT 0,
    embedding    BLOB,
    fetched_at   TEXT NOT NULL DEFAULT (datetime('now')),
    topic        TEXT,                          -- bandit arm for searxng candidates
    image_url    TEXT,                          -- title image: page og:image, else the source's
    description  TEXT,                          -- page meta description (card summary fallback)
    enriched     INTEGER NOT NULL DEFAULT 0     -- page fetched once for image/description
);

-- Onboarding topics (IAB Tier-1) = the anti-bubble bandit arms; seeded by
-- init_db, toggled via the /topics endpoints and the /ui picker.
CREATE TABLE IF NOT EXISTS topics (
    name       TEXT PRIMARY KEY,
    selected   INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL DEFAULT 'centroid',
    weight     REAL NOT NULL DEFAULT 1.0,        -- derived from the feedback log at
                                                 -- every rebuild, never updated online
    vector     BLOB NOT NULL,                    -- L2-normalized float32 centroid
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feed_items (
    rank         INTEGER NOT NULL,
    section      TEXT NOT NULL,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    score        REAL NOT NULL,
    reason       TEXT,
    cycle_ts     TEXT NOT NULL,
    PRIMARY KEY (cycle_ts, section, rank)
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    url          TEXT NOT NULL,                 -- normalized (urls.norm_url)
    axis         TEXT NOT NULL CHECK (axis IN ('interest', 'mood', 'saved')),
    value        TEXT NOT NULL,
    embedding    BLOB,                          -- copied from the candidate at write
                                                -- time so the signal survives pruning
                                                -- (the weight recompute needs it)
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One 'saved' event per page identity, enforced at the DB so the capture
-- endpoint and the poll's save-detection cannot race each other into
-- double-counting (writers use INSERT OR IGNORE against this index).
CREATE UNIQUE INDEX IF NOT EXISTS feedback_saved_url
    ON feedback(url) WHERE axis = 'saved';

CREATE TABLE IF NOT EXISTS ratings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER REFERENCES candidates(id) ON DELETE SET NULL,
    url          TEXT NOT NULL,                 -- normalized (urls.norm_url)
    value        REAL NOT NULL CHECK(value BETWEEN -1.0 AND 1.0),
    embedding    BLOB,                          -- copied from the candidate at write time
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per bookmark domain the feed sync has tried, so Miniflux discovery
-- runs once per domain instead of every cycle.
CREATE TABLE IF NOT EXISTS feed_domains (
    domain    TEXT PRIMARY KEY,
    status    TEXT NOT NULL CHECK (status IN ('subscribed', 'no_feed', 'failed')),
    feed_url  TEXT,
    detail    TEXT,
    tried_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The local "Saved" list: every Save lands here first, whether or not
-- Linkwarden is connected. Title, image and embedding are copied from the
-- candidate so a save outlives the 14-day candidate prune and keeps feeding
-- the profile. linkwarden_id is set once the save exists in Linkwarden
-- (created there directly, or pushed when Linkwarden is connected later).
CREATE TABLE IF NOT EXISTS saves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT NOT NULL,                -- as served
    url_key       TEXT NOT NULL UNIQUE,         -- normalized (urls.norm_url)
    title         TEXT,
    image_url     TEXT,
    embedding     BLOB,
    linkwarden_id INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Pages imported on the setup page (browser bookmarks export, pasted URLs):
-- profile points like bookmarks, for installs without Linkwarden. They are
-- not saves, so they are neither listed in Saved nor pushed to Linkwarden.
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    url_key     TEXT NOT NULL UNIQUE,           -- normalized (urls.norm_url)
    title       TEXT,
    description TEXT,                           -- page meta description (pasted URLs)
    source      TEXT NOT NULL,                  -- "bookmarks" | "urls"
    embedded    INTEGER NOT NULL DEFAULT 0,
    embedding   BLOB,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The built-in feed reader's subscriptions (minimal install, no Miniflux).
CREATE TABLE IF NOT EXISTS feeds (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    url       TEXT NOT NULL UNIQUE,
    site      TEXT,
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db(settings: Settings | None = None) -> None:
    """Create the relational schema and the sqlite-vec virtual tables.

    The vec column width is taken from ``embed_dim`` so it always matches the
    embedding model in use — changing the embed model means re-ingesting.
    """
    settings = settings or get_settings()
    dim = int(settings.embed_dim)
    with connection(settings) as conn:
        conn.executescript(SCHEMA)
        # Migration for DBs created while profile had no weight column (the
        # an early version had no weight column).
        try:
            conn.execute("ALTER TABLE profile ADD COLUMN weight REAL NOT NULL DEFAULT 1.0")
        except sqlite3.OperationalError as exc:
            # only "column already present" is expected; a locked DB or other
            # failure must not silently skip the migration
            if "duplicate column" not in str(exc).lower():
                raise
        # Migrations for older DBs: candidates.topic (bandit arm) and
        # the card enrichment columns.
        for column in (
            "topic TEXT",
            "image_url TEXT",
            "description TEXT",
            "enriched INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(f"ALTER TABLE candidates ADD COLUMN {column}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        # Backfill the Saved list from saves recorded before it
        # existed. Those could only happen through Linkwarden, so they are
        # marked as already there (linkwarden_id 0 = "in Linkwarden, id
        # unknown") and never pushed again. INSERT OR IGNORE keeps it idempotent.
        conn.execute(
            "INSERT OR IGNORE INTO saves(url, url_key, title, image_url, embedding, "
            "linkwarden_id, created_at) "
            "SELECT COALESCE(c.url, 'https://' || f.url), f.url, c.title, c.image_url, "
            "f.embedding, 0, f.created_at FROM feedback f "
            "LEFT JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.axis = 'saved' ORDER BY f.id"
        )
        # Seed the onboarding taxonomy (idempotent). Function-level import:
        # topics.py imports this module at top level.
        from .topics import seed_topics

        seed_topics(conn)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_links "
            f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_imports "
            f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_candidates "
            f"USING vec0(embedding float[{dim}] distance_metric=cosine)"
        )
        # Refuse to start against a DB built for a different embedder: vectors
        # from different models/dims are incompatible and fail only at insert
        # time with opaque errors. A mismatch requires wiping and re-ingesting.
        stored_model = get_meta(conn, "embed_model")
        stored_dim = get_meta(conn, "embed_dim")
        if stored_model is None:
            set_meta(conn, "embed_model", settings.llm_embed_model)
            set_meta(conn, "embed_dim", str(dim))
        elif stored_model != settings.llm_embed_model or int(stored_dim or 0) != dim:
            raise RuntimeError(
                f"database at {settings.db_path} was built with embedder "
                f"{stored_model}@{stored_dim}, but the configuration says "
                f"{settings.llm_embed_model}@{dim} — delete the database file "
                "to re-ingest with the new embedder (the backfill restores "
                "the full Linkwarden history automatically)"
            )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
