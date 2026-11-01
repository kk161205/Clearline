"""The checks behind the startup summary and Settings -> System status: names, states and
timings only, never a value. Each one runs on its own, so one failure can't hide the others."""

import asyncio
import logging
import time
from urllib.parse import urlsplit

from .ai import PROVIDERS
from .config import Settings
from .db import MIGRATIONS
from .logsafe import safe
from .paypal import PayPalClient
from .store import TimelineStore

log = logging.getLogger("clearline.startup")


def check(name: str, detail: str, ok: bool | None = True) -> dict:
    """ok None: neither good nor bad, just a fact (e.g. production mode off)."""
    return {"name": name, "detail": detail, "ok": ok}


async def database_check(store: TimelineStore) -> tuple[dict, int | None]:
    """The check, and the round trip in ms (None if the database is down)."""
    started = time.monotonic()
    try:
        await asyncio.to_thread(store.ping)
    except Exception as err:  # any database failure; the driver's message can name the host
        return check("Database", f"fail ({type(err).__name__})", False), None
    latency = round((time.monotonic() - started) * 1000)
    return check("Database", f"ok ({latency} ms)"), latency


async def migrations_check(store: TimelineStore) -> dict:
    latest = MIGRATIONS[-1][0]
    try:
        version = await asyncio.to_thread(store.db.schema_version)
    except Exception as err:
        return check("Migrations", f"fail ({type(err).__name__})", False)
    return check("Migrations", f"version {version}", version == latest)


async def paypal_check(paypal: PayPalClient) -> dict:
    try:
        await paypal.check_credentials()
    except Exception as err:  # including a reply that isn't what PayPal promises
        return check("PayPal token", f"fail ({safe(err)})", False)
    return check("PayPal token", "ok")


def ai_check(settings: Settings, store: TimelineStore) -> dict:
    keys = [p for p, cfg in PROVIDERS.items() if getattr(settings, cfg["key"])]
    if not keys:
        return check("AI provider", "none (no keys)", False)
    return check("AI provider", f"{store.ai_state()['active_provider']} (keys: {', '.join(keys)})")


async def run_checks(settings: Settings, store: TimelineStore, paypal: PayPalClient) -> tuple[list[dict], int | None]:
    """All the checks, and the database latency in ms."""
    database, latency = await database_check(store)
    checks = [database, await migrations_check(store), await paypal_check(paypal),
              check("Webhook ID", "set" if settings.paypal_webhook_id else "missing",
                    bool(settings.paypal_webhook_id))]
    try:
        checks.append(await asyncio.to_thread(ai_check, settings, store))
    except Exception as err:  # the provider state lives in the database
        checks.append(check("AI provider", f"fail ({type(err).__name__})", False))
    checks += [check("AG Studio key", "present" if settings.ag_studio_license_key else "missing", None),
               check("Production mode", "on" if settings.production else "off", None),
               check("Public base URL", urlsplit(settings.base_url).hostname or "not set", None)]
    return checks, latency


async def log_startup_summary(settings: Settings, store: TimelineStore, paypal: PayPalClient) -> None:
    """One line per check when the app starts or wakes. A failed check is a warning, and never
    stops the app: it's here to be read."""
    checks, _ = await run_checks(settings, store, paypal)
    for item in checks:
        log.log(logging.WARNING if item["ok"] is False else logging.INFO,
                "%s: %s", item["name"], item["detail"])
