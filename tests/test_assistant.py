"""Day 3: evidence pack, guardrails, one-tap actions and dispute sync (no network)."""

import json

import httpx
import respx

from app.assistant import Recommendation, check_guardrails
from app.store import TimelineStore
from tests.support import (API, DISPUTE_ID, GEMINI, GROQ, PAYER_EMAIL, DisputeOnPayPal, ai_reply,
                           events, fight, kinds, make_client, multipart_json, offer,
                           open_dispute, setup_paypal, shipped_order)
from tests.test_webhooks import capture_order

DISPUTES = f"{API}/v1/customer/disputes"
FACTS = {"tracking.carrier": "DHL", "tracking.number": "1234567890",
         "dispute.reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "dispute.amount": "120.00",
         "dispute.allowed_offer_types": ["REFUND"]}


def rec(**fields) -> Recommendation:
    return Recommendation.model_validate(fight(**fields))


# ---- Guardrails --------------------------------------------------------------

def test_guardrails_accept_a_good_answer():
    assert check_guardrails(rec(), FACTS) == []
    assert check_guardrails(Recommendation.model_validate(offer(40)), FACTS) == []


def test_guardrails_reject_unknown_fact_key():
    bad = rec(reasons=[{"text": "Delivered.", "fact_keys": ["tracking.delivered_at"]}])
    assert "don't exist" in check_guardrails(bad, FACTS)[0]


def test_guardrails_reject_offer_above_dispute_amount():
    assert any("at most 120.0" in e
               for e in check_guardrails(Recommendation.model_validate(offer(150)), FACTS))
    assert check_guardrails(Recommendation.model_validate(offer(0)), FACTS)


def test_guardrails_reject_fight_without_tracking():
    facts = {k: v for k, v in FACTS.items() if not k.startswith("tracking.")}
    good_reason = [{"text": "Item not received claim.", "fact_keys": ["dispute.reason"]}]
    errors = check_guardrails(rec(reasons=good_reason), facts)
    assert errors == ["FIGHT on an item-not-received claim needs tracking facts, and there are none"]


def test_guardrails_reject_email_or_link_in_text():
    assert check_guardrails(rec(message_to_buyer="Write to help@shop.example.com"), FACTS)
    assert check_guardrails(rec(note_to_paypal="See https://track.example/123"), FACTS)
    assert check_guardrails(rec(message_to_buyer="word " * 130), FACTS)


@respx.mock
def test_guardrail_rejection_retries_once_then_ai_failed(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    bad = fight(message_to_buyer="Email us at help@shop.example.com")
    gemini = respx.post(GEMINI).mock(side_effect=[ai_reply(bad), ai_reply(bad)])
    groq = respx.post(GROQ).mock(return_value=ai_reply(fight()))
    client, settings = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)

    [failed] = events(client, "ORDER1", "ai_failed")
    assert "guardrails rejected" in failed["payload"]["error"]
    assert gemini.call_count == 2
    assert "broke these rules" in json.loads(gemini.calls[1].request.content)["messages"][-1]["content"]
    # Guardrail failures are about the answer, not an outage: no provider switch.
    assert not groq.called
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "gemini"


