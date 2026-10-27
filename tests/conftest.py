"""Shared test setup: never let a real .env leak AI keys into tests (no network, no cost)."""

import pytest


@pytest.fixture(autouse=True)
def no_real_ai_keys(monkeypatch):
    # Environment variables beat .env in pydantic-settings, so blank keys win over real ones.
    # Tests that need AI pass keys explicitly to Settings(...), which beats both.
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    # No background PayPal polling in tests unless a test turns it on explicitly.
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
