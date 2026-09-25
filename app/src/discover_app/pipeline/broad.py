"""The anti-bubble "broad" section: epsilon-greedy bandit over onboarding topics.

Bandit state is **derived from the logs each cycle** (like the profile weights —
idempotent, replayable, no online state): serves per arm come from
``feed_items(section='broad')`` joined to ``candidates.topic``; rewards are
``saved`` / latest-``interest:up`` feedback on broad-served items. Because
serves and rewards are pruned together with their candidates after
``CANDIDATE_MAX_AGE_DAYS``, this is effectively a sliding-window bandit —
deliberate, since news-exploration interests are non-stationary. Exploit
picks the best Laplace-smoothed reward rate; with probability ε a uniformly
random selected topic explores instead (ε≈0.2).

Broad items are deliberately NOT LLM-reranked — exploration must not be
relevance-filtered; the "why" line is just "exploring: <topic>".
"""

from __future__ import annotations

import random
import sqlite3

from ..config import Settings
from ..urls import norm_url


def arm_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """{topic: (serves, rewards)} derived from the served/feedback logs."""
    serves = dict(
        conn.execute(
            "SELECT c.topic, COUNT(*) FROM feed_items f "
            "JOIN candidates c ON c.id = f.candidate_id "
            "WHERE f.section = 'broad' AND c.topic IS NOT NULL GROUP BY c.topic"
        )
    )
    # An item rewards its arm at most once (keeps rewards <= serves, so no
    # arm's Laplace rate exceeds 1): rewarded iff saved, or its LATEST
    # interest event is 'up' — repeated clicks are an event log and only the
    # newest opinion counts, mirroring the centroid-weight recompute.
    rewards = dict(
        conn.execute(
            "SELECT c.topic, COUNT(*) FROM candidates c "
            "WHERE c.topic IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM feed_items f "
            "            WHERE f.candidate_id = c.id AND f.section = 'broad') "
            "AND (EXISTS (SELECT 1 FROM feedback fb "
            "             WHERE fb.candidate_id = c.id AND fb.axis = 'saved') "
            "     OR (SELECT fb.value FROM feedback fb "
            "         WHERE fb.candidate_id = c.id AND fb.axis = 'interest' "
            "         ORDER BY fb.id DESC LIMIT 1) = 'up') "
            "GROUP BY c.topic"
        )
    )
    return {
        topic: (int(serves.get(topic, 0)), int(rewards.get(topic, 0)))
        for topic in set(serves) | set(rewards)
    }


def choose_arm(
    selected: list[str],
    stats: dict[str, tuple[int, int]],
    epsilon: float,
    rng: random.Random,
) -> str:
    """Epsilon-greedy: explore a uniform random selected topic with prob ε,
    otherwise exploit the best Laplace-smoothed reward rate ((r+1)/(s+2)) —
    the smoothing gives never-served arms a fair optimistic prior."""
    if rng.random() < epsilon:
        return rng.choice(selected)

    def rate(topic: str) -> float:
        serves, rewards = stats.get(topic, (0, 0))
        return (rewards + 1) / (serves + 2)

    best = max(rate(t) for t in selected)
    return rng.choice([t for t in selected if rate(t) == best])


def pick_broad_items(
    conn: sqlite3.Connection,
    settings: Settings,
    selected: list[str],
    slots: int,
    rng: random.Random | None = None,
) -> list[dict]:
    """Fill the broad slots: per slot choose an arm, then serve that topic's
    newest not-yet-served candidate. Curated exclusions apply (own bookmarks,
    anything served in any section). Slots without material are skipped —
    a thin day yields a shorter broad section, never filler."""
    if not selected or slots <= 0:
        return []
    rng = rng or random.Random()  # noqa: S311 - bandit exploration, not crypto
    stats = arm_stats(conn)
    saved_urls = {norm_url(row[0]) for row in conn.execute("SELECT url FROM links")}
    served = {row[0] for row in conn.execute("SELECT DISTINCT candidate_id FROM feed_items")}
    pool: dict[str, list[sqlite3.Row]] = {}
    for topic in selected:
        rows = conn.execute(
            "SELECT id, url, topic, embedding FROM candidates "
            "WHERE topic = ? AND source = 'searxng' AND embedding IS NOT NULL "
            "ORDER BY id DESC",
            (topic,),
        ).fetchall()
        pool[topic] = [
            row
            for row in rows
            if row["id"] not in served and norm_url(row["url"]) not in saved_urls
        ]

    picks: list[dict] = []
    chosen: set[int] = set()
    for _ in range(slots):
        candidates_left = [t for t in selected if any(r["id"] not in chosen for r in pool[t])]
        if not candidates_left:
            break
        arm = choose_arm(candidates_left, stats, settings.epsilon, rng)
        row = next(r for r in pool[arm] if r["id"] not in chosen)
        chosen.add(row["id"])
        picks.append({"id": row["id"], "topic": row["topic"], "embedding": row["embedding"]})
    return picks
