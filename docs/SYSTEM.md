# Clearline: system guide

What Clearline is, how it is built, how money and data move through it, how it is protected,
and how it is configured. Status, the live check, automation and the plan to submission are in
[STATUS.md](STATUS.md).

Everything here was read from the code at commit `f3132a9` (30 Oct 2026).

## 1. What Clearline is

Clearline helps a small online seller who is paid through PayPal see where every payment is,
and sort out problems before they become disputes. It keeps one timeline per sale: order,
capture, shipping, buyer questions, refunds, disputes and invoices. The seller works from that
timeline. AI drafts the answers, and the seller approves every action that moves money.

| Who | What they get |
|---|---|
| Seller | `/seller` (today's to-dos, orders, disputes, invoices, settings), `/dashboard` (money in flight), `/insights` (their own report with an AI analyst) |
| Buyer | A private link per order (`/o/<token>`): what was bought, shipping, refunds, and a way to ask a question or request a refund |
| AI agents | An MCP server at `/mcp`. Buyer agents use the order link; the seller's own agent uses an agent key for read-only tools |

**Hackathon.** PayPal AI Hackathon, open track. Deadline: **13 Nov 2026, 1:30 AM IST**
(12 Nov, 20:00 UTC).

| Prize targeted | Feature that serves it |
|---|---|
| PayPal + AI | Dispute assistant: facts from PayPal, AI recommendation with server-side guardrails, seller approves, Clearline sends evidence, messages or offers. Also AI invoice drafts, reminder drafts and buyer-page summaries |
| Agentic Commerce | MCP server: a buyer's agent can check an order and request a refund; the seller's auto-resolve policy can refund instantly within caps. Read-only merchant tools for the seller's own agent |
| Demo | One clear UI ("the clear line"), a live site on Render with a judge account, and real PayPal Sandbox flows |
| AG Grid (sponsor) | `/dashboard` on AG Grid Community + AG Charts Community; `/insights` on AG Studio, with its AI assistant running through Clearline's own AI client |

## 2. Architecture

```mermaid
flowchart TB
  subgraph Clients["Who calls Clearline"]
    direction LR
    pages["Browser pages<br/>checkout / · buyer link /o/token · login, /setup<br/>/seller · /dashboard · /insights · /agents"]
    agents["AI agents<br/>buyer agent: order link<br/>seller's agent: Bearer agent key"]
  end
  pp["PayPal REST<br/>Orders · Payments · Disputes · Invoicing · webhook verify"]

  subgraph App["FastAPI app on Render (uvicorn locally)"]
    direction TB
    web["main.py: routes, deny-by-default session check<br/>auth.py · accounts.py"]
    mcp["agents.py: MCP server at /mcp"]
    domain["buyer.py · policy.py · invoicing.py<br/>evidence.py · dashboard.py"]
    aimods["assistant.py · insights_ai.py · ai.py"]
    ppmod["paypal.py: the only PayPal client"]
    sched["scheduler.py: auto-sync every 3 min,<br/>hourly session cleanup"]
    store["store.py + db.py"]
    web --> domain
    web --> aimods
    mcp --> domain
    domain --> ppmod
    sched --> ppmod
    domain --> store
    aimods --> store
    web --> store
  end

  subgraph AI["AI providers"]
    direction LR
    gemini["Gemini (primary)"]
    groq["Groq (fallback)"]
  end
  cdn["jsDelivr: AG Studio 3.0.0 (SRI)"]
  subgraph Data["Database"]
    direction LR
    sqlite[("SQLite file, local")]
    neon[("Neon Postgres, production")]
  end

  pages -- "HTTPS: pages and JSON" --> web
  agents -- "POST /mcp" --> mcp
  pages -. "/insights loads the script" .-> cdn
  ppmod -- "REST calls out" --> pp
  pp -- "webhooks in: POST /webhooks/paypal" --> web
  aimods --> gemini
  aimods -. "fallback" .-> groq
  store --> sqlite
  store --> neon
```

- **Hosting.** Production runs on Render (`https://clearline-cbyb.onrender.com`) with Neon
  Postgres. Locally it runs under uvicorn with a SQLite file. ngrok gives the local app a
  public URL so PayPal webhooks can reach it.
- **Calls out.** Only `paypal.py` talks to PayPal. Only `ai.py` (dispute assistant, buyer
  summaries, invoice drafts) and `insights_ai.py` (Insights analyst) talk to Gemini and Groq.
  The browser loads AG Studio from jsDelivr, pinned with an integrity hash. AG Grid and AG
  Charts are served by the app itself.
- **Calls in.** Browsers, AI agents on `/mcp`, and PayPal webhooks on `/webhooks/paypal`.
  Each webhook is verified with PayPal before it is used.
- **Background.** `scheduler.py` runs the auto-sync every `AUTO_SYNC_MINUTES` (default 3). It
  pulls disputes and invoice statuses from PayPal, as a safety net for missed webhooks. An
  hourly job deletes expired sessions.

## 3. Tech stack

| Layer | Technology + version | Why | Sponsor / licence |
|---|---|---|---|
| Language | Python 3.13.5 | One language for server, AI glue and tests | PSF |
| Web framework | FastAPI 0.162.0 on Starlette 1.10.0, Uvicorn 0.54.0 | Async, typed request models, small | MIT / BSD |
| HTTP client | httpx 0.28.1 | Async calls to PayPal and AI; mockable with respx | BSD |
| Validation, config | pydantic 2.14.1, pydantic-settings 2.16.1 | Strict models for AI answers and requests; settings from env | MIT |
| Passwords | argon2-cffi 25.1.0 (Argon2id) | Current best practice for password hashing | MIT |
| AI agents | mcp 2.3.0 (official MCP Python SDK) | Standard protocol for agents; Streamable HTTP in-process | MIT |
| Database | SQLite 3.50 (stdlib) locally; Postgres on Neon via psycopg 3.3.6 + psycopg-pool 3.3.3 | Zero setup locally; managed Postgres in production, free tier | Neon free tier; psycopg LGPL |
| Payments | PayPal REST APIs (Orders v2, Payments v2, Disputes v1, Invoicing v2, webhooks) and PayPal JS SDK v6 | The hackathon platform | PayPal Sandbox |
| AI | Gemini `gemini-3.5-flash-lite` (primary), Groq `openai/gpt-oss-120b` (fallback), both through their OpenAI-compatible endpoints | Fast, cheap, free tiers; one client for both | Provider terms |
| Grid and charts | AG Grid Community 36.2.0, AG Charts Community 14.2.0, self-hosted in `app/static/vendor/` | Sponsor; fast grid; no CDN needed | MIT (licence files alongside) |
| Report builder | AG Studio 3.0.0 from jsDelivr with SRI | Sponsor; drag-and-drop report with an AI agent framework | Commercial (AG Grid Ltd); runs as a trial with a watermark until a licence key is set |
| Font | Instrument Sans, self-hosted | Clean UI; no font CDN | SIL OFL |
| Hosting | Render web service; ngrok for local webhooks | Simple deploys; stable tunnel for Sandbox webhooks | Render free tier |
| Tests | pytest 9.1.1, respx 0.23.1 (PayPal and AI mocked); Playwright 1.64.1 for screenshots (outside the repo) | Offline tests, no credentials needed | MIT / Apache 2.0 |

## 4. Core flows

### a) Checkout, capture, shipping, settled

```mermaid
sequenceDiagram
  actor Buyer
  participant Page as Checkout page
  participant App as Clearline
  participant PP as PayPal
  actor Seller
  Buyer->>Page: Pick an item
  Page->>App: POST /api/orders (item id only)
  App->>PP: Create order (price from the server catalogue)
  PP-->>App: order id
  App-->>Page: order id
  Buyer->>PP: Approve in the PayPal window
  Page->>App: POST /api/orders/id/capture
  App->>PP: Capture order
  PP-->>App: capture id, status
  App->>App: Timeline: order_created, payment_captured
  PP-)App: Webhook PAYMENT.CAPTURE.COMPLETED
  App->>PP: Verify webhook signature
  App->>App: Timeline: webhook event (deduplicated by event id)
  Seller->>App: Mark shipped (carrier, tracking number)
  App->>PP: Add tracking to the order
  App->>App: Timeline: tracking_added
  Note over App: No dispute, no hold, no open buyer request = Settled
```

The price never comes from the browser. A failed capture is recorded as `capture_failed`.

### b) Dispute: webhook or sync, AI recommendation, seller approves

```mermaid
sequenceDiagram
  participant PP as PayPal
  participant App as Clearline
  participant AI as Gemini / Groq
  actor Seller
  alt Webhook
    PP-)App: CUSTOMER.DISPUTE.CREATED / UPDATED
    App->>PP: Verify signature
    App->>PP: Get dispute (full details)
  else Auto-sync every 3 min, or the Sync button
    App->>PP: List disputes, get new or changed ones
  end
  App->>App: Timeline: dispute_created / dispute_updated
  App->>App: build_facts (code gathers the facts)
  App->>AI: Facts as JSON, strict schema
  AI-->>App: Decision, reasons citing fact keys, drafts
  App->>App: Guardrails (unknown facts, offer limits, tracking, no fabrication, no emails or links)
  App->>App: Timeline: ai_recommendation
  Seller->>App: Approve (may edit texts, attach a document)
  App->>PP: Get dispute (never act on stale state)
  loop Each planned step
    App->>PP: Re-check dispute
    App->>PP: provide-evidence / send-message / make-offer / accept-claim
    App->>App: Timeline: dispute_action_sent, skipped or failed
  end
  PP-)App: CUSTOMER.DISPUTE.RESOLVED
  App->>App: Timeline: dispute_resolved
```

- FIGHT sends evidence first. Tracking is sent for inquiries; claims (`CHARGEBACK` stage) take
  only documents, such as `PROOF_OF_DELIVERY_SIGNATURE` with a PDF the seller uploads.
- A step PayPal no longer allows is skipped, not failed. There is no automatic retry.
- If Gemini looks down or its key is bad, the dispute assistant switches to Groq and stays
  there until the seller resets it (`POST /api/ai/reset`).

### c) Buyer link, refund request, policy or seller

```mermaid
sequenceDiagram
  actor Buyer
  participant Page as Buyer page /o/token
  participant App as Clearline
  participant PP as PayPal
  actor Seller
  Seller->>App: Copy the buyer link (HMAC of the order id)
  Buyer->>Page: Open link
  Page->>App: GET /api/o/token
  App-->>Page: Order facts, cached AI summary (or a template sentence)
  Buyer->>App: POST /api/o/token/requests (refund, reason, message)
  App->>App: Rate limit per link, strip PII, timeline: buyer_request
  alt Auto-resolve policy on and every rule passes
    App->>PP: Refund capture (explicit amount)
    App->>App: Timeline: refund_sent (trigger auto-policy), seller_reply (auto)
    App-->>Buyer: Refunded
  else Policy off or a rule fails
    App->>App: Timeline: auto_policy_skipped (which rule)
    App-->>Buyer: Sent to the seller
    Seller->>App: Reply, and/or Refund full or partial (confirm)
    App->>PP: Refund capture
    App->>App: Timeline: seller_reply, refund_sent
  end
```

The policy is off by default. Its rules: allowed reasons, per-order maximum, daily cap,
currency, no dispute, not shipped, and nothing left to refund. Every check runs on
Clearline's records before PayPal is called, under a lock so two requests can't both fit the
daily cap.

### d) Invoice: AI draft, send, reminder, paid

```mermaid
sequenceDiagram
  actor Seller
  participant App as Clearline
  participant AI as Gemini / Groq
  participant PP as PayPal
  actor Client
  Seller->>App: One sentence ("Logo work for Acme, 300 USD, due in 14 days")
  App->>App: Strip emails, links, phone numbers
  App->>AI: Extract a draft (strict schema)
  App->>App: Check the draft against the seller's words (no invented amounts or fees)
  App-->>Seller: Prefilled form to review
  Seller->>App: Create draft
  App->>PP: Create draft invoice
  Seller->>App: Send (a separate click)
  App->>PP: Send invoice
  PP-)Client: Invoice email
  Note over App: Overdue: Needs you
  Seller->>App: Draft reminder
  App->>AI: Polite reminder, tone by days overdue
  App-->>Seller: Draft to edit (template if the AI fails)
  Seller->>App: Send reminder
  App->>PP: Remind invoice
  Client->>PP: Pays
  PP-)App: INVOICING.INVOICE.PAID (or found by auto-sync)
  App->>App: Timeline: invoice_paid
  Note over App,Seller: Seller country IN: export checklist (purpose code, FIRA)
```

### e) AI agents on MCP

```mermaid
sequenceDiagram
  participant BA as Buyer agent
  participant SA as Seller's agent
  participant Guard as McpGuard (/mcp)
  participant App as Clearline
  participant PP as PayPal
  BA->>Guard: tools/call request_refund (order_link, reason, amount)
  Guard->>Guard: Origin check, body size limit
  Guard->>App: Tool runs
  App->>App: Order link to order (HMAC), rate limit, strip PII
  App->>App: Same path as the buyer page (via agent)
  alt Fits the auto-resolve policy
    App->>PP: Refund capture
    App-->>BA: status refunded, amount
  else
    App-->>BA: sent to the seller, who will decide
  end
  SA->>Guard: tools/call money_in_flight, Authorization Bearer cl_live_...
  Guard->>App: SHA-256 of key, not revoked, owner not disabled
  App-->>SA: Read-only data from Clearline's records (no PayPal call)
```

Buyer tools: `get_order_status`, `message_seller`, `request_refund`, `get_request_status`.
Merchant tools, all read-only: `list_attention_items`, `get_dispute_brief`, `money_in_flight`.
The call log keeps the tool, role, time, a short outcome and the order's last 6 characters.

### f) Insights AI

```mermaid
sequenceDiagram
  actor Seller
  participant Studio as AG Studio (browser)
  participant App as Clearline /api/insights/ai
  participant G as Gemini
  participant Q as Groq
  Seller->>Studio: "What needs me this week?"
  loop Each model turn (max 6, or 8 when building widgets)
    Studio->>App: Conversation items + tool schemas
    App->>App: Session check, 60 turns/hour, strip PII, keep allowed tools only
    App->>App: Put back Gemini's thought signatures (SignatureCache)
    App->>G: Chat request
    alt Gemini fails
      App->>Q: Same turn, reduced tools, trimmed history (this request only)
    end
    App-->>Studio: Text and tool calls (our ids)
    Studio->>Studio: Run tools: view schema, execute query, add or configure widget
  end
  Studio-->>Seller: Answer, or a new chart in the report
```

The analyst can only read and arrange the report. Studio's data comes from the same PII-free
rows as the dashboard, including `needs_you`, `deadline` and `act_by`.

### g) Login, sessions, /setup, agent keys

```mermaid
sequenceDiagram
  actor Seller
  participant App as Clearline
  participant DB as Database
  Note over App: Start with no users: random setup code in the log (memory only)
  Seller->>App: /setup: code, email, password twice
  App->>App: Constant-time code check, 10 tries / 15 min per IP
  App->>DB: users row (Argon2id hash)
  Seller->>App: POST /api/login (email, password)
  App->>App: 20 tries / 15 min per IP
  App->>DB: Find user, verify hash (dummy hash for unknown emails)
  alt 5th wrong password
    App->>DB: locked_until = now + 15 min
  end
  App->>DB: sessions row: id = SHA-256(token)
  App-->>Seller: Cookie with the raw token (HttpOnly, SameSite=Lax, Secure over HTTPS)
  Seller->>App: Settings, Agent access, Create key
  App->>DB: api_keys row: SHA-256(key), prefix
  App-->>Seller: cl_live_... shown once
```

More accounts, password resets and disabling go through the CLI:
`python -m app.users create | reset-password | disable | enable | list`. `--password-stdin`
covers scripts. `/setup` returns 404 once any account exists.

## 5. Data model

There is no `orders` table. An order is all the `events` rows that share its `order_id`.
Every status the UI shows is computed from those rows.

```mermaid
erDiagram
  events {
    int id PK
    text order_id "PayPal order id, invoice id or 'other'"
    text kind "order_created, payment_captured, ..."
    text payload "JSON"
    float created_at
  }
  processed_webhooks {
    text event_id PK
    float received_at
  }
  buyer_summaries {
    text order_id PK
    text state_key
    text text
    text provider
    text model
    float created_at
  }
  ai_state {
    int id PK "always 1"
    text active_provider
    float switched_at
    text reason
  }
  app_settings {
    text key PK "e.g. auto_resolve_policy"
    text value "JSON"
    float updated_at
  }
  users {
    int id PK
    text email UK
    text password_hash
    float created_at
    float last_login_at
    int failed_logins
    float locked_until
    int disabled
  }
  sessions {
    text id PK "SHA-256 of the cookie token"
    int user_id FK
    float created_at
    float last_seen_at
    float expires_at
  }
  api_keys {
    int id PK
    int user_id FK
    text name
    text key_hash UK
    text prefix
    float created_at
    float last_used_at
    float revoked_at
  }
  insights_layouts {
    text user_key PK "the user's id"
    text state "JSON"
    float updated_at
  }
  agent_calls {
    int id PK
    float at
    text role
    text tool
    text outcome
    text order_ref "last 6 characters"
  }
  schema_version {
    int version
  }
  users ||--o{ sessions : has
  users ||--o{ api_keys : owns
  users ||--o| insights_layouts : saves
  events }o--o| buyer_summaries : "same order_id"
```

The relationships are logical: there are no foreign keys. The same schema runs on SQLite and
Postgres. On Postgres, ids are `BIGINT` identities and times are `DOUBLE PRECISION` epoch
seconds. `schema_version` records how far migrations have run; they run at startup.

**Event kinds** (main ones): `order_created`, `payment_captured`, `capture_failed`,
`tracking_added`, `webhook_payment_capture_completed / pending / reversed`, `buyer_request`,
`seller_reply`, `refund_sent`, `refund_failed`, `auto_policy_skipped`, `dispute_created`,
`dispute_updated`, `dispute_resolved`, `ai_recommendation`, `dispute_action_sent / skipped /
failed`, `invoice_status`, `invoice_send_sent`, `invoice_reminder_sent`, `invoice_paid`,
`invoice_action_failed`, `export_purpose_suggested / set`, `export_fira`.

### Money states and the clear line

Every paid order or invoice is in exactly one state (`app/dashboard.py`):

| State | Label | When |
|---|---|---|
| `disputed` | Disputed | An open dispute (latest state by PayPal's `update_time`), or the capture was reversed |
| `held` | Waiting | PayPal holds the capture (pending), or a buyer request has no reply or refund yet |
| `settled` | Settled | None of the above (net of refunds) |
| `unpaid` | Unpaid / Overdue | An invoice not paid yet |

**The clear line** draws each order's history as one horizontal line. Each segment is
coloured by the money state at that time, so the seller sees where the money was and is now.
The same component (`history.js`) is used on `/seller` and in the dashboard's side panel.

### Needs you, Deadline, Act by

| Field | Rule |
|---|---|
| `needs_you` | True when it's the seller's move: a dispute `WAITING_FOR_SELLER_RESPONSE`, an unanswered buyer request, a paid order with no tracking (and not fully refunded), or an overdue invoice. False when waiting on the buyer or PayPal, or nothing to do |
| `deadline` | The dispute's `seller_response_due_date`, or the invoice due date while unpaid |
| `act_by` | The deadline if there is one; today if `needs_you` with no deadline; empty otherwise |
| `next` | Plain words: "Respond by 3 Nov", "Buyer asked for a refund", "Add tracking", "Waiting for the buyer", "Nothing to do" |

## 6. Security and AI guardrails

| What | How |
|---|---|
| Seller passwords | Argon2id (argon2-cffi defaults), rehashed at login if defaults change. 12–128 characters, not a common password, not containing the email's local part |
| Sessions | Database rows keyed by SHA-256 of a random 32-byte token; the raw token is only in the cookie. HttpOnly, SameSite=Lax, Secure over HTTPS. 7 days idle, 30 days absolute. Rotated at login; logout and password changes delete rows |
| Brute force | 5 wrong passwords lock the account for 15 min. 20 login attempts / 15 min per IP; 10 setup attempts / 15 min per IP. One generic error message; unknown emails are verified against a dummy hash |
| Deny by default | Only 20 public routes (checkout, buyer link and API, webhooks, login, setup, healthz, design assets, MCP). Everything else needs a session |
| Cross-site requests | POST/PUT/PATCH/DELETE with a session and a foreign `Origin` get 403; login, setup and logout too. MCP checks `Origin` (same host or localhost) |
| First account | `/setup` needs a one-time code from the server log (memory only, constant-time compare); 404 once any account exists. No public sign-up |
| Agent keys | `cl_live_` + random, stored as SHA-256, shown once, revocable; merchant tools are read-only |
| Webhooks | Every event verified with PayPal (`verify-webhook-signature`), deduplicated by event id. Without `PAYPAL_WEBHOOK_ID` the endpoint returns 503. `ALLOW_UNVERIFIED_WEBHOOKS` exists for local tests only and logs a loud warning |
| Buyer links | `/o/<token>`: HMAC-SHA256 of the order id with `APP_SECRET`; compared in constant time; unknown and forged links look the same (404). `Referrer-Policy: no-referrer` on that page |
| Personal data and AI | Emails, links and phone-like numbers stripped before any AI call (`strip_pii`). Buyer pages, dashboard, Insights and merchant tools carry no buyer email, name or address. Logs carry counts, ids and statuses only |
| Fabrication checks | The dispute AI must cite existing fact keys; offers must be above 0 and at most the disputed amount; no FIGHT on not-received without tracking; no advice to create or alter documents; no emails or links in drafts; under 120 words. Invoice and reminder drafts are checked against the seller's own words; buyer summaries against the order's facts |
| Human approval | Dispute actions, invoices (create, send, remind) and seller refunds each need a click and a confirm. The AI never calls PayPal |
| Auto-policy | Off by default. When on, only within the seller's caps (per order, per day, reasons, currency, no dispute, not shipped), with an explicit amount |
| Evidence files | Type from magic bytes (PDF, PNG, JPG, GIF), under 5 MB, name sanitised, sent to PayPal from memory and never stored |
| Database URL and secrets | Never logged; `DATABASE_URL` is hidden from settings repr; CLI tools hide driver messages that could name the host |

### Known gaps (verified in the code and on the live site)

1. **Client IP behind a proxy.** With `TRUST_PROXY=true`, `client_ip` takes the **first**
   `X-Forwarded-For` hop (`app/main.py`). A client can put any value there, so the per-IP
   login and setup limits can be dodged. Fix: take the hop added by Render (the last one).
   The per-account lockout still applies.
2. **No security headers.** The live site sends no CSP, HSTS, `X-Frame-Options` or
   `X-Content-Type-Options` (checked on `/login`). Only the buyer page sets `Referrer-Policy`.
3. **No production mode.** `/docs` and `/openapi.json` are on (seller-only).
   `ALLOW_UNVERIFIED_WEBHOOKS` is not refused when running in production.
4. **Rate limits are in memory.** They reset on restart and are per process. That is fine for
   one Render instance; with two or more instances they would need the database.
5. **Buyer links never expire.** A link is valid as long as `APP_SECRET` is unchanged, and
   one link can't be revoked on its own. Changing `APP_SECRET` breaks every link.
6. **MCP Origin check** compares against `X-Forwarded-Host` whether or not `TRUST_PROXY` is
   set. Browsers can't set that header, so the risk is low.
7. **No overall request size limit.** Only specific limits exist (MCP body 1 MB, AI turn
   1 MB, layout 256 KB, evidence 5 MB).
8. **AG Studio licence.** No key is set on Render, so `/insights` shows the trial watermark.

## 7. Configuration

Every setting lives in `app/config.py`. Each is read from an environment variable of the same
name in capitals, or from `.env` locally. "Local" says whether the local `.env` sets it. It
was checked through the app's settings loader, so no values were read.

| Name | What it does | Required | Secret | Default | Local | Render |
|---|---|---|---|---|---|---|
| `PAYPAL_CLIENT_ID` | PayPal REST app id | Yes | No (public in the browser) | none | set | Yes |
| `PAYPAL_CLIENT_SECRET` | PayPal REST app secret | Yes | Yes | none | set | Yes |
| `PAYPAL_ENV` | `sandbox` or `live` | No | No | `sandbox` | default | Optional |
| `DATABASE_PATH` | SQLite file when no `DATABASE_URL` | No | No | `clearline.db` | default | No |
| `DATABASE_URL` | Postgres connection string (Neon) | Production | Yes | empty (SQLite) | empty | Yes |
| `PAYPAL_WEBHOOK_ID` | Verifies incoming webhooks; without it webhooks get 503 | For webhooks | Low (but keep private) | empty | set | Yes (Render's own webhook) |
| `ALLOW_UNVERIFIED_WEBHOOKS` | Accept unverifiable webhooks (local tests only) | No | No | `false` | default | Never |
| `GEMINI_API_KEY` | Primary AI | For AI | Yes | empty | set | Yes |
| `GEMINI_MODEL` | Gemini model name | No | No | `gemini-3.5-flash-lite` | default | No |
| `GROQ_API_KEY` | Fallback AI | For fallback | Yes | empty | set | Yes |
| `GROQ_MODEL` | Groq model name | No | No | `openai/gpt-oss-120b` | default | No |
| `AI_TIMEOUT_SECONDS` | Per AI call | No | No | `20` | default | No |
| `AUTO_SYNC_MINUTES` | Dispute and invoice safety-net sync; 0 = off | No | No | `3` | default | Optional |
| `APP_SECRET` | Signs buyer links; random per start if empty | Yes in production | Yes | empty | set | Yes (same as local, so old links keep working) |
| `TRUST_PROXY` | Trust `X-Forwarded-*` (Secure cookie, client IP) | Behind a proxy | No | `false` | default | `true` |
| `SELLER_DISPLAY_NAME` | Name buyers see | No | No | `Demo Design Studio` | default | Optional |
| `SELLER_COUNTRY` | `IN` shows the India export checklist | No | No | `IN` | default | Optional |
| `AG_STUDIO_LICENSE_KEY` | Removes the AG Studio watermark | No | Yes | empty | empty | When the trial key arrives |
| `LOG_LEVEL` | Clearline's log level | No | No | `INFO` | default | No |
| `SELLER_PASSWORD`, `MERCHANT_API_KEY` | Retired; ignored with a warning | No | n/a | n/a | not set | Never |

### Proposal: fewer environment variables (not implemented)

Keep only real secrets and deployment wiring in the environment. Give everything else a good
default or move it to a Settings page stored in the database (`app_settings` already exists).

| Before (env) | After |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID` | Env (secrets) |
| `DATABASE_URL`, `APP_SECRET` | Env (secret, wiring) |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `AG_STUDIO_LICENSE_KEY` | Env (secrets) |
| `PAYPAL_ENV` | Env (wiring); default `sandbox` |
| `TRUST_PROXY` | Automatic: on when Render's `RENDER` variable is present; env still overrides |
| `DATABASE_PATH` | Default only (local) |
| `SELLER_DISPLAY_NAME`, `SELLER_COUNTRY` | Settings page (DB) |
| `AUTO_SYNC_MINUTES` | Settings page (DB), default 3 |
| `GEMINI_MODEL`, `GROQ_MODEL`, `AI_TIMEOUT_SECONDS` | Code defaults; Settings page for advanced use |
| `LOG_LEVEL` | Code default |
| `ALLOW_UNVERIFIED_WEBHOOKS` | Local only: refused when `RENDER` is set |

**Minimal list for Render:** `DATABASE_URL`, `APP_SECRET`, `PAYPAL_CLIENT_ID`,
`PAYPAL_CLIENT_SECRET`, `PAYPAL_WEBHOOK_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, and later
`AG_STUDIO_LICENSE_KEY`. That is 8 variables (7 today), instead of up to 19.
