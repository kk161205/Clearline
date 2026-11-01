"""Logging for Clearline: set up once at startup, and safe to ship to a log service (Render).

Every log line carries counts, ids and statuses only: never secrets, keys, the AG Studio
licence key, buyer emails or names, or AI prompt or reply content. Errors are logged through
safe(), which keeps an HTTP status, PayPal's error name and debug_id, or a short reason, and
drops provider error text (PayPal's validation details can echo submitted values, and AI
providers can echo the request).
"""

import contextvars
import logging
import re
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import PurePath

import httpx

HANDLER_NAME = "clearline-console"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s"
http_log = logging.getLogger("clearline.http")
request_id = contextvars.ContextVar("request_id", default="-")
# The buyer's token is the whole of what protects /o/<token>, so it never reaches a log.
TOKEN_PATH = re.compile(r"^(/(?:api/)?o/)[^/]+")
EVENT_TYPE = re.compile(r"[A-Za-z0-9._-]{1,80}")
SAFE_AI_REASON = re.compile(r"HTTP \d{3}|timeout|network error: \w+|invalid JSON after retry: \d+ errors"
                            r"|unreadable reply|[A-Z_]+ is not set|no AI key configured")


class RequestIdFilter(logging.Filter):
    """Puts the current request's id on every record ("-" outside a request)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get()
        return True


def console_handler(stream=None) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def configure_logging(level: str = "INFO") -> None:
    """INFO (or LOG_LEVEL) for the "clearline" loggers, printed to the console. Records still
    propagate (pytest's caplog sees them); the console handler is only added when nothing at the
    root already prints them, so no line appears twice. Uvicorn's access log is switched off, as
    --no-access-log would: RequestLog writes one better line for each request. Uvicorn checks
    for handlers on each connection, so this works from here, after it has set up its logging."""
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    logger = logging.getLogger("clearline")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if not logging.getLogger().handlers and not any(h.get_name() == HANDLER_NAME for h in logger.handlers):
        handler = console_handler()
        handler.set_name(HANDLER_NAME)
        logger.addHandler(handler)


@contextmanager
def root_logging_kept():
    """Undo what a library does to the root logger. The MCP SDK sets it up when its server is
    created, which would print every Clearline line twice and each httpx request's URL too."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def redact_path(path: str) -> str:
    """A request path that's safe to log: buyer tokens out, control characters and anything
    non-ASCII escaped so a crafted URL can't forge a log line, and a sensible length."""
    path = path.encode("ascii", "backslashreplace").decode()
    path = re.sub(r"[\x00-\x1f\x7f]", "?", path)[:200]
    return TOKEN_PATH.sub(r"\1…", path)


def event_label(event: dict) -> str:
    """A webhook's event type for the logs. It's unverified input at that point."""
    value = event.get("event_type")
    return value if isinstance(value, str) and EVENT_TYPE.fullmatch(value) else "unknown"


class RequestLog:
    """One line per request at INFO, with an id that's also on every log line made during the
    request and in the X-Request-ID response header. Always our own id, never a client's.
    /healthz is polled all day, so it logs nothing while it's healthy."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        current = uuid.uuid4().hex[:12]
        token = request_id.set(current)
        started, status = time.monotonic(), 500

        async def with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", current.encode())]
            await send(message)

        try:
            await self.app(scope, receive, with_id)
        finally:
            if scope["path"] != "/healthz" or status >= 400:
                http_log.info("%s %s %d %d ms", scope["method"], redact_path(scope["path"]), status,
                              round((time.monotonic() - started) * 1000))
            request_id.reset(token)


def short_reason(err) -> str:
    """An AI provider failure as an HTTP status and a short error class, e.g. "HTTP 429
    rate_limited", for what gets stored (the provider-switch timeline event). No provider text."""
    m = re.match(r"HTTP (\d{3})", err.reason or "")
    if m:
        code = int(m.group(1))
        kind = ("rate_limited" if code == 429 else "auth_error" if code in (401, 403)
                else "server_error" if code >= 500 else "bad_request")
        return f"HTTP {code} {kind}"
    reason = err.reason or ""
    for prefix, kind in (("timeout", "timeout"), ("network error", "network_error"),
                         ("invalid JSON", "invalid_reply"), ("unreadable", "invalid_reply")):
        if reason.startswith(prefix):
            return kind
    return "missing_key" if "is not set" in reason or "no AI key" in reason else "error"


def where(err: BaseException) -> str:
    """Where an error came from, for the logs: safe()'s description plus its file and line.
    Stands in for a traceback, whose message could carry a provider's or driver's text."""
    frames = traceback.extract_tb(err.__traceback__)
    if not frames:
        return safe(err)
    return f"{safe(err)} at {PurePath(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}"


def safe(err: BaseException) -> str:
    """A short description of an error that is safe to log."""
    from .ai import AIError
    from .paypal import PayPalError
    if isinstance(err, PayPalError):
        name = err.body.get("name") if isinstance(err.body, dict) else None
        return " ".join(filter(None, [f"HTTP {err.status}", name, err.debug_id and f"debug_id {err.debug_id}"]))
    if isinstance(err, AIError):
        m = SAFE_AI_REASON.search(err.reason or "")
        return f"{err.provider}: {m.group(0) if m else 'error'}"
    if isinstance(err, httpx.HTTPError):
        return type(err).__name__
    return type(err).__name__
