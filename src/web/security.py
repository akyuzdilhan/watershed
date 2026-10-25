"""Public safety: a per-IP rate limit on the Flask server, and who may decide.

In Azure Container Apps, Easy Auth signs the producer in and passes the user in the `X-MS-CLIENT-PRINCIPAL-NAME` header. Anyone can send that header to an app without Easy Auth, so it is trusted only when `EASY_AUTH_ENABLED` is true. In `PUBLIC_MODE` (on unless set to 0) a visitor sees the console read only, and the producer callbacks (decisions, edits, feedback) require a trusted user, optionally one of `PRODUCERS` (comma-separated); with `PUBLIC_MODE=0`, locally, everyone is the local producer.

The rate limit counts page loads and Dash callbacks per IP over a sliding minute: `RATE_LIMIT_PER_MIN`, 600 by default. The console's desk (about 80 requests a minute), its graphics or insights panel (12) and the overlay (60), open together from one address, send about 150.
"""

import os
import threading
import time
from collections import deque

from flask import Flask, Response, request

PRINCIPAL_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"
LOCAL_PRODUCER = "local-producer"
LOGIN_URL = "/.auth/login/aad?post_login_redirect_uri=/"
DEFAULT_PER_MIN = 600
LIMITED_PREFIXES = ("/_dash-update-component", "/_dash-layout", "/_dash-dependencies")
PAGES = ("/", "/overlay")


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def public_mode() -> bool:
    return _flag("PUBLIC_MODE", "1")


def easy_auth() -> bool:
    return _flag("EASY_AUTH_ENABLED", "0")


def producer() -> str | None:
    """The signed-in producer, the local producer outside public mode, or None (a visitor: read only, decisions refused)."""
    if not public_mode():
        return LOCAL_PRODUCER
    if not easy_auth():
        return None
    try:
        name = request.headers.get(PRINCIPAL_HEADER, "").strip()
    except RuntimeError:  # outside a request
        return None
    if not name:
        return None
    allowed = {p.strip().lower() for p in os.environ.get("PRODUCERS", "").split(",") if p.strip()}
    return name if not allowed or name.lower() in allowed else None


def client_ip() -> str:
    """The address the ingress saw: the last X-Forwarded-For entry (the ones before it are whatever the client sent)."""
    forwarded = [p.strip() for p in request.headers.get("X-Forwarded-For", "").split(",") if p.strip()]
    return forwarded[-1] if forwarded else (request.remote_addr or "unknown")


def rate_limit() -> int:
    """Requests a minute per IP: `RATE_LIMIT_PER_MIN`, DEFAULT_PER_MIN when it is not set."""
    value = os.environ.get("RATE_LIMIT_PER_MIN", "").strip()
    return int(value) if value else DEFAULT_PER_MIN


class RateLimiter:
    """Sliding one-minute window per key, in memory (per process: each gunicorn worker counts on its own)."""

    def __init__(self, per_minute: int = DEFAULT_PER_MIN, window_s: float = 60.0) -> None:
        self.per_minute = per_minute
        self.window_s = window_s
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()
        self.last_sweep = time.monotonic()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self.lock:
            if now - self.last_sweep > self.window_s:
                self.hits = {k: q for k, q in self.hits.items() if q and now - q[-1] < self.window_s}
                self.last_sweep = now
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] >= self.window_s:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def limited(path: str) -> bool:
    return path in PAGES or path.startswith(LIMITED_PREFIXES)


def install(server: Flask, per_minute: int | None = None) -> RateLimiter:
    """Answers 429 to an IP past its quota on page and callback routes; static assets are not counted."""
    limiter = RateLimiter(per_minute or rate_limit())

    @server.before_request
    def _rate_limit():
        if limited(request.path) and not limiter.allow(client_ip()):
            return Response("Too many requests", status=429, headers={"Retry-After": "30"})
        return None

    server.extensions["watershed_rate_limiter"] = limiter
    return limiter
