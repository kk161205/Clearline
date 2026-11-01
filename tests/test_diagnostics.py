"""Logging and diagnostics: request lines, request ids, /healthz logging, the startup summary,
what failures log, Settings -> System status, and that no secret reaches any log line."""

import asyncio
import io
import logging
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.diagnostics import log_startup_summary, run_checks
from app.logsafe import configure_logging, console_handler, event_label, redact_path
from app.main import create_app
from app.paypal import PayPalClient
from app.store import TimelineStore
from tests.support import GEMINI, ai_reply, db_target, make_client, setup_paypal
from tests.test_webhooks import API, mock_token, post_event

REAL_TOKEN_CHECK = PayPalClient.check_credentials  # conftest stubs it, for tests that start the app
SECRETS = {"paypal_client_secret": "paypal-secret-value-123", "gemini_api_key": "gemini-key-value-456",
           "groq_api_key": "groq-key-value-789", "ag_studio_license_key": "ag-licence-value-012",
           "app_secret": "app-secret-value-that-is-long-enough-345"}


@pytest.fixture
def lines():
    """Everything Clearline logs, formatted as it is on Render."""
    stream = io.StringIO()
    handler = console_handler(stream)
    logger = logging.getLogger("clearline")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(level)


def run_startup(client) -> None:
    """Start the app and wait for its background startup summary, then stop it."""
    with client:
        while not client.app.state.startup_task.done():
            time.sleep(0.01)


def request_lines(stream) -> list[str]:
    return [line for line in stream.getvalue().splitlines() if " clearline.http " in line]


# ---- Paths ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path, logged", [
    ("/o/abc123token", "/o/…"),
    ("/api/o/abc123token", "/api/o/…"),
    ("/api/o/abc123token/status", "/api/o/…/status"),
    ("/api/o/abc123token/requests", "/api/o/…/requests"),
    ("/api/orders/ORDER1/timeline", "/api/orders/ORDER1/timeline"),
    ("/seller", "/seller"),
    ("/x\nFAKE LINE", "/x?FAKE LINE"),
    ("/café", "/caf\\xe9"),
])
def test_paths_are_redacted(path, logged):
    assert redact_path(path) == logged


def test_long_paths_are_cut():
    assert len(redact_path("/" + "a" * 5000)) == 200


def test_event_types_from_an_unverified_webhook_are_checked():
    assert event_label({"event_type": "CUSTOMER.DISPUTE.CREATED"}) == "CUSTOMER.DISPUTE.CREATED"
    assert event_label({"event_type": "x\nFAKE LINE"}) == "unknown" and event_label({}) == "unknown"


# ---- One line per request ------------------------------------------------------------------

def test_one_line_per_request_with_status_time_and_id(tmp_path, lines):
    client, _ = make_client(tmp_path)
    lines.truncate(0), lines.seek(0)
    res = client.get("/api/orders?secret=in-the-query")
    [line] = request_lines(lines)
    rid = res.headers["x-request-id"]
    assert len(rid) == 12 and f"[{rid}]" in line
    assert " INFO " in line and "GET /api/orders 200 " in line and line.endswith(" ms")
    assert "in-the-query" not in line


def test_a_buyer_token_never_reaches_a_log_line(tmp_path, lines):
    client, _ = make_client(tmp_path)
    client.get("/o/private-buyer-token")
    client.get("/api/o/private-buyer-token/status")
    assert "/o/…" in lines.getvalue() and "private-buyer-token" not in lines.getvalue()


def test_a_clients_request_id_is_ignored_and_ids_differ(tmp_path, lines):
    client, _ = make_client(tmp_path)
    first = client.get("/api/config", headers={"X-Request-ID": "forged"}).headers["x-request-id"]
    assert first != "forged" and first != client.get("/api/config").headers["x-request-id"]


