# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /usr/local/bin/uv

# Unprivileged runtime user. The entrypoint drops privileges with setpriv (from
# util-linux, already in the base image) — no gosu needed.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app --shell /usr/sbin/nologin app

# DejaVu Sans for the reader chat's PDFs (umlauts, quotes, symbols)
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (cached unless the lockfile changes). The uv cache
# mount persists wheel downloads across builds so re-runs don't re-download.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the application itself.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev \
 && chmod +x docker-entrypoint.sh

ENV DATA_DIR=/data
EXPOSE 8000

# Entrypoint fixes bind-mount ownership as root, then drops to the app user.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["/app/.venv/bin/discover-app"]
