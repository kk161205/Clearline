# Clearline: status, live check, automation and plan

How the system works is in [SYSTEM.md](SYSTEM.md). This page is the honest state of things on
1 Nov 2026 (after the hardening round), and what's left before the deadline: **13 Nov 2026,
1:30 AM IST** (12 Nov, 20:00 UTC).

## 8. Status matrix

- **Built:** the commit that added it.
- **Tested offline:** tests that pass with PayPal and the AI mocked. 439 in total, all
  passing on SQLite. CI also runs the database tests on a Postgres container on every push.
- **Live in Sandbox:** tested by hand against PayPal Sandbox.
- **On Render:** what was checked on `https://clearline-cbyb.onrender.com` (section 9).

| Feature | Built | Tested offline | Live in Sandbox | On Render | Notes |
|---|---|---|---|---|---|
| Checkout, capture | `5182e43` | `test_flow`, `test_webhooks` | Yes | Not verified | Server-side price catalogue |
| Mark shipped (tracking) | `5182e43` | `test_flow` | Yes | Not verified | |
| Verified webhooks | `5182e43` | `test_webhooks` (10) | Yes | Not verified | PayPal now delivers to Render only; no delivery seen yet. Settings → System status shows the last one |
| Dispute sync (button; every 3 min) | `0959b11`; auto `0c36ca0` | `test_sync_sandbox` (11) | Yes | Yes | Render's auto-sync listed 4 disputes and 1 invoice, no errors |
| AI dispute recommendation | `0959b11` | `test_assistant` (21), `test_ai` (10) | Yes | Not verified | Sticky Gemini to Groq fallback |
| FIGHT on an inquiry (evidence, message skipped) | `2396d25` | `test_assistant` | Yes | Not verified | Evidence first, re-check before each step |
| Chargeback document evidence | `31546d2` | `test_documents` (24) | Yes (`PROOF_OF_DELIVERY_SIGNATURE` + PDF) | Not verified | Files never stored |
| Buyer page and partial refund | `b4d72dc` | `test_buyer` (17) | Yes (40 USD partial) | Not verified | |
| Invoices: AI draft, create, send, paid | `15b1e0d` | `test_invoices` (20) | Yes (300 USD paid) | Data visible | |
| India export checklist | `15b1e0d` | `test_invoices` | Yes | Data visible | Purpose code, FIRA |
| Invoice reminders (overdue) | `15b1e0d` | `test_invoices` | **No** | No | Needs an invoice that is actually overdue |
| MCP buyer agent + auto-refund | `997cc35` | `test_agents` (20) | Yes (120 USD instant) | MCP answers `initialize` | |
| Auto-policy negative case (shipped order) | `997cc35` | `test_agents` | **No** | No | Covered offline only |
| MCP merchant tools + agent keys | `997cc35`, `e0c36e5` | `test_agents`, `test_auth` | No | No keys created yet | Read-only |
| UI design system, the clear line | `68e9f58` | `test_seller_page` (14) | Yes | Yes (screenshots) | |
| Money in flight dashboard (AG Grid) | `5f87977` | `test_dashboard` (9) | Yes | Yes (screenshot) | |
| Insights (AG Studio) | `8997fe7` | `test_insights` (16) | Yes | Yes (watermark: no licence key) | |
| Insights AI (Gemini, Groq) | `ab00cf3` to `1a8de5b` | `test_insights_ai` (34) | Yes ("needs me this week", bar chart) | Not verified | |
| Seller accounts, sessions, /setup, CLI | `e0c36e5` | `test_auth` (59) | Locally | **Yes** (login, redirects, `/setup` 404, Secure cookie) | Judge account works |
| Postgres (Neon), migration, `/healthz` | `f3132a9` | `test_db` (11), `test_migrate` (5) + the Postgres run | n/a | **Yes** (`/healthz` ok) | Data copied: 102 rows |
| Checkout on Render | | | | **Partly** | The button renders with no CSP violations; no payment tried |
| Production mode, proxy IP, headers, CSP | `6a8d40b` | `test_security` (33) | n/a | **Yes** (headers, anonymous routes, checkout CSP) | Zero CSP violations on 8 page types locally in production mode |
| Fewer env vars, Settings → Business | `f0fdfac` | `test_business` (16) | Locally | Not verified | Needs the Render clean-up below |
| Client IP behind Cloudflare and Render, HEAD on pages | B6, B7 | `test_security` | n/a | **Yes** (the chain measured live, one spoofed login sent) | |
| CI, secret scan, pre-demo check | see section 10 | `test_check` (19), `test_secret_scan` (15) | n/a | **Yes** (`app.check` passes on the live site) | |
| Request logs, startup summary, System status | `9413cac` | `test_diagnostics` (33) | Locally | **Partly** (`X-Request-ID`; login-only page not opened) | Render's log lines not seen from here |

## 9. Live deployment check (1 Nov 2026, read-only)

Anonymous, read-only checks of the deployed `9413cac`. Nothing was sent to PayPal, apart from the
SDK loading on the checkout page. Pages behind a login were last checked on 30 Oct, by screenshot.

| Check | Result |
|---|---|
| `GET /healthz` | 200 |
| `GET /login` | 200 |
| `GET /seller`, `/docs`, `/redoc`, `/openapi.json` without a session | 303 to `/login` (signed in: 404 for the three API pages, by test) |
| `GET /api/orders`, `/api/system/status`, `/api/business` without a session | 401 |
| `GET /setup` | 404 (accounts exist) |
| Security headers | HSTS, CSP, `nosniff`, `Referrer-Policy`, `Permissions-Policy` all present, redirects included |
| `X-Request-ID` | On every response |
| Checkout page in Chrome | PayPal button renders, zero CSP violations; hosts: the app and `www.sandbox.paypal.com` |
| `/login` in Chrome | Zero CSP violations |

