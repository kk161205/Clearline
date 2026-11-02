"""Production hardening: the client IP behind a proxy, production mode, the public base URL,
security headers and the Content-Security-Policy of each page type."""

import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from app.config import Settings
from app.main import create_app
from app.buyer import buyer_token
from app.security import describe_ip, forwarded_client, request_view
from app.store import TimelineStore
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

LIVE_CHAIN = "106.219.147.116, 104.23.213.7, 10.29.147.20"  # measured on Render, 2 Nov 2026


@pytest.mark.parametrize("header, expected", [
    (LIVE_CHAIN, "106.219.147.116"),
    ("1.2.3.4, " + LIVE_CHAIN, "106.219.147.116"),                 # spoofed on the left
    ("8.8.8.8, 1.2.3.4, " + LIVE_CHAIN, "106.219.147.116"),
    ("106.219.147.116, 104.23.213.7, 10.29.147.20, 10.31.0.171", "106.219.147.116"),  # two internal hops
    ("106.219.147.116, 10.29.147.20", "106.219.147.116"),          # no Cloudflare in front
    ("106.219.147.116", "106.219.147.116"),
    ("2a00:1450:4001:81b::200e, 104.23.213.7, 10.29.147.20", "2a00:1450:4001:81b::200e"),  # IPv6 client
    ("1.2.3.4, 2a00:1450:4001:81b::200e, 104.23.213.7, 10.29.147.20", "2a00:1450:4001:81b::200e"),
    ("106.219.147.116, 2606:4700:10::6816:1, 10.29.147.20", "106.219.147.116"),  # Cloudflare IPv6 edge
    ("106.219.147.116, 2a06:98c0:3600::103, fd00::1", "106.219.147.116"),
    ("::ffff:106.219.147.116, ::ffff:104.23.213.7, 10.29.147.20", "106.219.147.116"),  # IPv4 as IPv6
    ("104.28.215.83, 104.23.213.7, 10.29.147.20", "104.28.215.83"),    # a client on WARP: Cloudflare's own range
    ("1.2.3.4, 104.28.215.83, 104.23.213.7, 10.29.147.20", "104.28.215.83"),  # ... can't spoof past that
    ("10.0.0.1, 192.168.1.1, 100.64.0.1, 127.0.0.1, 169.254.0.1", None),      # only private hops
    ("1.2.3.4, 10.9.9.9, 104.23.213.7, 10.29.147.20", None),       # a private "client": don't walk on
    ("104.23.213.7, 10.29.147.20", None),                          # nothing left of the edge
    ("garbage, 104.23.213.7, 10.29.147.20", None),
    ("1.2.3.4, garbage, 10.29.147.20", None),
    ("1.2.3.4:5678, 104.23.213.7", None),
    ("", None),
])
def test_forwarded_client_finds_the_address_cloudflare_saw(header, expected):
    assert forwarded_client(header) == expected


def proxied(tmp_path, **overrides):
    return TestClient(create_app(Settings(**production(tmp_path, **overrides))))


def login_from(client, forwarded=None, **headers):
    if forwarded is not None:
        headers["X-Forwarded-For"] = forwarded
    return client.post("/api/login", json={"email": "a@example.com", "password": "nope"}, headers=headers)


def test_spoofed_leading_values_do_not_change_the_rate_limit_key(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="clearline")
    client = proxied(tmp_path)
    for attempt in range(21):  # the limit is 20 per IP; a fresh left-hand value on each try
        res = login_from(client, f"10.9.8.{attempt}, 7.7.7.{attempt}, " + LIVE_CHAIN)
    assert res.status_code == 429
    assert "Login blocked: too many attempts from 106.219.147.116" in caplog.text
    assert "7.7.7." not in caplog.text


def test_different_clients_have_different_rate_limit_keys(tmp_path):
    client = proxied(tmp_path)
    for _ in range(20):
        login_from(client, LIVE_CHAIN)
    assert login_from(client, LIVE_CHAIN).status_code == 429
    assert login_from(client, "49.36.1.2, 104.23.213.7, 10.29.147.20").status_code == 401


def test_a_chain_with_nothing_left_is_unknown_and_never_uvicorns_value(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="clearline")
    login_from(proxied(tmp_path), "10.0.0.1, 10.0.0.2")
    assert "from unknown" in caplog.text and "testclient" not in caplog.text


def test_cf_connecting_ip_is_never_the_source(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="clearline")
    login_from(proxied(tmp_path), LIVE_CHAIN, **{"CF-Connecting-IP": "9.9.9.9", "True-Client-IP": "8.8.4.4"})
    assert "from 106.219.147.116" in caplog.text and "from 9.9.9.9" not in caplog.text


def test_a_differing_cf_connecting_ip_warns_once_and_masked(tmp_path, caplog):
    client = proxied(tmp_path)
    for _ in range(3):
        login_from(client, LIVE_CHAIN, **{"CF-Connecting-IP": "9.9.9.9"})
    warnings = [r.getMessage() for r in caplog.records if "Client IP check" in r.getMessage()]
    assert len(warnings) == 1
    assert "9.9.9.x" in warnings[0] and "106.219.147.x" in warnings[0]
    assert "9.9.9.9" not in warnings[0] and "106.219.147.116" not in warnings[0]


def test_a_matching_cf_connecting_ip_is_quiet(tmp_path, caplog):
    login_from(proxied(tmp_path), LIVE_CHAIN, **{"CF-Connecting-IP": "106.219.147.116"})
    assert "Client IP check" not in caplog.text


