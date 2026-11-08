"""The narrated demo film: the live site on the left, "behind the scenes" on the right.

    python -m app.film [--only 1,3,9] [--voice af_heart] [--out DIR]

Records each scene at 1440x1080 with Playwright (the cursor, guards and off-camera sign-ins of
app.record), marks each step as it happens, and pulls that step's proof from the live timeline.
Then, for each cut, it renders a 480px panel per step (a map with the active path, what
happened, how, and the proof), speaks a narration with Kokoro (offline, open source), and stitches
two films with ffmpeg: clearline-demo.mp4 (tight) and clearline-deep-dive.mp4 (fuller). Each step
lasts as long as its narration or its footage, whichever is longer.

Every "How" line names a mechanism that's in the code; CLAIMS below says where.

Changes the demo story like app.record does (evidence sent, a refund, a reminder), so make fresh
ones first: python -m app.demo story --only dispute,refund-to-seller. Needs Playwright, an
ffmpeg with libx264, and Kokoro (kokoro-onnx, with its model files in KOKORO_DIR). Never in CI.
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .e2e import Config, Run, assert_sandbox
from .record import PAUSE, STAGE, Stage, ffmpeg_exe, length, open_card

OUT = r"D:\Temp\claude\clearline\film"
KOKORO_DIR = os.environ.get("KOKORO_DIR", r"D:\Temp\claude\clearline\tts-models")
APP = {"width": 1440, "height": 1080}
PANEL_W = 480
TIME_ZONE, ZONE_LABEL = "Asia/Kolkata", "IST"  # the filmed pages and the panel's proof agree
# While filming: the login's and the sidebar's email blurred, and any email in a page's text shown as "buyer@…".
MASK_EMAILS = r"""
(() => {
  const re = /[\w.+-]+@[\w-]+(\.[\w-]+)+/g;
  const clean = (root) => {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) if (re.test(n.nodeValue)) n.nodeValue = n.nodeValue.replace(re, 'buyer@…');
  };
  document.addEventListener('DOMContentLoaded', () => {
    const s = document.createElement('style');
    s.textContent = '#email, [data-account-email] { filter: blur(7px); }';
    document.head.appendChild(s);
    clean(document.body);
    new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach((n) =>
      n.nodeType === 3 ? (re.test(n.nodeValue) && (n.nodeValue = n.nodeValue.replace(re, 'buyer@…'))) : n.nodeType === 1 && clean(n))))
      .observe(document.body, { childList: true, subtree: true, characterData: true });
  });
})();
"""
LIVE_URL = "https://clearline-cbyb.onrender.com"
REPO_URL = "https://github.com/kk161205/Clearline"

# Where each "How" claim lives in the code (checked before it went into STEPS).
CLAIMS = {
    "PayPal JS SDK v6": "app/config.py sdk_url (web-sdk/v6/core)",
    "Orders v2 create, capture, track": "app/paypal.py create_order, capture_order, add_tracking",
    "PayPal-Request-Id on money calls": "app/paypal.py _request(idempotent=True)",
    "Price from the server's catalogue": "app/catalogue.py CATALOGUE; app/main.py create_order",
    "HMAC-SHA256 buyer link, 404 otherwise": "app/buyer.py buyer_token; app/main.py order_from_token",
    "verify-webhook-signature": "app/paypal.py verify_webhook_signature",
    "Repeats ignored by event id": "app/store.py mark_webhook_processed",
    "Argon2id passwords": "app/accounts.py HASHER",
    "Session row, cookie holds a random token (hashed as the row id), HttpOnly": "app/accounts.py session_id; app/main.py start_session",
    "/setup is 404 once an account exists": "app/main.py setup_page",
    "One summary request, 304 when unchanged": "app/main.py seller_summary",
    "Disputes v1 get, provide-evidence": "app/paypal.py get_dispute, provide_*_evidence",
    "Facts without names or emails": "app/assistant.py build_facts, strip_pii",
    "Reasons must cite facts; FIGHT needs tracking; no emails or links": "app/assistant.py check_guardrails",
    "Gemini first, Groq if it fails": "app/ai.py complete",
    "Checked again before each step": "app/main.py act_on_dispute",
    "Messages PII-stripped": "app/main.py submit_request (strip_pii)",
    "Auto-policy rules": "app/policy.py Policy, decide",
    "Payments v2 refund with the amount": "app/paypal.py refund_capture; app/main.py send_refund",
    "MCP request_refund, scoped by the link": "app/agents.py request_refund",
    "Invoicing v2 remind": "app/paypal.py remind_invoice",
    "No threats, fees or invented numbers": "app/invoicing.py reminder_problems",
    "Insights turns PII-stripped, Groq for that request only": "app/insights_ai.py; app/ai.py chat_tools",
}

# The map in the panel: where each part sits (x, y) in a 440x300 box.
NODES = {"buyer": ("Buyer's browser", 70, 40), "seller": ("Seller's browser", 70, 260),
         "agent": ("AI agent (MCP)", 70, 150), "server": ("Clearline server\n(Render)", 220, 150),
         "paypal": ("PayPal (Sandbox)", 370, 40), "ai": ("Gemini / Groq", 370, 150),
         "db": ("Database (Neon)", 370, 260)}


@dataclass
class Step:
    key: str
    path: list            # [(from, to)] arrows lit up on the map
    what: str
    how: list
    caption: str          # one short line, in the panel under the proof
    short: str            # narration, tight cut
    long: str             # narration, deep dive


S = lambda *a: Step(*a)  # noqa: E731
STEPS = {s.key: s for s in [
    S("intro", [], "", [], "", "Every PayPal payment, clearly.",
      "Every PayPal payment, clearly. A tour of what happens behind each click."),
    S("landing", [("server", "db")], "A console for small sellers on PayPal",
      ["One FastAPI app on Render", "Postgres on Neon; Gemini and Groq for the AI"],
      "Every PayPal payment, on one clear line.",
      "Clearline is for small sellers on PayPal: see where every payment stands, and settle problems before they turn into disputes.",
      "Clearline is a console for small sellers who ship across borders on PayPal. Each order's money sits on one clear line, buyers can ask before they dispute, and AI drafts the answers a seller approves. It's one FastAPI app on Render, with Postgres on Neon."),
    S("create", [("buyer", "server"), ("server", "paypal")], "The buyer clicks the PayPal button",
      ["PayPal JS SDK v6 opens the checkout", "The server creates the order: Orders v2", "The price comes from the server's catalogue"],
      "A buyer pays with PayPal.",
      "A buyer clicks the PayPal button. The server creates the order with PayPal, at its own price.",
      "A buyer clicks the PayPal button, which comes from PayPal's JavaScript SDK, version six. The server creates the order through Orders v2, and the price comes from the server's own catalogue, so nobody can change it in the browser."),
    S("approve", [("buyer", "paypal")], "The buyer approves in PayPal's window",
      ["A PayPal Sandbox buyer account", "Clearline never handles card or bank details"],
      "They approve it in PayPal's own window.",
      "They approve in PayPal's own window. Clearline never sees card details.",
      "The buyer approves in PayPal's own window, signed in with a Sandbox test account. Card and bank details stay with PayPal; Clearline never handles them."),
    S("capture", [("buyer", "server"), ("server", "paypal")], "The payment is captured",
      ["Orders v2 capture", "A PayPal-Request-Id makes a retry safe"],
      "Captured. The money is in.",
      "The server captures the payment, with a request id so a retry can never charge twice.",
      "Back on the page, the server captures the payment through Orders v2. Every call that moves money carries a PayPal request id, so a retry can never charge the buyer twice."),
    S("link", [("buyer", "server")], "A private page for this order, no account",
      ["The link's token: HMAC-SHA256 of the order id", "Unknown or forged links get a 404"],
      "A private order page. No account needed.",
      "The buyer gets a private page for the order: a signed link, no account needed.",
      "The buyer lands on a private page for this order. The link carries an HMAC of the order id, so it can't be guessed, and a forged or unknown link just gets a not found. No account, no password."),
    S("webhook", [("paypal", "server"), ("server", "db")], "PayPal confirms by webhook",
      ["PAYMENT.CAPTURE.COMPLETED", "Checked with verify-webhook-signature", "Repeats ignored by event id"],
      "PayPal confirms it, and Clearline checks the signature.",
      "Seconds later PayPal confirms by webhook. Clearline verifies it with PayPal before it trusts it.",
      "A few seconds later, PayPal sends a webhook to say the capture completed. Clearline asks PayPal to verify its signature before using it, and ignores repeats by event id, then records it on the order's timeline."),
    S("login", [("seller", "server"), ("server", "db")], "The seller signs in",
      ["Password checked with Argon2id", "A session row in the database; the cookie is HttpOnly",
       "/setup is closed once an account exists"],
      "The seller signs in.",
      "The seller signs in. Passwords are checked with Argon2id, and the session lives in the database.",
      "Now the seller signs in. The password is checked against an Argon2id hash. The browser gets an HttpOnly cookie holding a random token, and the database keeps only its hash. The first-account setup page is closed for good once an account exists."),
    S("today", [("seller", "server")], "Today: only what needs the seller",
      ["One summary request; 304 when nothing changed", "Disputes, buyer requests, overdue invoices, orders to ship"],
      "Today shows only what needs you.",
      "Today shows only what needs the seller. One request refreshes it, and nothing at all when nothing changed.",
      "Today lists only what needs the seller: disputes, buyer questions, overdue invoices, orders to ship. The whole page refreshes with one request, and when nothing has changed the server answers with an empty not modified."),
    S("ship", [("seller", "server"), ("server", "paypal")], "Shipped with DHL",
      ["Tracking sent to PayPal: Orders v2 track", "The buyer's page shows it too"],
      "Add tracking. PayPal and the buyer see it.",
      "Shipping is one step. The tracking goes to PayPal, and the buyer's page shows it.",
      "Shipping is one step: the tracking number goes to PayPal through the Orders track call, which matters if a dispute comes later, and the buyer's private page shows it straight away."),
    S("dispute", [("buyer", "paypal"), ("paypal", "server"), ("server", "db")], "The buyer opened a dispute at PayPal",
      ["CUSTOMER.DISPUTE webhook, verified", "The full case fetched: Disputes v1"],
      "A buyer says the item never arrived.",
      "Here a buyer has told PayPal the item never arrived. The webhook brings the case in.",
      "On this shipped order, the buyer told PayPal the item never arrived. A dispute webhook arrives, Clearline verifies it, and fetches the full case from the Disputes API."),
    S("ai", [("server", "ai")], "AI recommends an answer",
      ["Facts from the timeline, no names or emails", "Every reason must cite a fact; guardrails check",
       "Gemini first, Groq if Gemini fails"],
      "AI drafts the answer from the facts.",
      "AI reads the facts, with no names or emails, and recommends fighting it with the tracking. Each reason cites a fact.",
      "The AI gets the facts from the order's timeline, with no names or emails, and recommends fighting the claim with the tracking. Every reason has to cite a fact, and guardrails reject anything else, like fighting a non-delivery claim without tracking. Gemini answers first; Groq takes over if Gemini fails."),
    S("approve-dispute", [("seller", "server"), ("server", "paypal")], "The seller approves: evidence sent",
      ["Nothing goes to PayPal until the seller clicks", "Disputes v1 provide-evidence, checked again before each step"],
      "You approve. Clearline sends the evidence.",
      "Nothing goes to PayPal until the seller approves. Then the evidence is sent.",
      "Nothing is sent until the seller approves. Clearline checks the case again before each step, then sends the tracking as evidence through the Disputes API."),
    S("request", [("buyer", "server")], "The buyer asks for a refund",
      ["The message is stored without emails or phone numbers", "Auto-policy: a shipped order goes to the seller"],
      "Buyers can ask before they dispute.",
      "Another buyer asks for a refund on their order page. It has shipped, so the policy leaves it to the seller.",
      "Another buyer uses their order page to ask for a refund. Their message is stored without emails or phone numbers. The seller's auto-refund rules only cover orders that haven't shipped, so this one waits for the seller."),
    S("refund", [("seller", "server"), ("server", "paypal")], "The seller refunds part of it",
      ["Payments v2 refund with the exact amount", "Checked against what's left to refund"],
      "You decide, even a partial refund.",
      "The seller refunds forty dollars of it, checked against what's left to refund.",
      "The seller decides on a partial refund of forty dollars. The amount is checked against what's left before the Payments refund call, which always names the exact amount."),
    S("refund-webhook", [("paypal", "server"), ("server", "db")], "PayPal confirms the refund",
      ["PAYMENT.CAPTURE.REFUNDED webhook, verified"],
      "PayPal confirms the refund.",
      "PayPal confirms the refund by webhook, too.",
      "And PayPal confirms the refund with a webhook of its own, verified like every other."),
    S("mcp", [("agent", "server")], "A buyer's AI agent asked for a refund",
      ["MCP tool call: request_refund", "Scoped to one order by the buyer's private link"],
      "AI agents can use Clearline too.",
      "A buyer's own AI agent asked for a refund through Clearline's MCP server, using the buyer's private link.",
      "Clearline is also an MCP server. Here a buyer's own AI agent called the request refund tool, with the buyer's private link, which limits it to that one order. Every call is logged here, without personal data."),
    S("policy", [("server", "paypal")], "Refunded by the seller's own rules",
      ["Auto-policy: allowed reasons, per-order and daily limits, not shipped", "Payments v2 refund, no click needed"],
      "Your rules refunded it at once.",
      "It fit the seller's rules, so Clearline refunded it at once, within the daily limit.",
      "It fit the seller's own rules: an allowed reason, under the per-order and daily limits, and not shipped yet. So Clearline refunded it straight away, with no click, and the buyer's agent was told."),
    S("overdue", [("seller", "server")], "An invoice is overdue",
      ["Invoicing v2 status, synced from PayPal"],
      "An invoice is overdue.",
      "An invoice to Kapoor Interiors is overdue.",
      "An invoice to Kapoor Interiors is past its due date; Clearline keeps invoice status in step with PayPal."),
    S("draft", [("server", "ai")], "AI drafts a polite reminder",
      ["Only the invoice's facts go to the AI", "No threats, fees or invented numbers; else a plain template"],
      "AI drafts a polite reminder.",
      "AI drafts a polite reminder. Threats, fees or invented numbers would be rejected.",
      "The AI drafts a reminder from the invoice's facts alone. A draft with threats, fees or numbers that aren't in the facts is rejected, and a plain template is used instead."),
    S("remind", [("seller", "server"), ("server", "paypal")], "The reminder is sent through PayPal",
      ["Invoicing v2 remind"],
      "You send it through PayPal.",
      "The seller sends it, and PayPal emails the client.",
      "The seller can edit it, then sends it; PayPal emails the client through the Invoicing remind call."),
    S("dashboard", [("seller", "server"), ("server", "db")], "Money in flight",
      ["Built in one pass from every order's timeline", "No personal data on this page"],
      "All the money in flight, in one view.",
      "The dashboard shows all the money in flight, built from the same timelines, with no personal data.",
      "The dashboard shows where all the money is: settled, waiting, disputed, unpaid. It's built in one pass from the same timelines, and carries no personal data."),
    S("ask", [("seller", "server"), ("server", "ai")], "A question in plain words",
      ["AG Studio's agent; each turn goes through Clearline", "PII-stripped; Groq takes over for that request only"],
      "Ask about your shop in plain words.",
      "In Insights, the seller just asks: which orders need me this week?",
      "In Insights, the seller asks in plain words: which orders need me this week? Each turn of the AG Studio agent goes through Clearline, stripped of personal data, to Gemini, with Groq standing in for that request only."),
    S("chart", [("server", "ai")], "A chart, added by the AI",
      ["Read-only: it arranges the report, it can't move money"],
      "It can add a chart, too.",
      "And it can build a chart: refunds by buyer country.",
      "It can build the report too, here a chart of refunds by buyer country. It's read-only: it can arrange the report, but it can't move money."),
    S("status", [("server", "paypal"), ("server", "db"), ("server", "ai")], "Every check, run live",
      ["Database, PayPal token, webhook id, AI keys", "Shown to the seller only"],
      "Checked and green.",
      "And System status runs every check live: all green.",
      "Finally, System status runs every check live: the database, the PayPal token, the webhook id, the AI keys. All green."),
    S("outro", [], "", [], "", "Try it live, and read the code on GitHub.",
      "That's Clearline. Try it live at the address on screen, and read the code on GitHub."),
]}


@dataclass
class Shot:
    scene: int
    title: str
    marks: list = field(default_factory=list)   # (step key, scene clock seconds, proof function)
    clips: list = field(default_factory=list)   # (path, src_offset, a, b, crop)
    cuts: list = field(default_factory=list)    # (a, b) scene clock seconds to drop
    status: str = "PASS"
    detail: str = ""
    proofs: dict = field(default_factory=dict)


class Film(Stage):
    def __init__(self, run: Run, out: Path):
        super().__init__(run, out)
        self.shot: Shot | None = None
        self.started = 0.0
        self.extra = {}

    def context(self, who: str, folder: Path):
        state = self.run.states.get(who) if who else None
        context = self.run.browser.new_context(
            viewport=APP, record_video_dir=str(folder), record_video_size=APP, color_scheme="light",
            timezone_id=TIME_ZONE, storage_state=str(state) if state and state.exists() else None)
        context.add_init_script(STAGE)
        context.add_init_script(MASK_EMAILS)
        self.run.watch(context, self.flow)
        return context

    def now(self) -> float:
        return time.monotonic() - self.started

    def step(self, key: str, proof=None):
        """This step starts now. `proof` runs after the scene, so its data has arrived."""
        self.ready()
        self.shot.marks.append((key, self.now(), proof))

    def cut(self, a: float, b: float):
        if b - a > 0.3:
            self.shot.cuts.append((a, b))

    def wait_cut(self, check, seconds: float, keep: float = 1.2):
        """Wait for `check`, and drop the waiting from the film (all but `keep` s at each end)."""
        a = self.now()
        deadline = time.monotonic() + seconds
        ok = False
        while time.monotonic() < deadline:
            if check():
                ok = True
                break
            time.sleep(2)
        self.cut(a + keep, self.now() - keep)
        return ok


def when(ts: float) -> str:
    """In the time zone the filmed pages show, so the panel and the page agree."""
    return datetime.fromtimestamp(ts, ZoneInfo(TIME_ZONE)).strftime("%H:%M:%S ") + ZONE_LABEL


def event_proof(film: Film, order_id: str, kind: str, fields=(), until: float | None = None) -> str:
    """The latest `kind` event as the panel's proof; with `until`, the latest one by then (a step
    filmed before a later event mustn't show that event's state)."""
    events = [e for e in film.api.get(f"/api/orders/{order_id}/timeline")
              if e["kind"] == kind and (until is None or e["created_at"] <= until)]
    if not events:
        return f"{kind}: not on the timeline yet"
    e = events[-1]
    extra = ", ".join(f"{k} {e['payload'].get(k)}" for k in fields if e["payload"].get(k) is not None)
    return f"{kind} · {when(e['created_at'])}" + (f"\n{extra}" if extra else "")


def kinds(film: Film, order_id: str) -> list[str]:
    return film.api.kinds(order_id)


# ---- Scenes -------------------------------------------------------------------------------------

def s_landing(film: Film, page):
    page.goto(film.config.base + "/", wait_until="load")
    film.step("landing", lambda: "GET / · 200\nThe default Content-Security-Policy, nothing third-party")
    page.wait_for_timeout(1500)
    film.point(page, page.get_by_role("link", name="Try the shop"), click=False)


def s_buy(film: Film, page):
    page.goto(film.config.base + "/shop", wait_until="load")
    button = page.locator("paypal-button")
    button.wait_for(state="visible", timeout=60000)
    created = {}
    page.on("response", lambda r: created.update(id=r.json().get("orderId"))
            if r.request.method == "POST" and r.url.endswith("/api/orders") and r.status == 200 else None)
    film.step("create", lambda: event_proof(film, created["id"], "order_created", ("amount", "currency")))
    with page.expect_popup(timeout=30000) as opening:
        film.point(page, button)
    popup = opening.value
    film.popup = (popup, time.monotonic())
    film.popup_size = popup.evaluate("() => [innerWidth, innerHeight]")
    film.step("approve", lambda: "PayPal checkout window · www.sandbox.paypal.com")
    popup.wait_for_timeout(2000)
    try:
        film.run.approve_popup(popup, film.flow)
    finally:
        film.popup_closed = time.monotonic()  # a failed approval still ends the popup's piece
    film.step("capture", lambda: event_proof(film, created["id"], "payment_captured", ("status",)))
    page.locator("#paid").wait_for(state="visible", timeout=60000)
    page.wait_for_timeout(PAUSE * 2)
    film.order_id = created.get("id")
    film.point(page, page.locator("#paid-help"))
    page.wait_for_load_state("load")
    film.step("link", lambda: "GET /o/… · 200 (the token is never logged)\nA wrong token: 404")
    page.wait_for_timeout(PAUSE * 2)
    film.step("webhook", lambda: event_proof(film, created["id"], "webhook_payment_capture_completed", ("status",)))
    film.wait_cut(lambda: "webhook_payment_capture_completed" in kinds(film, created["id"]), 90)
    page.reload(wait_until="load")
    page.wait_for_timeout(PAUSE * 2)


def s_login(film: Film, page):
    seen = {}
    page.goto(film.config.base + "/login", wait_until="load")

    def cookie():  # the browser's own record of the cookie, read right after the login
        c = seen.get("cookie") or {}
        flags = [name for name, on in (("HttpOnly", c.get("httpOnly")), ("Secure", c.get("secure")),
                                       (f"SameSite={c.get('sameSite')}", c.get("sameSite"))) if on]
        setup = film.api.http.get("/setup").status_code
        return f"cookie clearline_session · {' · '.join(flags)}\nGET /setup · {setup}"

    film.step("login", cookie)
    film.type(page, page.locator("#email"), film.config.seller_email)
    page.locator("#password").fill(film.config.seller_password)  # dots on screen
    page.wait_for_timeout(600)
    film.point(page, page.locator("button[type=submit]"))
    page.wait_for_url("**/seller")
    seen["cookie"] = next((c for c in page.context.cookies() if c["name"] == "clearline_session"), None)
    page.locator("#attention ul.attention, #attention .empty").first.wait_for(timeout=60000)

    def today():
        return f"{page_count[0]} items on Today\nGET /api/seller/summary · 200, then 304"

    page_count = [0]
    film.step("today", today)
    page.wait_for_timeout(PAUSE)
    page_count[0] = page.locator("#attention ul.attention li").count()
    for item in page.locator("#attention ul.attention li").all()[:3]:
        film.point(page, item, click=False)


def s_ship(film: Film, page):
    order_id = film.order_id or film.find(lambda o, k, full: "payment_captured" in k and "tracking_added" not in k
                                          and not any(x.startswith("dispute") for x in k) and "refund_sent" not in k,
                                          "order waiting to ship")
    page.goto(film.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    row = open_card(film, page, order_id)
    film.step("ship", lambda: event_proof(film, order_id, "tracking_added", ("carrier", "tracking_number")))
    form = row.locator("form.ship")
    film.type(page, form.locator("[name=tracking_number]"), "1234567890")
    form.locator("[name=carrier]").select_option("DHL")
    film.point(page, form.locator("button"))
    page.get_by_text("Marked as shipped").first.wait_for(timeout=30000)
    page.wait_for_timeout(PAUSE * 2)


def s_dispute(film: Film, page):
    order_id = film.find(lambda o, k, full: "ai_recommendation" in full and "dispute_action_sent" not in full
                         and "dispute_resolved" not in k, "dispute with a recommendation to approve")
    page.goto(film.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    shown = time.time()  # the case as it stands on screen, not after the evidence goes
    film.step("dispute", lambda: event_proof(film, order_id, "dispute_created", ("reason",)) + "\n"
              + event_proof(film, order_id, "dispute_updated", ("status",), until=shown).replace("\n", ", "))
    row = open_card(film, page, order_id)
    film.point(page, row.locator(".dispute"), click=False)
    ai = row.locator(".ai")
    film.step("ai", lambda: event_proof(film, order_id, "ai_recommendation", ("provider", "trigger")).replace(
        "\n", "\n") + "\n" + recommendation_line(film, order_id))
    for part in (ai.locator(".ai-head"), ai.locator("ul, ol").first, ai.locator('[data-field="message_to_buyer"]')):
        if part.count():
            film.point(page, part, click=False)
    film.step("approve-dispute", lambda: event_proof(film, order_id, "dispute_action_sent", ("endpoint",)))
    film.point(page, row.locator("[data-approve]"))
    with page.expect_response(lambda r: r.url.endswith("/act"), timeout=90000) as answered:
        film.point(page, page.locator("[data-ok]"))
    assert answered.value.ok, f"PayPal refused: HTTP {answered.value.status}"
    row.get_by_text("Sent to PayPal").first.wait_for(timeout=30000)
    page.wait_for_timeout(PAUSE)
    film.point(page, row.locator(".history").get_by_text("Sent to PayPal").last, click=False)


def recommendation_line(film: Film, order_id: str) -> str:
    rec = [e for e in film.api.get(f"/api/orders/{order_id}/timeline") if e["kind"] == "ai_recommendation"]
    if not rec:
        return ""
    r = rec[-1]["payload"]["recommendation"]
    cited = sorted({k for reason in r.get("reasons", []) for k in reason.get("fact_keys", [])})
    return f"{r['decision']}, {round(r['confidence'] * 100)}% · cites {', '.join(cited[:3])}"


def s_refund(film: Film, page):
    order_id = film.find(lambda o, k, full: "tracking_added" in k and "buyer_request" in k and "refund_sent" not in k,
                         "shipped order with a refund request")
    link = film.api.get(f"/api/orders/{order_id}/buyer")["link"]
    page.goto(link if link.startswith("http") else film.config.base + link, wait_until="load")
    film.step("request", lambda: event_proof(film, order_id, "buyer_request", ("type",)) + "\n"
              + event_proof(film, order_id, "auto_policy_skipped", ("reason",)).split("\n")[-1])
    page.wait_for_timeout(PAUSE)
    film.glide(page, 500)
    page.goto(film.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    row = open_card(film, page, order_id)
    buyer = row.locator(".buyer")
    film.point(page, buyer.locator(".req").first, click=False)
    film.step("refund", lambda: event_proof(film, order_id, "refund_sent", ("amount", "currency", "mode")))
    film.point(page, buyer.locator('[data-refund="partial"]').first)
    film.type(page, page.locator("[data-input]"), "40.00")
    film.point(page, page.locator("[data-ok]"))
    page.get_by_text("Refund of 40.00 USD sent").first.wait_for(timeout=60000)
    page.wait_for_timeout(PAUSE)
    film.step("refund-webhook", lambda: event_proof(film, order_id, "webhook_payment_capture_refunded", ("status",)))
    film.wait_cut(lambda: "webhook_payment_capture_refunded" in kinds(film, order_id), 120)
    page.reload(wait_until="load")
    row = open_card(film, page, order_id)
    film.point(page, row.locator(".history li").last, click=False)


def s_agent(film: Film, page):
    order_id = film.find(lambda o, k, full: "refund_sent" in k and any(
        e["kind"] == "buyer_request" and e["payload"].get("via") == "agent" for e in o["events"]), "agent refund")
    page.goto(film.config.base + "/agents", wait_until="load")

    def call():
        calls = [c for c in film.api.get("/api/agents")["calls"] if c["tool"] == "request_refund"]
        c = calls[0] if calls else None
        return f"request_refund · {when(c['at'])}\nresult: {c['outcome']} · order …{c['order_ref']}" if c else "no call logged"

    film.step("mcp", call)
    page.wait_for_timeout(PAUSE)
    film.point(page, page.locator("#calls tr").first, click=False)
    page.goto(film.config.base + "/seller", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)
    row = open_card(film, page, order_id)
    film.step("policy", lambda: event_proof(film, order_id, "refund_sent", ("amount", "trigger", "rule")))
    for stop in row.locator(".history li").all()[-3:]:
        film.point(page, stop, click=False)
    film.point(page, row.locator(".buyer").get_by_text("via AI agent").first, click=False)


def s_invoice(film: Film, page):
    page.goto(film.config.base + "/seller", wait_until="load")
    item = page.locator("#attention ul.attention li").filter(has_text="overdue").first
    item.wait_for(timeout=60000)
    invoice_id = film.extra.get("invoice_id")
    film.step("overdue", lambda: event_proof(film, invoice_id, "invoice_status", ("number", "status", "due_date"))
              if invoice_id else "invoice_status: overdue")
    film.point(page, item, click=False)
    drafted = {}
    page.on("response", lambda r: drafted.update(r.json()) if r.url.endswith("/reminder-draft") and r.ok else None)
    film.step("draft", lambda: f"reminder draft · source {drafted.get('source')}, {drafted.get('provider') or 'template'}"
              f"\n{drafted.get('days_overdue')} day{'' if drafted.get('days_overdue') == 1 else 's'} overdue")
    film.point(page, item.locator("button"))
    cell = page.locator("[data-reminder]").filter(has=page.locator("[data-rem-send]")).first
    cell.locator("[data-rem-send]").wait_for(state="visible", timeout=90000)
    film.point(page, cell.locator("textarea, input").first, click=False)
    film.step("remind", lambda: event_proof(film, invoice_id, "invoice_reminder_sent", ("days_overdue",))
              if invoice_id else "invoice_reminder_sent")
    film.point(page, cell.locator("[data-rem-send]"))
    with page.expect_response(lambda r: r.url.endswith("/remind"), timeout=90000) as answered:
        film.point(page, page.locator("[data-ok]"))
    if answered.value.status != 200:
        film.shot.detail = f"PayPal didn't send the reminder (HTTP {answered.value.status}); filmed up to the draft."
        page.wait_for_timeout(PAUSE * 2)
        return
    page.get_by_text("Reminder sent").first.wait_for(timeout=60000)
    page.wait_for_timeout(PAUSE)


def s_dashboard(film: Film, page):
    page.goto(film.config.base + "/dashboard", wait_until="load")
    page.locator(".kpi").first.wait_for(timeout=60000)
    kpis = {}

    def proof():
        k = film.api.get("/api/dashboard")["kpis"]
        return (f"captured {k['captured']} · refunded {k['refunded']}\nopen disputes {k['disputes_open']} · "
                f"unpaid invoices {k['invoices_unpaid']}").replace("'", "")

    film.step("dashboard", proof)
    page.wait_for_timeout(PAUSE)
    for kpi in page.locator(".kpi").all()[:3]:
        film.point(page, kpi, click=False)
    film.glide(page, 700, steps=14)


def s_insights(film: Film, page):
    saved = film.api.layout()
    turns, takes = {"n": 0, "failed": 0}, {}
    page.on("response", lambda r: turns.update(n=turns["n"] + 1, failed=turns["failed"] + (r.status >= 400))
            if r.url.endswith("/api/insights/ai") else None)
    try:
        page.goto(film.config.base + "/insights", wait_until="load")
        page.locator("canvas").first.wait_for(state="visible", timeout=60000)
        film.ready()
        film.point(page, page.get_by_role("button", name="Edit layout"))
        box = page.locator("textarea.ag-studio-ai-assistant-textarea")
        if not box.is_visible():
            film.point(page, page.locator(".ag-studio-panel-header", has_text="AI Assistant"))
        for key, question in (("ask", "Which orders need me this week?"),
                              ("chart", "Add a bar chart of refunded amount by buyer country")):
            for attempt in range(3):  # a failed step means a fresh chat: only the clean take stays
                take = film.now()
                if attempt == 0:
                    film.step(key, lambda key=key: takes.get(key, ""))
                turns.update(n=0, failed=0)  # count this take only
                film.point(page, page.locator(".ag-studio-ai-thread-new-button"))
                film.type(page, box, question)
                film.point(page, page.locator(".ag-studio-ai-assistant-send-button"))
                stop = page.get_by_role("button", name="Stop")
                stop.first.wait_for(state="visible", timeout=30000)
                film.wait_cut(lambda: not stop.first.is_visible(), 180, keep=1.0)
                page.wait_for_timeout(PAUSE)
                takes[key] = f"{turns['n']} AI turns through /api/insights/ai · {turns['failed']} failed" + (
                    f"\n(take {attempt + 1}: the earlier take had a failed step)" if attempt else "")
                chat = page.locator(".ag-studio-panel", has_text="AI Assistant").first.inner_text()
                failed = page.locator(".ag-studio-ai-step-row-failed").count() or re.search(r"\b\d+ failed\b|could not", chat, re.I)
                if not failed:
                    if key == "ask":
                        page.wait_for_timeout(2000)  # the first answer stays on screen before the next question
                    break
                film.cut(take, film.now())  # drop the take with a failed step
        found = page.evaluate("""() => {
          const titles = [...document.querySelectorAll('#studio *')].filter((e) => e.children.length === 0
            && /by buyer country/i.test(e.textContent) && !e.closest('[class*=ai-]'));
          let el = titles.pop();
          if (!el) return false;
          el.scrollIntoView({ block: 'start', behavior: 'smooth' });  // its title at the top: the whole chart in view
          return true;
        }""")
        page.wait_for_timeout(PAUSE * 2)
        if not found:
            film.shot.detail = "The new chart wasn't found to bring into view."
    finally:
        film.api.restore_layout(saved)


def s_status(film: Film, page):
    page.goto(film.config.base + "/seller#settings", wait_until="load")
    page.locator("#order-list article").first.wait_for(timeout=60000)

    def proof():
        status = film.api.get("/api/system/status")
        bad = [c["name"] for c in status["checks"] if c["ok"] is False]
        return f"{len(status['checks'])} checks · {'all passed' if not bad else 'failed: ' + ', '.join(bad)}\nversion {status['version']}"

    film.step("status", proof)
    film.point(page, page.locator("#system summary"))
    page.locator("#system-checks dt").first.wait_for(timeout=30000)
    page.locator("#system-checks").evaluate("e => e.scrollIntoView({block: 'center', behavior: 'smooth'})")
    page.wait_for_timeout(PAUSE * 2)


SCENES = [(1, "Landing page", "seller", s_landing), (2, "A buyer pays", "buyer", s_buy),
          (3, "Seller login", None, s_login), (4, "Ship", "seller", s_ship), (5, "Dispute", "seller", s_dispute),
          (6, "Refund request", "seller", s_refund), (7, "Agent refund", "seller", s_agent),
          (8, "Overdue invoice", "seller", s_invoice), (9, "Money in flight", "seller", s_dashboard),
          (10, "Insights", "seller", s_insights), (11, "System status", "seller", s_status)]


def film_scene(film: Film, number: int, title: str, who, fn) -> Shot:
    shot = Shot(number, title)
    film.shot, film.popup, film.begin = shot, None, None
    folder = film.out / f"raw-{number:02d}"
    context = film.context(who, folder)
    page = context.new_page()
    film.started = time.monotonic()
    print(f"== scene {number}: {title}", flush=True)
    try:
        fn(film, page)
    except Exception as err:
        shot.status, shot.detail = "FAIL", f"{type(err).__name__}: {str(err)[:240]}"
        film.flow.shot(f"scene {number} failed", page)
    finally:
        end = film.now()
        popup = film.popup
        context.close()
    for key, at, proof in shot.marks:
        try:
            shot.proofs[key] = proof() if proof else ""
        except Exception as err:
            shot.proofs[key] = f"(proof unavailable: {type(err).__name__})"
    begin = (film.begin - film.started - 0.3) if film.begin else 0
    main = Path(page.video.path())
    pieces = [(main, 0.0, begin, end, None)]
    if popup:
        opened, closed = popup[1] - film.started, film.popup_closed - film.started
        pieces = [(main, 0.0, begin, opened, None), (Path(popup[0].video.path()), opened, opened, closed, tuple(film.popup_size)),
                  (main, 0.0, closed, end, None)]
    for a, b in sorted(shot.cuts):  # take the cut intervals out of whichever piece holds them
        out = []
        for path, off, x, y, crop in pieces:
            if b <= x or a >= y:
                out.append((path, off, x, y, crop))
                continue
            if a > x:
                out.append((path, off, x, a, crop))
            if b < y:
                out.append((path, off, b, y, crop))
        pieces = out
    shot.clips = [p for p in pieces if p[3] - p[2] > 0.2]
    print(f"   {shot.status} {shot.detail}", flush=True)
    return shot


# ---- Rendering ------------------------------------------------------------------------------------

def timeline(shot: Shot, t: float) -> float:
    """Where scene-clock time t lands in the scene's base video."""
    pos = 0.0
    for _, _, a, b, _ in shot.clips:
        if t < a:
            return pos
        if t < b:
            return pos + (t - a)
        pos += b - a
    return pos


