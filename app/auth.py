"""Seller login: password check, signed session cookies, rate limits, and the public-route list.

Everything is deny-by-default: only the routes in PUBLIC_ROUTES are open (checkout, the buyer
page and its API, PayPal webhooks, the login itself). Server-side jobs (auto-sync, webhook
processing) don't go through HTTP, so they never need a session.
"""

import hashlib
import hmac
import re
import secrets
import time
from collections import deque

SESSION_COOKIE = "clearline_seller"
SESSION_SECONDS = 12 * 3600

# (method, path regex) that need no login. HEAD is treated like GET.
PUBLIC_ROUTES = [
    ("GET", r"/"),
    ("GET", r"/api/config"),
    ("POST", r"/api/orders"),                    # checkout: create order
    ("POST", r"/api/orders/[^/]+/capture"),      # checkout: capture after approval
    ("GET", r"/o/[^/]+"),                        # buyer clarity page
    ("GET", r"/api/o/[^/]+"),
    ("GET", r"/api/o/[^/]+/status"),             # checkout "paid" restore
    ("POST", r"/api/o/[^/]+/requests"),
    ("POST", r"/webhooks/paypal"),
    ("GET", r"/login"),
    ("POST", r"/api/login"),
    ("POST", r"/api/logout"),
    ("GET", r"/favicon\.ico"),
    # MCP server for AI agents (Day 6). Not a seller session: buyer tools are scoped by the
    # order link, merchant tools need MERCHANT_API_KEY as a Bearer token (checked in agents.py).
    ("POST", r"/mcp"),
    ("GET", r"/mcp"),
    ("DELETE", r"/mcp"),
]
_PUBLIC = [(m, re.compile(rf"^{p}$")) for m, p in PUBLIC_ROUTES]


def is_public(method: str, path: str) -> bool:
    """True if this request needs no seller login."""
    method = "GET" if method == "HEAD" else method
    return any(m == method and rx.match(path) for m, rx in _PUBLIC)


class RateLimiter:
    """At most `limit` hits per key in a sliding `window` of seconds (in memory)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = {}

    def _recent(self, key: str) -> deque:
        hits = self.hits.setdefault(key, deque())
        while hits and hits[0] <= time.monotonic() - self.window:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> None:
        self._recent(key).append(time.monotonic())


class SellerAuth:
    """Password check and sessions. A session is a random ID signed with the app secret and
    also kept server-side, so logout really ends it (and a restart logs everyone out)."""

    def __init__(self, secret: str, password: str | None):
        self.secret = secret.encode()
        self.password = password or ""
        self.sessions: dict[str, float] = {}  # session id -> expiry (epoch seconds)
        self.failures = RateLimiter(limit=5, window=15 * 60)        # per client
        self.all_failures = RateLimiter(limit=30, window=15 * 60)   # across all clients

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def check_password(self, attempt: str) -> bool:
        """Constant-time compare (hashing first also hides the password's length)."""
        expected = hashlib.sha256(self.password.encode()).digest()
        given = hashlib.sha256((attempt or "").encode()).digest()
        return self.enabled and hmac.compare_digest(expected, given)

    def _sign(self, session_id: str) -> str:
        return hmac.new(self.secret, f"seller:{session_id}".encode(), hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        session_id = secrets.token_urlsafe(24)
        self.sessions[session_id] = time.time() + SESSION_SECONDS
        return f"{session_id}.{self._sign(session_id)}"

    def valid(self, cookie: str | None) -> bool:
        if not cookie or "." not in cookie:
            return False
        session_id, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(session_id)):
            return False
        expiry = self.sessions.get(session_id)
        if not expiry or expiry < time.time():
            self.sessions.pop(session_id, None)
            return False
        return True

    def end(self, cookie: str | None) -> None:
        if cookie and "." in cookie:
            self.sessions.pop(cookie.rsplit(".", 1)[0], None)
