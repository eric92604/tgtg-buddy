"""Session cookie auth + first-run app password setup."""

from __future__ import annotations

from typing import Callable

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app import secrets as secrets_mod

SESSION_AUTH_KEY = "authenticated"
PUBLIC_PATHS = {
    "/health",
    "/setup",
    "/login",
    "/favicon.ico",
}


def is_first_run() -> bool:
    """True when no app password has been set yet."""
    try:
        return not secrets_mod.has_app_password()
    except RuntimeError:
        # Master key missing — treat as first-run so /setup can explain.
        return True


def login_user(request: Request) -> None:
    request.session[SESSION_AUTH_KEY] = True


def logout_user(request: Request) -> None:
    request.session.clear()


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_AUTH_KEY))


class AuthRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated browsers to /setup or /login."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if path.startswith("/static") or path in PUBLIC_PATHS:
            return await call_next(request)

        # Always allow health checks.
        if path == "/health":
            return await call_next(request)

        try:
            first = is_first_run()
        except Exception:
            first = True

        if first and path not in ("/setup",):
            if path.startswith("/api"):
                return Response("Setup required", status_code=401)
            return RedirectResponse("/setup", status_code=303)

        if not first and not is_authenticated(request):
            if path.startswith("/api"):
                return Response("Unauthorized", status_code=401)
            return RedirectResponse("/login", status_code=303)

        return await call_next(request)