def base_video(ff: str, shot: Shot, out: Path) -> Path:
    target = out / f"base-{shot.scene:02d}.mp4"
    inputs, parts = [], []
    for i, (path, off, a, b, crop) in enumerate(shot.clips):
        inputs += ["-ss", f"{max(a - off, 0):.3f}", "-t", f"{b - a:.3f}", "-i", str(path)]
        cut = f"crop={min(crop[0], APP['width'])}:{min(crop[1], APP['height'])}:0:0,scale=-2:{APP['height'] - 60}," if crop else ""
        parts.append(f"[{i}:v]{cut}scale={APP['width']}:{APP['height']}:force_original_aspect_ratio=decrease,"
                     f"pad={APP['width']}:{APP['height']}:(ow-iw)/2:{'30' if crop else '(oh-ih)/2'}:color=0xF6F8FA,fps=30,setsar=1[v{i}]")
    graph = ";".join(parts) + ";" + "".join(f"[v{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=0[out]"
    subprocess.run([ff, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-pix_fmt", "yuv420p", str(target)], check=True)
    return target


def map_svg(path: list) -> str:
    lit = {(a, b) for a, b in path}
    involved = {n for edge in lit for n in edge}
    edges = [("buyer", "server"), ("seller", "server"), ("agent", "server"), ("server", "paypal"), ("server", "ai"),
             ("server", "db"), ("buyer", "paypal"), ("paypal", "server")]
    lines = []
    for a, b in edges:
        on = (a, b) in lit or (b, a) in lit
        if (a, b) == ("paypal", "server") and ("paypal", "server") not in lit:
            continue
        (_, x1, y1), (_, x2, y2) = NODES[a], NODES[b]
        if on and (b, a) in lit and (a, b) not in lit:
            x1, y1, x2, y2 = x2, y2, x1, y1
        bend = 18 if {a, b} == {"buyer", "paypal"} else 0
        lines.append(f'<path d="M{x1},{y1} Q{(x1 + x2) / 2},{(y1 + y2) / 2 - bend} {x2},{y2}" '
                     f'stroke="{"#0E8C7E" if on else "#D9E0E7"}" stroke-width="{3 if on else 1.5}" fill="none"'
                     f'{" marker-end=\"url(#arrow)\"" if on else ""}/>')
    nodes = []
    for key, (label, x, y) in NODES.items():
        on = key in involved
        rows = label.split("\n")
        text = "".join(f'<tspan x="{x}" dy="{0 if i == 0 else 13}">{html.escape(r)}</tspan>' for i, r in enumerate(rows))
        nodes.append(f'<rect x="{x - 62}" y="{y - 20}" width="124" height="{40 if len(rows) == 1 else 48}" rx="9" '
                     f'fill="{"#E4F3F1" if on else "#FFFFFF"}" stroke="{"#0E8C7E" if on else "#AEB9C4"}" stroke-width="{2 if on else 1}"/>'
                     f'<text x="{x}" y="{y + (4 if len(rows) == 1 else -1)}" text-anchor="middle" font-size="11.5" '
                     f'font-weight="{650 if on else 500}" fill="{"#0B6E63" if on else "#5B6B7C"}">{text}</text>')
    return ('<svg viewBox="0 0 440 300" width="440" height="300"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" '
            'refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#0E8C7E"/>'
            '</marker></defs>' + "".join(lines) + "".join(nodes) + "</svg>")


PANEL_CSS = """
@font-face { font-family: "Instrument Sans"; src: url("%s/static/fonts/instrument-sans-latin.woff2") format("woff2"); }
* { box-sizing: border-box; } body { margin: 0; width: 480px; height: 1080px; background: #F6F8FA; color: #142231;
  font-family: "Instrument Sans", "Segoe UI", sans-serif; border-left: 1px solid #D9E0E7; padding: 28px 20px; }
.k { font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: #5B6B7C; font-weight: 650; margin: 0 0 8px; }
.scene { font-size: 13px; color: #5B6B7C; margin-bottom: 14px; } .map { background: #fff; border: 1px solid #D9E0E7;
  border-radius: 14px; padding: 10px 0; margin-bottom: 22px; } h2 { font-size: 26px; line-height: 1.2; margin: 0 0 22px; }
ul { margin: 0 0 22px; padding-left: 20px; font-size: 18px; line-height: 1.45; } li { margin-bottom: 6px; }
.proof { font-family: "Cascadia Mono", Consolas, monospace; font-size: 14.5px; line-height: 1.5; background: #fff;
  border: 1px solid #D9E0E7; border-left: 4px solid #0E8C7E; border-radius: 10px; padding: 12px 14px; white-space: pre-wrap; }
.caption { position: absolute; left: 20px; right: 20px; bottom: 28px; background: #142231; color: #fff; border-radius: 12px;
  padding: 16px 18px; font-size: 22px; line-height: 1.35; font-weight: 600; }
body { position: relative; }
"""


def panel_html(base: str, scene: int, title: str, step: Step, proof: str) -> str:
    how = "".join(f"<li>{html.escape(h)}</li>" for h in step.how)
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{PANEL_CSS % base}</style></head><body>"
            f"<div class='k'>Behind the scenes</div><div class='scene'>Scene {scene} · {html.escape(title)}</div>"
            f"<div class='map'>{map_svg(step.path)}</div>"
            f"<div class='k'>What happened</div><h2>{html.escape(step.what)}</h2>"
            f"<div class='k'>How</div><ul>{how}</ul>"
            f"<div class='k'>Proof, from the live timeline</div><div class='proof'>{html.escape(proof or '—')}</div>"
            f"<div class='caption'>{html.escape(step.caption)}</div></body></html>")


CARD_CSS = """
@font-face { font-family: "Instrument Sans"; src: url("%s/static/fonts/instrument-sans-latin.woff2") format("woff2"); }
body { margin: 0; width: 1920px; height: 1080px; background: #F6F8FA; color: #142231; display: grid; place-items: center;
  font-family: "Instrument Sans", "Segoe UI", sans-serif; } .c { text-align: center; } .line { width: 6px; height: 120px;
  background: #0E8C7E; border-radius: 3px; margin: 0 auto 36px; } h1 { font-size: 84px; margin: 0 0 20px; letter-spacing: -.01em; }
p { font-size: 34px; color: #5B6B7C; margin: 10px 0; } .url { color: #0B6E63; font-weight: 650; }
"""


def card_html(base: str, title: str, lines: list[str]) -> str:
    body = "".join(f"<p class='{cls}'>{html.escape(t)}</p>" for cls, t in lines)
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CARD_CSS % base}</style></head><body><div class='c'>"
            f"<div class='line'></div><h1>{html.escape(title)}</h1>{body}</div></body></html>")


