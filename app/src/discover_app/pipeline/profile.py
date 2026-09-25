"""Build the interest profile as k-means centroids over what the user keeps.

The points are Linkwarden bookmarks (when connected), the local Saved list and
upvoted pages — Linkwarden is optional, and without it the profile grows from
saves and upvotes alone.

Multiple centroids capture several distinct interests rather than one averaged
blob. Centroids are L2-normalized so a cosine MATCH against the candidate vec
table is a meaningful similarity.

Per-centroid weights are **derived state**: ``rebuild_profile``
wipes and reinserts centroids every cycle, so weights are recomputed here from
the feedback event log on each rebuild — every signal is assigned to its
nearest *new* centroid with exponential age-decay. Nothing weight-shaped is
updated online; the log is the source of truth (online
re-weighting with decay, no retraining).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

import numpy as np
import sqlite_vec
from sklearn.cluster import KMeans

from ..config import Settings, get_settings
from ..db import connection
from ..urls import norm_url

log = logging.getLogger(__name__)

# Signal strength per event kind. A save is the strongest positive (the
# save-back is the core signal); thumbs adjust more gently; mood is a filter
# facet, never a relevance signal, and is excluded in the SQL below.
# Ratings follow the same scale: +1.0 ≈ save (2.0), +0.5 ≈ up (1.0),
# 0.0 is neutral (no contribution), -0.5 ≈ down (-1.0), -1.0 is strongly negative (-2.0).
_SAVED_BASE = 2.0
_INTEREST_BASE = {"up": 1.0, "down": -1.0}
_RATING_BASE: dict[float, float] = {
    1.0: 2.0,
    0.5: 1.0,
    0.0: 0.0,
    -0.5: -1.0,
    -1.0: -2.0,
}

# No centroid dies: the floor keeps every interest cluster in the pool
# (the Exploring section's bandit does the real exploration).
_WEIGHT_FLOOR = 0.25


def _load_profile_vectors(conn, dim: int) -> np.ndarray:
    """The points the interest clusters are built from, one per page:
    Linkwarden bookmarks (when connected), the local Saved list, pages
    imported on the setup page, and pages the user upvoted (latest vote per
    page). Linkwarden is optional, so an install
    without it builds its profile from saves and upvotes alone. Vectors from a
    previous embedder (other dimension) are skipped."""
    seen: set[str] = set()
    vectors: list[np.ndarray] = []

    def add(url_key: str, blob: bytes | None) -> None:
        if not blob or url_key in seen:
            return
        vec = np.frombuffer(blob, dtype=np.float32)
        if vec.shape[0] == dim:
            seen.add(url_key)
            vectors.append(vec)

    for row in conn.execute("SELECT url, embedding FROM links WHERE embedding IS NOT NULL"):
        add(norm_url(row["url"]), row["embedding"])
    for row in conn.execute("SELECT url_key, embedding FROM saves WHERE embedding IS NOT NULL"):
        add(row["url_key"], row["embedding"])
    for row in conn.execute("SELECT url_key, embedding FROM imports WHERE embedding IS NOT NULL"):
        add(row["url_key"], row["embedding"])
    for row in conn.execute(
        "SELECT f.url, f.embedding FROM feedback f WHERE f.embedding IS NOT NULL "
        "AND f.value = 'up' AND f.id IN ("
        "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url)"
    ):
        add(row["url"], row["embedding"])
    if not vectors:
        return np.empty((0, dim), dtype=np.float32)
    return np.vstack(vectors)


def _centroid_weights(
    conn: sqlite3.Connection, centroids: np.ndarray, half_life_days: float, min_assign_sim: float
) -> np.ndarray:
    """Recompute per-centroid weights from the feedback log.

    Events considered: all ``saved`` events (unique per page identity by
    schema), the LATEST ``interest`` event per page (repeated clicks are an
    event log; only the newest opinion counts), and all ``ratings`` events
    (each distinct rating value counts — the latest value per URL drives the
    UI, but the weight recompute includes every rating as a learning signal).
    ``mood`` never contributes.  Rows without an embedding (candidate fed
    back before its embed ran) are skipped, as are vectors from a previous
    embedder (dimension mismatch).
    """
    weights = np.ones(len(centroids), dtype=np.float64)
    rows = conn.execute(
        "SELECT axis, value, embedding, created_at FROM feedback "
        "WHERE embedding IS NOT NULL AND (axis = 'saved' OR id IN ("
        "  SELECT MAX(id) FROM feedback WHERE axis = 'interest' GROUP BY url))"
    ).fetchall()
    now = datetime.now(UTC)
    nearest_sims: list[float] = []
    gated = 0
    for row in rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        if vec.shape[0] != centroids.shape[1]:
            continue
        norm = float(np.linalg.norm(vec)) or 1.0
        sims = centroids @ (vec / norm)
        nearest = int(np.argmax(sims))
        nearest_sims.append(float(sims[nearest]))
        # Min-cosine gate: Exploring feedback can be far from
        # every interest cluster — such events reward the bandit (via the
        # feedback log join in pipeline/broad.py), never the centroid weights.
        # The threshold is embedder-dependent (e5-family cosines are compressed
        # upward), hence a Setting; the distribution logged below is the data
        # to calibrate it with.
        if float(sims[nearest]) < min_assign_sim:
            gated += 1
            continue
        created = datetime.fromisoformat(row["created_at"])
        # created_at is naive UTC from SQLite's datetime('now'); convert rather
        # than relabel if a future writer ever stores an offset-aware string.
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / half_life_days)
        base = _SAVED_BASE if row["axis"] == "saved" else _INTEREST_BASE.get(row["value"], 0.0)
        weights[nearest] += base * decay
    # Include ratings as additional learning signals. Each rating row (each
    # distinct value per URL counts; re-rating the same value is idempotent).
    rating_rows = conn.execute(
        "SELECT value, embedding, created_at FROM ratings WHERE embedding IS NOT NULL"
    ).fetchall()
    for row in rating_rows:
        vec = np.frombuffer(row["embedding"], dtype=np.float32)
        if vec.shape[0] != centroids.shape[1]:
            continue
        norm = float(np.linalg.norm(vec)) or 1.0
        sims = centroids @ (vec / norm)
        nearest = int(np.argmax(sims))
        nearest_sims.append(float(sims[nearest]))
        if float(sims[nearest]) < min_assign_sim:
            gated += 1
            continue
        created = datetime.fromisoformat(row["created_at"])
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC)
        age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / half_life_days)
        base = _RATING_BASE.get(row["value"], 0.0)
        weights[nearest] += base * decay
    if nearest_sims:
        log.info(
            "centroid weights: %d signals, nearest-sim %.2f..%.2f (median %.2f), "
            "%d gated below %.2f",
            len(nearest_sims),
            min(nearest_sims),
            max(nearest_sims),
            float(np.median(nearest_sims)),
            gated,
            min_assign_sim,
        )
    return np.maximum(weights, _WEIGHT_FLOOR)


def rebuild_profile(settings: Settings | None = None) -> int:
    """Recompute centroids from bookmarks, saves and upvotes. Returns the
    cluster count (0 = no profile yet: the feed runs on Exploring alone)."""
    settings = settings or get_settings()
    with connection(settings) as conn:
        vectors = _load_profile_vectors(conn, settings.embed_dim)
        if len(vectors) == 0:
            conn.execute("DELETE FROM profile")
            log.warning("rebuild_profile: nothing saved, upvoted or bookmarked yet")
            return 0
        k = max(1, min(settings.profile_clusters, len(vectors)))
        kmeans = KMeans(n_clusters=k, n_init="auto", random_state=0).fit(vectors)
        normalized = np.vstack(
            [c / (float(np.linalg.norm(c)) or 1.0) for c in kmeans.cluster_centers_]
        ).astype(np.float32)
        weights = _centroid_weights(
            conn, normalized, settings.feedback_half_life_days, settings.min_assign_sim
        )
        conn.execute("DELETE FROM profile")
        for centroid, weight in zip(normalized, weights, strict=True):
            conn.execute(
                "INSERT INTO profile(kind, weight, vector) VALUES('centroid', ?, ?)",
                (float(weight), sqlite_vec.serialize_float32(centroid.tolist())),
            )
    log.info(
        "rebuild_profile: %d centroids from %d pages (weights %.2f..%.2f)",
        k,
        len(vectors),
        float(weights.min()),
        float(weights.max()),
    )
    return k
