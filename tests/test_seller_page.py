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
