"""Shared embedding step: embed rows lacking a vector and store them.

The serialized float32 blob is written both to the base row (``embedding`` column,
read back by the profile/MMR code) and to the matching ``vec_*`` virtual table
(used for brute-force KNN). Keep the embed model's dimension in sync with the vec
column width (``embed_dim``).
"""

from __future__ import annotations

import logging

import sqlite_vec

from ..clients.llm import LLMClient
from ..config import Settings
from ..db import connection

log = logging.getLogger(__name__)

BATCH = 32


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
    if not rows:
        return 0
    done = 0
    skipped = 0
    # Build text for every row up front, clamping to the per-doc budget.
    prepared: list[str] = []
    truncated = 0
    for row in rows:
        raw = (row["text"] or "").strip() or row["url"] or "untitled"
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
            for row, vec in zip(rows[i:end], vectors, strict=True):
                blob = sqlite_vec.serialize_float32(vec)
                conn.execute(
                    f"INSERT OR REPLACE INTO {vec_table}(rowid, embedding) VALUES(?, ?)",  # noqa: S608
                    (row["id"], blob),
                )
                conn.execute(
                    f"UPDATE {table} SET embedded = 1, embedding = ? WHERE id = ?",  # noqa: S608
                    (blob, row["id"]),
                )
        done += end - i
        i = end
    if skipped:
        log.warning("embedded %d/%d rows from %s (%d skipped)", done, len(rows), table, skipped)
    else:
        log.info("embedded %d rows from %s", done, table)
    return done


async def embed_pending_links(settings: Settings, llm: LLMClient) -> int:
    return await _embed_pending(
        "links",
        "vec_links",
        "COALESCE(name,'') || ' ' || COALESCE(description,'') || ' ' || COALESCE(text_content,'')",
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
