# discover-app

The aiblinx service: a FastAPI app with a built-in daily scheduler. It mirrors
the pages you keep (Linkwarden bookmarks, imports, saves), clusters them into
interests, collects stories (Hacker News, site feeds, optional Miniflux and
SearXNG), ranks them (similarity → optional LLM re-rank → diversity), and
serves the phone feed, an Atom feed and a daily digest.

Architecture and settings: [../docs/architecture.md](../docs/architecture.md).

## Local development

```bash
cp ../.env.example .env     # root template -> app/.env; set DATA_DIR=./data and an AI preset
uv sync                     # install deps into .venv
uv run discover-app         # serve on http://0.0.0.0:8000
```

The pipeline needs an AI endpoint (e.g. `OPENROUTER_API_KEY`); everything else
is optional. The service starts and serves `/healthz` without it.

## Endpoints

| Method | Path               | Purpose                                  |
|--------|--------------------|------------------------------------------|
| GET    | `/healthz`         | status + row counts                      |
| GET    | `/feed`            | cached feed as JSON                      |
| GET    | `/feed.atom`       | Atom feed for any reader                 |
| GET    | `/digest`          | markdown digest                          |
| GET    | `/ui`              | the phone feed                           |
| GET    | `/setup`           | imports, topics, connections, first build |
| POST   | `/admin/run-cycle` | trigger a full pipeline cycle (`X-Admin-Token`) |

The scheduler runs `run_poll` (default every 15 min) and `run_cycle` (daily 06:00 UTC);
cron strings are configurable via `POLL_CRON` / `CYCLE_CRON`.

## Tests & lint

```bash
uv run pytest        # network-free unit tests
uv run ruff check    # lint
uv run ruff format   # format
```
