# How aiblinx works

aiblinx is one FastAPI service (`app/`) with a built-in scheduler and a single
SQLite database (with [sqlite-vec](https://github.com/asg017/sqlite-vec) for
vector search). Everything else is optional and reached over HTTP.

```
 your pages ─┐                                 ┌─▶ phone feed (/ui), Atom, digest
 Linkwarden  │   ┌──────────── discover-app ───┴────────────┐
 imports     ├──▶│ profile ◀─ saves · up/down                │
 saves       │   │ sources: Hacker News · site feeds         │──▶ AI model (embeddings,
             │   │          (built-in or Miniflux) · SearXNG │    optional re-rank)
             └──▶│ rank → diversify → enrich → publish       │
                 └───────────────────────────────────────────┘
```

## What it learns from

The interest profile is built from the pages you keep, one point per page:

- Linkwarden bookmarks, when connected
- pages imported on the setup page (browser bookmarks export, pasted links)
- stories you saved in aiblinx
- stories you upvoted (the latest vote per page counts)

The points are clustered with k-means into up to 8 interests. Each interest's
weight comes from your taps: a save counts +2, more like this +1, less like
this −1, and each signal loses half its weight every 30 days. Nothing is
retrained; the profile is recomputed from the log every cycle.

## The daily cycle

A light poll runs every 15 minutes: it mirrors new Linkwarden bookmarks and
embeds new imports. The full cycle runs once a day (06:00 UTC by default); each
step is isolated, so one failing source never costs you the day's feed.

1. **Ingest:** mirror Linkwarden bookmarks and push local saves made before it
   was connected.
2. **Sync feeds:** for sites you keep at least two pages from, find the site's
   feed (`<link rel="alternate">`, then common paths such as `/feed`) and
   subscribe it, in Miniflux or the built-in reader. Up to 20 new sites a day.
3. **Gather:** fetch Hacker News, the subscribed feeds and, for the topics you
   picked, SearXNG. Drop duplicates, advertorials and stories older than 14
   days, then embed the new ones.
4. **Profile:** rebuild the interest clusters and their weights.
5. **Rank:** shortlist stories close to your interests. The chat model, if
   configured, re-reads the top 50 and writes one line on why each fits.
   Maximal Marginal Relevance then thins out near-duplicates.
6. **Explore:** fill 30% of the slots from your chosen topics, picked by an
   epsilon-greedy bandit (20% exploration) that learns from what you save
   there. Without a profile yet, Exploring gets every slot.
7. **Enrich:** read each chosen article's page once for its preview image and
   description.
8. **Publish:** the phone page, an Atom feed and a daily markdown digest.

Card summaries are built from whole sentences of the feed text or the page
description, never cut mid-word.

## Data

One SQLite file in `DATA_DIR`: bookmarks, imports, saves, candidate stories and
their vectors, the feedback log, the profile, topics and feed history. The
embedding model and its dimension are fixed per database.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/ui` | the phone feed |
| GET | `/setup` | imports, topics, connections, building the first feed |
| POST | `/setup/import` | `{kind: bookmarks\|urls\|opml, content}` |
| POST | `/setup/build` | build the first feed now (only while none exists) |
| GET | `/setup/status` | build progress |
| GET | `/feed` | the current feed as JSON |
| GET | `/feed.atom` | Atom feed (with `APP_PASSWORD`: `?token=` from `/setup`) |
| GET | `/digest` | markdown digest |
| POST | `/feed/{id}/save` | save a story (and to Linkwarden when connected) |
| POST | `/feed/{id}/interest` | `{value: up\|down}` |
| GET / POST | `/topics`, `/topics/{name}` | Exploring topics |
| GET / POST | `/login` | optional login |
| GET | `/healthz` | status and counts |
| POST | `/admin/run-cycle` | run a full cycle now (`X-Admin-Token`) |

## Settings

All settings are environment variables (or `app/.env` for local runs). Empty
values count as unset.

| Variable | Default | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | — | one-key AI setup; fills every unset `LLM_*` below |
| `LLM_BASE_URL` / `LLM_TOKEN` | hosted NVIDIA NIM / — | any OpenAI-compatible endpoint |
| `LLM_CHAT_MODEL` | preset | re-rank and explanations |
| `LLM_RERANK` | `true` | `false` = no chat model: similarity ranking, no "why" line |
| `LLM_EMBED_BASE_URL` | = `LLM_BASE_URL` | separate embeddings endpoint |
| `LLM_EMBED_MODEL` / `EMBED_DIM` | preset | fixed per database |
| `LLM_EMBED_INPUT_TYPE` | derived | `passage` for NVIDIA's API, none elsewhere |
| `LLM_TIMEOUT_S` / `LLM_MAX_RETRIES` | `180` / `4` | per call; 429/5xx retried with backoff |
| `LINKWARDEN_BASE_URL` / `LINKWARDEN_TOKEN` | — | optional; empty token = saves stay local |
| `LINKWARDEN_COLLECTION_ID` | `1` | where saves land (an id, not a name) |
| `MINIFLUX_URL` / `MINIFLUX_TOKEN` | `http://miniflux:8080` / — | optional; without a token the built-in reader is used |
| `MINIFLUX_ADMIN_USER` / `MINIFLUX_ADMIN_PASSWORD` | — | lets the app create its own Miniflux API key |
| `SEARXNG_URL` | — | empty = Exploring off |
| `HACKERNEWS_ENABLED` / `HACKERNEWS_LIST` | `true` / `beststories` | |
| `FEED_SYNC_MIN_LINKS` / `FEED_SYNC_PER_CYCLE` | `2` / `20` | |
| `FEED_SIZE` / `BROAD_RATIO` / `EPSILON` | `30` / `0.3` / `0.2` | feed length, Exploring share, bandit exploration |
| `RERANK_TOP_N` | `50` | stories sent to the chat model |
| `FEEDBACK_HALF_LIFE_DAYS` | `30` | decay of taps |
| `CANDIDATE_MAX_AGE_DAYS` | `14` | freshness window |
| `AD_FILTER_PATTERNS` | `anzeige:`, `advertorial`, `sponsored` | title substrings dropped |
| `POLL_CRON` / `CYCLE_CRON` | `7-59/15 * * * *` / `0 6 * * *` | UTC |
| `LINK_TARGET` | `same` | `new` opens articles in a new tab |
| `APP_PASSWORD` | — | optional login |
| `ADMIN_TOKEN` | — | protects `POST /admin/run-cycle` |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | used in the Atom feed |
| `DATA_DIR` | `./data` (`/data` in the image) | database location |
