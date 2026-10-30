"""Seller page rendering logic, run in node against realistic timelines (skipped without node)."""

import html as htmllib
import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from tests.support import (DISPUTE_ID, GEMINI, DisputeOnPayPal, ai_reply, events, fight,
                           make_client, open_dispute, setup_paypal, shipped_order)
from tests.test_webhooks import API

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def render(*scenarios):
    out = subprocess.run([NODE, str(ROOT / "tests/js/page_harness.js"),
                          str(ROOT / "app/static/seller.html")],
                         input=json.dumps(list(scenarios)), capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)  # not the Windows code page
    return json.loads(out.stdout)


def dispute(kind, status, update_time, allowed_decisions, t):
    return {"kind": kind, "created_at": t, "payload": {
        "dispute_id": "PP-R-WDJ-1", "status": status, "dispute_life_cycle_stage": "CHARGEBACK",
        "update_time": update_time, "allowed_decisions": allowed_decisions,
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "dispute_amount": {"value": "120.00", "currency_code": "USD"}}}


RECOMMENDATION = {"kind": "ai_recommendation", "created_at": 3, "payload": {
    "dispute_id": "PP-R-WDJ-1", "provider": "gemini", "model": "m", "latency_ms": 900,
    "recommendation": {"decision": "FIGHT", "confidence": 0.95, "missing_evidence": [],
                       "reasons": [{"text": "Shipped.", "fact_keys": ["tracking.number"]}],
                       "message_to_buyer": "Hi", "note_to_paypal": "Note", "offer": None}}}

# The real order 53S487510J053815W: still UNDER_REVIEW, but PayPal later allowed a Refund.
UNDER_REVIEW_REFUND_ALLOWED = [
    dispute("dispute_created", "UNDER_REVIEW", "2026-10-27T04:46:13.737Z", [], 1),
    RECOMMENDATION,
    dispute("dispute_updated", "UNDER_REVIEW", "2026-10-27T05:04:13.827Z", ["REFUND"], 4),
]


def test_sandbox_button_shows_for_under_review_even_when_refund_allowed():
    [result] = render({"env": "sandbox", "events": UNDER_REVIEW_REFUND_ALLOWED})
    assert result["sandbox"] is True
    assert result["compact"] is False  # full card, since Refund is allowed


def test_sandbox_button_shows_without_any_ai_recommendation():
    events = [dispute("dispute_created", "UNDER_REVIEW", "2026-10-27T04:46:13.737Z", [], 1)]
    [result] = render({"env": "sandbox", "events": events})
    assert result["sandbox"] is True and result["compact"] is True


def test_sandbox_button_never_in_live_mode_or_other_statuses():
    waiting = [dispute("dispute_created", "WAITING_FOR_SELLER_RESPONSE",
                       "2026-10-27T04:46:13.737Z", ["FIGHT"], 1), RECOMMENDATION]
    live, not_loaded, seller_turn = render(
        {"env": "live", "events": UNDER_REVIEW_REFUND_ALLOWED},
        {"env": None, "events": UNDER_REVIEW_REFUND_ALLOWED},  # /api/config failed to load
        {"env": "sandbox", "events": waiting})
    assert not live["sandbox"] and not not_loaded["sandbox"] and not seller_turn["sandbox"]
    assert seller_turn["approve"] is True


def test_out_of_order_rows_use_latest_update_time():
    # The older "created" row arrives last; the newer state must still win.
    events = [UNDER_REVIEW_REFUND_ALLOWED[2], RECOMMENDATION,
              dispute("dispute_created", "WAITING_FOR_SELLER_RESPONSE",
                      "2026-10-27T04:00:00.000Z", ["FIGHT"], 9)]
    [result] = render({"env": "sandbox", "events": events})
    assert result["sandbox"] is True and result["approve"] is False