def test_only_one_function_reads_uvicorns_client():
    uses = []
    for path in (ROOT / "app").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        uses += [(path.name, text[:m.start()].rfind("\ndef ")) for m in
                 re.finditer(r"request\.client\b|\.client\.host|scope\[[\"']client[\"']\]|scope\.get\([\"']client", text)]
    security = (ROOT / "app/security.py").read_text(encoding="utf-8")
    start = security.index("\ndef peer_address(")
    assert {name for name, _ in uses} == {"security.py"}, uses  # only peer_address, below
    assert {at for _, at in uses} == {start}, uses
    callers = (ROOT / "app/main.py").read_text(encoding="utf-8").count("peer_address(request)")
    assert callers == 2  # client_ip() outside Render, and the System status row


def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(tmp_path, caplog):
    client, _ = make_client(tmp_path, login=False)
    caplog.set_level(logging.INFO, logger="clearline")
    client.post("/api/login", json={"email": "a@example.com", "password": "nope"},
                headers={"X-Forwarded-For": "106.219.147.116"})
    assert "from testclient" in caplog.text and "106.219.147.116" not in caplog.text


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
    policy = csp(seller.get("/shop"))
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
    for path in ("/", "/shop", "/seller", "/dashboard", "/insights", "/agents", "/login"):
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


# ---- How a request arrived: Settings -> System status ------------------------------------------

@pytest.mark.parametrize("value, ip, kind", [
    ("106.219.147.116", "106.219.147.x", "public"),
    ("104.23.213.7", "104.23.213.x", "Cloudflare"),
    ("10.26.23.136", "10.26.23.x", "private"),
    ("192.168.1.9", "192.168.1.x", "private"),
    ("172.16.0.1", "172.16.0.x", "private"),
    ("100.64.0.1", "100.64.0.x", "private"),     # shared address space (RFC 6598)
    ("127.0.0.1", "127.0.0.x", "private"),
    ("169.254.1.1", "169.254.1.x", "private"),
    ("2606:4700:4700::1111", "2606:4700:4700:x", "Cloudflare"),
    ("2a00:1450:4001::1", "2a00:1450:4001:x", "public"),
    ("fd00::1", "fd00:0000:0000:x", "private"),
    ("not-an-address", "not an IP", "invalid"),
    ("<script>alert(1)</script>", "not an IP", "invalid"),
])
def test_addresses_are_masked_and_classified(value, ip, kind):
    assert describe_ip(value) == {"ip": ip, "kind": kind}


def test_request_view_lists_hops_and_header_names_only():
    headers = Headers(raw=[(b"x-forwarded-for", b"106.219.147.116, 10.31.0.171"), (b"x-forwarded-for", b"10.26.23.136"),
                           (b"cf-connecting-ip", b"106.219.147.116"), (b"x-real-ip", b"1.2.3.4")])
    view = request_view(headers, "10.0.0.5", "10.26.23.136")
    assert view["header_lines"] == 2 and [h["kind"] for h in view["hops"]] == ["public", "private", "private"]
    assert view["client"] == {"ip": "10.26.23.x", "kind": "private"}
    assert view["peer"] == {"ip": "10.0.0.x", "kind": "private"}
    assert view["headers"] == ["CF-Connecting-IP", "X-Real-IP"]
    assert "106.219.147.116" not in str(view) and "1.2.3.4" not in str(view)


def test_system_status_shows_how_the_request_arrived_to_the_seller_only(tmp_path):
    settings = Settings(**production(tmp_path))
    client = TestClient(create_app(settings))
    assert client.get("/api/system/status").status_code == 401
    login_as(client, settings)
    res = client.get("/api/system/status", headers={"X-Forwarded-For": LIVE_CHAIN})
    request = res.json()["request"]
    assert request["client"] == {"ip": "106.219.147.x", "kind": "public"}
    assert [h["kind"] for h in request["hops"]] == ["public", "Cloudflare", "private"]
    assert "106.219.147.116" not in res.text


# ---- HEAD ----------------------------------------------------------------------------------

def test_head_answers_like_get_with_no_body_on_public_pages(tmp_path):
    client, settings = make_client(tmp_path)
    TimelineStore(settings.database_target).add("O1", "payment_captured", {"capture_id": "C1"})
    client.cookies.clear()
    for path in ("/", "/shop", "/login", "/healthz", f"/o/{buyer_token(settings.app_secret, 'O1')}", "/static/ui.css"):
        res = client.head(path)
        assert res.status_code == 200 and res.content == b"", path
        assert res.headers["content-type"] == client.get(path).headers["content-type"], path
        assert res.headers["x-content-type-options"] == "nosniff" and res.headers["x-request-id"], path


def test_head_on_seller_pages_follows_the_login_rules(seller, tmp_path):
    for path in ("/seller", "/dashboard", "/insights", "/agents"):
        res = seller.head(path)
        assert res.status_code == 200 and res.content == b"", path
    seller.post("/api/logout")
    seller.cookies.clear()
    res = seller.head("/seller", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/login"


def test_head_on_an_unknown_page_is_404_and_the_api_is_unchanged(seller):
    assert seller.head("/nothing-here").status_code == 404
    assert seller.head("/api/orders").status_code == 405
    assert seller.head("/webhooks/paypal").status_code == 405


def test_head_is_logged_as_head(tmp_path, caplog):
    client, _ = make_client(tmp_path, login=False)
    caplog.set_level(logging.INFO, logger="clearline")
    client.head("/")
    assert "HEAD / 200" in caplog.text