# Each scene's narration opens with its topic; the intro with the title.
TOPICS = {"intro": "Clearline.", 1: "What it is.", 2: "Checkout.", 3: "The seller's day.", 4: "Shipping.", 5: "Disputes.",
          6: "Refund requests.", 7: "Agents.", 8: "Invoices.", 9: "Money in flight.", 10: "Insights.", 11: "System status."}
# Silence placed by hand between sentences (Kokoro's own silence is trimmed off each sentence's ends).
# Commas and colons are left to Kokoro: a sentence is always spoken in one go.
AFTER_TOPIC, BETWEEN_SENTENCES, BETWEEN_SCENES, FILM_START, FILM_END = 0.5, 0.3, 0.7, 0.4, 0.8
SPEED = 0.95


def phrases(text: str) -> list[tuple[str, float]]:
    """A line as (sentence, the pause after it). Each sentence is one Kokoro call, so its commas
    and colons keep their natural rise instead of falling like a sentence's end."""
    sentences = [s.strip() for s in re.findall(r"[^.!?]+[.!?]?", text) if s.strip()]
    return [(s, BETWEEN_SENTENCES if i < len(sentences) - 1 else 0.0) for i, s in enumerate(sentences)]


class Voice:
    def __init__(self, voice: str, cache: Path):
        sys.path.insert(0, os.environ.get("KOKORO_PATH", r"D:\Temp\claude\clearline\tts"))
        from kokoro_onnx import Kokoro
        import soundfile
        self.sf, self.voice, self.cache = soundfile, voice, cache
        self.kokoro = Kokoro(str(Path(KOKORO_DIR) / "kokoro-v1.0.onnx"), str(Path(KOKORO_DIR) / "voices-v1.0.bin"))
        cache.mkdir(parents=True, exist_ok=True)

    def phrase(self, text: str):
        """One sentence (or topic), with Kokoro's own silence trimmed from both ends."""
        import numpy as np
        samples, rate = self.kokoro.create(text, voice=self.voice, speed=SPEED, lang="en-us")
        loud = np.flatnonzero(np.abs(samples) > 0.01)
        if loud.size:
            pad = int(rate * 0.02)
            samples = samples[max(loud[0] - pad, 0):loud[-1] + pad]
        return samples, rate

    def say(self, text: str, topic: str | None = None, lead: float = 0.0) -> tuple[Path, float]:
        """The line spoken sentence by sentence with the pauses placed exactly; `lead` s of silence first."""
        import numpy as np
        key = json.dumps([self.voice, SPEED, topic, text, lead])
        target = self.cache / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.wav"
        if not target.exists():
            parts, rate = [], 24000
            plan = ([(topic, AFTER_TOPIC)] if topic else []) + phrases(text)
            for words, pause in plan:
                samples, rate = self.phrase(words)
                parts += [samples, np.zeros(int(rate * pause), dtype=samples.dtype)]
            audio = np.concatenate([np.zeros(int(rate * lead), dtype=parts[0].dtype)] + parts)
            self.sf.write(str(target), audio, rate)
        info = self.sf.info(str(target))
        return target, info.frames / info.samplerate


