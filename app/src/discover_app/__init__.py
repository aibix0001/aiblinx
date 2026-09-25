"""aiblinx discover-app — a self-hosted personal discovery feed for Linkwarden."""

from __future__ import annotations


def main() -> None:
    """Console-script entrypoint: serve the FastAPI app with uvicorn."""
    import uvicorn

    uvicorn.run(
        "discover_app.app:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container service binds all interfaces by design
        port=8000,
    )