@respx.mock
def test_guardrail_rejection_then_fixed_answer_is_stored(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    unknown = fight(reasons=[{"text": "Delivered.", "fact_keys": ["tracking.delivered_at"]}])
    respx.post(GEMINI).mock(side_effect=[ai_reply(unknown), ai_reply(fight())])
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    [stored] = events(client, "ORDER1", "ai_recommendation")
    assert stored["payload"]["recommendation"]["reasons"][0]["fact_keys"] == ["tracking.carrier",
                                                                              "tracking.number"]


# ---- Facts and PII -----------------------------------------------------------

@respx.mock
def test_prompt_contains_facts_but_no_pii(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    respx.post(f"{API}/v2/checkout/orders").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1", "status": "CREATED"}))
    respx.post(f"{API}/v2/checkout/orders/ORDER1/capture").mock(
        return_value=httpx.Response(201, json={
            "id": "ORDER1", "status": "COMPLETED",
            "payer": {"email_address": PAYER_EMAIL, "name": {"given_name": "Jane"},
                      "payer_id": "PAYERID123", "address": {"country_code": "US"}},
            "purchase_units": [{"payments": {"captures": [{"id": "CAP1"}]}}]}))
    respx.post(f"{API}/v2/checkout/orders/ORDER1/track").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1"}))
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    client.post("/api/orders", json={"item_id": "brand-kit"})
    client.post("/api/orders/ORDER1/capture")
    client.post("/api/orders/ORDER1/tracking", json={"tracking_number": "1234567890",
                                                     "carrier": "DHL"})
    open_dispute(client)

    body = gemini.calls[0].request.content.decode()
    assert PAYER_EMAIL not in body and "PAYERID123" not in body and "Jane" not in body
    assert "555 0100" not in body  # phone number in the buyer's message
    facts = json.loads(json.loads(body)["messages"][1]["content"].split("FACTS:\n", 1)[1]
                       .split("\n\nRecommend", 1)[0])
    assert facts["tracking.number"] == "1234567890"
    assert facts["dispute.reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert facts["dispute.amount"] == "120.00" and facts["dispute.due_date"]
    assert facts["payment.payer_country"] == "US"
    assert facts["dispute.buyer_messages"][0].startswith("Never arrived.")
    assert "[email removed]" in facts["dispute.buyer_messages"][0]


# ---- When it runs ------------------------------------------------------------

@respx.mock
def test_runs_after_dispute_webhook_once_per_update_time(tmp_path):
    setup_paypal()
    paypal = DisputeOnPayPal()
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    shipped_order(client)

    open_dispute(client, "WH-EVT-1")
    assert gemini.call_count == 1
    open_dispute(client, "WH-EVT-2")  # PayPal resends "created" with the same update_time
    assert gemini.call_count == 1
    assert len(events(client, "ORDER1", "ai_recommendation")) == 1

    # The seller can always force a fresh one.
    client.post(f"/api/disputes/{DISPUTE_ID}/recommendation")
    assert gemini.call_count == 2

    # A real change on PayPal (new update_time) picked up by Sync gets a new recommendation.
    paypal.data["update_time"] = "2026-10-27T09:00:00.000Z"
    respx.get(DISPUTES).mock(return_value=httpx.Response(200, json={"items": [
        {"dispute_id": DISPUTE_ID, "update_time": "2026-10-27T09:00:00.000Z",
         "status": "WAITING_FOR_SELLER_RESPONSE"}], "links": []}))
    client.post("/api/disputes/sync")
    assert gemini.call_count == 3
    latest = client.get(f"/api/disputes/{DISPUTE_ID}/recommendation").json()
    assert latest["payload"]["dispute_update_time"] == "2026-10-27T09:00:00.000Z"


@respx.mock
def test_no_auto_run_for_unmatched_or_without_keys(tmp_path):
    setup_paypal()
    DisputeOnPayPal(disputed_transactions=[{"seller_transaction_id": "CAP-UNKNOWN"}])
    gemini = respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    open_dispute(client)
    assert kinds(client, f"unmatched:{DISPUTE_ID}") == ["dispute_created"]
    assert not gemini.called

    (tmp_path / "nokeys").mkdir()
    client, _ = make_client(tmp_path / "nokeys", gemini_key=None, groq_key=None)
    shipped_order(client)
    open_dispute(client)
    assert "ai_recommendation" not in kinds(client) and not gemini.called
    assert client.post(f"/api/disputes/{DISPUTE_ID}/recommendation").status_code == 503


# ---- One-tap actions ---------------------------------------------------------

def disputed_with_recommendation(tmp_path, recommendation, **dispute):
    setup_paypal()
    paypal = DisputeOnPayPal(**dispute)
    respx.post(GEMINI).mock(return_value=ai_reply(recommendation))
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    assert events(client, "ORDER1", "ai_recommendation")
    return client, paypal


@respx.mock
def test_fight_sends_message_and_tracking_evidence(tmp_path):
    client, paypal = disputed_with_recommendation(tmp_path, fight())
    msg = respx.post(f"{DISPUTES}/{DISPUTE_ID}/send-message").mock(
        return_value=httpx.Response(200, json={"links": [{"rel": "self"}]}))
    evidence = respx.post(f"{DISPUTES}/{DISPUTE_ID}/provide-evidence").mock(
        side_effect=lambda request: (paypal.data.update(update_time="2026-10-27T10:00:00.000Z",
                                                        status="UNDER_REVIEW")
                                     or httpx.Response(200, json={"links": [{"rel": "self"}]})))

    resp = client.post(f"/api/disputes/{DISPUTE_ID}/act",
                       json={"action": "FIGHT", "message_to_buyer": "Edited by the seller.",
                             "note_to_paypal": "Shipped with DHL 1234567890."})
    assert resp.status_code == 200, resp.text
    assert [s["endpoint"] for s in resp.json()["steps"]] == ["send-message", "provide-evidence"]

    assert multipart_json(msg.calls[0].request) == {"message": "Edited by the seller."}
    assert multipart_json(evidence.calls[0].request) == {"evidences": [{
        "evidence_type": "PROOF_OF_FULFILLMENT",
        "evidence_info": {"tracking_info": [{"carrier_name": "DHL",
                                             "tracking_number": "1234567890"}]},
        "notes": "Shipped with DHL 1234567890."}]}
    for route in (msg, evidence):
        assert route.calls[0].request.headers["PayPal-Request-Id"]

    sent = events(client, "ORDER1", "dispute_action_sent")
    assert [e["payload"]["endpoint"] for e in sent] == ["send-message", "provide-evidence"]
    # The dispute was re-fetched afterwards and its new state recorded.
    last = events(client, "ORDER1", "dispute_updated")[-1]["payload"]
    assert last["status"] == "UNDER_REVIEW" and last["source"] == "action"


@respx.mock
def test_offer_calls_make_offer_with_json_body(tmp_path):
    client, _ = disputed_with_recommendation(tmp_path, offer(40))
    route = respx.post(f"{DISPUTES}/{DISPUTE_ID}/make-offer").mock(
        return_value=httpx.Response(200, json={"links": []}))
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/act",
                       json={"action": "OFFER", "offer": {"type": "REFUND", "amount": 35}})
    assert resp.status_code == 200, resp.text
    request = route.calls[0].request
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "note": "Order shipped with DHL tracking 1234567890 before the dispute.",  # AI draft
        "offer_type": "REFUND", "offer_amount": {"currency_code": "USD", "value": "35.00"}}
    assert "PayPal-Request-Id" in request.headers

    too_much = client.post(f"/api/disputes/{DISPUTE_ID}/act",
                           json={"action": "OFFER", "offer": {"type": "REFUND", "amount": 500}})
    assert too_much.status_code == 400 and route.call_count == 1


@respx.mock
def test_refund_calls_accept_claim_for_full_amount(tmp_path):
    client, _ = disputed_with_recommendation(tmp_path, fight())
    route = respx.post(f"{DISPUTES}/{DISPUTE_ID}/accept-claim").mock(
        return_value=httpx.Response(200, json={"links": []}))
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/act",
                       json={"action": "REFUND", "note_to_paypal": "Refunding in full."})
    assert resp.status_code == 200, resp.text
    # REFUND = full disputed amount; PayPal's schema says to omit refund_amount for it.
    assert multipart_json(route.calls[0].request) == {"accept_claim_type": "REFUND",
                                                      "note": "Refunding in full."}