MAX_SILENCE = 1.5  # seconds without narration, anywhere in the film
MAX_SPEED = 2.5    # the most footage is sped up before the middle of it is cut instead


def segment(ff: str, out: Path, *, video: Path | None, start: float, foot: float, panel: Path | None, card: Path | None,
            audio: Path, speech: float, tail: float) -> tuple[Path, float]:
    """One step: footage (or a card) with its panel and narration. It lasts as long as the
    narration plus a short beat, so silence never runs past MAX_SILENCE: footage longer than that
    is sped up (up to MAX_SPEED), and what's still too long loses the middle of its dead time,
    keeping its start (the click) and its end (the result)."""
    total = speech + tail  # the narration, then the pause before the next step
    target = out / f"seg-{len(list(out.glob('seg-*.mp4'))):03d}.mp4"
    if card:
        inputs = ["-loop", "1", "-t", f"{total:.3f}", "-i", str(card), "-i", str(audio)]
        graph = "[0:v]scale=1920:1080,fps=30,setsar=1[out]"
        amap = "1:a"
    else:
        speed = min(max(foot / total, 1.0), MAX_SPEED)
        keep = total * speed  # seconds of footage that fit at that speed
        if foot > keep + 0.05:  # drop the middle: the first 60% of what fits, then the end
            head, end = keep * 0.6, keep * 0.4
            pick = f"select='between(t,0,{head:.3f})+between(t,{foot - end:.3f},{foot:.3f})',setpts=N/FRAME_RATE/TB,"
        else:
            pick = ""
        inputs = ["-ss", f"{start:.3f}", "-t", f"{foot:.3f}", "-i", str(video), "-loop", "1", "-t", f"{total:.3f}", "-i", str(panel),
                  "-i", str(audio)]
        graph = (f"[0:v]fps=30,{pick}setpts=(PTS-STARTPTS)/{speed:.4f},fps=30,tpad=stop_mode=clone:stop_duration={total:.3f},"
                 f"trim=duration={total:.3f}[v];[1:v]scale={PANEL_W}:1080,fps=30[p];[v][p]hstack=inputs=2,setsar=1[out]")
        amap = "2:a"
    subprocess.run([ff, "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]", "-map", amap,
                    "-af", f"apad,atrim=duration={total:.3f},aresample=48000", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-t", f"{total:.3f}",
                    str(target)], check=True)
    return target, total


