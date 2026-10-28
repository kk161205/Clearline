# Clearline

## Day 1: the money path

Proves the money path end to end in the PayPal Sandbox: a US buyer pays an
order, it's captured into the India business account, and the seller adds
shipment tracking. Every step is recorded on the order's timeline.

### Run it (Windows, PowerShell)

```powershell
cd clearline
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env      # then paste your Sandbox Client ID + Secret into .env
uvicorn app.main:create_app --factory --reload
```

(macOS/Linux: `source .venv/bin/activate` and `cp` instead of `copy`.)

### Test the flow

1. Open http://localhost:8000 and click the PayPal button.
2. Log in with your **US Personal** Sandbox account (email + password from
   Developer Dashboard → Sandbox Accounts → ⋮ → View/edit account) and approve.
3. You should see "Payment completed".
4. Open http://localhost:8000/seller, enter any tracking number, click **Mark shipped**.
5. Check the payment landed: log in to https://www.sandbox.paypal.com with the
   **IN Business** account.

API docs: http://localhost:8000/docs

## Offline tests (mocked PayPal, no credentials needed)

```
python -m pytest -q
```

## Day 2: webhooks and disputes

PayPal tells Clearline about disputes and capture changes by webhook. The app
verifies each webhook with PayPal, ignores repeats, and puts dispute events on
the right order's timeline, so the seller page shows a red **Disputed** badge with
the reason, amount and response deadline.

PayPal has to reach your laptop, so the app runs behind a tunnel. The default is **ngrok
with your free fixed dev domain**: the URL never changes, so you register the PayPal webhook
once. Cloudflare quick tunnels still work as a fallback, but their URL changes on every restart.

### 1. Start the app and a tunnel (PowerShell)

Terminal 1:

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:create_app --factory --reload
```

#### Tunnel on Windows: ngrok (default)

One-time setup (`tools/` is git-ignored):

1. Create a free account at https://ngrok.com.
2. Download the Windows (amd64) zip from https://ngrok.com/download, extract it, and put
   `ngrok.exe` in `tools\`. Check it with `tools\ngrok.exe version`.
3. Save your authtoken (dashboard → **Your Authtoken**). This stores it in ngrok's own
   config in your user profile, not in this project:
   ```powershell
   tools\ngrok.exe config add-authtoken <your token>
   ```
4. Note your dev domain (dashboard → **Domains**), e.g. `<your-domain>.ngrok-free.dev`.

Every session, in terminal 2:

```powershell
tools\ngrok.exe http 8000 --url=https://<your-domain>.ngrok-free.dev
```

Leave it running. The public URL is always `https://<your-domain>.ngrok-free.dev`. ngrok
connects out over port 443, so it also works on networks that block Cloudflare tunnels.

#### Fallback: Cloudflare quick tunnel

```powershell
New-Item -ItemType Directory -Force tools
Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile tools\cloudflared.exe
tools\cloudflared.exe tunnel --url http://localhost:8000
```

Wait for `Registered tunnel connection`, then copy the `https://<random>.trycloudflare.com`
URL. It changes **every time cloudflared restarts**, so you must update the webhook URL in the
Dashboard each time. If you get Cloudflare **Error 1033** and the log says
`QUIC connection failed` / `HTTP/2 connection is blocked`, your network blocks port 7844
(common on college or office Wi-Fi): turn on the Cloudflare WARP VPN (1.1.1.1 app) or use a
phone hotspot, or use ngrok instead. The background auto-sync (Day 3) picks up disputes whose
webhooks were missed while a tunnel was down.

### 2. Register the webhook

1. Developer Dashboard → **Apps & Credentials** → your Sandbox app → **Webhooks** → **Add Webhook**
   (or edit the existing one).
2. Webhook URL: `https://<your-domain>.ngrok-free.dev/webhooks/paypal`
   (with the Cloudflare fallback: `https://<random>.trycloudflare.com/webhooks/paypal`).