@respx.mock
def test_action_not_allowed_returns_409(tmp_path):
    base = f"{DISPUTES}/{DISPUTE_ID}"
    client, paypal = disputed_with_recommendation(tmp_path, offer(40))
    calls = respx.post(url__startswith=f"{base}/").mock(return_value=httpx.Response(200, json={}))

    # PayPal moved the dispute to the CHARGEBACK/claim stage: no offers, no messages.
    paypal.data.update(dispute_life_cycle_stage="CHARGEBACK",
                       links=[{"rel": "accept_claim", "method": "POST", "href": f"{base}/accept-claim"}])
    for action in ("OFFER", "FIGHT"):
        resp = client.post(f"/api/disputes/{DISPUTE_ID}/act", json={"action": action})
        assert resp.status_code == 409, resp.text
    paypal.data.update(status="RESOLVED")
    assert client.post(f"/api/disputes/{DISPUTE_ID}/act",
                       json={"action": "REFUND"}).status_code == 409
    assert not calls.called


@respx.mock
def test_paypal_rejection_is_recorded_without_retry(tmp_path):
    client, _ = disputed_with_recommendation(tmp_path, fight())
    msg = respx.post(f"{DISPUTES}/{DISPUTE_ID}/send-message").mock(
        return_value=httpx.Response(400, json={"name": "INVALID_REQUEST"}))
    evidence = respx.post(f"{DISPUTES}/{DISPUTE_ID}/provide-evidence").mock(
        return_value=httpx.Response(200, json={}))
    resp = client.post(f"/api/disputes/{DISPUTE_ID}/act", json={"action": "FIGHT"})
    assert resp.status_code == 502
    assert msg.call_count == 1 and not evidence.called  # stop at the first rejection
    [failed] = events(client, "ORDER1", "dispute_action_failed")
    assert failed["payload"]["paypal_status"] == 400 and failed["payload"]["endpoint"] == "send-message"