def test_claim_stage_fight_needs_a_file_before_approve():
    claim = dispute("dispute_updated", "WAITING_FOR_SELLER_RESPONSE", "2026-10-27T05:32:04.065Z",
                    ["FIGHT", "REFUND"], 4)
    claim["payload"]["document_evidence"] = [
        {"type": "PROOF_OF_FULFILLMENT", "label": "Proof of shipment (e.g. shipping receipt)"},
        {"type": "PROOF_OF_DELIVERY_SIGNATURE", "label": "Signed delivery receipt"}]
    inquiry = dispute("dispute_updated", "WAITING_FOR_SELLER_RESPONSE", "2026-10-27T05:32:04.065Z",
                      ["FIGHT"], 4)
    inquiry["payload"]["document_evidence"] = []
    with_claim, with_inquiry = render({"env": "sandbox", "events": [RECOMMENDATION, claim]},
                                      {"env": "sandbox", "events": [RECOMMENDATION, inquiry]})

    card = with_claim["card"]
    assert 'type="file" data-field="evidence_file"' in card
    assert '<option value="PROOF_OF_DELIVERY_SIGNATURE" selected>Signed delivery receipt</option>' in card
    assert "PROOF_OF_REFUND" not in card
    approve = re.search(r"<button data-approve[^>]*>", card).group(0)
    assert "disabled" in approve and 'data-needs-file="1"' in approve
    assert "it never generates proof" in card

    card = with_inquiry["card"]  # inquiry stage: unchanged, no file needed
    assert "evidence_file" not in card
    assert "disabled" not in re.search(r"<button data-approve[^>]*>", card).group(0)


def test_invoice_timeline_rows_have_plain_labels():
    rows = [{"kind": kind, "created_at": 1, "payload": payload} for kind, payload in [
        ("invoice_status", {"status": "SENT", "number": "0042", "amount": "50.00", "currency": "USD"}),
        ("invoice_send_sent", {"invoice_id": "INV2-1"}),
        ("invoice_reminder_sent", {"subject": "Friendly reminder"}),
        ("invoice_status", {"status": "MARKED_AS_PAID", "number": "0042"}),
        ("invoice_paid", {"number": "0042", "amount": "50.00", "currency": "USD"}),
        ("invoice_action_failed", {"action": "send", "error": "PayPal said no"}),
    ]]
    [out] = render({"env": "sandbox", "events": rows})
    labels = re.findall(r'<div class="cl-title">([^<]+)</div>', out["timeline"])
    assert labels == ["Invoice status: Sent", "Invoice sent", "Reminder sent",
                      "Invoice status: Paid", "Paid", "Invoice action failed"]
    assert "invoice_" not in visible_text(out["timeline"])


# ---- Plain words only, and money actions behind the in-app confirm -----------------------

# A PayPal code: SNAKE_CASE, or one of the single-word codes we store.
RAW_CODE = re.compile(r"\b[A-Z]{2,}(?:_[A-Z]+)+\b|\b(?:INQUIRY|CHARGEBACK|RESOLVED|COMPLETED|PENDING|"
                      r"FIGHT|REFUND|OFFER|DRAFT|SENT|PAID|FEDEX|OTHER)\b")


def visible_text(html):
    """What a reader sees: tags and attributes removed. PayPal's raw error (collapsed under
    "details" for support) is the one place codes are allowed, so it's dropped too."""
    html = re.sub(r'<details class="tech">.*?</details>', " ", html, flags=re.S)
    return htmllib.unescape(re.sub(r"<[^>]+>", " ", html))


