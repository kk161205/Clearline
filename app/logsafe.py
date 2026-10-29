"""Logging for Clearline: set up once at startup, and safe to ship to a log service (Render).

Every log line carries counts, ids and statuses only: never secrets, keys, the AG Studio
licence key, buyer emails or names, or AI prompt or reply content. Errors are logged through
safe(), which keeps an HTTP status, PayPal's error name and debug_id, or a short reason, and
drops provider error text (PayPal's validation details can echo submitted values, and AI
providers can echo the request).
"""

import logging
import re

import httpx

HANDLER_NAME = "clearline-console"
SAFE_AI_REASON = re.compile(r"HTTP \d{3}|timeout|network error: \w+|invalid JSON after retry: \d+ errors"
                            r"|unreadable reply|[A-Z_]+ is not set|no AI key configured")


def configure_logging(level: str = "INFO") -> None:
    """INFO (or LOG_LEVEL) for the "clearline" loggers, printed to the console. Uvicorn's own
    loggers are left alone. Records still propagate, so pytest's caplog sees them."""
    logger = logging.getLogger("clearline")
    logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if not any(h.get_name() == HANDLER_NAME for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.set_name(HANDLER_NAME)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


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