# ---- Sync and event order ----------------------------------------------------

@respx.mock
def test_sync_inserts_new_skips_unchanged_records_changed(tmp_path):
    setup_paypal()
    paypal = DisputeOnPayPal()
    respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    capture_order(client)

    listing = {"items": [{"dispute_id": DISPUTE_ID, "update_time": paypal.data["update_time"],
                          "status": "WAITING_FOR_SELLER_RESPONSE"}],
               "links": [{"rel": "next", "href": f"{DISPUTES}?page=2"}]}
    pages = respx.get(DISPUTES).mock(side_effect=[
        httpx.Response(200, json=listing), httpx.Response(200, json={"items": [], "links": []}),
        httpx.Response(200, json={**listing, "links": []}),
        httpx.Response(200, json={"items": [{**listing["items"][0], "status": "UNDER_REVIEW",
                                             "update_time": "2026-10-28T00:00:00.000Z"}],
                                  "links": []})])

    assert client.post("/api/disputes/sync").json() == {
        "listed": 1, "new": 1, "changed": 0, "unchanged": 0, "failed": 0}
    assert pages.call_count == 2  # followed the "next" link
    assert dict(pages.calls[0].request.url.params) == {"page_size": "50", "page": "1"}
    assert kinds(client).count("dispute_created") == 1

    assert client.post("/api/disputes/sync").json()["unchanged"] == 1
    assert kinds(client).count("dispute_created") == 1  # no duplicate row

    paypal.data.update(status="UNDER_REVIEW", update_time="2026-10-28T00:00:00.000Z")
    assert client.post("/api/disputes/sync").json()["changed"] == 1
    assert events(client, "ORDER1", "dispute_updated")[-1]["payload"]["status"] == "UNDER_REVIEW"


def test_current_state_uses_latest_update_time(tmp_path):
    store = TimelineStore(str(tmp_path / "t.db"))
    store.add("ORDER1", "dispute_updated", {"dispute_id": "D1", "status": "UNDER_REVIEW",
                                           "update_time": "2026-10-27T10:00:00.000Z"})
    # "created" arrives after "updated" (we saw this happen), with an older update_time.
    store.add("ORDER1", "dispute_created", {"dispute_id": "D1", "status": "WAITING_FOR_SELLER_RESPONSE",
                                           "update_time": "2026-10-26T17:00:00.000Z"})
    state = store.latest_dispute_state("D1")
    assert state["kind"] == "dispute_updated" and state["payload"]["status"] == "UNDER_REVIEW"