def build_cut(ff: str, name: str, tight: bool, shots: list[Shot], bases: dict, pictures, voice: Voice, out: Path) -> tuple[Path, list]:
    work = out / f"work-{name}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    script, segments = [], []

    def add(key, topic=None, tail=BETWEEN_SENTENCES, lead=0.0, **kw):
        step = STEPS[key]
        text = step.short if tight else step.long
        audio, speech = voice.say(text, topic, lead)
        seg, total = segment(ff, work, audio=audio, speech=speech, tail=tail, **kw)
        segments.append(seg)
        script.append((key, total, f"{topic} {text}" if topic else text))

    add("intro", topic=TOPICS["intro"], lead=FILM_START, tail=BETWEEN_SCENES,
        video=None, start=0, foot=0, panel=None, card=pictures["intro"])
    for shot in shots:
        if shot.status != "PASS" or not shot.marks:
            continue
        base = bases[shot.scene]
        whole = length(ff, base)
        points = [0.0] + [timeline(shot, at) for _, at, _ in shot.marks[1:]] + [whole]
        for i, (key, _, _) in enumerate(shot.marks):
            a, b = points[i], max(points[i + 1], points[i] + 0.5)
            last = i == len(shot.marks) - 1
            add(key, topic=TOPICS.get(shot.scene) if i == 0 else None, tail=BETWEEN_SCENES if last else BETWEEN_SENTENCES,
                video=base, start=a, foot=b - a, panel=pictures[(shot.scene, key)], card=None)
    add("outro", tail=FILM_END, video=None, start=0, foot=0, panel=None, card=pictures["outro"])
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{s.name}'\n" for s in segments), encoding="utf-8")
    joined = work / "joined.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                    str(joined)], check=True, cwd=work)
    film = out / f"{name}.mp4"
    loudnorm(ff, joined, film)
    shutil.rmtree(work)
    return film, script


