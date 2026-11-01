"""Settings -> Business, the variables it replaced, and the other retired variables."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.ai import AI_TIMEOUT_SECONDS, GEMINI_MODEL
from app.business import BUSINESS_KEY, Business, load_business
from app.config import Settings
from app.main import create_app
from app.store import TimelineStore
from tests.support import db_target, make_client

ALL_RETIRED = {"TRUST_PROXY": "true", "SELLER_DISPLAY_NAME": "Old Name Studio", "SELLER_COUNTRY": "us",
               "AUTO_SYNC_MINUTES": "7", "GEMINI_MODEL": "old-model", "GROQ_MODEL": "old-groq",
               "AI_TIMEOUT_SECONDS": "99", "PAYPAL_ENV": "live"}


def start(tmp_path, caplog=None, **overrides):
    """The app on a fresh or existing database, with no local .env involved."""
    if caplog is not None:
        caplog.set_level(logging.INFO, logger="clearline")
    settings = Settings(_env_file=None, paypal_client_id="cid", paypal_client_secret="secret",
                        database_url=db_target(tmp_path), **overrides)
    return TestClient(create_app(settings)), settings


# ---- The seller's page ---------------------------------------------------------------------

def test_defaults_are_todays_values(tmp_path):
    client, _ = make_client(tmp_path)
    saved = client.get("/api/business").json()
    assert saved["seller_name"] == "Demo Design Studio" and saved["seller_country"] == "IN"
    assert Business().auto_sync_minutes == 3


def test_saving_changes_what_buyers_and_the_app_see(tmp_path):
    client, settings = make_client(tmp_path)
    res = client.put("/api/business", json={"seller_name": "  Asha Prints ", "seller_country": "us",
                                            "auto_sync_minutes": 10})
    assert res.status_code == 200
    assert res.json() == {"seller_name": "Asha Prints", "seller_country": "US", "auto_sync_minutes": 10}
    assert TimelineStore(settings.database_target).get_setting(BUSINESS_KEY)["seller_name"] == "Asha Prints"
    assert client.get("/api/disputes/sync/status").json()["auto_sync_minutes"] == 10
    assert client.get("/api/export/ORDER1").status_code == 404  # no India checklist any more


@pytest.mark.parametrize("change", [{"seller_name": ""}, {"seller_country": "India"},
                                    {"auto_sync_minutes": -1}, {"auto_sync_minutes": 5000}])
def test_bad_values_are_refused(tmp_path, change):
    client, _ = make_client(tmp_path)
    body = {"seller_name": "A", "seller_country": "IN", "auto_sync_minutes": 3, **change}
    assert client.put("/api/business", json=body).status_code == 422
    assert client.get("/api/business").json()["seller_name"] == "Demo Design Studio"


def test_business_needs_a_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get("/api/business").status_code == 401
    assert client.put("/api/business", json={}).status_code == 401


def test_a_new_interval_applies_without_a_restart(tmp_path):
    client, _ = make_client(tmp_path, auto_sync_minutes=0)
    body = {"seller_name": "A", "seller_country": "IN"}
    with client:
        assert client.app.state.auto_sync_task is None
        client.put("/api/business", json={**body, "auto_sync_minutes": 5})
        first = client.app.state.auto_sync_task
        assert first is not None
        client.put("/api/business", json={**body, "auto_sync_minutes": 9})
        assert client.app.state.auto_sync_task is not first
        client.put("/api/business", json={**body, "auto_sync_minutes": 0})
        assert client.app.state.auto_sync_task is None


# ---- First start: the old variables move into the database -----------------------------------

def test_first_start_copies_the_old_values_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SELLER_DISPLAY_NAME", "Old Name Studio")
    monkeypatch.setenv("SELLER_COUNTRY", "us")
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "7")
    start(tmp_path)
    store = TimelineStore(db_target(tmp_path))
    assert store.get_setting(BUSINESS_KEY) == {"seller_name": "Old Name Studio", "seller_country": "US",
                                               "auto_sync_minutes": 7}
    store.set_setting(BUSINESS_KEY, Business(seller_name="Changed on the page").model_dump())
    client, settings = start(tmp_path)  # still set in the environment, but the database wins
    assert load_business(store, settings).seller_name == "Changed on the page"


def test_only_the_values_that_are_set_are_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_SYNC_MINUTES", "0")
    start(tmp_path)
    saved = TimelineStore(db_target(tmp_path)).get_setting(BUSINESS_KEY)
    assert saved == {**Business().model_dump(), "auto_sync_minutes": 0}


def test_nothing_is_saved_without_old_values(tmp_path):
    start(tmp_path)
    assert TimelineStore(db_target(tmp_path)).get_setting(BUSINESS_KEY) is None


def test_invalid_old_values_are_not_copied_and_the_app_still_starts(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SELLER_COUNTRY", "not-a-country")
    client, _ = start(tmp_path, caplog)
    assert client.get("/healthz").status_code == 200
    assert TimelineStore(db_target(tmp_path)).get_setting(BUSINESS_KEY) is None
    assert "invalid seller_country" in caplog.text and "not-a-country" not in caplog.text


# ---- Retired variables -----------------------------------------------------------------------

def test_retired_variables_are_ignored_with_one_warning_each(tmp_path, monkeypatch, caplog):
    for name, value in ALL_RETIRED.items():
        monkeypatch.setenv(name, value)
    client, settings = start(tmp_path, caplog)
    for name, value in ALL_RETIRED.items():
        assert caplog.text.count(f"{name} is no longer used; remove it from your environment.") == 1, name
    for value in ("old-model", "old-groq"):
        assert value not in caplog.text
    # Ignored: nothing moved to the live PayPal, no proxy is trusted, the code's AI values hold.
    assert settings.api_base == "https://api-m.sandbox.paypal.com" and settings.trust_proxy is False
    assert client.app and GEMINI_MODEL != "old-model" and AI_TIMEOUT_SECONDS == 20
    assert "old-model" not in repr(settings) and "Old Name Studio" not in repr(settings)


def test_no_warnings_without_retired_variables(tmp_path, caplog):
    start(tmp_path, caplog)
    assert "no longer used" not in caplog.text


def test_database_path_is_retired_on_render_only(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_PATH", "elsewhere.db")
    assert Settings(_env_file=None).retired_variables == []  # locally it still picks the file
    assert Settings(_env_file=None, render="true").retired_variables == ["DATABASE_PATH"]


def test_trust_proxy_follows_render():
    assert Settings(_env_file=None, render="true").trust_proxy is True
    assert Settings(_env_file=None, app_env="production").trust_proxy is False
    assert Settings(_env_file=None).paypal_env == "sandbox"
