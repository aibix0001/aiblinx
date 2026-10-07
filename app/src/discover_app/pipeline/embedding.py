"""Shared embedding step: embed rows lacking a vector and store them.

The serialized float32 blob is written both to the base row (``embedding`` column,
read back by the profile/MMR code) and to the matching ``vec_*`` virtual table
(used for brute-force KNN). Keep the embed model's dimension in sync with the vec
column width (``embed_dim``).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections import Counter
from urllib.parse import urlsplit

import sqlite_vec

from ..clients.llm import LLMClient
from ..config import Settings
from ..db import connection, get_meta, set_meta

log = logging.getLogger(__name__)

BATCH = 32
_LINK_TEXT_KEY = "link_text_key"


async def _embed_pending(
    table: str,
    vec_table: str,
    text_expr: str,
    settings: Settings,
    llm: LLMClient,
) -> int:
    with connection(settings) as conn:
        rows = conn.execute(
            f"SELECT id, url, {text_expr} AS text FROM {table} WHERE embedded = 0"  # noqa: S608
        ).fetchall()
    return await _embed_rows(
        table, vec_table, [(row["id"], row["text"], row["url"]) for row in rows], settings, llm
    )


async def _embed_rows(
    table: str,
    vec_table: str,
    rows: list[tuple[int, str | None, str | None]],
    settings: Settings,
    llm: LLMClient,
) -> int:
    """Embed ``(id, text, url)`` rows and store the vectors; an empty text
    falls back to the URL."""
    if not rows:
        return 0
    done = 0
    skipped = 0
    # Build text for every row up front, clamping to the per-doc budget.
    prepared: list[str] = []
    truncated = 0
    for _, text, url in rows:
        raw = (text or "").strip() or url or "untitled"
        if len(raw) > settings.embed_max_chars:
            # long article text is normal; the head carries the topic
            truncated += 1
            raw = raw[: settings.embed_max_chars]
        prepared.append(raw)
    if truncated:
        log.info(
            "%s: %d of %d texts cut to the %d-char embedding cap",
            table,
            truncated,
            len(rows),
            settings.embed_max_chars,
        )
    # Group into batches bounded by aggregate character budget, never
    # exceeding the fixed row limit.
    batch_size = BATCH
    batch_budget = settings.embed_max_batch_chars
    i = 0
    while i < len(rows):
        # Build batches bounded by aggregate character budget, never
        # exceeding the fixed row limit.
        chars = 0
        end = i  # exclusive upper bound, grows as rows fit
        while end < len(rows) and end - i < batch_size:
            row_chars = len(prepared[end])
            if chars + row_chars > batch_budget:
                break
            chars += row_chars
            end += 1
        if end == i:  # single row exceeds batch budget; per-doc cap bounds it, force-advance
            end += 1
        texts = prepared[i:end]
        try:
            vectors = await llm.embed(texts)
        except Exception:  # noqa: BLE001 - network/model error; continue with next batch
            log.warning("%s batch at row %d failed; %d skipped", table, i, end - i)
            skipped += end - i
            i = end
            continue
        with connection(settings) as conn:
            for (row_id, _, _), vec in zip(rows[i:end], vectors, strict=True):
                blob = sqlite_vec.serialize_float32(vec)
                # vec0 tables reject INSERT OR REPLACE on an existing rowid
                conn.execute(f"DELETE FROM {vec_table} WHERE rowid = ?", (row_id,))  # noqa: S608
                conn.execute(
                    f"INSERT INTO {vec_table}(rowid, embedding) VALUES(?, ?)",  # noqa: S608
                    (row_id, blob),
                )
                conn.execute(
                    f"UPDATE {table} SET embedded = 1, embedding = ? WHERE id = ?",  # noqa: S608
                    (blob, row_id),
                )
        done += end - i
        i = end
    if skipped:
        log.warning("embedded %d/%d rows from %s (%d skipped)", done, len(rows), table, skipped)
    else:
        log.info("embedded %d rows from %s", done, table)
    return done


# Linkwarden stores what its crawler saw, and for some sites that is a wall:
# a consent banner (every golem.de page), a login or error page (x.com's
# "Something went wrong", "Access Denied", "504 Gateway Time-out"). Such
# pages embed close together into a vector that matches every story a
# little, and k-means turns them into a "matches everything" interest.
#
# A name, description or text that this many bookmarks share word for word
# is the site's, not the page's.
_SHARED_MIN = 3
# Stored text shorter than this is a wall or an error page, not an article.
_MIN_ARTICLE_CHARS = 400
# A page needs this many words of its own (title, description, URL path) to
# get a vector; with fewer it says nothing about its topic and stays out of
# the profile.
_MIN_WORDS = 3
# Bump when the rules above change: every link is embedded again.
_LINK_TEXT_VERSION = "1"
_URL_NOISE = {"html", "htm", "php", "aspx", "index", "status", "www"}


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _url_words(url: str) -> list[str]:
    """Words in the URL path (slugs often name the topic): letters only,
    three or more, without file extensions and the like."""
    path = urlsplit(url).path
    return [
        w for w in re.split(r"[^A-Za-zÀ-ÿ]+", path) if len(w) >= 3 and w.lower() not in _URL_NOISE
    ]


def link_texts(rows: list[sqlite3.Row]) -> tuple[dict[int, str | None], str]:
    """The text each link is embedded from, by link id, and a key that
    changes whenever the shared (site) texts change.

    Shared names, descriptions and texts are dropped, and so is text too
    short to be an article. Without article text, the title and description
    are joined by the URL path words. A link left with fewer than
    ``_MIN_WORDS`` words maps to ``None``: it gets no vector."""
    counts: Counter[str] = Counter()
    for row in rows:
        for value in {_norm(row["name"]), _norm(row["description"]), _norm(row["text_content"])}:
            if value:
                counts[value] += 1
    shared = sorted(value for value, n in counts.items() if n >= _SHARED_MIN)
    shared_set = set(shared)

    def own(value: str | None) -> str:
        value = _norm(value)
        return "" if value in shared_set else value

    texts: dict[int, str | None] = {}
    for row in rows:
        name, description, body = (
            own(row["name"]),
            own(row["description"]),
            own(row["text_content"]),
        )
        if len(body) < _MIN_ARTICLE_CHARS:
            body = " ".join(_url_words(row["url"] or ""))
        text = " ".join(part for part in (name, description, body) if part)
        words = re.findall(r"[^\W\d_]{3,}", text)
        texts[row["id"]] = text if len(words) >= _MIN_WORDS else None
    key = hashlib.sha256("\n".join([_LINK_TEXT_VERSION, *shared]).encode()).hexdigest()
    return texts, key


async def embed_pending_links(settings: Settings, llm: LLMClient) -> int:
    """Embed new and changed links from their own text (see ``link_texts``).
    When the shared texts change — a site's wall shows up on a third
    bookmark, or the rules change — every link is embedded again."""
    with connection(settings) as conn:
        rows = conn.execute(
            "SELECT id, url, name, description, text_content, embedded FROM links"
        ).fetchall()
        texts, key = link_texts(rows)
        if get_meta(conn, _LINK_TEXT_KEY) != key:
            conn.execute("UPDATE links SET embedded = 0")
            set_meta(conn, _LINK_TEXT_KEY, key)
            pending = list(rows)
        else:
            pending = [row for row in rows if not row["embedded"]]
        # No text of its own: no vector, so it never shapes the profile.
        blank = [row["id"] for row in pending if texts[row["id"]] is None]
        for link_id in blank:
            conn.execute("UPDATE links SET embedded = 1, embedding = NULL WHERE id = ?", (link_id,))
            conn.execute("DELETE FROM vec_links WHERE rowid = ?", (link_id,))
    if blank:
        log.info("links: %d pages have no text of their own, left out of the profile", len(blank))
    return await _embed_rows(
        "links",
        "vec_links",
        [(row["id"], texts[row["id"]], row["url"]) for row in pending if texts[row["id"]]],
        settings,
        llm,
    )


async def embed_pending_imports(settings: Settings, llm: LLMClient) -> int:
    """Imported pages have no article text: title, meta description and the
    URL itself (its path words often name the topic) carry the meaning."""
    return await _embed_pending(
        "imports",
        "vec_imports",
        "COALESCE(title,'') || ' ' || COALESCE(description,'') || ' ' || url",
        settings,
        llm,
    )


async def embed_pending_candidates(settings: Settings, llm: LLMClient) -> int:
    return await _embed_pending(
        "candidates",
        "vec_candidates",
        "COALESCE(title,'') || ' ' || COALESCE(snippet,'')",
        settings,
        llm,
    )


# Saves, feedback and ratings carry a copy of their page's vector (so they
# outlive the candidate prune). After an embedding-model change those copies
# are cleared, and this re-embeds them from the text still at hand: the
# candidate's title and snippet while it exists, else the saved title, else
# the URL. Mood feedback never feeds the profile, so it is left alone.
_SIGNAL_TEXT = {
    "saves": "SELECT s.id, COALESCE(s.title, '') || ' ' || s.url AS text "
    "FROM saves s WHERE s.embedding IS NULL",
    "feedback": "SELECT f.id, COALESCE(c.title || ' ' || COALESCE(c.snippet, ''), "
    "s.title || ' ' || s.url, f.url) AS text FROM feedback f "
    "LEFT JOIN candidates c ON c.id = f.candidate_id "
    "LEFT JOIN saves s ON s.url_key = f.url "
    "WHERE f.embedding IS NULL AND f.axis != 'mood'",
    "ratings": "SELECT r.id, COALESCE(c.title || ' ' || COALESCE(c.snippet, ''), "
    "s.title || ' ' || s.url, r.url) AS text FROM ratings r "
    "LEFT JOIN candidates c ON c.id = r.candidate_id "
    "LEFT JOIN saves s ON s.url_key = r.url WHERE r.embedding IS NULL",
}


async def embed_pending_signals(settings: Settings, llm: LLMClient) -> int:
    """Re-embed saves, feedback and ratings that have no vector; returns the
    number embedded. A no-op in normal operation."""
    done = 0
    for table, query in _SIGNAL_TEXT.items():
        with connection(settings) as conn:
            rows = conn.execute(query).fetchall()
        texts = [(row["text"] or "untitled")[: settings.embed_max_chars] for row in rows]
        i = 0
        while i < len(rows):
            end, chars = i, 0
            while end < len(rows) and end - i < BATCH:
                if end > i and chars + len(texts[end]) > settings.embed_max_batch_chars:
                    break
                chars += len(texts[end])
                end += 1
            try:
                vectors = await llm.embed(texts[i:end])
            except Exception:  # noqa: BLE001 - retried next cycle
                log.warning("%s re-embed batch at row %d failed", table, i)
                i = end
                continue
            with connection(settings) as conn:
                for row, vec in zip(rows[i:end], vectors, strict=True):
                    conn.execute(
                        f"UPDATE {table} SET embedding = ? WHERE id = ?",  # noqa: S608
                        (sqlite_vec.serialize_float32(vec), row["id"]),
                    )
            done += end - i
            i = end
    if done:
        log.info("embedded %d saved/feedback signals", done)
    return done
