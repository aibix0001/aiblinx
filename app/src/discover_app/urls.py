"""URL identity normalization shared by ranking, ingest save-detection, and capture."""

from __future__ import annotations

from urllib.parse import urlsplit

# Tracking params that never change page identity; anything else is kept
# (a conservative list — dropping real params would merge distinct pages).
_TRACKING_PARAMS = {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}


def norm_url(url: str) -> str:
    """Normalize for saved-vs-candidate identity: scheme- and www-insensitive,
    tracking params and fragments stripped, trailing slash ignored."""
    parts = urlsplit(url.strip())
    host = (parts.netloc or "").lower().removeprefix("www.")
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair
        and not pair.split("=", 1)[0].lower().startswith("utm_")
        and pair.split("=", 1)[0].lower() not in _TRACKING_PARAMS
    )
    return f"{host}{parts.path.rstrip('/')}" + (f"?{query}" if query else "")
