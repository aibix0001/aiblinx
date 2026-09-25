# Contributing

Thanks for helping. aiblinx is a small project; the aim is a private feed that
stays simple to run.

## Development

```bash
cd app
uv sync
uv run pytest -q              # network-free tests
uv run ruff check . && uv run ruff format --check .
cp ../.env.example .env       # set DATA_DIR=./data and an AI preset
uv run discover-app           # http://localhost:8000/ui
```

To run the full stack from source:
`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`.

## Pull requests

- One topic per pull request, with tests for behaviour changes.
- `ruff check`, `ruff format --check` and `pytest` must pass (CI runs them).
- Keep new dependencies to a minimum; the app deliberately has few.
- Don't commit secrets, `.env` files or databases.

## Reporting bugs

Open an issue with what you did, what you expected and what happened, plus the
relevant `docker compose logs discover-app` lines (remove tokens and URLs you
don't want to share). Security problems: see [SECURITY.md](SECURITY.md).
