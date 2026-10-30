"""Shared test setup: never let a real .env leak AI keys into tests (no network, no cost)."""

import pytest
from argon2 import PasswordHasher

import app.accounts


@pytest.fixture(autouse=True)
def no_real_ai_keys(monkeypatch):
    # Environment variables beat .env in pydantic-settings, so blank keys win over real ones.
    # Tests that need AI pass keys explicitly to Settings(...), which beats both.
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    # No background PayPal polling in tests unless a test turns it on explicitly.
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    # Never use the real app secret; tests pass their own. The retired SELLER_PASSWORD and
    # MERCHANT_API_KEY are unset (their own test sets them to check they're ignored).
    monkeypatch.setenv("APP_SECRET", "")
    monkeypatch.delenv("SELLER_PASSWORD", raising=False)
    monkeypatch.delenv("MERCHANT_API_KEY", raising=False)
    monkeypatch.setenv("AG_STUDIO_LICENSE_KEY", "")


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch):
    """Argon2id at a tiny cost so tests stay fast; the app always uses the library defaults."""
    monkeypatch.setattr(app.accounts, "HASHER", PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))
