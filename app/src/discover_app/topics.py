"""Onboarding topics: the IAB Content Taxonomy Tier-1 seed list.

The 23 user-facing Tier-1 categories (junk nodes — Uncategorized, Non-Standard
Content, Illegal Content — never included). Selections seed the anti-bubble
bandit arms and the per-topic SearXNG queries; the SearXNG source stays inert
until at least one topic is selected.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .db import connection

IAB_TIER1: tuple[str, ...] = (
    "Arts & Entertainment",
    "Automotive",
    "Business",
    "Careers",
    "Education",
    "Family & Parenting",
    "Food & Drink",
    "Health & Fitness",
    "Hobbies & Interests",
    "Home & Garden",
    "Law, Government & Politics",
    "News",
    "Personal Finance",
    "Pets",
    "Real Estate",
    "Religion & Spirituality",
    "Science",
    "Shopping",
    "Society",
    "Sports",
    "Style & Fashion",
    "Technology & Computing",
    "Travel",
)


def seed_topics(conn: sqlite3.Connection) -> None:
    """Idempotently ensure every taxonomy topic exists (all unselected)."""
    conn.executemany("INSERT OR IGNORE INTO topics(name) VALUES(?)", [(t,) for t in IAB_TIER1])


def list_topics(settings: Settings) -> list[dict]:
    with connection(settings) as conn:
        rows = conn.execute("SELECT name, selected FROM topics ORDER BY name").fetchall()
    return [{"name": row["name"], "selected": bool(row["selected"])} for row in rows]


def selected_topics(settings: Settings) -> list[str]:
    with connection(settings) as conn:
        rows = conn.execute("SELECT name FROM topics WHERE selected = 1 ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def set_topic(settings: Settings, name: str, selected: bool) -> None:
    """Toggle one topic. Raises LookupError for a name outside the taxonomy."""
    with connection(settings) as conn:
        cur = conn.execute(
            "UPDATE topics SET selected = ?, updated_at = datetime('now') WHERE name = ?",
            (1 if selected else 0, name),
        )
        if cur.rowcount == 0:
            raise LookupError(f"unknown topic {name!r}")
