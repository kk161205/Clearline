"""Buyer clarity page, buyer requests, seller replies, refunds and the counter (no network)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from app.buyer import buyer_token, summary_problems, template_summary
from app.store import TimelineStore
from tests.support import (API, DISPUTE_ID, GEMINI, GROQ, DisputeOnPayPal, make_client,
                           open_dispute, setup_paypal, shipped_order)
from tests.test_webhooks import capture_order

PAYER_EMAIL = "sb-xf47mg53569019@personal.example.com"
REFUND_URL = f"{API}/v2/payments/captures/CAP1/refund"


LONG_ID = "5O190127TN364715T"  # a realistic 17-character PayPal order ID


def captured_with_pii(client, order_id="ORDER1"):
    """An order captured with the buyer's email, name and payer ID in PayPal's response."""
    respx.post(f"{API}/v2/checkout/orders").mock(
        return_value=httpx.Response(201, json={"id": order_id, "status": "CREATED"}))
    respx.post(f"{API}/v2/checkout/orders/{order_id}/capture").mock(
        return_value=httpx.Response(201, json={
            "id": order_id, "status": "COMPLETED",
            "payer": {"email_address": PAYER_EMAIL, "name": {"given_name": "Jane", "surname": "Roe"},
                      "payer_id": "PAYERID123", "address": {"country_code": "US"}},
            "purchase_units": [{"payments": {"captures": [{"id": "CAP1"}]}}]}))
    client.post("/api/orders", json={"item_id": "brand-kit"})
    return client.post(f"/api/orders/{order_id}/capture").json()


def summary_reply(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"summary": text})}}]})


def link_of(client, order_id="ORDER1"):
    return client.get(f"/api/orders/{order_id}/buyer").json()["link"]


def api_of(link):
    return f"/api/o/{link.split('/')[-1]}"


# ---- Link and page --------------------------------------------------------------

@respx.mock
def test_token_valid_invalid_forged_and_old_secret(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path)
    capture_order(client)
    link = client.post("/api/orders/ORDER1/capture").json()["buyerLink"]  # same link every time
    token = link.split("/")[-1]
    assert link == link_of(client) and len(token) >= 22

    assert client.get(link).status_code == 200
    assert client.get(link).headers["referrer-policy"] == "no-referrer"
    assert client.get(f"/api/o/{token}").status_code == 200
    forged = token[:-1] + ("A" if token[-1] != "A" else "B")
    old_secret = buyer_token("some-other-secret", "ORDER1")
    for bad in (forged, old_secret, "nope", "x" * 200):
        assert client.get(f"/o/{bad}").status_code == 404
        assert client.get(f"/api/o/{bad}").status_code == 404
        assert client.post(f"/api/o/{bad}/requests", json={"type": "not_arrived"}).status_code == 404


@respx.mock
def test_buyer_page_shows_facts_but_no_pii(tmp_path):
    setup_paypal()
    respx.post(f"{API}/v2/checkout/orders/{LONG_ID}/track").mock(return_value=httpx.Response(201, json={}))
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    link = captured_with_pii(client, LONG_ID)["buyerLink"]
    client.post(f"/api/orders/{LONG_ID}/tracking", json={"tracking_number": "999999999999999", "carrier": "DHL"})

    page = client.get(api_of(link)).json()
    raw = json.dumps(page)
    for pii in (PAYER_EMAIL, "Jane", "Roe", "PAYERID123", "CAP1", LONG_ID, LONG_ID[:-6]):
        assert pii not in raw, pii
    assert page["order_ref"] == LONG_ID[-6:] == "64715T"
    assert page["seller"] == "Demo Design Studio"
    assert page["item"].startswith("Printed brand kit")
    assert (page["amount"], page["currency"], page["paid_with"], page["country"]) == \
        ("120.00", "USD", "PayPal", "US")
    assert page["shipping"]["carrier"] == "DHL" and page["shipping"]["tracking_number"] == "999999999999999"
    assert page["summary"]["source"] == "template"  # no AI key: the template, instantly
    # One short line that doesn't repeat the page's headline, seller, item or amount.
    assert page["summary"]["text"] == "It's on its way with DHL. Follow it with the tracking number below."
    assert "deliver" not in page["summary"]["text"].lower()


@respx.mock
def test_ai_summary_is_cached_per_order_state(tmp_path):
    setup_paypal()
    respx.post(f"{API}/v2/checkout/orders/ORDER1/track").mock(return_value=httpx.Response(201, json={}))
    gemini = respx.post(GEMINI).mock(return_value=summary_reply(
        "You paid 120.00 USD to Demo Design Studio for your brand kit with PayPal."))
    client, settings = make_client(tmp_path)
    link = captured_with_pii(client)["buyerLink"]

    first = client.get(api_of(link)).json()
    assert first["summary"]["source"] == "template"  # page answered without waiting
    assert gemini.call_count == 1  # ...and the summary was made in the background
    assert PAYER_EMAIL not in gemini.calls[0].request.content.decode()  # no PII to the AI

    second = client.get(api_of(link)).json()
    assert second["summary"] == {"source": "ai", "text":
                                 "You paid 120.00 USD to Demo Design Studio for your brand kit with PayPal."}
    client.get(api_of(link))
    assert gemini.call_count == 1  # cached: no new AI call for the same state

    # Shipping changes the state: template again until the new summary is ready.
    client.post("/api/orders/ORDER1/tracking", json={"tracking_number": "1234567890", "carrier": "DHL"})
    assert client.get(api_of(link)).json()["summary"]["source"] == "template"
    assert gemini.call_count == 2
    assert TimelineStore(settings.database_target).buyer_summary("ORDER1")["provider"] == "gemini"


@respx.mock
def test_ai_failure_or_invented_date_falls_back_to_template(tmp_path):
    setup_paypal()
    gemini = respx.post(GEMINI).mock(return_value=summary_reply(
        "Your brand kit will arrive on 17 Feb 2099."))  # invented delivery date (never today's paid-on date)
    client, _ = make_client(tmp_path)
    link = captured_with_pii(client)["buyerLink"]
    for _ in range(3):
        assert client.get(api_of(link)).json()["summary"]["source"] == "template"
    assert gemini.call_count == 2  # one corrective retry, then never again for this state


@respx.mock
def test_summary_failure_never_switches_the_ai_provider(tmp_path):
    """Day 5 fix: only the dispute assistant may switch Gemini -> Groq."""
    setup_paypal()
    gemini = respx.post(GEMINI).mock(return_value=httpx.Response(503))  # would switch for disputes
    groq = respx.post(GROQ).mock(return_value=summary_reply("You paid 120.00 USD."))
    client, settings = make_client(tmp_path)
    link = captured_with_pii(client)["buyerLink"]
    page = client.get(api_of(link)).json()
    assert page["summary"]["source"] == "template"  # shown at once, failure only logged
    assert gemini.call_count == 1 and not groq.called
    assert TimelineStore(settings.database_target).ai_state()["active_provider"] == "gemini"
    assert client.get("/api/orders/other/timeline").status_code == 404  # no ai_provider_switched


def test_summary_guardrail_and_template():
    facts = {"seller": "S", "item": "Kit", "amount": "120.00", "currency": "USD",
             "paid_on": "27 Oct 2026", "shipped": True, "carrier": "DHL",
             "tracking_number": "999", "shipped_on": "27 Oct 2026"}
    assert summary_problems("Your order shipped with DHL on 27 Oct 2026 (tracking 999).", facts) == []
    assert summary_problems("Expected delivery: 2 Nov 2026.", facts)
    assert summary_problems("Write to help@seller.example", facts)
    assert summary_problems("You paid S 120.00 USD for Kit with PayPal on 27 Oct 2026, and your order "
                            "shipped with DHL the same day; it should arrive soon, see tracking 999.", facts)
    assert template_summary(facts) == "It's on its way with DHL. Follow it with the tracking number below."
    assert template_summary({**facts, "shipped": False}) == "The seller adds tracking here as soon as it ships."
    assert template_summary({**facts, "refunded": "40.00"}) == "A refund of 40.00 USD was sent to you."
    assert template_summary({**facts, "refunded": "120.00"}).startswith("Refunds go back to the way you paid")
    for text in (template_summary(facts), template_summary({**facts, "shipped": False})):
        assert "120.00" not in text and "Kit" not in text and "S " not in text  # nothing the headline says


@respx.mock
def test_checkout_restore_uses_public_status_endpoint(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path, login=False)  # the buyer has no seller session
    link = captured_with_pii(client)["buyerLink"]
    token = link.split("/")[-1]
    assert client.get(f"/api/o/{token}/status").json() == {
        "paid": True, "orderRef": "ORDER1"[-6:], "buyerLink": link}
    assert client.get("/api/orders/ORDER1/timeline").status_code == 401  # no longer public
    assert client.get("/api/o/forged/status").status_code == 404


# ---- Buyer requests and seller replies -------------------------------------------

@respx.mock
def test_buyer_requests_stored_stripped_and_shown_to_seller(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    link = captured_with_pii(client)["buyerLink"]
    url = f"{api_of(link)}/requests"
    assert client.post(url, json={"type": "refund", "message": "x"}).status_code == 400  # no reason
    assert client.post(url, json={"type": "unrecognised",
                                  "message": f"Not me! Email {PAYER_EMAIL} or call +1 415 555 0100"}).status_code == 200
    assert client.post(url, json={"type": "refund", "reason": "damaged",
                                  "message": "<script>alert(1)</script> box was crushed"}).status_code == 200

    seller = client.get("/api/orders/ORDER1/buyer").json()
    [first, second] = seller["requests"]
    assert first["type"] == "unrecognised" and PAYER_EMAIL not in first["message"]
    assert "[email removed]" in first["message"] and "555 0100" not in first["message"]
    assert second["reason"] == "Item arrived damaged"
    assert second["message"] == "<script>alert(1)</script> box was crushed"  # escaped when shown
    assert "buyer_request" in [e["kind"] for e in client.get("/api/orders/ORDER1/timeline").json()]


@respx.mock
def test_buyer_submissions_are_rate_limited_per_link(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    url = f"{api_of(captured_with_pii(client)['buyerLink'])}/requests"
    codes = [client.post(url, json={"type": "not_arrived", "message": str(i)}).status_code
             for i in range(7)]
    assert codes == [200] * 5 + [429, 429]


@respx.mock
def test_seller_reply_shows_on_buyer_page(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    link = captured_with_pii(client)["buyerLink"]
    rid = client.post(f"{api_of(link)}/requests", json={"type": "not_arrived"}).json()["request_id"]
    assert client.post(f"/api/orders/ORDER1/requests/{rid}/reply",
                       json={"message": "It's with DHL; tracking updates daily."}).status_code == 200
    assert client.post("/api/orders/ORDER1/requests/nope/reply", json={"message": "x"}).status_code == 404
    [req] = client.get(api_of(link)).json()["requests"]
    assert req["replies"][0]["message"] == "It's with DHL; tracking updates daily."


# ---- Refunds ---------------------------------------------------------------------

@respx.mock
def test_refund_bodies_match_paypal_schema(tmp_path):
    setup_paypal()
    route = respx.post(REFUND_URL).mock(side_effect=[
        httpx.Response(201, json={"id": "REF1", "status": "COMPLETED"}),
        httpx.Response(201, json={"id": "REF2", "status": "COMPLETED"})])
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    link = captured_with_pii(client)["buyerLink"]

    partial = client.post("/api/orders/ORDER1/refund", json={"mode": "partial", "amount": "40"})
    assert partial.json() == {"ok": True, "status": "COMPLETED", "amount": "40.00", "currency": "USD"}
    assert json.loads(route.calls[0].request.content) == {
        "amount": {"currency_code": "USD", "value": "40.00"}}
    assert route.calls[0].request.headers["PayPal-Request-Id"]

    # Over-refund is refused before any PayPal call (80.00 left).
    assert client.post("/api/orders/ORDER1/refund", json={"mode": "partial", "amount": "80.01"}).status_code == 400
    assert client.post("/api/orders/ORDER1/refund", json={"mode": "partial", "amount": "0"}).status_code == 400
    assert client.post("/api/orders/ORDER1/refund", json={"mode": "partial", "amount": "abc"}).status_code == 400
    assert route.call_count == 1

    # "Full" refunds the rest: empty body, PayPal refunds captured minus previous refunds.
    full = client.post("/api/orders/ORDER1/refund", json={"mode": "full"})
    assert full.json()["amount"] == "80.00"
    assert json.loads(route.calls[1].request.content) == {}
    assert route.calls[0].request.headers["PayPal-Request-Id"] != route.calls[1].request.headers["PayPal-Request-Id"]
    assert client.post("/api/orders/ORDER1/refund", json={"mode": "full"}).status_code == 409
    assert route.call_count == 2

    refunds = client.get(api_of(link)).json()["refunds"]
    assert [(r["amount"], r["currency"]) for r in refunds] == [("40.00", "USD"), ("80.00", "USD")]


@respx.mock
def test_refund_failure_recorded_without_retry(tmp_path):
    setup_paypal()
    route = respx.post(REFUND_URL).mock(return_value=httpx.Response(422, json={
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg-r1",
        "details": [{"issue": "CAPTURE_FULLY_REFUNDED", "description": "The capture has already been fully refunded"}]}))
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    captured_with_pii(client)
    resp = client.post("/api/orders/ORDER1/refund", json={"mode": "full", "request_id": "R1"})
    assert resp.status_code == 502 and route.call_count == 1
    [failed] = [e for e in client.get("/api/orders/ORDER1/timeline").json() if e["kind"] == "refund_failed"]
    assert failed["payload"]["paypal_error"]["details"][0]["issue"] == "CAPTURE_FULLY_REFUNDED"
    assert failed["payload"]["debug_id"] == "dbg-r1" and failed["payload"]["request_id"] == "R1"
    # Nothing was refunded, so the full amount is still refundable.
    assert client.get("/api/orders/ORDER1/buyer").json()["refundable"] == "120.00"


# ---- Mark shipped twice, and the counter ---------------------------------------------

@respx.mock
def test_double_mark_shipped_is_ignored(tmp_path):
    setup_paypal()
    track = respx.post(f"{API}/v2/checkout/orders/ORDER1/track").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1"}))
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    capture_order(client)
    body = {"tracking_number": "999999999999999", "carrier": "DHL"}
    assert client.post("/api/orders/ORDER1/tracking", json=body).json() == {"ok": True}
    assert client.post("/api/orders/ORDER1/tracking", json=body).json() == {"ok": True, "duplicate": True}
    assert track.call_count == 1
    kinds = [e["kind"] for e in client.get("/api/orders/ORDER1/timeline").json()]
    assert kinds.count("tracking_added") == 1
    # A genuinely different shipment is still recorded.
    client.post("/api/orders/ORDER1/tracking", json={"tracking_number": "111", "carrier": "DHL"})
    assert track.call_count == 2


@respx.mock
def test_questions_answered_before_dispute_counter(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    client, settings = make_client(tmp_path, gemini_key=None, groq_key=None)
    store = TimelineStore(settings.database_target)
    assert client.get("/api/stats").json() == {"questions_answered_before_dispute": 0,
                                               "auto_resolved": 0}

    shipped_order(client)  # ORDER1: the buyer asks, then disputes anyway -> not counted
    link = link_of(client)
    client.post(f"{api_of(link)}/requests", json={"type": "not_arrived"})
    assert client.get("/api/stats").json()["questions_answered_before_dispute"] == 1
    open_dispute(client)
    assert client.get("/api/stats").json()["questions_answered_before_dispute"] == 0

    for oid in ("ORDER2", "ORDER3"):  # two more captured orders whose buyers only asked
        store.add(oid, "order_created", {"item_id": "brand-kit", "amount": "120.00", "currency": "USD"})
        store.add(oid, "payment_captured", {"capture_id": f"CAP-{oid}", "status": "COMPLETED"})
        client.post(f"{api_of(link_of(client, oid))}/requests", json={"type": "unrecognised"})
    store.add("ORDER4", "payment_captured", {"capture_id": "CAP4"})  # no question: not counted
    assert client.get("/api/stats").json()["questions_answered_before_dispute"] == 2


# ---- Buyer page rendering (node harness) ---------------------------------------------

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def render_buyer(*scenarios):
    out = subprocess.run([NODE, str(ROOT / "tests/js/order_harness.js"),
                          str(ROOT / "app/static/order.html")],
                         input=json.dumps(list(scenarios)), capture_output=True, text=True,
                         encoding="utf-8", timeout=30, check=True)
    return json.loads(out.stdout)


@needs_node
@respx.mock
def test_buyer_page_renders_real_api_data_escaped(tmp_path):
    setup_paypal()
    respx.post(f"{API}/v2/checkout/orders/{LONG_ID}/track").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/v2/payments/captures/CAP1/refund").mock(
        return_value=httpx.Response(201, json={"id": "R", "status": "COMPLETED"}))
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    link = captured_with_pii(client, LONG_ID)["buyerLink"]
    client.post(f"/api/orders/{LONG_ID}/tracking", json={"tracking_number": "999999999999999", "carrier": "DHL"})
    rid = client.post(f"{api_of(link)}/requests", json={
        "type": "refund", "reason": "damaged", "message": "<img src=x onerror=alert(1)> crushed"}).json()["request_id"]
    client.post(f"/api/orders/{LONG_ID}/requests/{rid}/reply", json={"message": "Sorry! <b>Refunding</b> now."})
    client.post(f"/api/orders/{LONG_ID}/refund", json={"mode": "partial", "amount": "40", "request_id": rid})
    data = client.get(api_of(link)).json()

    closed, unrecognised, refund = render_buyer({"data": data}, {"data": data, "open": "unrecognised"},
                                                {"data": data, "open": "refund"})
    # Facts, not PII.
    assert "Demo Design Studio" in closed and "120.00 USD" in closed and "Paid with PayPal" in closed
    assert f"Order …{LONG_ID[-6:]}" in closed and LONG_ID not in closed and PAYER_EMAIL not in closed
    assert "999999999999999" in closed and "DHL" in closed
    assert "Refund of 40.00 USD sent" in closed
    assert 'data-source="template"' in closed
    # Buyer and seller text is escaped, never live HTML.
    assert "<img src=x" not in closed and "&#60;img src=x onerror=alert(1)&#62; crushed" in closed
    assert "<b>Refunding</b>" not in closed and "&#60;b&#62;Refunding&#60;/b&#62;" in closed
    # One-tap help panels.
    assert "This is a payment of 120.00 USD to Demo Design Studio" in unrecognised
    assert "Still not right? Contact the seller" in unrecognised and 'data-send="unrecognised"' in unrecognised
    assert '<option value="damaged">Item arrived damaged</option>' in refund and 'data-send="refund"' in refund


@needs_node
def test_buyer_page_not_shipped_and_no_requests():
    data = {"order_ref": "ABC123", "seller": "S", "item": "Kit", "amount": "10.00", "currency": "USD",
            "paid_on": "27 Oct 2026", "paid_with": "PayPal", "country": None,
            "shipping": {"shipped": False}, "summary": {"text": "You paid 10.00 USD.", "source": "ai"},
            "charge_explanation": "x", "requests": [], "refunds": [],
            "request_types": {"not_arrived": "My order hasn't arrived"}, "refund_reasons": {}}
    [page] = render_buyer({"data": data, "open": "not_arrived"})
    assert "Not shipped yet" in page and 'data-source="ai"' in page
    assert "Your messages" not in page and "Refund of" not in page
    assert "Delivery not confirmed yet" not in page


@needs_node
def test_buyer_line_shows_next_step_without_a_date():
    data = {"order_ref": "ABC123", "seller": "S", "item": "Kit", "amount": "10.00", "currency": "USD",
            "paid_on": "27 Oct 2026", "paid_with": "PayPal", "country": None,
            "shipping": {"shipped": True, "carrier": "FedEx", "tracking_number": "7789 2210", "shipped_on": "28 Oct 2026"},
            "summary": {"text": "It's on its way.", "source": "template"}, "charge_explanation": "x",
            "requests": [], "refunds": [], "request_types": {}, "refund_reasons": {}}
    [page] = render_buyer({"data": data})
    ahead = re.search(r'<li class="cl-stop ahead".*?</li>', page, re.S).group(0)
    assert "Delivery not confirmed yet" in ahead and "Oct" not in ahead  # never an invented date
    assert "Shipped with FedEx" in page and '<span class="num">7789 2210</span>' in page