LOUDNESS = "I=-16:TP=-2.0:LRA=11"  # true peak aimed under -1.5 dB with room to spare


def loudnorm(ff: str, source: Path, target: Path):
    """Two-pass loudness normalisation of the whole mix: about -16 LUFS, true peak below -1.5 dB."""
    probe = subprocess.run([ff, "-hide_banner", "-i", str(source), "-af", f"loudnorm={LOUDNESS}:print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    measured = json.loads(probe.stderr[probe.stderr.rindex("{"):probe.stderr.rindex("}") + 1])
    second = (f"loudnorm={LOUDNESS}:measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
              f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
              f"offset={measured['target_offset']}:linear=true")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(source), "-c:v", "copy", "-af", f"{second},aresample=48000",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target)], check=True)


def write_script(path: Path, script: list, shots: list[Shot]):
    titles = {k: (s.scene, s.title) for s in shots for k, _, _ in s.marks}
    clock, lines = 0.0, []
    for key, total, text in script:
        scene = titles.get(key)
        lines.append(f"[{int(clock // 60)}:{clock % 60:04.1f}] {('Scene %d, %s' % scene) if scene else key.title()} "
                     f"({total:.1f} s)\n{text}\n")
        clock += total
    path.write_text("\n".join(lines), encoding="utf-8")


