"""The public-route list and a small rate limiter (seller accounts themselves: accounts.py).

Everything is deny-by-default: only the routes in PUBLIC_ROUTES are open (checkout, the buyer
page and its API, PayPal webhooks, login and first-account setup, the MCP endpoint). Server-side
jobs (auto-sync, webhook processing) don't go through HTTP, so they never need a session.
"""

import re
import time
from collections import deque

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
    ("GET", r"/setup"),                          # first account only (404 once one exists)
    ("POST", r"/api/setup"),
    ("GET", r"/healthz"),                         # load balancer health check: {"ok", "db"} only
    ("GET", r"/favicon\.ico"),
    ("GET", r"/static/(?:ui\.(?:css|js)|fonts/instrument-sans-latin(?:-ext)?\.woff2)"),  # design system + font
    # MCP server for AI agents (Day 6). Not a seller session: buyer tools are scoped by the
    # order link, merchant tools need an agent key as a Bearer token (checked in agents.py).
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
