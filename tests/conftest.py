"""Shared test setup: never let a real .env leak AI keys into tests (no network, no cost)."""

import pytest
import time_machine
from argon2 import PasswordHasher

import app.accounts
from app.paypal import PayPalClient
from tests import pg
from tests.clock import NOON


@pytest.fixture(autouse=True)
def noon_utc():
    """Every test runs at the same time of day; the clock keeps ticking from there."""
    with time_machine.travel(NOON, tick=True):
        yield


@pytest.fixture(autouse=True)
def no_real_ai_keys(monkeypatch):
    # Environment variables beat .env in pydantic-settings, so blank keys win over real ones.
    # Tests that need AI pass keys explicitly to Settings(...), which beats both.
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    # Never use the real app secret; tests pass their own. Retired variables are unset (their
    # own tests set them to check they're ignored), and so are Render's.
    monkeypatch.setenv("APP_SECRET", "")
    for name in ("SELLER_PASSWORD", "MERCHANT_API_KEY", "TRUST_PROXY", "PAYPAL_ENV", "GEMINI_MODEL",
                 "GROQ_MODEL", "AI_TIMEOUT_SECONDS", "SELLER_DISPLAY_NAME", "SELLER_COUNTRY",
                 "AUTO_SYNC_MINUTES", "DATABASE_PATH", "RENDER", "RENDER_EXTERNAL_URL", "APP_ENV",
                 "PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AG_STUDIO_LICENSE_KEY", "")
    # Tests pick their own database (SQLite in tmp_path, or a Postgres schema with
    # TEST_DATABASE_URL); a DATABASE_URL in the environment must never reach them.
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True)
def drop_postgres_test_schemas():
    """With TEST_DATABASE_URL: drop the schemas this test created (nothing else)."""
    yield
    pg.cleanup()


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """Argon2id at a tiny cost so tests stay fast; the app always uses the library defaults."""
    monkeypatch.setattr(app.accounts, "HASHER", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))


@pytest.fixture(autouse=True)
def no_paypal_call_at_startup(monkeypatch):
    """The startup summary asks PayPal for a token; tests that start the app make no network call."""
    async def nothing(self):
        return None

    monkeypatch.setattr(PayPalClient, "check_credentials", nothing)