3. Select these events:
   - `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `CUSTOMER.DISPUTE.RESOLVED`
   - `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.CAPTURE.REVERSED`
4. Save, copy the **Webhook ID**, and put it in `.env`:
   ```
   PAYPAL_WEBHOOK_ID=<the webhook id>
   ```
5. Restart the app (Ctrl+C in terminal 1, run uvicorn again).

> With ngrok's fixed domain you do this once. With the Cloudflare fallback the URL changes on
> every restart, so edit the webhook in the Dashboard each time. The Webhook ID stays the same
> when you edit the URL. If you delete the webhook and add a new one, update `PAYPAL_WEBHOOK_ID`.

### 3. Real dispute test

1. Pay the $120 order on http://localhost:8000 (Day 1 flow) if you haven't already.
2. Log in at https://www.sandbox.paypal.com as the **US Personal** account.
3. Open the $120 payment, choose **Report a problem** (Resolution Center),
   pick **"I didn't receive an item I bought"** (item not received) and submit.
4. Watch http://localhost:8000/seller. It refreshes every 10 seconds. Within a minute or so
   the order should show **Disputed**, "Item not received", USD 120.00 and "Respond by …".

### 4. Quick test with the Webhook Simulator

Developer Dashboard → **Webhooks Simulator** → paste the same `/webhooks/paypal` URL →
event type `CUSTOMER.DISPUTE.CREATED` → **Send Test**.

Simulator events are mock data and don't pass signature verification, so this needs
`ALLOW_UNVERIFIED_WEBHOOKS=true` in `.env` (restart after changing it). The event lands in the
**Unmatched and other PayPal events** section at the bottom of the seller page, because its
dispute isn't about one of your orders. Set it back to `false` when you're done.

### Troubleshooting

- `503` from `/webhooks/paypal`: `PAYPAL_WEBHOOK_ID` is not set or the app wasn't restarted.
- `400 Webhook signature verification failed`: the Webhook ID doesn't belong to the URL that
  sent the event, or the event came from the Simulator. Check the uvicorn log.
- Nothing arrives: the tunnel isn't running, or (Cloudflare fallback) its URL changed. Check that
  the Dashboard's webhook URL is your tunnel URL + `/webhooks/paypal`. Meanwhile **Sync disputes**
  on `/seller` (or the auto-sync) picks up missed disputes.
- ngrok `ERR_NGROK_4018` / authentication failed: the authtoken isn't saved; run the
  `config add-authtoken` command again.

## Day 3: AI dispute assistant

When a dispute is waiting for the seller, Clearline gathers the facts it knows (order,
payment, tracking, dispute reason, amount, deadline, the buyer's messages) and asks an AI to
recommend **Fight**, **Refund** or **Offer**, with reasons that cite those facts plus a draft
message to the buyer and a note to PayPal.

**Nothing is ever sent to PayPal until the seller clicks "Approve & send".** The AI only
reasons and drafts; code collects the facts and makes the PayPal calls, and only after
Approve. Names, emails, addresses and account IDs are never sent to the AI.

### 1. Get two free API keys (no card needed)

- Gemini: https://aistudio.google.com → **Get API key**
- Groq: https://console.groq.com → **API Keys** → **Create API Key**

Add them to `.env` (see `.env.example` for all AI settings):

```
GEMINI_API_KEY=...
GROQ_API_KEY=...
```

Restart the app after editing `.env`.

### 2. Sticky fallback

Gemini (`gemini-3.5-flash-lite`) is used first. If it fails with a rate limit (429), a server
error (5xx), a timeout or network error, a bad key (401/403), or returns invalid JSON twice,
Clearline switches to Groq (`openai/gpt-oss-120b`) **and stays on Groq**, even after a
restart. It never switches back by itself. A 400 from Gemini means our request is wrong, so
it does not switch; it records "AI unavailable" so the bug can be fixed.

```powershell
curl.exe http://localhost:8000/api/ai/status            # which provider is active and why
curl.exe -X POST http://localhost:8000/api/ai/reset     # go back to Gemini
```

### 3. Test with the existing dispute

1. Open http://localhost:8000/seller. Click **Sync disputes** at the top: it pulls disputes
   from PayPal and records only new or changed ones (it shows the counts).
2. Under the red dispute panel, the **AI recommendation** card appears (or click
   **Regenerate**). It shows the decision, confidence, reasons with the facts they cite,
   missing evidence, and which AI answered ("AI: Gemini …" or "AI: Groq … (fallback)").
3. Edit the message to the buyer and the note to PayPal if you like.
4. Click **Approve & send** and confirm. What happens:
   - **Fight**: messages the buyer (inquiry stage) and, if PayPal allows it, submits your
     tracking number as proof of shipment.
   - **Offer**: sends the offer (inquiry stage only).
   - **Refund**: accepts the claim; PayPal refunds the full disputed amount.
5. The result appears on the timeline ("Sent to PayPal" or "PayPal rejected action").
   Clearline checks a fresh copy of the dispute first and refuses actions PayPal doesn't
   currently allow. It never retries a rejected action on its own.

### 4. Auto-sync (safety net for missed webhooks)

Quick tunnels die (sleep, network changes), and PayPal's webhooks then go nowhere. The app
therefore also pulls disputes from PayPal every `AUTO_SYNC_MINUTES` (default 3; `0` turns it
off). It only reads from PayPal. "Last synced: … (auto)" next to **Sync disputes** shows when
it last ran.

### 5. Sandbox test tool: "PayPal asks the seller for evidence"

Sandbox claims often sit in `UNDER_REVIEW`, where the seller can't act. On `/seller`, such
disputes show a dashed button **Sandbox: simulate PayPal asking the seller for evidence**. It
calls PayPal's sandbox-only *Update dispute status* endpoint (`require-evidence` with
`{"action": "SELLER_EVIDENCE"}`), which moves the dispute to `WAITING_FOR_SELLER_RESPONSE` so
the AI recommendation and Approve flow can be tested. It doesn't exist in live mode (the
button is hidden and the endpoint returns 404).

### 6. Evidence findings (PayPal Sandbox, claim stage)

**Clearline only uploads files the seller provides; it never generates proof.**

On an item-not-received claim (`CHARGEBACK` stage, `WAITING_FOR_SELLER_RESPONSE`) where PayPal
requested `PROOF_OF_FULFILLMENT`, `PROOF_OF_REFUND` and `PROOF_OF_DELIVERY_SIGNATURE`, we tried
these `provide-evidence` requests, one at a time:

| # | Request | Result |
|---|---|---|
| 1 | `PROOF_OF_FULFILLMENT`, tracking only (multipart `input`) | 400 `EVIDENCE_TYPE_IS_NOT_ALLOWED` |
| 2 | Same, as plain JSON | 400 `EVIDENCE_TYPE_IS_NOT_ALLOWED` |
| 3 | `PROOF_OF_FULFILLMENT`, notes only | 400 `EVIDENCE_TYPE_IS_NOT_ALLOWED` |
| 4 | `PROOF_OF_FULFILLMENT`, tracking + PDF in `evidence-file` | 400 `EVIDENCE_TYPE_IS_NOT_ALLOWED` |
| 5 | `PROOF_OF_FULFILLMENT`, PDF + notes | 400 `EVIDENCE_TYPE_IS_NOT_ALLOWED` |
| 6 | `PROOF_OF_DELIVERY_SIGNATURE`, PDF + notes | **200**, filed by PayPal as `OTHER`; dispute went to `UNDER_REVIEW` |

So in the claim stage PayPal refused proof of fulfillment in every form, and accepted a
document-backed delivery-signature submission. Clearline's Fight flow therefore works like this:

- **Inquiry stage:** tracking evidence (`PROOF_OF_FULFILLMENT`), then the message to the buyer.
- **Claim stage:** the seller attaches their own document (one PDF, JPG, PNG or GIF, at most
  5 MB) and picks the evidence type from what PayPal requests. It is sent like request 6:
  JSON in `input` (`evidence_type`, `notes`, `documents: [{"name": ...}]`) plus the file in
  `evidence-file`. The server checks the file type by its content, the size and the name; the
  timeline keeps only the file's name, type, size and SHA-256, never its contents.

## Day 4: buyer clarity page

Most "item not received" and "I don't recognise this" disputes start because the buyer can't
quickly see what they paid for and where it is. Day 4 gives every buyer a private page that
answers that instantly, plus one-tap ways to ask the seller before going to PayPal.

### What the buyer sees

After paying, the checkout page shows **"Questions about this order?"**, a private link
`/o/<token>`. The token is an HMAC of the order ID with `APP_SECRET`, so it can't be guessed;
unknown or forged links get a 404. No login is needed. The page shows:

- seller name (`SELLER_DISPLAY_NAME`), item, amount, date, "Paid with PayPal", country, and
  only the last 6 characters of the order ID; never the buyer's email, name or address
- shipping status: carrier, tracking number and ship date; **never an invented delivery date**
- a one-line summary. An AI writes it in the background the first time a buyer opens a given
  order state, and it is cached until something changes (e.g. shipping or a refund). The page
  never waits for it: until it's ready, or if the AI fails or adds a number that isn't in the
  facts, a plain template sentence is shown. The page answers in milliseconds.
- three help buttons: **I don't recognise this charge** (explains it, then "Still not right?
  Contact the seller"), **My order hasn't arrived** (tracking + message), **I want a refund**
  (reason + message). Messages are stored with emails, links and phone numbers removed, are
  always shown escaped, and are limited to 5 per link per hour.
- the seller's replies, and refunds ("Refund of 40.00 USD sent").

### What the seller sees

On `/seller`, each captured order has its buyer link (with **Copy link**) and a **Buyer
requests** list with a reply box; replies appear on the buyer's page. Refund requests also get
**Refund full amount** and **Refund partial…** buttons. They call PayPal's
`POST /v2/payments/captures/{capture_id}/refund` only after you confirm: full = empty body
(PayPal refunds what's left), partial = `{"amount": {"currency_code", "value"}}`. The amount
is checked first (more than 0, at most the captured amount minus earlier refunds), and
nothing is retried if PayPal refuses. At the top, **"Questions answered before a dispute: N"**
counts orders whose buyer asked a question and never opened a dispute.

### Seller login (set this up first)

Everything except checkout, the buyer page and its API, and PayPal webhooks needs the seller
login: `/seller`, orders, timelines, tracking, disputes, AI, sync, refunds and `/docs`. Add to
`.env`, then restart uvicorn (it doesn't reload on `.env` changes):

```
APP_SECRET=<long random value>     # python -c "import secrets; print(secrets.token_urlsafe(32))"
SELLER_PASSWORD=<your password>
```

- Without `SELLER_PASSWORD`, `/seller` shows "set SELLER_PASSWORD in .env" and seller APIs
  return 503.
- Without `APP_SECRET`, a random one is used and the app logs a warning: buyer links and
  logins then stop working after each restart.
- Log in at `/login`. The session cookie is HttpOnly, SameSite=Lax, Secure over https (ngrok),
  and lasts 12 hours. **Log out** ends it. After 5 wrong passwords, logins from that client
  pause for 15 minutes. Webhooks and the auto-sync keep working without anyone logged in.

### Test it

1. Set `APP_SECRET` and `SELLER_PASSWORD`, restart uvicorn, and log in at
   http://localhost:8000/login.
2. Pay an order on http://localhost:8000, then click **Questions about this order?** (or
   **Copy link** on `/seller`).
3. On the buyer page, click **I want a refund**, pick a reason and send.
4. On `/seller`, reply to it, then click **Refund partial…** (e.g. 40.00) and confirm.
5. Reload the buyer page: your reply and "Refund of 40.00 USD sent" appear.

## Day 5: invoices, reminders and India export paperwork

### Invoices (PayPal Invoicing v2)

On `/seller` → **Invoices** → **New invoice**, type what you want to bill, e.g.
*"Bill Acme Corp 1200 USD for logo design, due in 14 days"*, and click **Draft with AI**.

- The AI only fills the form: client name, line items, currency, due date. Emails, links and
  phone numbers are removed from your text before the AI sees it.
- Its draft is rejected if it uses an amount, quantity, client name, currency or due date
  that isn't in your text, or adds an email. No AI key, or AI down? Fill in the form yourself.
  These AI calls never switch the app's AI provider.
- **You type the client's email** in its own field; the AI never fills it.
- **Create draft** makes a PayPal *draft* invoice. Nothing is sent to the client.
- **Send invoice** (in the list, a separate click) sends it by PayPal email.

The list shows number, client, amount, due date and status. Status is Draft, Sent, Paid,
Overdue, Cancelled or Refunded. PayPal has no "overdue" status, so Clearline shows Overdue for
a sent, unpaid invoice past its due date. Statuses update from PayPal's `INVOICING.INVOICE.*`
webhooks and from the background auto-sync, and each invoice keeps its own timeline (keyed by
invoice ID).

**Smart reminders:** an overdue invoice gets **Send reminder**. The AI drafts a short, polite
note whose tone depends on how many days overdue it is. Facts only: drafts with threats, late
fees, interest, legal language or invented numbers are rejected and replaced by a plain
template. You edit it and click to send it through PayPal's `remind` endpoint. Nothing is
sent automatically, and PayPal allows 2 reminders per invoice per day.

**Paid invoices** appear in your orders with a buyer link like any order. The buyer page shows
the last 6 characters of the invoice number, what was billed, and the amount; there's no
shipping section for invoices. Refund buttons aren't offered for invoice payments yet.

**PayPal webhook events to tick** (Developer Dashboard → your app → Webhooks):
`INVOICING.INVOICE.CREATED`, `INVOICING.INVOICE.UPDATED`, `INVOICING.INVOICE.SENT`,
`INVOICING.INVOICE.SCHEDULED`, `INVOICING.INVOICE.PAID`, `INVOICING.INVOICE.UNPAID`,
`INVOICING.INVOICE.CANCELLED`, `INVOICING.INVOICE.REFUNDED`,
`INVOICING.INVOICE.AUTOMATIC-REMINDER-SENT`. (The `USAGE-BILLING.INVOICE.*` events belong to
PayPal's usage-based billing product, not Invoicing.)

### India export paperwork checklist

*Guidance, not legal or tax advice.* With `SELLER_COUNTRY=IN` (the default), every payment
received from abroad gets an **Export paperwork** box:

- **Purpose code:** pick one from the 14 codes PayPal accepts (PayPal's official
  `purpose_code_enum`, "Required only for India"). **Ask AI to suggest** gives one code with a
  one-line reason. It is shown as a suggestion only and never set for you. Note: PayPal
  applies the purpose code set **on your PayPal account** (Settings → Purpose code); its API
  has no per-invoice or per-payment purpose code, so this box is your record of what the
  payment was for. Check that your account's code matches.
- **FIRA:** tick "downloaded" once you have it. PayPal's free monthly digital FIRA covers the
  previous month's international payments and is ready by the 15th (Reports → Statements →
  FIRA); single transactions need a Custom FIRA request.
- **Sources:** [PayPal: What is the purpose code?](https://www.paypal.com/in/cshelp/article/what-is-the-purpose-code-help978),
  [PayPal: Change your purpose code](https://www.paypal.com/in/cshelp/article/how-do-i-change-my-purpose-code-help977),
  [PayPal: Monthly Digital FIRA](https://www.paypal.com/in/brc/article/paypal-monthly-digital-firc),
  [PayPal: purpose code list](https://developer.paypal.com/api/partner-referrals/v2/definitions/purpose_code_enum),
  [RBI: purpose codes for forex receipts](https://www.rbi.org.in/upload/notification/pdfs/52220.pdf).

### Test it

1. On `/seller`, open **New invoice**, type the example sentence, click **Draft with AI**, add
   a client email (a Sandbox personal account), then **Create draft**.
2. Click **Send invoice**. Log in to https://www.sandbox.paypal.com as that buyer and pay it.
3. Within a few minutes (webhook or auto-sync) it shows **Paid** and appears in your orders
   with a buyer link and the Export paperwork box.

## Day 6: AI agents (MCP server)

Clearline is also an [MCP](https://modelcontextprotocol.io) server, so a buyer's or seller's AI
assistant can use it directly. It runs inside the app (no extra process) at:

```
https://<your-domain>/mcp        e.g. https://copper-chastise-waggle.ngrok-free.dev/mcp
```

Transport: **Streamable HTTP**, stateless with JSON replies (official MCP Python SDK `mcp==2.3.0`,
spec 2026-07-28; older 2025-x clients work too). `/agents` (seller login) shows the URL, the
tools and the last 20 agent calls (tool, time, result, order ref; no personal data).

| Tool | For | Credential | What it does |
|---|---|---|---|
| `get_order_status` | buyer | order link | Item, amount, date, seller, shipping, latest seller reply, refunds |
| `message_seller` | buyer | order link | Message the seller (PII-stripped, max 1000 chars, rate-limited) |
| `request_refund` | buyer | order link | Ask for a refund (reason, message, optional amount); auto-resolve policy applies |
| `get_request_status` | buyer | order link | The buyer's requests, statuses and seller replies |
| `list_attention_items` | merchant | Bearer key | Disputes waiting for you (due dates), unanswered buyer requests, overdue invoices |
| `get_dispute_brief` | merchant | Bearer key | Facts, latest AI recommendation, what PayPal allows now |
| `money_in_flight` | merchant | Bearer key | Captured, refunded, disputed, invoiced-unpaid totals per currency |

- **Buyer tools** take the buyer's private link (`https://…/o/<token>`) as `order_link`. It
  scopes the call to that one order; forged or unknown links get "Unknown order link".