def test_lines_made_during_a_request_carry_its_id(tmp_path, lines):
    client, _ = make_client(tmp_path, login=False)
    res = client.post("/api/login", json={"email": "a@example.com", "password": "nope"})
    rid = res.headers["x-request-id"]
    during = [line for line in lines.getvalue().splitlines() if "Login failed" in line]
    assert during and f"[{rid}]" in during[0]


def test_lines_outside_a_request_have_no_id(lines):
    logging.getLogger("clearline").info("background job")
    assert "[-]: background job" in lines.getvalue()


def test_every_answer_has_an_id_even_redirects_401s_and_errors(tmp_path, monkeypatch):
    anonymous, _ = make_client(tmp_path, login=False)
    for path in ("/seller", "/api/orders", "/nothing"):
        res = anonymous.get(path, follow_redirects=False)
        assert res.headers["x-request-id"], path
        assert res.headers["x-content-type-options"] == "nosniff", path  # outside the login check
        assert "content-security-policy" in res.headers, path

    def boom(self, *args):
        raise RuntimeError("private detail")

    monkeypatch.setattr("app.main.Accounts.authenticate", boom)
    res = anonymous.post("/api/login", json={"email": "a@example.com", "password": "nope"})
    assert res.status_code == 500 and res.headers["x-request-id"]


def test_the_mcp_sdk_leaves_the_root_logger_alone(tmp_path):
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    make_client(tmp_path, login=False)
    assert (list(root.handlers), root.level) == before  # or every line would print twice


def test_uvicorns_access_log_is_off():
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.NullHandler())
    configure_logging()
    assert not access.hasHandlers()


# ---- /healthz ----------------------------------------------------------------------------

def test_healthz_logs_nothing_while_healthy(tmp_path, lines):
    client, _ = make_client(tmp_path, login=False)
    lines.truncate(0), lines.seek(0)
    for _ in range(3):
        assert client.get("/healthz").status_code == 200
    assert lines.getvalue() == ""


def test_healthz_warns_on_failure_and_says_when_it_recovers(tmp_path, lines, monkeypatch):
    client, _ = make_client(tmp_path, login=False)
    lines.truncate(0), lines.seek(0)

    def down(self):
        raise ConnectionError("could not connect to db.internal")

    with monkeypatch.context() as patch:
        patch.setattr(TimelineStore, "ping", down)
        assert client.get("/healthz").status_code == 503
    assert " WARNING " in lines.getvalue() and "database unreachable" in lines.getvalue()
    assert "db.internal" not in lines.getvalue()

    lines.truncate(0), lines.seek(0)
    client.get("/healthz")
    assert " INFO " in lines.getvalue() and "database is back" in lines.getvalue()
    lines.truncate(0), lines.seek(0)
    client.get("/healthz")
    assert lines.getvalue() == ""  # only once


# ---- The startup summary --------------------------------------------------------------------

def app_parts(tmp_path, **overrides):
    settings = Settings(**{"_env_file": None, "paypal_client_id": "cid", "paypal_client_secret": "shh",
                           "database_url": db_target(tmp_path), **overrides})
    http = httpx.AsyncClient(base_url=settings.api_base)
    return settings, TimelineStore(settings.database_target), PayPalClient(settings, http)


def summary(tmp_path, lines, **overrides) -> str:
    settings, store, paypal = app_parts(tmp_path, **overrides)
    asyncio.run(log_startup_summary(settings, store, paypal))
    return lines.getvalue()


@respx.mock
def test_startup_summary_is_one_line_per_check(tmp_path, lines):
    mock_token()
    text = summary(tmp_path, lines, paypal_webhook_id="WH-1", gemini_api_key="g", groq_api_key="q",
                   ag_studio_license_key="key", render_external_url="https://x.onrender.com",
                   render="true")
    logged = [line.split("clearline.startup [-]: ")[1] for line in text.splitlines()]
    assert [line.split(":")[0] for line in logged] == [
        "Database", "Migrations", "PayPal token", "Webhook ID", "AI provider", "AG Studio key",
        "Production mode", "Public base URL"]
    assert logged[0].startswith("Database: ok (") and logged[0].endswith(" ms)")
    assert logged[1].startswith("Migrations: version ")
    assert logged[2:] == ["PayPal token: ok", "Webhook ID: set", "AI provider: gemini (keys: gemini, groq)",
                          "AG Studio key: present", "Production mode: on", "Public base URL: x.onrender.com"]


