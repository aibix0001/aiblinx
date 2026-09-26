"""Runtime configuration sourced from environment variables / a local ``.env``.

Field names map case-insensitively to env vars (e.g. ``llm_base_url`` <-
``LLM_BASE_URL``). The LLM vars are named generically rather than ``OLLAMA_*``
because the ``openai`` client is vendor-neutral: Ollama-local, Ollama-on-a-GPU-box,
or any hosted OpenAI-compatible endpoint is a one-variable change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# OpenRouter preset (measured live 2026-09-25). Chat: a
# cheap paid model (~5 s and ~$0.002 per daily re-rank) because the free chat
# models were rate-limited, gated or took 10 minutes. Embeddings: a free model
# that separates topics well across German and English. LLM_RERANK=false
# makes the whole preset free (no re-rank, no "why" line).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_MODEL = "google/gemini-2.5-flash-lite"
OPENROUTER_EMBED_MODEL = "nvidia/nemotron-3-embed-1b:free"
OPENROUTER_EMBED_DIM = 2048


class Settings(BaseSettings):
    # env_ignore_empty: an empty variable means "not set", so Compose can pass
    # every setting through as ${VAR:-} and the defaults / presets still apply.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- storage ---
    data_dir: Path = Path("./data")
    db_filename: str = "discover.db"

    # --- LLM (OpenAI-compatible) — default: NVIDIA NIM hosted API ---
    llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    llm_token: str = ""  # required for hosted NIM (nvapi-...); a local Ollama ignores it
    llm_chat_model: str = "meta/llama-3.3-70b-instruct"
    llm_embed_base_url: str = ""  # falls back to llm_base_url when empty
    llm_embed_model: str = "nvidia/nv-embedqa-e5-v5"
    embed_dim: int = 1024  # MUST equal the sqlite-vec column width; set before first ingest
    embed_max_chars: int = Field(default=4096, ge=1)  # hard cap per document before embedding
    embed_max_batch_chars: int = Field(default=65536, ge=1)  # aggregate budget per batch
    llm_concurrency: int = Field(default=3, ge=1)  # semaphore: protect one GPU
    # Hosted free tiers are slow and rate-limited: every call gets a timeout,
    # and 429/5xx answers are retried with backoff (honouring Retry-After).
    llm_timeout_s: float = Field(default=180.0, gt=0.0)
    llm_max_retries: int = Field(default=4, ge=0)
    # One-key start: with an OpenRouter key, every LLM_* setting
    # that is not set explicitly takes the OpenRouter preset below.
    openrouter_api_key: str = ""
    # NVIDIA embedders need input_type ("passage" for stored docs, "query" for queries);
    # set "" for plain-OpenAI embedders such as Ollama's nomic-embed-text.
    # Deliberate choice: BOTH bookmarks and candidates embed as "passage" so all
    # vectors share one space; doc-doc similarity is off-label for an asymmetric
    # QA model but consistent. Switching to query-space centroids would require
    # a second input_type path and a full re-ingest.
    # None = derive: "passage" for NVIDIA's hosted API, nothing elsewhere (plain
    # OpenAI-style embedders reject the field). Set explicitly to override.
    llm_embed_input_type: str | None = None

    # --- Linkwarden (optional: empty token = local saves only) ---
    linkwarden_base_url: str = "http://linkwarden:3000"
    linkwarden_token: str = ""
    linkwarden_collection_id: int = 1  # where saves land; always address by id
    # Exploring saves land here instead, kept out of the interest profile.
    # Unset: a collection named "Exploring" is found or created on first use.
    linkwarden_explore_collection_id: int | None = None

    # --- candidate sources ---
    miniflux_url: str = "http://miniflux:8080"
    miniflux_token: str = ""
    # With the Miniflux admin login and no token, the app creates its own API
    # key once and keeps it in the database.
    miniflux_admin_user: str = ""
    miniflux_admin_password: str = ""
    # Feed sync: subscribe Miniflux to the site feeds of bookmarked domains
    feed_sync_min_links: int = Field(default=2, ge=1)  # bookmarks needed per domain
    feed_sync_per_cycle: int = Field(default=20, ge=0)  # discovery attempts per cycle
    # Platforms whose homepage feed is not what the bookmarks point at
    feed_sync_exclude: list[str] = [
        "github.com",
        "x.com",
        "twitter.com",
        "medium.com",
        "youtube.com",
        "reddit.com",
        "linkedin.com",
    ]
    hackernews_enabled: bool = True
    hackernews_list: str = "beststories"  # topstories | newstories | beststories
    hackernews_limit: int = Field(default=100, ge=1)
    # Exploring source; fetches run only for selected topics
    searxng_url: str = ""  # e.g. http://searxng:8080 (compose profile "searxng"); empty = off
    # Case-insensitive substrings; a matching title is dropped at ingestion
    # (advertorials: golem.de prefixes "Anzeige:", others use "Advertorial"/"Sponsored").
    ad_filter_patterns: list[str] = ["anzeige:", "advertorial", "sponsored"]
    # Card enrichment: each served item's page is fetched once for its
    # og:image and description
    enrich_concurrency: int = Field(default=5, ge=1)
    enrich_timeout_s: float = Field(default=8.0, gt=0.0)

    # --- ranking ---
    knn_k: int = 50
    # Interest clusters. Unset: scales with the profile (see cluster_count);
    # a number fixes it.
    profile_clusters: int | None = Field(default=None, ge=1)
    mmr_lambda: float = Field(default=0.6, ge=0.0, le=1.0)
    feed_size: int = 30
    llm_rerank: bool = True
    rerank_top_n: int = Field(default=50, ge=1)  # top ~50 by similarity go to the LLM
    candidate_max_age_days: int = Field(default=14, ge=1)  # prune + freshness window
    # Exponential age-decay half-life for feedback signals in the centroid
    # weight recompute (online re-weighting with decay, no retraining).
    feedback_half_life_days: float = Field(default=30.0, gt=0.0)
    # Feedback must be at least this cosine-close to SOME centroid to reweight
    # it; below the gate it only rewards the bandit. Embedder-dependent (e5
    # cosines are compressed upward) — calibrate from the nearest-sim
    # distribution logged by each profile rebuild.
    min_assign_sim: float = Field(default=0.2, ge=0.0, le=1.0)
    # --- Exploring section ---
    epsilon: float = Field(default=0.2, ge=0.0, le=1.0)  # bandit explore rate
    broad_ratio: float = Field(default=0.3, ge=0.0, le=1.0)  # share of feed slots
    public_base_url: str = "http://localhost:8000"  # Atom self-link
    # Card links: "same" opens the article in the feed's tab (swipe back, like
    # Google Discover); "new" opens a new tab.
    link_target: Literal["same", "new"] = "same"
    admin_token: str = ""  # when set, POST /admin/* requires X-Admin-Token
    # Optional login: empty = open (a private self-hosted feed); set = every
    # page and action needs the password once per device (session cookie).
    app_password: str = ""

    # --- scheduler (5-field crontab, UTC) ---
    # poll offset from minute 0 so it never fires together with cycle_cron
    poll_cron: str = "7-59/15 * * * *"
    cycle_cron: str = "0 6 * * *"  # full digest cycle, daily 06:00

    @model_validator(mode="after")
    def apply_openrouter_preset(self) -> Settings:
        if self.openrouter_api_key:
            preset = {
                "llm_base_url": OPENROUTER_BASE_URL,
                "llm_token": self.openrouter_api_key,
                "llm_chat_model": OPENROUTER_CHAT_MODEL,
                "llm_embed_model": OPENROUTER_EMBED_MODEL,
                "embed_dim": OPENROUTER_EMBED_DIM,
                "llm_embed_input_type": "",
            }
            for name, value in preset.items():
                if name not in self.model_fields_set:
                    setattr(self, name, value)
        if self.llm_embed_input_type is None:
            endpoint = self.llm_embed_base_url or self.llm_base_url
            self.llm_embed_input_type = "passage" if "api.nvidia.com" in endpoint else ""
        return self

    @property
    def rerank_enabled(self) -> bool:
        """The chat model is optional: without one, ranking is by similarity
        and cards carry no "why" line."""
        return self.llm_rerank and bool(self.llm_chat_model)

    @model_validator(mode="after")
    def validate_embedding_budgets(self) -> Settings:
        if self.embed_max_chars > self.embed_max_batch_chars:
            raise ValueError("embed_max_batch_chars must be at least embed_max_chars")
        return self

    @property
    def linkwarden_enabled(self) -> bool:
        """Linkwarden is optional: without a token, saves stay local."""
        return bool(self.linkwarden_token)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename


@lru_cache
def get_settings() -> Settings:
    return Settings()