- **Merchant tools** need `Authorization: Bearer <MERCHANT_API_KEY>` (new `.env` setting, then
  restart). Without the key set they don't exist. They are **read-only**: refunds and dispute
  answers stay your click in `/seller`, and the tools return that link.
- Buyer-written text is treated as data, never instructions: PII-stripped, length- and
  rate-limited, shown escaped, and marked `buyer_text` for merchant agents.

### Connect an agent

Generate a key and add it to `.env` as `MERCHANT_API_KEY=` (only for merchant tools):
`.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"`

- **Claude Code** (buyer tools / merchant tools):
  ```powershell
  claude mcp add --transport http clearline-buyer https://<your-domain>/mcp
  claude mcp add --transport http clearline-seller https://<your-domain>/mcp --header "Authorization: Bearer <MERCHANT_API_KEY>"
  ```
- **Claude Desktop**: Settings → Connectors → *Add custom connector* → URL
  `https://<your-domain>/mcp` (buyer tools; custom connectors don't send custom headers). For the
  merchant tools, add to `claude_desktop_config.json`:
  ```json
  {"mcpServers": {"clearline-seller": {"command": "npx", "args": ["-y", "mcp-remote",
    "https://<your-domain>/mcp", "--header", "Authorization: Bearer <MERCHANT_API_KEY>"]}}}
  ```
- **MCP Inspector**: `npx @modelcontextprotocol/inspector`, Transport *Streamable HTTP*, URL
  `https://<your-domain>/mcp`, connection *Via Proxy*; for merchant tools add the header
  `Authorization: Bearer <MERCHANT_API_KEY>`. Then *List Tools* and call one.

### Example prompts

- Buyer agent: *"Here's my order link https://<your-domain>/o/… — where is my order, and has
  the seller replied?"* · *"It never arrived. Ask for a refund (reason: not received) and tell
  me what happens."*
- Merchant agent: *"What needs my attention in Clearline today? Sort by due date."* ·
  *"Brief me on dispute PP-D-… and what PayPal lets me do."* · *"How much money is in flight?"*

### Auto-resolve policy (off by default)

On `/seller`, **Auto-resolve policy**: on/off, max refund per order (default 0), allowed
reasons, only if the order has no dispute, a daily total cap, and only if not yet shipped.

A refund request (buyer page or buyer agent) that fits **every** rule is refunded at once
through the normal refund path (PayPal-Request-Id, full error recording, no retry). The refund
always names the amount, so PayPal can't refund more than was checked: captured minus refunded,
the per-order max (counting earlier refunds) and today's cap (UTC day), all checked before the
call. It's recorded as `refund_sent` with trigger `auto-policy` and the rule that matched, an
automatic reply tells the buyer, and `/seller` shows an **Auto-resolved** badge (counted in
"Questions answered before a dispute"). Anything else goes to you as before, with the rule that
didn't fit on the timeline (the buyer isn't told which). Invoice payments are never auto-refunded.