@respx.mock
def test_startup_summary_says_what_is_missing(tmp_path, lines, monkeypatch):
    monkeypatch.setattr(PayPalClient, "check_credentials", REAL_TOKEN_CHECK)
    respx.post(f"{API}/v1/oauth2/token").mock(return_value=httpx.Response(
        401, json={"name": "invalid_client", "debug_id": "DBG-1"}))
    text = summary(tmp_path, lines)
    assert "PayPal token: fail (HTTP 401 invalid_client debug_id DBG-1)" in text
    assert "Webhook ID: missing" in text and "AI provider: none (no keys)" in text
    assert "AG Studio key: missing" in text and "Production mode: off" in text
    assert "Public base URL: not set" in text
    assert "shh" not in text


def test_a_database_failure_does_not_stop_the_other_checks(tmp_path, monkeypatch):
    settings, store, paypal = app_parts(tmp_path)

    def down(self, *args):
        raise ConnectionError("could not connect to db.internal")

    monkeypatch.setattr(TimelineStore, "ping", down)
    monkeypatch.setattr(TimelineStore, "ai_state", down)
    monkeypatch.setattr(store.db, "schema_version", lambda: down(None))
    with respx.mock:
        mock_token()
        checks, latency = asyncio.run(run_checks(settings, store, paypal))
    assert latency is None and len(checks) == 8
    assert checks[0] == {"name": "Database", "detail": "fail (ConnectionError)", "ok": False}
    assert "db.internal" not in str(checks)


def test_the_app_runs_the_summary_when_it_starts(tmp_path, lines, monkeypatch):
    client, _ = make_client(tmp_path, login=False)
    run_startup(client)
    assert "Production mode: off" in lines.getvalue()


# ---- What failures log --------------------------------------------------------------------

@respx.mock
def test_paypal_failures_log_the_name_and_debug_id(tmp_path, lines):
    client, _ = make_client(tmp_path, login=False)
    mock_token()
    respx.post(f"{API}/v2/checkout/orders").mock(return_value=httpx.Response(422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "DBG-9", "message": "buyer@example.com is wrong"}))
    assert client.post("/api/orders", json={"item_id": "brand-kit"}).status_code == 502
    text = lines.getvalue()
    assert "PayPal POST /v2/checkout/orders failed: HTTP 422 UNPROCESSABLE_ENTITY debug_id DBG-9" in text
    assert "buyer@example.com" not in text


@respx.mock
def test_ai_failures_log_provider_status_and_class(tmp_path, lines):
    from app.ai import AIClient, AIError
    settings, store, _ = app_parts(tmp_path, gemini_api_key="g")
    ai = AIClient(settings, store)
    respx.post(GEMINI).mock(return_value=httpx.Response(429, json={"error": {"message": "private text"}}))

    async def call():
        await ai._post("gemini", [], "x", {})

    with pytest.raises(AIError):
        asyncio.run(call())
    assert "AI call failed: gemini, HTTP 429 rate_limited" in lines.getvalue()
    assert "private text" not in lines.getvalue()


@respx.mock
def test_webhook_verification_failures_log_the_event_type_and_reason(tmp_path, lines):
    client, _ = make_client(tmp_path, login=False)
    event = {"id": "WH-1", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {}}
    assert client.post("/webhooks/paypal", json=event).status_code == 400
    assert ("Webhook verification failed (CUSTOMER.DISPUTE.CREATED): missing signature headers"
            in lines.getvalue())

    mock_token()
    respx.post(f"{API}/v1/notifications/verify-webhook-signature").mock(
        return_value=httpx.Response(200, json={"verification_status": "FAILURE"}))
    headers = {h: "x" for h in ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                                "paypal-transmission-sig", "paypal-transmission-time")}
    assert client.post("/webhooks/paypal", json=event, headers=headers).status_code == 400
    assert "Webhook verification failed (CUSTOMER.DISPUTE.CREATED): PayPal says FAILURE" in lines.getvalue()