def every_kind_of_event():
    """One timeline with every event kind and code the seller page can show."""
    def ev(kind, **payload):
        return {"kind": kind, "created_at": 1 + len(rows), "payload": {"dispute_id": "PP-R-WDJ-1", **payload}}
    rows = []
    for kind, payload in [
        ("order_created", {"amount": "120.00", "currency": "USD", "status": "CREATED"}),
        ("payment_captured", {"status": "COMPLETED", "payer_country": "US"}),
        ("webhook_payment_capture_pending", {"status": "PENDING", "pending_reason": "PENDING_REVIEW",
                                             "amount": {"value": "120.00", "currency_code": "USD"}}),
        ("webhook_payment_capture_completed", {"status": "COMPLETED", "amount": {"value": "120.00", "currency_code": "USD"}}),
        ("webhook_other", {"event_type": "CHECKOUT.ORDER.APPROVED"}),
        ("tracking_added", {"carrier": "FEDEX", "tracking_number": "999"}),
        ("buyer_request", {"request_id": "R1", "type": "not_arrived", "via": "agent"}),
        ("seller_reply", {"request_id": "R1", "message": "On its way", "auto": True}),
        ("refund_sent", {"amount": "40.00", "currency": "USD", "mode": "partial", "status": "PENDING",
                         "trigger": "auto-policy", "rule": "Item not received up to 50.00 USD"}),
        ("auto_policy_skipped", {"request_id": "R1", "reason": "Over the per-order maximum"}),
        ("ai_recommendation", RECOMMENDATION["payload"]),
        ("ai_failed", {"error": "timeout"}),
        ("ai_provider_switched", {"from": "gemini", "to": "groq", "reason": "rate limited"}),
        ("dispute_action_sent", {"action": "FIGHT", "endpoint": "provide-evidence", "run_id": "1"}),
        ("dispute_action_skipped", {"action": "FIGHT", "endpoint": "send-message", "run_id": "1",
                                    "reason": "dispute is waiting for the buyer"}),
        ("dispute_action_failed", {"action": "OFFER", "endpoint": "make-offer", "run_id": "2",
                                   "error": "PayPal rejected this step", "paypal_error": MULTI_DETAIL_ERROR}),
        ("invoice_status", {"status": "MARKED_AS_PAID", "number": "0042"}),
        ("invoice_action_failed", {"action": "send", "error": "PayPal said no"}),
    ]:
        rows.append(ev(kind, **payload))
    return rows


