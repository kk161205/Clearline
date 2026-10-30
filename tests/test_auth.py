"""Seller accounts: /setup, login, lockout, DB sessions, Origin check, password changes, the
users CLI and the retired env settings. Protected vs public routes. No network."""

import io
import logging
import re

import pytest
import respx
from argon2 import PasswordHasher

import app.accounts as accounts_module
from app.accounts import SESSION_COOKIE, Accounts, password_problem, setup_code_matches
from app.auth import PUBLIC_ROUTES, is_public
from app.config import Settings
from app.store import TimelineStore
from app.users import main as users_cli
from tests.support import (DISPUTE_ID, SELLER_EMAIL, TEST_PASSWORD, DisputeOnPayPal, db_settings,
                           db_target, login_as, make_client, open_dispute, query, setup_paypal,
                           shipped_order)

GENERIC = "Email or password is incorrect."
DAY = 86400

SELLER_ROUTES = [
    ("GET", "/seller"), ("GET", "/api/orders"), ("GET", "/api/orders/ORDER1/timeline"),
    ("POST", "/api/orders/ORDER1/tracking"), ("GET", "/api/orders/ORDER1/buyer"),
    ("POST", "/api/orders/ORDER1/refund"), ("POST", "/api/orders/ORDER1/requests/R/reply"),
    ("GET", "/api/stats"), ("POST", "/api/disputes/sync"), ("GET", "/api/disputes/sync/status"),
    ("POST", f"/api/disputes/{DISPUTE_ID}/act"), ("GET", f"/api/disputes/{DISPUTE_ID}/recommendation"),
    ("POST", f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence"),
    ("GET", "/api/ai/status"), ("POST", "/api/ai/reset"), ("GET", "/docs"), ("GET", "/openapi.json"),
    ("GET", "/agents"), ("GET", "/api/agents"), ("GET", "/api/policy"), ("PUT", "/api/policy"),
    ("GET", "/insights"), ("GET", "/api/insights/layout"), ("PUT", "/api/insights/layout"),
    ("GET", "/api/account"), ("POST", "/api/account/password"),
    ("POST", "/api/account/sign-out-everywhere"),
    ("GET", "/api/agent-keys"), ("POST", "/api/agent-keys"), ("DELETE", "/api/agent-keys/1"),
]


class Clock:
    """Frozen time for app.accounts (sessions, lockouts)."""

    def __init__(self, monkeypatch, start=1_800_000_000.0):
        self.t = start
        monkeypatch.setattr(accounts_module, "now", lambda: self.t)

    def advance(self, seconds):
        self.t += seconds


def new_client(tmp_path, caplog=None, **overrides):
    """A fresh app on the test DB, nobody logged in."""
    if caplog is not None:
        caplog.set_level(logging.INFO, logger="clearline")
    return make_client(tmp_path, login=False, **overrides)


def with_account(tmp_path, email=SELLER_EMAIL, password=TEST_PASSWORD, **overrides):
    client, settings = new_client(tmp_path, **overrides)
    user_id = Accounts(TimelineStore(settings.database_target)).create_user(email, password)
    return client, settings, user_id


def login(client, email=SELLER_EMAIL, password=TEST_PASSWORD, **kw):
    return client.post("/api/login", json={"email": email, "password": password}, **kw)


def setup_code(caplog) -> str:
    return re.findall(r"use setup code: ([A-Z0-9-]{14})", caplog.text)[-1]


# ---- Protected vs public routes ---------------------------------------------------------

@pytest.mark.parametrize("method,path", SELLER_ROUTES)
def test_seller_routes_need_a_session(tmp_path, method, path):
    client, _, _ = with_account(tmp_path)
    resp = client.request(method, path, follow_redirects=False, json={})
    if method == "GET" and not path.startswith("/api/"):
        assert resp.status_code == 303 and resp.headers["location"] == "/login"  # pages redirect
    else:
        assert resp.status_code == 401 and resp.json()["detail"] == "Log in as the seller first."


def test_pages_send_you_to_setup_while_there_is_no_account(tmp_path):
    client, _ = new_client(tmp_path)
    assert client.get("/seller", follow_redirects=False).headers["location"] == "/setup"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/setup"
    assert client.get("/api/orders").status_code == 401


def test_public_routes_are_exactly_the_agreed_ones():
    assert len(PUBLIC_ROUTES) == 20  # adding a public route should be a deliberate change
    for method, path in [("GET", "/"), ("GET", "/api/config"), ("POST", "/api/orders"),
                         ("POST", "/api/orders/X/capture"), ("GET", "/o/abc"), ("GET", "/api/o/abc"),
                         ("GET", "/api/o/abc/status"), ("POST", "/api/o/abc/requests"),
                         ("POST", "/webhooks/paypal"), ("GET", "/login"), ("POST", "/api/login"), ("POST", "/api/logout"),
                         ("GET", "/setup"), ("POST", "/api/setup"), ("HEAD", "/"), ("GET", "/healthz"),
                         ("POST", "/mcp")]:  # MCP: scoped by order link / agent key
        assert is_public(method, path), (method, path)
    for method, path in SELLER_ROUTES + [("GET", "/api/orders/X"), ("POST", "/api/o/abc"),
                                         ("GET", "/o/abc/extra"), ("DELETE", "/api/orders"),
                                         ("GET", "/api/signup"), ("POST", "/api/users")]:
        assert not is_public(method, path), (method, path)


@respx.mock
def test_checkout_buyer_page_and_webhook_work_without_login(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    seller, _ = make_client(tmp_path)  # logged in: sets up the order
    shipped_order(seller)
    link = seller.get("/api/orders/ORDER1/buyer").json()["link"]

    public, _ = new_client(tmp_path)  # same DB, no session
    assert public.get("/").status_code == 200
    assert public.get("/api/config").status_code == 200
    assert public.get(link).status_code == 200
    assert public.get(f"/api/o{link[2:]}").json()["order_ref"] == "ORDER1"[-6:]
    assert open_dispute(public).json() == {"status": "accepted"}  # webhook: no session needed


# ---- /setup ------------------------------------------------------------------------------

def test_setup_needs_the_logged_code_and_works_once(tmp_path, caplog):
    client, settings = new_client(tmp_path, caplog)
    code = setup_code(caplog)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    assert client.get("/setup").status_code == 200
    body = {"email": "Owner@Example.com", "password": "a long enough passphrase",
            "confirm": "a long enough passphrase"}

    assert client.post("/api/setup", json={**body, "code": ""}).status_code == 403
    wrong = client.post("/api/setup", json={**body, "code": "AAAA-BBBB-CCCC"})
    assert wrong.status_code == 403 and "setup code" in wrong.json()["detail"]
    mismatch = client.post("/api/setup", json={**body, "code": code, "confirm": "something else!!"})
    assert mismatch.status_code == 422
    weak = client.post("/api/setup", json={**body, "code": code, "password": "password1234",
                                           "confirm": "password1234"})
    assert weak.status_code == 422
    assert TimelineStore(settings.database_target).count_users() == 0

    caplog.clear()
    made = client.post("/api/setup", json={**body, "code": code.lower()})  # case doesn't matter
    assert made.status_code == 200
    assert client.get("/api/account").json()["email"] == "owner@example.com"  # logged in, lowercased
    assert code not in caplog.text and "Owner" not in caplog.text and "owner@" not in caplog.text

    # Now there's an account: /setup is gone and the code is dead, even from another client.
    other, _ = new_client(tmp_path)
    assert client.get("/setup").status_code == 404 and other.get("/setup").status_code == 404
    again = other.post("/api/setup", json={**body, "code": code, "email": "second@example.com"})
    assert again.status_code == 404
    assert TimelineStore(settings.database_target).count_users() == 1


def test_setup_code_is_new_each_start_and_not_logged_once_an_account_exists(tmp_path, caplog):
    new_client(tmp_path, caplog)
    first = setup_code(caplog)
    caplog.clear()
    new_client(tmp_path, caplog)
    assert setup_code(caplog) != first

    Accounts(TimelineStore(db_target(tmp_path))).create_user(SELLER_EMAIL, TEST_PASSWORD)
    caplog.clear()
    client, _ = new_client(tmp_path, caplog)
    assert "setup code" not in caplog.text
    assert client.get("/setup").status_code == 404


def test_setup_code_dies_when_an_account_appears_another_way(tmp_path, caplog):
    client, settings = new_client(tmp_path, caplog)
    code = setup_code(caplog)
    Accounts(TimelineStore(settings.database_target)).create_user(SELLER_EMAIL, TEST_PASSWORD)  # CLI
    res = client.post("/api/setup", json={"code": code, "email": "x@example.com",
                                          "password": "another long passphrase",
                                          "confirm": "another long passphrase"})
    assert res.status_code == 404


def test_setup_attempts_are_rate_limited(tmp_path):
    client, _ = new_client(tmp_path)
    body = {"code": "AAAA-BBBB-CCCC", "email": "x@example.com", "password": "a long enough passphrase",
            "confirm": "a long enough passphrase"}
    codes = [client.post("/api/setup", json=body).status_code for _ in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]


def test_setup_code_comparison():
    assert setup_code_matches(" abcd-efgh-jkmn ", "ABCD-EFGH-JKMN")
    assert not setup_code_matches("ABCD-EFGH-JKMP", "ABCD-EFGH-JKMN")
    assert not setup_code_matches("ABCD-EFGH-JKMN", None)  # no code once an account exists
    assert not setup_code_matches("", "ABCD-EFGH-JKMN")


# ---- Passwords ---------------------------------------------------------------------------

def test_password_policy():
    email = "maria.lopez@example.com"
    assert "12" in password_problem("short-pass", email)
    assert password_problem("x" * 129, email)
    assert password_problem("password1234", email)  # a common password
    assert password_problem("PASSWORD1234", email)  # case doesn't help
    assert password_problem("i-am-Maria.Lopez-2026", email)  # contains the email's local part
    assert password_problem("correct horse battery", email) is None  # no other complexity rules
    assert password_problem("x" * 128, email) is None and password_problem("x" * 12, email) is None


def test_passwords_are_argon2id_and_rehashed_when_params_change(tmp_path, monkeypatch):
    client, settings, user_id = with_account(tmp_path)
    store = TimelineStore(settings.database_target)
    before = store.user_by_id(user_id)["password_hash"]
    assert before.startswith("$argon2id$") and TEST_PASSWORD not in before

    monkeypatch.setattr(accounts_module, "HASHER", PasswordHasher(time_cost=2, memory_cost=2048, parallelism=1))
    assert login(client).status_code == 200
    after = store.user_by_id(user_id)["password_hash"]
    assert after != before and "m=2048,t=2,p=1" in after
    assert login(new_client(tmp_path)[0]).status_code == 200  # still the same password


def test_library_defaults_are_used_outside_tests():
    import importlib

    fresh = importlib.reload(accounts_module)  # undoes the test fixture's fast profile
    try:
        default = PasswordHasher()
        assert (fresh.HASHER.time_cost, fresh.HASHER.memory_cost, fresh.HASHER.parallelism) == \
            (default.time_cost, default.memory_cost, default.parallelism)
    finally:
        fresh.HASHER = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)


# ---- Login, lockout, rate limit --------------------------------------------------------

def test_login_ok_bad_and_unknown_email_look_the_same(tmp_path, monkeypatch, caplog):
    client, settings, user_id = with_account(tmp_path, caplog=caplog)
    verified = []
    real_verify = accounts_module._verify
    monkeypatch.setattr(accounts_module, "_verify", lambda h, p: verified.append(h) or real_verify(h, p))

    bad = login(client, password="not the password")
    unknown = login(client, email="nobody@example.com")
    assert bad.status_code == unknown.status_code == 401
    assert bad.json() == unknown.json() == {"detail": GENERIC}
    assert len(verified) == 2  # the unknown email was still checked against a (dummy) hash
    assert client.get("/api/orders").status_code == 401

    ok = login(client, email="  SELLER@example.COM ")  # emails are matched lowercased
    assert ok.status_code == 200 and client.get("/api/orders").status_code == 200
    assert TimelineStore(settings.database_target).user_by_id(user_id)["last_login_at"]

    assert f"Login ok: user {user_id} from testclient" in caplog.text
    assert f"Login failed: user {user_id} (bad) from testclient" in caplog.text
    assert "Login failed: user unknown (bad) from testclient" in caplog.text
    for secret in (SELLER_EMAIL, "nobody@", "not the password", TEST_PASSWORD, "$argon2",
                   client.cookies.get(SESSION_COOKIE)):
        assert secret not in caplog.text


def test_five_failures_lock_the_account_for_15_minutes(tmp_path, monkeypatch, caplog):
    clock = Clock(monkeypatch)
    client, settings, user_id = with_account(tmp_path, caplog=caplog)
    for _ in range(5):
        assert login(client, password="wrong password!").json() == {"detail": GENERIC}
    locked = login(client)  # the right password, but locked: same answer
    assert locked.status_code == 401 and locked.json() == {"detail": GENERIC}
    assert f"user {user_id} is locked" in caplog.text

    clock.advance(14 * 60)
    assert login(client).status_code == 401
    clock.advance(61)
    assert login(client).status_code == 200  # unlocked after 15 minutes
    user = TimelineStore(settings.database_target).user_by_id(user_id)
    assert user["failed_logins"] == 0 and user["locked_until"] is None


def test_login_is_rate_limited_per_ip(tmp_path):
    client, _, _ = with_account(tmp_path)
    for i in range(20):  # different unknown emails: no account lockout involved
        assert login(client, email=f"guess{i}@example.com").status_code == 401
    blocked = login(client)  # even the right password, from this IP
    assert blocked.status_code == 429 and blocked.json()["detail"] == "Too many attempts, try again later."


def test_disabled_account_cannot_log_in(tmp_path):
    client, settings, user_id = with_account(tmp_path)
    Accounts(TimelineStore(settings.database_target)).set_disabled(user_id, True)
    assert login(client).json() == {"detail": GENERIC}


# ---- Sessions ----------------------------------------------------------------------------

def test_session_cookie_flags_and_only_a_hash_is_stored(tmp_path):
    client, settings, user_id = with_account(tmp_path)
    resp = login(client)
    cookie = resp.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie and "secure" not in cookie  # plain http
    token = client.cookies.get(SESSION_COOKIE)
    [row] = query(settings.database_target, "SELECT id, user_id FROM sessions")
    sid, owner = row["id"], row["user_id"]
    assert owner == user_id and sid == Accounts.session_id(token) and token not in sid


def test_x_forwarded_proto_counts_only_with_trust_proxy(tmp_path):
    client, _, _ = with_account(tmp_path)
    resp = login(client, headers={"X-Forwarded-Proto": "https"})
    assert "secure" not in resp.headers["set-cookie"].lower()  # TRUST_PROXY is off by default

    proxied, _ = new_client(tmp_path, trust_proxy=True)
    resp = login(proxied, headers={"X-Forwarded-Proto": "https"})  # what Render/ngrok send
    assert "secure" in resp.headers["set-cookie"].lower()


def test_logout_deletes_the_session(tmp_path):
    client, settings, _ = with_account(tmp_path)
    login(client)
    stolen = client.cookies.get(SESSION_COOKIE)
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/orders").status_code == 401
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, stolen)  # an old copy of the cookie is dead too
    assert client.get("/api/orders").status_code == 401
    assert query(settings.database_target, "SELECT COUNT(*) AS n FROM sessions") == [{"n": 0}]