**What the deploys show about configuration:** the app started in production mode, so Render has
`DATABASE_URL`, an `APP_SECRET` of at least 32 characters, the PayPal id and secret and
`PAYPAL_WEBHOOK_ID`, and `ALLOW_UNVERIFIED_WEBHOOKS` is off.

## 10. Automation proposals

| # | Automation | What it does | Effort | Demo value |
|---|---|---|---|---|
| 1 | ✅ **CI on GitHub Actions** | Every push and pull request: the full test suite on SQLite, plus the database tests against a Postgres service container | Done | High: no broken deploys during the last two weeks |
| 2 | ✅ **Secret scan in CI** | `scripts/secret_scan.py` fails the build on a Neon host, a Postgres URL with credentials, an agent key, PayPal, Google or Groq keys, or a private key, in the tree or in any new commit | Done | High: one leak would be costly |
| 3 | ✅ **Pre-demo check command** (`python -m app.check --base <url>`) | Wakes Render, then checks the public pages, headers and CSP, blocked API pages, and with a login System status. Prints a pass/fail table | Done | High: run it 10 minutes before judging |
| 4 | ✅ **Deploy pipeline** | CI calls Render's deploy hook only after all three jobs pass, then runs `app.check` until the new commit is serving | Done | High: a red deploy never reaches judges |
| 5 | **Demo data reset on Neon** (`python -m app.demo_reset`) | Rebuilds one fresh "demo story": an order to ship, an open buyer request, an inquiry dispute waiting on the seller, an overdue invoice, an agent refund. Refuses to run without a flag and a backup | M | High: every demo starts from the same story |
| 6 | **Playwright demo walk** | Walks the story on the live site (login, today, dispute card, buyer page, dashboard, Insights AI question), recording video and screenshots | M | High: backup footage if Sandbox or Wi-Fi fails |
| 7 | **Screenshot script in the repo** | Move the screenshot script into `tools/`, with a `--live` mode, for README and Devpost images in light and dark at phone and desktop widths | S | Medium |
| 8 | **Faster tests** | Run with `pytest -n auto` (xdist) on SQLite. For Postgres, share one schema per test file, or run in CI next to the database (each query costs about 270 ms from India) | S | Medium: today SQLite takes 3 min, Postgres 27 min |
| 9 | **Webhook check** | A small script lists the app's PayPal webhooks (read-only) and confirms the Render URL is subscribed, for the pre-demo check | S | Medium |
| 10 | **Windows dev friction** | A `dev.ps1` for local UI checks, and a Defender exclusion for `.venv` and the pytest temp folder (needs admin), for quicker installs and test runs | S | Low |

**What the demo reset can and can't create.** PayPal ids must be real for any later action:

| Story item | How |
|---|---|
| Order to ship | Real: create the order through the API. The buyer approves in the Sandbox window, so this needs a Playwright login to the Sandbox buyer account, then capture |
| Buyer request | Real: through the buyer link (Clearline only, no PayPal call) |
| Agent refund | Real: MCP `request_refund` with the policy on (a Sandbox refund) |
| Inquiry dispute | **Can't be created by the API.** The buyer opens it in the Sandbox Resolution Center (manual, or Playwright on the buyer account). Seeded events can show it, but Approve would fail on a fake id |
| Overdue invoice | **Can't be overdue on day one.** PayPal won't take a past due date, so create it with the shortest term a few days before the demo and wait. Or seed it for display only, knowing Remind would fail |
| Paid invoice, resolved dispute | Real only after buyer actions in Sandbox |

Seeded rows must use ids that PayPal never sees, and must be marked as demo data so no button
fires a PayPal call on them.

## 11. Plan to submission

```mermaid
gantt
  title Clearline: 1 Nov to 12 Nov 2026
  dateFormat YYYY-MM-DD
  axisFormat %d %b
  section Render and PayPal (you)
  Render env clean-up                        :crit, r1, 2026-11-01, 1d
  Live Sandbox run on Render                 :crit, r3, 2026-11-02, 1d
  section Automation
  Demo data reset                            :a3, 2026-11-04, 2d
  Playwright demo walk + footage             :a4, 2026-11-06, 2d
  section AG Studio (you)
  Request 45-day trial key                   :crit, s1, 2026-11-07, 2d
  Set AG_STUDIO_LICENSE_KEY on Render        :crit, s2, after s1, 1d
  section Demo and submission
  Overdue invoice for the reminder demo      :d0, 2026-11-05, 1d
  Final live checks (reminder, policy case)  :d1, 2026-11-09, 1d
  Demo video recording (you) + backup footage :crit, d2, 2026-11-10, 2d
  Devpost write-up                           :d3, 2026-11-10, 2d
  Judges' testing instructions               :d4, 2026-11-11, 1d
  Submit by 12 Nov (deadline 13 Nov, 01.30 IST) :milestone, d5, 2026-11-12, 0d
```

| Item | Who | Blocked on you? |
|---|---|---|
| Render environment clean-up (delete the retired variables) | You (Render dashboard) | **Yes** |
| Live Sandbox run on Render (checkout under the CSP, first webhook, System status) | You, with Claude checking | **Yes** (buyer approval in Sandbox) |
| Demo data reset, Playwright walk | Claude | Sandbox buyer steps are yours |
| AG Studio 45-day trial (request on 7–8 Nov, lasts to about 22 Dec) | You (email AG Grid) | **Yes** |
| Overdue invoice for the reminder demo (create about 5 Nov, shortest term) | You click, or Claude via the app | Partly |
| Demo video | You (voice, screen) | **Yes** |
| Devpost write-up, judges' instructions | Claude drafts, you submit | Submit is yours |
