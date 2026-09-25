"""Render the cached feed as an Atom feed and a markdown digest."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from feedgen.feed import FeedGenerator

from ..config import Settings, get_settings
from ..db import connection

_REASON_MAX = 300  # length-cap LLM "why" text before rendering it verbatim


def current_items(conn: sqlite3.Connection, section: str = "curated") -> list[sqlite3.Row]:
    """Rows of the latest cycle only — feed_items keeps history for the
    served-item exclusion, readers see just the current feed."""
    return conn.execute(
        "SELECT f.rank, f.section, f.score, f.reason, f.candidate_id, "
        "f.cycle_ts, c.published_at, c.url, c.title, c.snippet, c.source, "
        "c.image_url, c.description "
        "FROM feed_items f JOIN candidates c ON c.id = f.candidate_id "
        "WHERE f.section = ? "
        "AND f.cycle_ts = (SELECT value FROM meta WHERE key = 'last_cycle_ts') "
        "ORDER BY f.rank",
        (section,),
    ).fetchall()


def _reason(row: sqlite3.Row) -> str:
    return (row["reason"] or "")[:_REASON_MAX]


def render_atom(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    feed = FeedGenerator()
    feed.id("urn:aiblinx:discover")
    feed.title("aiblinx — Personal Discover")
    feed.link(href=f"{settings.public_base_url.rstrip('/')}/feed.atom", rel="self")
    feed.updated(datetime.now(UTC))
    with connection(settings) as conn:
        rows = list(current_items(conn)) + list(current_items(conn, section="broad"))
    for row in rows:
        entry = feed.add_entry()
        entry.id(row["url"])
        entry.title(row["title"] or row["url"])
        entry.link(href=row["url"])
        summary = _reason(row) or row["snippet"] or ""
        entry.summary(f"{summary}\n\n— {row['source']} (score {row['score']:.2f})")
    return feed.atom_str(pretty=True).decode()


def render_markdown(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [f"# Your Discover digest — {today}", ""]
    with connection(settings) as conn:
        rows = current_items(conn)
        broad = current_items(conn, section="broad")
    if not rows and not broad:
        lines.append("_No items yet — the pipeline has not produced a feed._")
        return "\n".join(lines)
    base = settings.public_base_url.rstrip("/")

    def _item_lines(row) -> list[str]:
        out = [f"## {row['rank'] + 1}. [{row['title'] or row['url']}]({row['url']})"]
        if _reason(row):
            out.append(f"> {_reason(row)}")
        # one link per item into the action page (the endpoints are POSTs, so
        # the digest carries no direct action links)
        out.append(
            f"`{row['source']}` · score {row['score']:.2f} · "
            f"[rate]({base}/ui#c{row['candidate_id']})"
        )
        out.append("")
        return out

    for row in rows:
        lines.extend(_item_lines(row))
    if broad:
        lines.append("# Beyond your bubble")
        lines.append("")
        for row in broad:
            lines.extend(_item_lines(row))
    return "\n".join(lines)