### Demo

1. Set the policy on (e.g. max 150, cap 300, reasons "Item not received"), save.
2. Pay an order, copy its buyer link (don't mark it shipped).
3. In Claude Code (`clearline-buyer`): *"My order link is … It never arrived; please ask for a
   refund."* → refunded automatically; `/agents` shows the calls, `/seller` the badge.
4. In `clearline-seller`: *"What needs my attention?"* → it lists items and links to `/seller`.

## Layout

| File | Purpose |
|---|---|
| `app/paypal.py` | REST client: token caching, orders, capture, tracking, webhook verification, disputes and dispute actions |
| `app/store.py` | SQLite timeline of events per order, processed webhook IDs, active AI provider |
| `app/ai.py` | OpenAI-compatible AI client with sticky Gemini → Groq fallback |
| `app/assistant.py` | Facts, prompt, schema and guardrails for dispute recommendations |
| `app/main.py` | FastAPI routes, server-side price catalogue, `/webhooks/paypal`, dispute actions and sync |
| `app/static/buyer.html` | Checkout page using PayPal JS SDK v6 |
| `app/static/seller.html` | Seller's orders, dispute alerts, AI recommendation card with Approve, buyer requests and refunds |
| `app/static/order.html` | Buyer clarity page (`/o/<token>`) |
| `app/static/login.html` | Seller login |
| `app/buyer.py` | Buyer links, buyer page facts, cached AI summary, refund totals |
| `app/auth.py` | Seller login, sessions, rate limits, the public-route list |
| `app/evidence.py` | Checks for seller-provided dispute documents |
| `app/invoicing.py` | Invoice drafts, statuses, reminder drafts, verified purpose codes |
| `app/agents.py` | MCP server (`/mcp`): buyer and merchant tools, call log, Origin/Bearer guard |
| `app/policy.py` | Auto-resolve policy rules for refunds without a seller click |
| `app/static/agents.html` | `/agents`: MCP URL, tools and the last 20 agent calls |
