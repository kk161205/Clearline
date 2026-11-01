"""Production hardening: the client IP behind a proxy, production mode, the public base URL,
security headers and the Content-Security-Policy of each page type."""

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import forwarded_client
from tests.support import db_target, login_as, make_client

ROOT = Path(__file__).parent.parent
LONG_SECRET = "a-test-only-secret-of-at-least-32-characters"
SDK_SANDBOX = "https://www.sandbox.paypal.com"


def production(tmp_path, **overrides) -> dict:
    """Everything production mode asks for, as Settings(...) arguments."""
    return {"_env_file": None, "render": "true", "database_url": db_target(tmp_path),
            "app_secret": LONG_SECRET, "paypal_client_id": "cid", "paypal_client_secret": "shh",
            "paypal_webhook_id": "WH-1", **overrides}


def csp(response) -> dict[str, str]:
    """The Content-Security-Policy header as {directive: sources}."""
    pairs = [d.strip().split(" ", 1) for d in response.headers["content-security-policy"].split(";")]
    return {name: value for name, value in pairs}


# ---- The client's IP ---------------------------------------------------------------------

@pytest.mark.parametrize("header, hops, expected", [
    ("198.51.100.7", 1, "198.51.100.7"),
    ("1.2.3.4, 198.51.100.7", 1, "198.51.100.7"),   # what the client sent stays on the left
    ("1.2.3.4, 5.6.7.8, 198.51.100.7", 2, "5.6.7.8"),
    ("198.51.100.7", 2, None),                       # fewer hops than proxies: not trusted
    ("", 1, None),
])
def test_forwarded_client_counts_from_the_right(header, hops, expected):
    assert forwarded_client(header, hops) == expected


def test_spoofed_leading_values_do_not_change_the_rate_limit_key(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="clearline")
    client = TestClient(create_app(Settings(**production(tmp_path))))
    for attempt in range(21):  # the limit is 20 per IP; a fresh left-hand value on each try
        forwarded = f"10.9.8.{attempt}, 198.51.100.7"
        res = client.post("/api/login", json={"email": "a@example.com", "password": "nope"},
                          headers={"X-Forwarded-For": forwarded})
    assert res.status_code == 429
    assert "Login blocked: too many attempts from 198.51.100.7" in caplog.text
    assert "10.9.8." not in caplog.text


def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(tmp_path, caplog):
    client, _ = make_client(tmp_path, login=False)
    caplog.set_level(logging.INFO, logger="clearline")
    client.post("/api/login", json={"email": "a@example.com", "password": "nope"},
                headers={"X-Forwarded-For": "198.51.100.7"})
    assert "from testclient" in caplog.text and "198.51.100.7" not in caplog.text


# ---- Production mode ---------------------------------------------------------------------

def test_production_is_render_or_app_env(tmp_path):
    assert not Settings(_env_file=None).production
    assert Settings(_env_file=None, render="true").production
    assert Settings(_env_file=None, app_env="production").production


@pytest.mark.parametrize("setting, value, named", [
    ("database_url", None, "DATABASE_URL"),
    ("app_secret", None, "APP_SECRET"),
    ("app_secret", "too-short-value", "APP_SECRET"),
    ("paypal_client_id", "", "PAYPAL_CLIENT_ID"),
    ("paypal_client_secret", "", "PAYPAL_CLIENT_SECRET"),
    ("paypal_webhook_id", None, "PAYPAL_WEBHOOK_ID"),
    ("allow_unverified_webhooks", True, "ALLOW_UNVERIFIED_WEBHOOKS"),
])
def test_production_refuses_to_start_and_names_the_setting(tmp_path, setting, value, named):
    settings = Settings(**production(tmp_path, **{setting: value}))
    with pytest.raises(SystemExit) as stopped:
        create_app(settings)
    message = str(stopped.value)
    assert named in message and "Refusing to start" in message
    assert "too-short-value" not in message and "shh" not in message and LONG_SECRET not in message


def test_every_problem_is_named_at_once():
    message = Settings(_env_file=None, render="true", allow_unverified_webhooks=True).production_problems()
    assert len(message) == 6  # the five required settings plus the unverified-webhooks switch


def test_development_starts_without_the_production_settings(tmp_path):
    client = TestClient(create_app(Settings(_env_file=None, database_url=db_target(tmp_path))))
    assert client.get("/healthz").status_code == 200


def test_api_pages_exist_only_outside_production(tmp_path):
    dev, _ = make_client(tmp_path)
    assert dev.get("/openapi.json").status_code == 200 and dev.get("/docs").status_code == 200
    (tmp_path / "live").mkdir()
    settings = Settings(**production(tmp_path / "live"))
    client = TestClient(create_app(settings))
    login_as(client, settings)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_an_unexpected_error_shows_no_traceback(tmp_path, monkeypatch, caplog):
    client = TestClient(create_app(Settings(**production(tmp_path))))

    def boom(self, *args):
        raise RuntimeError("private detail: connection to db.internal failed")

    monkeypatch.setattr("app.main.Accounts.authenticate", boom)
    res = client.post("/api/login", json={"email": "a@example.com", "password": "nope"})
    assert res.status_code == 500
    assert res.json() == {"detail": "Something went wrong on our side."}
    assert "Traceback" not in res.text and "private detail" not in res.text
    assert res.headers["x-content-type-options"] == "nosniff"  # headers on errors too
    assert "RuntimeError at test_security.py" in caplog.text and "private detail" not in caplog.text


