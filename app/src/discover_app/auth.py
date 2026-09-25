"""Optional single-user login (``APP_PASSWORD``).

aiblinx is a private, self-hosted feed, so by default it is open like any
LAN service. Setting ``APP_PASSWORD`` puts every page and action behind one
password, entered once per device: the session cookie holds an HMAC derived
from the password, so changing the password signs every device out and the
server keeps no session state.

Reader apps can't log in, so the Atom feed accepts a secret ``token`` query
parameter instead (also derived from the password, shown on the setup page).
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .config import Settings, get_settings

COOKIE = "aiblinx_session"
_COOKIE_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetime at ~400 days

# Reachable without the password: the login itself, the health check (Docker),
# the home-screen manifest and icons (fetched by the OS without cookies), and
# the admin routes, which have their own X-Admin-Token.
_OPEN_PATHS = {"/login", "/healthz", "/manifest.webmanifest"}
_OPEN_PREFIXES = ("/icons/", "/admin/")


def _derive(password: str, purpose: str) -> str:
    return hmac.new(password.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def session_value(settings: Settings) -> str:
    return _derive(settings.app_password, "aiblinx-session-v1")


def feed_token(settings: Settings) -> str:
    """The Atom feed's secret query token (empty when no password is set)."""
    return _derive(settings.app_password, "aiblinx-atom-v1")[:32] if settings.app_password else ""


def password_ok(settings: Settings, attempt: str) -> bool:
    return bool(settings.app_password) and hmac.compare_digest(
        attempt.encode(), settings.app_password.encode()
    )


def set_session(response: Response, settings: Settings) -> None:
    response.set_cookie(
        COOKIE,
        session_value(settings),
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def _authorized(request: Request, settings: Settings) -> bool:
    path = request.url.path
    if path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES):
        return True
    if path == "/feed.atom" and hmac.compare_digest(
        request.query_params.get("token", ""), feed_token(settings)
    ):
        return True
    cookie = request.cookies.get(COOKIE, "")
    return hmac.compare_digest(cookie, session_value(settings))


async def require_login(request: Request, call_next):
    """HTTP middleware: a no-op without ``APP_PASSWORD``. Pages redirect to
    the login page; API calls get 401 so the page can say what happened."""
    settings = get_settings()
    if not settings.app_password or _authorized(request, settings):
        return await call_next(request)
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return JSONResponse({"detail": "Log in first"}, status_code=401)
