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

PayPal has to reach your laptop, so we use a free Cloudflare quick tunnel.

### 1. Start the app and a tunnel (PowerShell)

```powershell
winget install --id Cloudflare.cloudflared     # once; open a new terminal afterwards
```

Terminal 1:

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:create_app --factory --reload
```

Terminal 2:

```powershell
cloudflared tunnel --url http://localhost:8000
```

Copy the `https://<random>.trycloudflare.com` URL it prints.

#### Tunnel on Windows (if winget doesn't work)

Use the standalone exe instead. `tools/` is git-ignored.

```powershell
New-Item -ItemType Directory -Force tools
Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile tools\cloudflared.exe
tools\cloudflared.exe tunnel --url http://localhost:8000
```

Wait for `Registered tunnel connection` in the output before using the URL.
If you get Cloudflare **Error 1033** and the log says `QUIC connection failed` /
`HTTP/2 connection is blocked`, your network blocks port 7844 (common on college or
office Wi-Fi). Turn on the Cloudflare WARP VPN (1.1.1.1 app) or use a phone hotspot,
then start the tunnel again.

### 2. Register the webhook

1. Developer Dashboard → **Apps & Credentials** → your Sandbox app → **Webhooks** → **Add Webhook**.
2. Webhook URL: `https://<random>.trycloudflare.com/webhooks/paypal`
3. Select these events:
   - `CUSTOMER.DISPUTE.CREATED`, `CUSTOMER.DISPUTE.UPDATED`, `CUSTOMER.DISPUTE.RESOLVED`
   - `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.REFUNDED`, `PAYMENT.CAPTURE.REVERSED`
4. Save, copy the **Webhook ID**, and put it in `.env`:
   ```
   PAYPAL_WEBHOOK_ID=<the webhook id>
   ```
5. Restart the app (Ctrl+C in terminal 1, run uvicorn again).

> **Warning:** a quick tunnel gets a new random URL every time `cloudflared` restarts.
> Each time, edit the webhook in the Dashboard to the new URL. The Webhook ID stays the same
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
- Nothing arrives: the tunnel URL changed. Update it in the Dashboard.

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

## Layout

| File | Purpose |
|---|---|
| `app/paypal.py` | REST client: token caching, orders, capture, tracking, webhook verification, disputes and dispute actions |
| `app/store.py` | SQLite timeline of events per order, processed webhook IDs, active AI provider |
| `app/ai.py` | OpenAI-compatible AI client with sticky Gemini → Groq fallback |
| `app/assistant.py` | Facts, prompt, schema and guardrails for dispute recommendations |
| `app/main.py` | FastAPI routes, server-side price catalogue, `/webhooks/paypal`, dispute actions and sync |
| `app/static/buyer.html` | Checkout page using PayPal JS SDK v6 |
| `app/static/seller.html` | Seller's orders, dispute alerts, AI recommendation card with Approve |
