"""python -m app.check against the real app in production mode (no network)."""

import pytest
from fastapi.testclient import TestClient

from app import check
from app.config import Settings
from app.main import create_app
from tests.support import TEST_PASSWORD, db_target, login_as

EMAIL = "seller@example.com"


@pytest.fixture
def site(tmp_path):
    settings = Settings(_env_file=None, render="true", database_url=db_target(tmp_path),
                        app_secret="a-test-only-secret-of-at-least-32-characters",
                        paypal_client_id="cid", paypal_client_secret="shh", paypal_webhook_id="WH-1",
                        gemini_api_key="g", render_git_commit="abcdef1234567890")
    client = TestClient(create_app(settings), base_url="https://testserver", follow_redirects=False)
    login_as(client, settings, EMAIL)  # /setup is 404 from here on
    client.post("/api/logout")
    client.cookies.clear()
    return client


def by_name(rows) -> dict:
    return {r.name: r for r in rows}


def run(client, **kwargs):
    return check.run(client, https=True, wake_seconds=0, sleep=lambda _: None, **kwargs)


def test_a_healthy_site_passes_without_a_login(site):
    rows = run(site)
    assert [r for r in rows if r.status != check.PASS] == []
    assert list(by_name(rows)) == ["Service awake", "/login is 200", "/setup is 404", "/ is 200", "/shop is 200",
                                   "Security headers on /login", "Security headers on /",
                                   "Security headers on /shop", "API pages blocked, signed out"]
    assert by_name(rows)["Service awake"].detail == "version abcdef1"


def test_a_login_adds_system_status_and_leaves_no_session(site):
    rows = by_name(run(site, email=EMAIL, password=TEST_PASSWORD))
    assert rows["Login"].status == rows["API pages blocked, signed in"].status == check.PASS
    for name in check.REQUIRED_CHECKS:
        assert rows[name].status == check.PASS, name
    assert rows["AG Studio key"].status == check.WARN and rows["Last webhook"].status == check.WARN
    assert site.get("/api/orders").status_code == 401  # logged out again


def test_a_wrong_password_fails_and_says_only_the_status(site):
    rows = by_name(run(site, email=EMAIL, password="not-the-password"))
    assert rows["Login"].status == check.FAIL and rows["Login"].detail == "HTTP 401"


def test_a_failed_system_check_fails_the_table(site, monkeypatch):
    async def bad_token(self):
        raise RuntimeError("no")

    monkeypatch.setattr("app.paypal.PayPalClient.check_credentials", bad_token)
    rows = by_name(run(site, email=EMAIL, password=TEST_PASSWORD))
    assert rows["PayPal token"].status == check.FAIL


class Fake:
    """A site that answers one path differently from the real app."""

    def __init__(self, real, path, status=None, drop=None):
        self.real, self.path, self.status, self.drop = real, path, status, drop

    def get(self, path, **kwargs):
        res = self.real.get(path, **kwargs)
        if path == self.path and self.status:
            res.status_code = self.status
        if path == self.path and self.drop:
            del res.headers[self.drop]
        return res

    def post(self, *args, **kwargs):
        return self.real.post(*args, **kwargs)


@pytest.mark.parametrize("path, status, row", [
    ("/login", 500, "/login is 200"),
    ("/setup", 200, "/setup is 404"),
    ("/docs", 200, "API pages blocked, signed out"),
    ("/openapi.json", 200, "API pages blocked, signed out"),
])
def test_a_wrong_status_fails(site, path, status, row):
    assert by_name(run(Fake(site, path, status=status)))[row].status == check.FAIL


@pytest.mark.parametrize("header", ["content-security-policy", "strict-transport-security",
                                    "x-content-type-options", "referrer-policy", "permissions-policy"])
def test_a_missing_header_fails(site, header):
    assert by_name(run(Fake(site, "/login", drop=header)))["Security headers on /login"].status == check.FAIL


def test_a_weak_csp_is_reported():
    assert check.csp_problems(None) == ["no CSP"]
    weak = "default-src *; script-src 'self' 'unsafe-inline' 'unsafe-eval'"
    assert check.csp_problems(weak) == ["default-src isn't 'self'", "frame-ancestors isn't 'none'",
                                        "script-src allows unsafe-inline", "script-src allows unsafe-eval"]


def test_wake_retries_until_the_service_answers(site):
    answers = iter([503, 503, None])
    pauses = []
    rows = check.run(FlakyHealth(site, answers), https=True, sleep=pauses.append)
    assert rows[0].status == check.PASS and len(pauses) == 2


class FlakyHealth:
    def __init__(self, real, answers):
        self.real, self.answers = real, answers

    def get(self, path, **kwargs):
        res = self.real.get(path, **kwargs)
        if path == "/healthz" and (status := next(self.answers)):
            res.status_code = status
        return res

    def post(self, *args, **kwargs):
        return self.real.post(*args, **kwargs)


def test_wake_gives_up_and_stops_there(site):
    client = Fake(site, "/healthz", status=503)
    rows = check.run(client, https=True, wake_seconds=0, sleep=lambda _: None)
    assert [r.status for r in rows] == [check.FAIL] and "HTTP 503" in rows[0].detail


def test_expect_commit_waits_for_the_new_version(site):
    rows = run(site, commit="0000000aaaa")
    assert [r.status for r in rows] == [check.FAIL] and "still on version abcdef1" in rows[0].detail
    assert run(site, commit="abcdef1234567890")[0].status == check.PASS


def test_main_needs_email_and_password_together(capsys):
    with pytest.raises(SystemExit):
        check.main(["--base", "https://x", "--login-email", "a@example.com"])
    assert "go together" in capsys.readouterr().err


def test_the_table_never_prints_the_password(site, capsys):
    print(check.table(run(site, email=EMAIL, password=TEST_PASSWORD)))
    assert TEST_PASSWORD not in capsys.readouterr().out
