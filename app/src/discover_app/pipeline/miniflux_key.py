"""Give the app a Miniflux API key without a manual step.

Miniflux only hands out API keys through its web UI or, with the admin login,
its API. When ``MINIFLUX_TOKEN`` is empty but the admin login is configured,
the app creates a key once and keeps it in the ``meta`` table; every later
cycle reuses it. An explicit ``MINIFLUX_TOKEN`` always wins.
"""

from __future__ import annotations

import logging

from ..clients.miniflux import create_api_key
from ..config import Settings
from ..db import connection, get_meta, set_meta

log = logging.getLogger(__name__)

_META_KEY = "miniflux_token"


def miniflux_configured(settings: Settings) -> bool:
    return bool(settings.miniflux_token or settings.miniflux_admin_user)


async def ensure_miniflux_token(settings: Settings) -> Settings:
    """Settings with a usable Miniflux token when one can be had; unchanged
    otherwise. Creation errors propagate (the cycle logs and continues)."""
    if settings.miniflux_token:
        return settings
    if not (settings.miniflux_admin_user and settings.miniflux_admin_password):
        return settings
    with connection(settings) as conn:
        token = get_meta(conn, _META_KEY)
    if not token:
        token = await create_api_key(settings, "aiblinx (created automatically)")
        with connection(settings) as conn:
            set_meta(conn, _META_KEY, token)
        log.info("miniflux: created an API key with the admin login")
    return settings.model_copy(update={"miniflux_token": token})
