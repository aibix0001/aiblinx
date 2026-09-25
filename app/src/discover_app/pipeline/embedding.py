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
    for i, row in enumerate(rows):
        raw = (row["text"] or "").strip() or row["url"] or "untitled"
        if len(raw) > settings.embed_max_chars:
            log.warning(
                "%s row %d oversized (%d chars, cap %d); truncated — url=%s",
                table,
                i,
                len(raw),
                settings.embed_max_chars,
                row["url"],
            )
            raw = raw[: settings.embed_max_chars]
        prepared.append(raw)
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
