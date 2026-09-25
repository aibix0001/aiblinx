"""Pydantic models for API responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class FeedItem(BaseModel):
    rank: int
    section: str  # "curated" | "facet" (facet query results) | "broad" (Exploring)
    candidate_id: int
    source: str
    url: str
    title: str
    snippet: str = ""
    score: float
    reason: str = ""
    # Card layout extras:
    published_at: str | None = None  # from candidates table
    discovered_at: str | None = None  # from feed_items cycle_ts
    favicon: str = ""  # derived from URL (google favicon service)
    image_url: str | None = None  # title image from the linked page (or its source)
    summary: str = ""  # plain-text teaser: feed snippet, else page description


class FeedResponse(BaseModel):
    count: int
    items: list[FeedItem]


class HealthResponse(BaseModel):
    status: str
    links: int
    candidates: int
    feed_items: int
    # Quality signal: suggestions the user saved
    saved_events: int = 0
    # Explicit ratings count
    ratings_count: int = 0


class FeedbackRequest(BaseModel):
    value: str  # interest: up|down; mood: happy|sad (validated in the pipeline)


class FeedbackResponse(BaseModel):
    status: str  # "recorded"
    candidate_id: int
    axis: str
    value: str
    # True when the Linkwarden tag mirror succeeded; False when the item is
    # not in Linkwarden (yet) or the write failed (logged). SQLite always won.
    mirrored: bool | None = None


class RatingResponse(BaseModel):
    status: str  # "recorded" or "existing"
    candidate_id: int
    value: float
    changed: bool


class CaptureResponse(BaseModel):
    status: Literal["saved", "already_saved"]
    linkwarden_id: int | None = None


class RunCycleResponse(BaseModel):
    status: str  # "cycle complete" | "cycle degraded"
    errors: list[str] = []


class Topic(BaseModel):
    name: str
    selected: bool


class TopicUpdate(BaseModel):
    selected: bool


# Setup / login. Uploads are read in the browser and posted as
# text; 20 MB comfortably fits a browser bookmarks export.
class ImportRequest(BaseModel):
    kind: Literal["bookmarks", "urls", "opml"]
    content: str = Field(max_length=20_000_000)


class ImportResponse(BaseModel):
    added: int
    skipped: int
    message: str


class LoginRequest(BaseModel):
    password: str = Field(max_length=1000)


class SetupStatus(BaseModel):
    running: bool
    has_feed: bool
    message: str = ""
