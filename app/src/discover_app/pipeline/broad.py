"""The anti-bubble "broad" section: epsilon-greedy bandit over onboarding topics.

Bandit state is **derived from the logs each cycle** (like the profile weights —
idempotent, replayable, no online state): serves per arm come from
``feed_items(section='broad')`` joined to ``candidates.topic``; rewards are
``saved`` / latest-``interest:up`` feedback on broad-served items, and a
story read in the reader without a save or a vote rewards
``reader_open_weight`` (0.1 by default). Because
serves and rewards are pruned together with their candidates after
``CANDIDATE_MAX_AGE_DAYS``, this is effectively a sliding-window bandit —
deliberate, since news-exploration interests are non-stationary. Exploit
picks the best Laplace-smoothed reward rate; with probability ε a uniformly
random selected topic explores instead (ε≈0.2).

Broad items are deliberately NOT LLM-reranked — exploration must not be
relevance-filtered; the "why" line is just "exploring: <topic>".

Within a topic, Exploring's own feedback steers which story is served: the
candidate closest to Exploring saves / upvotes and farthest from Exploring
downvotes wins (``explore_taste``). With probability ε the newest story is
served regardless, so a liked niche never narrows Exploring into a bubble.
This taste is separate from the interest profile — Exploring feedback never
reaches "For you".

Sites the reader cannot show are penalized here too (``readable.py``): the
penalty comes off the taste score, and the newest-story pick takes the newest
story of the least penalized sites.
"""

from __future__ import annotations

import random
import sqlite3

import numpy as np

from ..config import Settings
from ..urls import norm_url
from .readable import site_of


def arm_stats(conn: sqlite3.Connection, open_weight: float = 0.0) -> dict[str, tuple[int, float]]:
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
    # newest opinion counts, mirroring the centroid-weight recompute. A story
    # only read, with no save and no vote, rewards ``open_weight``.
    rewards: dict[str, float] = {}
    for topic, reward in conn.execute(
        "SELECT c.topic, CASE "
        "  WHEN EXISTS (SELECT 1 FROM feedback fb "
        "               WHERE fb.candidate_id = c.id AND fb.axis = 'saved') "
        "    OR (SELECT fb.value FROM feedback fb "
        "        WHERE fb.candidate_id = c.id AND fb.axis = 'interest' "
        "        ORDER BY fb.id DESC LIMIT 1) = 'up' THEN 1.0 "
        "  WHEN NOT EXISTS (SELECT 1 FROM feedback fb "
        "                   WHERE fb.url = o.url AND fb.axis IN ('saved', 'interest')) "
        "    AND o.id IS NOT NULL THEN ? "
        "  ELSE 0.0 END "
        "FROM candidates c LEFT JOIN opens o ON o.candidate_id = c.id AND o.section = 'broad' "
        "WHERE c.topic IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM feed_items f "
        "            WHERE f.candidate_id = c.id AND f.section = 'broad')",
        (open_weight,),
    ):
        if reward:
            rewards[topic] = rewards.get(topic, 0.0) + reward
    return {
        topic: (int(serves.get(topic, 0)), float(rewards.get(topic, 0.0)))
        for topic in set(serves) | set(rewards)
    }


# Taste ranking looks at this many of the topic's newest unserved stories.
_TASTE_WINDOW = 30


def _unit(blob: bytes) -> np.ndarray:
    vec = np.frombuffer(blob, dtype=np.float32)
    return vec / (float(np.linalg.norm(vec)) or 1.0)


def explore_taste(conn: sqlite3.Connection) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """(liked, disliked) unit vectors from Exploring feedback only: saves and
    latest-``up`` votes are liked, latest-``down`` votes disliked."""
    liked: list[np.ndarray] = []
    disliked: list[np.ndarray] = []
    for row in conn.execute(
        "SELECT axis, value, embedding FROM feedback "
        "WHERE section = 'broad' AND embedding IS NOT NULL AND (axis = 'saved' OR id IN ("
        "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url))"
    ):
        if row["axis"] == "saved" or row["value"] == "up":
            liked.append(_unit(row["embedding"]))
        elif row["value"] == "down":
            disliked.append(_unit(row["embedding"]))
    return liked, disliked


def _taste_score(blob: bytes, liked: list[np.ndarray], disliked: list[np.ndarray]) -> float:
    """Closest liked minus closest disliked; vectors from another embedder
    (other dimension) are ignored."""
    vec = _unit(blob)
    likes = [float(v @ vec) for v in liked if v.shape == vec.shape]
    dislikes = [float(v @ vec) for v in disliked if v.shape == vec.shape]
    return max(likes, default=float("-inf")) - max(dislikes, default=0.0)


def choose_arm(
    selected: list[str],
    stats: dict[str, tuple[int, float]],
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
    penalties: dict[str, float] | None = None,
) -> list[dict]:
    """Fill the broad slots: per slot choose an arm, then serve that topic's
    newest not-yet-served candidate. Curated exclusions apply (own bookmarks,
    anything served in any section). Slots without material are skipped —
    a thin day yields a shorter broad section, never filler."""
    if not selected or slots <= 0:
        return []
    rng = rng or random.Random()  # noqa: S311 - bandit exploration, not crypto
    penalties = penalties or {}

    def penalty(row: sqlite3.Row) -> float:
        return penalties.get(site_of(row["url"]), 0.0)

    stats = arm_stats(conn, settings.reader_open_weight if settings.reader_learn else 0.0)
    liked, disliked = explore_taste(conn)
    saved_urls = {norm_url(row[0]) for row in conn.execute("SELECT url FROM links")}
    served = {row[0] for row in conn.execute("SELECT DISTINCT candidate_id FROM feed_items")}
    pool: dict[str, list[sqlite3.Row]] = {}
    for topic in selected:
        rows = conn.execute(
            "SELECT id, url, title, snippet, topic, embedding FROM candidates "
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
        options = [r for r in pool[arm] if r["id"] not in chosen]
        if liked and rng.random() >= settings.epsilon:
            row = max(
                options[:_TASTE_WINDOW],
                key=lambda r: _taste_score(r["embedding"], liked, disliked) - penalty(r),
            )
        else:
            row = min(options, key=penalty)  # newest: pure exploration
        chosen.add(row["id"])
        picks.append(
            {
                "id": row["id"],
                "url": row["url"],
                "title": row["title"],
                "snippet": row["snippet"],
                "topic": row["topic"],
                "embedding": row["embedding"],
            }
        )
    return picks