def licence_present(config: Config) -> bool:
    from .e2e import Api
    return bool(Api(config).get("/api/insights/license").get("key"))


def honest_without_licence():
    """Without AG Studio's licence key, Insights shows the trial watermark and one check isn't
    green: the narration says so rather than "all green"."""
    step = STEPS["status"]
    step.what, step.caption = "Every check, run live", "Checked live. One key still to add."
    step.short = "And System status runs every check live. All pass, except the AG Studio licence, which is still on its trial."
    step.long = ("Finally, System status runs every check live: the database, the PayPal token, the webhook id and the AI "
                 "keys all pass. The one gap is the AG Studio licence key, still on its trial, which is why Insights shows "
                 "a watermark.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.film", description="Record the narrated demo film (Sandbox only).")
    parser.add_argument("--only", help="scene numbers, e.g. 1,3,9")
    parser.add_argument("--voice", default="af_heart", help="a Kokoro voice, e.g. af_heart, am_michael, bf_emma")
    parser.add_argument("--invoice-id", help="the overdue invoice for scene 8 (its proof)")
    parser.add_argument("--out", default=OUT)
    args = parser.parse_args(argv)
    wanted = {int(n) for n in args.only.split(",")} if args.only else {s[0] for s in SCENES}
    config = Config()
    for attempt in range(4):
        try:
            assert_sandbox(httpx.get(config.base + "/api/config", timeout=120).json())
            break
        except httpx.TransportError:
            if attempt == 3:
                raise
            time.sleep(5)
    ff = ffmpeg_exe()
    ag_key = licence_present(config)
    if not ag_key:
        honest_without_licence()
    print(f"AG Studio licence key on the live site: {'present' if ag_key else 'MISSING (the film says so)'}", flush=True)
    voice = Voice(args.voice, Path(args.out) / "voice-cache")
    out = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    from playwright.sync_api import sync_playwright
    shots, pictures = [], {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, slow_mo=50)
        run = Run(config, out, browser, None, None)
        film = Film(run, out)
        film.extra["invoice_id"] = args.invoice_id
        try:
            film.sign_in()
            for number, title, who, fn in SCENES:
                if number in wanted:
                    shots.append(film_scene(film, number, title, who, fn))
            painter = browser.new_page(viewport={"width": PANEL_W, "height": 1080})
            for shot in shots:
                for key, _, _ in shot.marks:
                    painter.set_content(panel_html(config.base, shot.scene, shot.title, STEPS[key], shot.proofs.get(key, "")))
                    painter.wait_for_timeout(150)
                    target = out / f"panel-{shot.scene:02d}-{key}.png"
                    painter.screenshot(path=str(target))
                    pictures[(shot.scene, key)] = target
            painter.set_viewport_size({"width": 1920, "height": 1080})
            for key, title, lines in (("intro", "Clearline", [("", "Every PayPal payment, clearly")]),
                                      ("outro", "Try Clearline", [("url", LIVE_URL.removeprefix("https://")),
                                                                  ("", REPO_URL.removeprefix("https://"))])):
                painter.set_content(card_html(config.base, title, lines))
                painter.wait_for_timeout(300)
                pictures[key] = out / f"card-{key}.png"
                painter.screenshot(path=str(pictures[key]))
        finally:
            run.restore()
            browser.close()
            for state in run.states.values():
                state.unlink(missing_ok=True)
    bases = {s.scene: base_video(ff, s, out) for s in shots if s.status == "PASS" and s.clips}
    results = {}
    for name, tight in (("clearline-demo", True), ("clearline-deep-dive", False)):
        film_path, script = build_cut(ff, name, tight, shots, bases, pictures, voice, out)
        write_script(out / f"{name}-narration.txt", script, shots)
        results[name] = (film_path, length(ff, film_path))
    (out / "steps.json").write_text(json.dumps([{"scene": s.scene, "title": s.title, "status": s.status, "detail": s.detail,
                                                 "steps": [k for k, _, _ in s.marks], "proofs": s.proofs} for s in shots],
                                               indent=1), encoding="utf-8")
    for raw in out.glob("raw-*"):
        shutil.rmtree(raw)
    for base in bases.values():
        base.unlink()
    for s in shots:
        print(f"{s.scene:>2} {s.title:<16} {s.status} {s.detail}")
    for name, (path, seconds) in results.items():
        print(f"{path}  {int(seconds // 60)}:{seconds % 60:04.1f}")
    return 0 if all(s.status == "PASS" for s in shots) else 1


if __name__ == "__main__":
    sys.exit(main())