# ---- Settings -> System status ----------------------------------------------------------------

def test_system_status_needs_a_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    res = client.get("/api/system/status")
    assert res.status_code == 401 and "checks" not in res.text


@respx.mock
def test_system_status_reports_the_checks_and_last_activity(tmp_path):
    client, _ = make_client(tmp_path)
    setup_paypal()
    empty = client.get("/api/system/status").json()
    assert [c["name"] for c in empty["checks"]][:3] == ["Database", "Migrations", "PayPal token"]
    assert empty["last_webhook"] is None and empty["version"] == "local"
    assert empty["ai_provider"] == "gemini" and empty["db_latency_ms"] >= 0
    assert empty["last_sync"]["last_synced_at"] is None

    post_event(client, {"id": "WH-EVT-1", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}})
    status = client.get("/api/system/status").json()
    assert status["last_webhook"]["event_type"] == "PAYMENT.CAPTURE.COMPLETED"
    assert status["last_webhook"]["at"] > 0


@respx.mock
def test_a_webhook_that_fails_verification_is_not_the_last_webhook(tmp_path):
    client, _ = make_client(tmp_path)
    mock_token()
    client.post("/webhooks/paypal", json={"id": "WH-1", "event_type": "X.Y"})
    assert client.get("/api/system/status").json()["last_webhook"] is None


@respx.mock
def test_system_status_shows_the_version_and_no_values(tmp_path):
    client, _ = make_client(tmp_path, render_git_commit="abcdef1234567890",
                            gemini_key=SECRETS["gemini_api_key"])
    mock_token()
    res = client.get("/api/system/status")
    assert res.json()["version"] == "abcdef1"
    assert SECRETS["gemini_api_key"] not in res.text and "cid" not in res.text


# ---- No secret in any log line --------------------------------------------------------------

@respx.mock
def test_no_secret_reaches_a_log_line(tmp_path, lines, monkeypatch):
    """Everything that logs, with distinctive values in every secret setting."""
    monkeypatch.setattr(PayPalClient, "check_credentials", REAL_TOKEN_CHECK)
    client, settings = make_client(tmp_path, paypal_client_secret=SECRETS["paypal_client_secret"],
                                   gemini_key=SECRETS["gemini_api_key"], groq_key=SECRETS["groq_api_key"],
                                   ag_studio_license_key=SECRETS["ag_studio_license_key"],
                                   app_secret=SECRETS["app_secret"])
    answers = [httpx.Response(401, json={"name": "invalid_client"})]
    respx.post(f"{API}/v1/oauth2/token").mock(side_effect=lambda request: answers.pop() if answers else httpx.Response(
        200, json={"access_token": "access-token-value", "expires_in": 3600}))
    respx.get(f"{API}/v1/customer/disputes").mock(return_value=httpx.Response(500, json={"name": "INTERNAL"}))
    respx.post(GEMINI).mock(return_value=ai_reply("{}"))
    client.get("/api/system/status")
    client.post("/api/disputes/sync")
    client.get("/api/system/status")
    client.get("/api/insights/license")
    client.get("/api/config")
    client.post("/webhooks/paypal", json={"id": "1", "event_type": "X.Y"})
    run_startup(client)
    text = lines.getvalue()
    assert "clearline.http" in text and "Database: ok" in text  # it did log
    for value in [*SECRETS.values(), "access-token-value", settings.database_url or "\0"]:
        assert value not in text


def test_production_settings_refusal_shows_no_value():
    settings = Settings(_env_file=None, render="true", app_secret="tiny-value", paypal_client_secret="shh-value")
    with pytest.raises(SystemExit) as stopped:
        TestClient(create_app(settings))
    assert "tiny-value" not in str(stopped.value) and "shh-value" not in str(stopped.value)
