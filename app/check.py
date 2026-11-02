"""Pre-demo check: is the live site ready? Read-only, apart from one login session it ends itself.

    python -m app.check --base https://clearline-cbyb.onrender.com
    python -m app.check --base <url> --login-email you@example.com --password-stdin

Wakes the service (Render's free tier sleeps), then checks the public pages, the security headers
and that the API pages are blocked. With a login it adds Settings -> System status. Prints a table
and exits 1 if anything failed. Needs no settings: it only talks to the site.
"""

import argparse
import sys
import time
from dataclasses import dataclass

import httpx

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
WAKE_SECONDS = 120  # a cold start on Render's free tier takes about a minute
WAKE_PAUSE = 5
API_PAGES = ("/docs", "/redoc", "/openapi.json")
HEADERS = {"x-content-type-options": "nosniff", "referrer-policy": "strict-origin-when-cross-origin"}
# Settings -> System status rows that can fail the check. The AG Studio key is optional.
REQUIRED_CHECKS = ("Database", "Migrations", "PayPal token", "Webhook ID", "AI provider")


@dataclass
class Row:
    name: str
    status: str
    detail: str = ""


def wake(client: httpx.Client, *, seconds: float, commit: str | None, sleep=time.sleep,
         clock=time.monotonic) -> Row:
    """Retry /healthz until it answers, and with `commit`, until that version is the one serving."""
    deadline, last = clock() + seconds, "no answer"
    while True:
        try:
            res = client.get("/healthz")
            version = res.headers.get("x-app-version", "unknown")
            if res.status_code != 200:
                last = f"HTTP {res.status_code}"
            elif commit and version != commit[:7]:
                last = f"still on version {version}, waiting for {commit[:7]}"
            else:
                return Row("Service awake", PASS, f"version {version}")
        except httpx.HTTPError as err:
            last = type(err).__name__
        if clock() >= deadline:
            return Row("Service awake", FAIL, f"gave up after {seconds:.0f} s: {last}")
        sleep(WAKE_PAUSE)


def status_is(client: httpx.Client, path: str, expected: int) -> Row:
    status = client.get(path).status_code
    return Row(f"{path} is {expected}", PASS if status == expected else FAIL, f"HTTP {status}")


def csp_problems(policy: str | None) -> list[str]:
    if not policy:
        return ["no CSP"]
    directives = {name: " ".join(value) for name, *value in
                  (part.split() for part in policy.split(";") if part.strip())}
    problems = [] if directives.get("default-src") == "'self'" else ["default-src isn't 'self'"]
    if directives.get("frame-ancestors") != "'none'":
        problems.append("frame-ancestors isn't 'none'")
    script = directives.get("script-src", "")
    problems += [f"script-src allows {word}" for word in ("unsafe-inline", "unsafe-eval") if word in script]
    return problems


def header_problems(res: httpx.Response, https: bool) -> list[str]:
    problems = [f"{name} is missing or wrong" for name, value in HEADERS.items()
                if res.headers.get(name) != value]
    for name in ("permissions-policy", "x-request-id"):
        if name not in res.headers:
            problems.append(f"{name} is missing")
    if https and "strict-transport-security" not in res.headers:
        problems.append("strict-transport-security is missing")
    return problems + csp_problems(res.headers.get("content-security-policy"))


def headers_row(client: httpx.Client, path: str, https: bool) -> Row:
    problems = header_problems(client.get(path), https)
    return Row(f"Security headers on {path}", FAIL if problems else PASS, "; ".join(problems))


def api_pages_row(client: httpx.Client, allowed: tuple[int, ...], name: str) -> Row:
    """Blocked means anything but the page: 404 signed in, or the login redirect signed out."""
    statuses = {path: client.get(path).status_code for path in API_PAGES}
    open_pages = [f"{path} HTTP {status}" for path, status in statuses.items() if status not in allowed]
    return Row(name, FAIL if open_pages else PASS, ", ".join(open_pages))


def system_rows(client: httpx.Client) -> list[Row]:
    res = client.get("/api/system/status")
    if res.status_code != 200:
        return [Row("System status", FAIL, f"HTTP {res.status_code}")]
    status = res.json()
    rows = []
    for item in status["checks"]:
        if item["name"] in REQUIRED_CHECKS:
            rows.append(Row(item["name"], FAIL if item["ok"] is False else PASS, item["detail"]))
        elif item["name"] == "AG Studio key":
            rows.append(Row(item["name"], PASS if item["detail"] == "present" else WARN, item["detail"]))
    webhook = status.get("last_webhook")
    rows.append(Row("Last webhook", PASS, webhook["event_type"]) if webhook
                else Row("Last webhook", WARN, "none received yet"))
    return rows


def logged_in_rows(client: httpx.Client, email: str, password: str) -> list[Row]:
    res = client.post("/api/login", json={"email": email, "password": password})
    if res.status_code != 200:
        return [Row("Login", FAIL, f"HTTP {res.status_code}")]
    try:
        return [Row("Login", PASS), api_pages_row(client, (404,), "API pages blocked, signed in"),
                *system_rows(client)]
    finally:
        client.post("/api/logout")  # leaves no session behind


def run(client: httpx.Client, *, https: bool, email: str | None = None, password: str | None = None,
        wake_seconds: float = WAKE_SECONDS, commit: str | None = None, sleep=time.sleep) -> list[Row]:
    rows = [wake(client, seconds=wake_seconds, commit=commit, sleep=sleep)]
    if rows[0].status == FAIL:
        return rows
    try:
        rows += [status_is(client, "/login", 200), status_is(client, "/setup", 404),
                 status_is(client, "/", 200), status_is(client, "/shop", 200),
                 headers_row(client, "/login", https), headers_row(client, "/", https),
                 headers_row(client, "/shop", https),
                 api_pages_row(client, (303, 401, 404), "API pages blocked, signed out")]
        if email:
            rows += logged_in_rows(client, email, password or "")
    except httpx.HTTPError as err:
        rows.append(Row("Connection", FAIL, type(err).__name__))
    return rows


def table(rows: list[Row]) -> str:
    width = max(len(r.name) for r in rows)
    return "\n".join(f"{r.status}  {r.name:<{width}}  {r.detail}".rstrip() for r in rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.check", description="Pre-demo check of a live Clearline.")
    parser.add_argument("--base", required=True, help="the site, e.g. https://clearline-cbyb.onrender.com")
    parser.add_argument("--login-email", help="also check Settings -> System status as this seller")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from stdin's first line")
    parser.add_argument("--wait", type=float, default=WAKE_SECONDS, help="seconds to wait for the service")
    parser.add_argument("--expect-commit", help="also wait until the site is serving this commit")
    args = parser.parse_args(argv)
    if bool(args.login_email) != args.password_stdin:
        parser.error("--login-email and --password-stdin go together")
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else None
    base = args.base.rstrip("/")
    with httpx.Client(base_url=base, timeout=30) as client:
        rows = run(client, https=base.startswith("https://"), email=args.login_email, password=password,
                   wake_seconds=args.wait, commit=args.expect_commit)
    print(table(rows))
    failed = sum(r.status == FAIL for r in rows)
    print(f"\n{failed} failed." if failed else "\nAll good.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