def test_logging_in_again_rotates_the_session(tmp_path):
    client, _, _ = with_account(tmp_path)
    login(client)
    first = client.cookies.get(SESSION_COOKIE)
    login(client)
    second = client.cookies.get(SESSION_COOKIE)
    assert second != first
    assert client.get("/api/orders").status_code == 200
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, first)
    assert client.get("/api/orders").status_code == 401


def test_sessions_expire_after_7_idle_days_or_30_days_in_all(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client, settings, _ = with_account(tmp_path)
    login(client)
    clock.advance(7 * DAY + 1)
    assert client.get("/api/orders").status_code == 401  # idle too long

    login(client)
    for _ in range(4):  # used every 6 days: still alive at day 24
        clock.advance(6 * DAY)
        assert client.get("/api/orders").status_code == 200
    clock.advance(6 * DAY)  # day 30
    assert client.get("/api/orders").status_code == 401  # absolute limit, however active


def test_cleanup_removes_expired_sessions(tmp_path, monkeypatch):
    clock = Clock(monkeypatch)
    client, settings, _ = with_account(tmp_path)
    login(client)
    login(new_client(tmp_path)[0])
    accounts = Accounts(TimelineStore(settings.database_target))
    assert accounts.cleanup() == 0
    clock.advance(8 * DAY)
    assert accounts.cleanup() == 2


def test_foreign_origin_is_refused_for_state_changes(tmp_path):
    client, _, _ = with_account(tmp_path)
    assert login(client, headers={"Origin": "https://evil.example"}).status_code == 403
    assert login(client, headers={"Origin": "http://testserver"}).status_code == 200

    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/agent-keys", json={"name": "x"}, headers=evil).status_code == 403
    assert client.put("/api/policy", json={}, headers=evil).status_code == 403
    assert client.delete("/api/insights/layout", headers=evil).status_code == 403
    assert client.post("/api/logout", headers=evil).status_code == 403
    assert client.get("/api/orders", headers=evil).status_code == 200  # reads aren't state changes
    same = client.post("/api/agent-keys", json={"name": "x"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 200
    assert client.post("/api/agent-keys", json={"name": "y"}).status_code == 200  # no Origin: not a browser


# ---- Settings -> Account -----------------------------------------------------------------

def test_change_password_ends_the_other_sessions(tmp_path):
    here, settings, _ = with_account(tmp_path)
    login(here)
    elsewhere, _ = new_client(tmp_path)
    login(elsewhere)
    new = "a different long passphrase"

    wrong = here.post("/api/account/password", json={"current": "nope", "password": new, "confirm": new})
    assert wrong.status_code == 403
    assert here.post("/api/account/password", json={"current": TEST_PASSWORD, "password": new,
                                                     "confirm": new + "x"}).status_code == 422
    assert here.post("/api/account/password", json={"current": TEST_PASSWORD, "password": "short",
                                                     "confirm": "short"}).status_code == 422
    assert elsewhere.get("/api/orders").status_code == 200

    done = here.post("/api/account/password", json={"current": TEST_PASSWORD, "password": new, "confirm": new})
    assert done.status_code == 200
    assert here.get("/api/orders").status_code == 200  # this device stays signed in
    assert elsewhere.get("/api/orders").status_code == 401  # every other one is out
    fresh, _ = new_client(tmp_path)
    assert login(fresh).status_code == 401 and login(fresh, password=new).status_code == 200


def test_sign_out_everywhere_and_the_account_email(tmp_path):
    here, _, user_id = with_account(tmp_path)
    login(here)
    elsewhere, _ = new_client(tmp_path)
    login(elsewhere)
    assert here.get("/api/account").json() == {"id": user_id, "email": SELLER_EMAIL}
    assert here.post("/api/account/sign-out-everywhere").status_code == 200
    assert here.get("/api/orders").status_code == 401 and elsewhere.get("/api/orders").status_code == 401


# ---- python -m app.users ------------------------------------------------------------------

def cli(tmp_path, monkeypatch, argv, passwords=()):
    answers = iter(passwords)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    return users_cli(argv, settings=Settings(**db_settings(tmp_path)))


def test_cli_create_reset_disable_enable_and_list(tmp_path, monkeypatch, capsys):
    pw, pw2 = "cli made passphrase", "cli reset passphrase"
    assert cli(tmp_path, monkeypatch, ["create", "--email", "Shop.Owner@Example.com"],
               ["mismatch passphrase", "other passphrase!", "short", "short", pw, pw]) == 0
    out = capsys.readouterr().out
    assert "don't match" in out and "Created seller account" in out and pw not in out
    store = TimelineStore(db_target(tmp_path))
    user = store.user_by_email("shop.owner@example.com")
    assert user["password_hash"].startswith("$argon2id$")
    with pytest.raises(SystemExit, match="already exists"):
        cli(tmp_path, monkeypatch, ["create", "--email", "shop.owner@example.com"], [pw, pw])

    client, _ = new_client(tmp_path)
    assert login(client, "shop.owner@example.com", pw).status_code == 200
    assert cli(tmp_path, monkeypatch, ["reset-password", "--email", "shop.owner@example.com"], [pw2, pw2]) == 0
    assert client.get("/api/orders").status_code == 401  # reset ends every session
    assert login(client, "shop.owner@example.com", pw).status_code == 401
    assert login(client, "shop.owner@example.com", pw2).status_code == 200

    assert cli(tmp_path, monkeypatch, ["disable", "--email", "shop.owner@example.com"]) == 0
    assert client.get("/api/orders").status_code == 401  # disable kills sessions
    assert login(client, "shop.owner@example.com", pw2).status_code == 401
    assert cli(tmp_path, monkeypatch, ["list"]) == 0
    listed = capsys.readouterr().out
    assert "shop.owner@example.com" in listed and "disabled" in listed and "$argon2" not in listed

    assert cli(tmp_path, monkeypatch, ["enable", "--email", "shop.owner@example.com"]) == 0
    assert login(client, "shop.owner@example.com", pw2).status_code == 200
    with pytest.raises(SystemExit, match="No account"):
        cli(tmp_path, monkeypatch, ["disable", "--email", "nobody@example.com"])


def test_cli_password_stdin_for_scripts(tmp_path, monkeypatch, capsys):
    def no_prompt(prompt=""):
        raise AssertionError("prompted although --password-stdin was given")

    monkeypatch.setattr("getpass.getpass", no_prompt)
    settings = Settings(**db_settings(tmp_path))
    first, second = "  spaced stdin passphrase  ", "second stdin passphrase"
    monkeypatch.setattr("sys.stdin", io.StringIO(first + "\r\nignored second line\n"))
    assert users_cli(["create", "--email", "script@example.com", "--password-stdin"], settings=settings) == 0
    client, _ = new_client(tmp_path)
    assert login(client, "script@example.com", first).status_code == 200  # spaces kept, CRLF dropped

    monkeypatch.setattr("sys.stdin", io.StringIO(second + "\n"))
    assert users_cli(["reset-password", "--email", "script@example.com", "--password-stdin"], settings=settings) == 0
    assert client.get("/api/orders").status_code == 401  # signed out everywhere
    assert login(client, "script@example.com", second).status_code == 200

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    with pytest.raises(SystemExit, match="12"):
        users_cli(["create", "--email", "weak@example.com", "--password-stdin"], settings=settings)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        users_cli(["create", "--email", "empty@example.com", "--password-stdin"], settings=settings)
    assert [u["email"] for u in TimelineStore(settings.database_target).list_users()] == ["script@example.com"]
    out = capsys.readouterr().out
    assert first.strip() not in out and second not in out


# ---- The retired SELLER_PASSWORD / MERCHANT_API_KEY ------------------------------------------

def test_old_env_settings_are_ignored_and_warned_about_by_name_only(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("SELLER_PASSWORD", "old-shared-password-value")
    monkeypatch.setenv("MERCHANT_API_KEY", "old-merchant-key-value")
    client, settings, _ = with_account(tmp_path, caplog=caplog)  # starts fine
    assert "SELLER_PASSWORD is no longer used; remove it from your environment." in caplog.text
    assert "MERCHANT_API_KEY is no longer used; remove it from your environment." in caplog.text
    assert "old-shared-password-value" not in caplog.text and "old-merchant-key-value" not in caplog.text
    assert "old-shared-password-value" not in repr(settings)

    assert client.post("/api/login", json={"password": "old-shared-password-value"}).status_code == 422
    assert login(client, password="old-shared-password-value").status_code == 401
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Authorization": "Bearer old-merchant-key-value"}
    mcp = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert mcp.status_code == 401


def test_no_warning_without_the_old_settings(tmp_path, caplog):
    new_client(tmp_path, caplog, _env_file=None)  # not even a local .env
    assert "no longer used" not in caplog.text


def test_login_as_helper_reuses_the_account(tmp_path):
    client, settings = new_client(tmp_path)
    first = login_as(client, settings)
    assert login_as(client, settings)["id"] == first["id"]
