"""Seller page rendering logic, run in node against realistic timelines (skipped without node)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def render(*scenarios):
    out = subprocess.run([NODE, str(ROOT / "tests/js/page_harness.js"),
                          str(ROOT / "app/static/seller.html")],
                         input=json.dumps(list(scenarios)), capture_output=True, text=True,
                         timeout=30, check=True)
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
    from tests.support import make_client
    client, _ = make_client(tmp_path)
    for path in ("/", "/seller"):
        assert client.get(path).headers["cache-control"] == "no-cache"