def test_no_raw_paypal_code_reaches_the_seller_page():
    base = every_kind_of_event()
    scenarios = []
    for stage in ("INQUIRY", "CHARGEBACK"):
        for status, allowed in [("WAITING_FOR_SELLER_RESPONSE", ["FIGHT", "REFUND", "OFFER"]),
                                ("WAITING_FOR_BUYER_RESPONSE", []), ("UNDER_REVIEW", []),
                                ("UNDER_REVIEW", ["REFUND"]), ("RESOLVED", [])]:
            for decision in ("FIGHT", "REFUND", "OFFER"):
                d = dispute("dispute_updated", status, "2026-10-27T05:00:00Z", allowed, 90)
                d["payload"].update(dispute_life_cycle_stage=stage, seller_response_due_date="2026-11-15T16:58:07Z",
                                    reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
                rec = json.loads(json.dumps(RECOMMENDATION))
                rec["created_at"] = 91
                rec["payload"]["recommendation"].update(
                    decision=decision, offer={"type": "REFUND", "amount": "40.00"} if decision == "OFFER" else None)
                scenarios.append({"env": "sandbox", "events": [*base, dispute("dispute_created", "OPEN", "", [], 2), d, rec]})
    scenarios[0]["buyer"] = {"link": "/o/tok", "refundable": "80.00", "currency": "USD",
                             "refunds": [{"amount": "40.00", "currency": "USD", "status": "PENDING", "at": 5}],
                             "requests": [{"id": "R1", "type": "refund", "reason": "Item not received", "amount": "40.00",
                                           "message": "Hi", "at": 4, "via": "agent", "replies": [],
                                           "auto_refund": {"amount": "40.00", "currency": "USD", "rule": "Item not received"}}]}
    scenarios[0]["exportData"] = {"suggestion": {"code": "P0802", "reason": "Design work"}, "purpose_code": None,
                                  "purpose_codes": {"P0802": "Software consultancy"}, "disclaimer": "Guidance only.",
                                  "payment": {"amount": "120.00", "currency": "USD", "country": "US", "description": "Kit"},
                                  "fira_downloaded": False, "sources": [{"label": "RBI", "url": "https://rbi.org.in"}]}
    for page in render(*scenarios):
        for part in ("card", "timeline", "buyer", "export"):
            text = visible_text(page[part])
            assert not RAW_CODE.search(text), (part, RAW_CODE.findall(text), text)
    assert "Your turn to respond" in visible_text(render(scenarios[0])[0]["card"])


def money_actions(answer):
    out = subprocess.run([NODE, str(ROOT / "tests/js/confirm_harness.js"), str(ROOT / "app/static/seller.html")],
                         input=json.dumps({"answer": answer}), capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)
    return {a["action"]: a for a in json.loads(out.stdout)}


def test_money_actions_use_the_in_app_confirm():
    script = (ROOT / "app/static/seller.html").read_text(encoding="utf-8")
    assert not re.search(r"(?<![\w.])(confirm|prompt|alert)\(", script)  # no browser dialogs at all

    cancelled = money_actions(False)
    assert len(cancelled) == 9
    for name, a in cancelled.items():  # Cancel: one dialog, nothing sent
        assert len(a["confirms"]) == 1 and a["fetches"] == [] and a["native"] == [], name

    confirmed = money_actions("25.00")  # what the dialog returns after a partial amount is typed
    for name, a in confirmed.items():  # OK: exactly one write, after the dialog
        assert len(a["confirms"]) == 1 and len(a["fetches"]) == 1 and a["native"] == [], name
    assert confirmed["refund partial"]["fetches"] == [
        'POST /api/orders/O1/refund {"mode":"partial","amount":"25.00","request_id":"R1"}']
    full = confirmed["refund full"]["confirms"][0]
    assert full["amount"] == "120.00 USD" and full["confirm"] == "Refund 120.00 USD"
    partial = confirmed["refund partial"]["confirms"][0]
    assert partial["input"]["max"] == "120.00" and partial["input"]["verb"] == "Refund"
    assert confirmed["dispute refund"]["confirms"][0]["confirm"] == "Refund 120.00 USD"
    assert any("can't be undone" in w for w in confirmed["dispute refund"]["confirms"][0]["what"])


def test_the_line_changes_colour_at_the_event_that_changed_the_state():
    events = [
        {"kind": "order_created", "created_at": 1, "payload": {"amount": "120.00", "currency": "USD"}},
        {"kind": "payment_captured", "created_at": 2, "payload": {"status": "COMPLETED"}},
        {"kind": "tracking_added", "created_at": 3, "payload": {"carrier": "DHL", "tracking_number": "1"}},
        {"kind": "buyer_request", "created_at": 4, "payload": {"request_id": "R1", "type": "not_arrived"}},
        {"kind": "seller_reply", "created_at": 5, "payload": {"request_id": "R1", "message": "On its way"}},
        dispute("dispute_created", "WAITING_FOR_SELLER_RESPONSE", "2026-10-27T04:00:00Z", ["FIGHT"], 6),
        dispute("dispute_resolved", "RESOLVED", "2026-10-28T04:00:00Z", [], 7),
    ]
    [out] = render({"env": "sandbox", "events": events})
    segs = re.findall(r'<li class="cl-stop[^"]*"[^>]*data-seg="(\w+)"', out["timeline"])
    # grey before payment, teal once paid, amber while the buyer waits, crimson from the claim, teal when it closes
    assert segs == ["none", "settled", "settled", "held", "settled", "disputed", "settled"]


def test_self_hosted_font_and_no_google_fonts(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    for name in ("instrument-sans-latin", "instrument-sans-latin-ext"):
        res = client.get(f"/static/fonts/{name}.woff2")  # public: the checkout and buyer pages use it
        assert res.status_code == 200 and res.headers["content-type"] == "font/woff2" and res.content[:4] == b"wOF2"
    seller, _ = make_client(tmp_path)  # logged in: anything not listed is a 404, not a redirect
    assert seller.get("/static/fonts/OFL.txt").status_code == 404  # only the listed files are served
    assert seller.get("/static/seller.html").status_code == 404
    for page in (ROOT / "app/static").glob("*.html"):
        assert "fonts.googleapis" not in page.read_text(encoding="utf-8") and "gstatic" not in page.read_text(encoding="utf-8")


def test_pages_are_served_with_no_cache(tmp_path):
    client, _ = make_client(tmp_path)
    for path in ("/", "/seller"):
        assert client.get(path).headers["cache-control"] == "no-cache"


# ---- PayPal's full error is stored and shown -------------------------------------

MULTI_DETAIL_ERROR = {
    "name": "INVALID_REQUEST",
    "message": "Request is not well-formed, syntactically incorrect, or violates schema.",
    "debug_id": "f858994916f8d",
    "details": [
        {"field": "/evidences/0/evidence_type", "value": "PROOF_OF_FULFILLMENT", "location": "body",
         "issue": "EVIDENCE_TYPE_IS_NOT_ALLOWED",
         "description": "The evidence type is not allowed for this dispute."},
        {"field": "/evidences/0/documents", "location": "body", "issue": "MISSING_REQUIRED_PARAMETER",
         "description": "A required document is missing."},
    ],
}
EXPECTED_LINES = [
    "HTTP 400 · INVALID_REQUEST · Request is not well-formed, syntactically incorrect, or violates "
    "schema. · debug_id f858994916f8d",
    "EVIDENCE_TYPE_IS_NOT_ALLOWED · field /evidences/0/evidence_type · value PROOF_OF_FULFILLMENT"
    " · location body · The evidence type is not allowed for this dispute.",
    "MISSING_REQUIRED_PARAMETER · field /evidences/0/documents · location body"
    " · A required document is missing.",
]


def html_lines(html):
    """The text of each line inside the collapsed details."""
    return [htmllib.unescape(line) for line in re.findall(r"<div>(.*?)</div>", html)]


@respx.mock
def test_multi_detail_paypal_error_is_stored_and_rendered_in_full(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    respx.post(f"{API}/v1/customer/disputes/{DISPUTE_ID}/provide-evidence").mock(
        return_value=httpx.Response(400, json=MULTI_DETAIL_ERROR))

    assert client.post(f"/api/disputes/{DISPUTE_ID}/act", json={"action": "FIGHT"}).status_code == 502
    [failed] = events(client, "ORDER1", "dispute_action_failed")
    assert failed["payload"]["paypal_error"] == {"http_status": 400, **{
        k: MULTI_DETAIL_ERROR[k] for k in ("name", "message", "debug_id")}, "details": [
        {"issue": "EVIDENCE_TYPE_IS_NOT_ALLOWED", "field": "/evidences/0/evidence_type",
         "value": "PROOF_OF_FULFILLMENT", "location": "body",
         "description": "The evidence type is not allowed for this dispute."},
        {"issue": "MISSING_REQUIRED_PARAMETER", "field": "/evidences/0/documents", "value": None,
         "location": "body", "description": "A required document is missing."}]}

    # Rendered from the stored timeline: card ("Sent to PayPal") and timeline row, collapsed.
    timeline = client.get("/api/orders/ORDER1/timeline").json()
    [page] = render({"env": "sandbox", "events": timeline})
    for html in (page["card"], page["timeline"]):
        assert '<details class="tech"><summary>details</summary>' in html
        assert all(line in html_lines(html) for line in EXPECTED_LINES), html_lines(html)


@respx.mock
def test_sandbox_helper_failure_stores_full_error(tmp_path):
    setup_paypal()
    DisputeOnPayPal(status="UNDER_REVIEW", dispute_life_cycle_stage="CHARGEBACK")
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    respx.post(f"{API}/v1/customer/disputes/{DISPUTE_ID}/require-evidence").mock(
        return_value=httpx.Response(400, json=MULTI_DETAIL_ERROR))
    assert client.post(f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence").status_code == 502
    [failed] = events(client, "ORDER1", "sandbox_status_change_failed")
    assert len(failed["payload"]["paypal_error"]["details"]) == 2
    [page] = render({"env": "sandbox", "events": client.get("/api/orders/ORDER1/timeline").json()})
    assert all(line in html_lines(page["timeline"]) for line in EXPECTED_LINES)


def test_seller_page_line_and_attention_treat_a_refund_as_an_answer():
    events = [
        {"kind": "order_created", "created_at": 1, "payload": {"amount": "120.00", "currency": "USD"}},
        {"kind": "payment_captured", "created_at": 2, "payload": {"status": "COMPLETED"}},
        {"kind": "buyer_request", "created_at": 3, "payload": {"request_id": "R1", "type": "refund"}},
        {"kind": "refund_sent", "created_at": 4, "payload": {"request_id": "R1", "amount": "20.00", "currency": "USD", "mode": "partial"}},
    ]
    [out] = render({"env": "sandbox", "events": events})
    segs = re.findall(r'<li class="cl-stop[^"]*"[^>]*data-seg="(\w+)"', out["timeline"])
    assert segs == ["none", "settled", "held", "settled"]  # amber while asked, teal once refunded