# ---- Public base URL -----------------------------------------------------------------------

def test_base_url_order_and_trailing_slash():
    assert Settings(_env_file=None).base_url == ""
    assert Settings(_env_file=None, render_external_url="https://x.onrender.com/").base_url == "https://x.onrender.com"
    assert Settings(_env_file=None, render_external_url="https://x.onrender.com",
                    public_base_url="https://shop.example.com").base_url == "https://shop.example.com"


def test_links_use_the_public_base_url(tmp_path):
    settings = Settings(**production(tmp_path, render_external_url="https://x.onrender.com"))
    client = TestClient(create_app(settings))
    login_as(client, settings)
    assert client.get("/api/agents").json()["mcp_url"] == "https://x.onrender.com/mcp"


def test_links_stay_relative_when_the_address_is_unknown(tmp_path):
    client, _ = make_client(tmp_path)
    assert client.get("/api/agents").json()["mcp_url"] == "/mcp"


# ---- Security headers ----------------------------------------------------------------------

def test_every_response_has_the_security_headers(tmp_path):
    client, _ = make_client(tmp_path)
    for path in ("/", "/seller", "/api/config", "/static/ui.css", "/healthz", "/nothing-here"):
        res = client.get(path)
        assert res.headers["x-content-type-options"] == "nosniff", path
        assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin", path
        assert csp(res)["frame-ancestors"] == "'none'", path
        assert "camera=()" in res.headers["permissions-policy"], path
        assert "strict-transport-security" not in res.headers, path  # production only


def test_hsts_is_on_in_production(tmp_path):
    client = TestClient(create_app(Settings(**production(tmp_path))))
    assert client.get("/healthz").headers["strict-transport-security"].startswith("max-age=")


def test_a_buyer_link_page_keeps_its_stricter_referrer_policy(tmp_path):
    from app.buyer import buyer_token
    from app.store import TimelineStore
    client, settings = make_client(tmp_path)
    TimelineStore(settings.database_target).add("O1", "payment_captured", {"capture_id": "C1"})
    res = client.get(f"/o/{buyer_token(settings.app_secret, 'O1')}")
    assert res.status_code == 200 and res.headers["referrer-policy"] == "no-referrer"


# ---- CSP per page type ---------------------------------------------------------------------

@pytest.fixture
def seller(tmp_path):
    return make_client(tmp_path)[0]


def test_default_policy_is_self_only(seller):
    policy = csp(seller.get("/seller"))
    assert policy["default-src"] == policy["script-src"] == policy["connect-src"] == "'self'"
    assert policy["object-src"] == "'none'" and policy["base-uri"] == "'self'"
    assert "cdn.jsdelivr" not in str(policy) and "paypal" not in str(policy)


def test_checkout_policy_lists_what_the_paypal_sdk_loads(seller):
    policy = csp(seller.get("/"))
    for directive in ("script-src", "connect-src", "frame-src", "img-src"):
        assert SDK_SANDBOX in policy[directive], directive
    assert "https://www.paypalobjects.com" in policy["font-src"]
    assert "https://www.paypal.com" not in str(policy)  # only the environment in use


def test_insights_adds_only_the_cdn(seller):
    policy = csp(seller.get("/insights"))
    assert policy["script-src"] == "'self' https://cdn.jsdelivr.net"
    assert "paypal" not in str(policy)


def test_buyer_order_page_loads_no_paypal_code(seller):
    assert not re.search(r"paypal\.com|paypalobjects", (ROOT / "app/static/order.html").read_text(encoding="utf-8"))
    assert not re.search(r"paypal\.com|paypalobjects", (ROOT / "app/static/pages/order.js").read_text(encoding="utf-8"))


def test_no_policy_allows_inline_scripts_or_eval(seller):
    for path in ("/", "/seller", "/dashboard", "/insights", "/agents", "/login"):
        script = csp(seller.get(path))["script-src"]
        assert "unsafe-inline" not in script and "unsafe-eval" not in script, path


def test_no_page_has_an_inline_script_or_handler():
    for page in (ROOT / "app/static").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\ssrc=)", html), page.name
        assert not re.search(r"<[a-z][^>]*\son[a-z]+=", html), page.name


def test_public_pages_can_load_their_scripts_without_a_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    for name in ("buyer", "order", "login", "setup"):
        res = client.get(f"/static/pages/{name}.js")
        assert res.status_code == 200 and "javascript" in res.headers["content-type"], name
    assert client.get("/static/pages/seller.js", follow_redirects=False).status_code == 303


def test_page_scripts_are_served_only_by_name(seller):
    assert seller.get("/static/pages/seller.js").status_code == 200
    assert seller.get("/static/pages/nope.js").status_code == 404
