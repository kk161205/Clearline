"""Shared helpers for the Day 3 (AI assistant) tests. Everything is mocked with respx."""

import copy
import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_webhooks import API, capture_order, mock_token, mock_verify, post_event

GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GROQ = "https://api.groq.com/openai/v1/chat/completions"
DISPUTE_ID = "PP-D-1"
PAYER_EMAIL = "buyer@us.example.com"


SELLER_PASSWORD = "test-pass"


def make_client(tmp_path, *, gemini_key="g-test", groq_key="q-test", paypal_env="sandbox",
                auto_sync_minutes=0, login=True, seller_password=SELLER_PASSWORD, **overrides):
    # Every AI setting is explicit so a real .env can't change the result.
    settings = Settings(paypal_client_id="cid", paypal_client_secret="secret",
                        database_path=str(tmp_path / "test.db"), paypal_webhook_id="WH-1",
                        gemini_api_key=gemini_key, gemini_model="gemini-test-lite",
                        groq_api_key=groq_key, groq_model="openai/gpt-oss-120b",
                        ai_timeout_seconds=5, paypal_env=paypal_env,
                        auto_sync_minutes=auto_sync_minutes, app_secret="test-secret",
                        seller_password=seller_password,
                        **{"seller_country": "IN", **overrides})
    http = httpx.AsyncClient(base_url=settings.api_base)
    client = TestClient(create_app(settings, http))
    if login and seller_password:
        assert client.post("/api/login", json={"password": seller_password}).status_code == 200
    return client, settings


def full_dispute(**overrides) -> dict:
    """A dispute record shaped like PayPal's GET /v1/customer/disputes/{id} for our order."""
    base = f"{API}/v1/customer/disputes/{DISPUTE_ID}"
    dispute = {
        "dispute_id": DISPUTE_ID,
        "create_time": "2026-10-26T16:58:07.000Z",
        "update_time": "2026-10-26T17:01:43.853Z",
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "dispute_amount": {"currency_code": "USD", "value": "120.00"},
        "seller_response_due_date": "2026-11-15T16:58:07.890Z",
        "disputed_transactions": [{"seller_transaction_id": "CAP1"}],
        "messages": [{"posted_by": "BUYER", "time_posted": "2026-10-26T16:58:07.000Z",
                      "content": f"Never arrived. Email me at {PAYER_EMAIL} or +1 415 555 0100."}],
        "evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "REQUESTED_FROM_SELLER"}],
        "allowed_response_options": {"accept_claim": {"accept_claim_types": ["PARTIAL_REFUND", "REFUND"]},
                                     "make_offer": {"offer_types": ["REFUND"]}},
        "links": [{"rel": "self", "method": "GET", "href": base}]
                 + [{"rel": a.replace("-", "_"), "method": "POST", "href": f"{base}/{a}"}
                    for a in ("accept-claim", "make-offer", "escalate", "send-message",
                              "provide-evidence")],
    }
    dispute.update(overrides)
    return dispute


class DisputeOnPayPal:
    """A mutable dispute served by the mocked GET endpoint, so tests can change it mid-flow."""

    def __init__(self, **overrides):
        self.data = full_dispute(**overrides)
        self.route = respx.get(f"{API}/v1/customer/disputes/{DISPUTE_ID}").mock(
            side_effect=lambda request: httpx.Response(200, json=copy.deepcopy(self.data)))


def fight(**overrides) -> dict:
    rec = {
        "decision": "FIGHT", "confidence": 0.8,
        "reasons": [{"text": "The order was shipped with tracking.",
                     "fact_keys": ["tracking.carrier", "tracking.number"]}],
        "missing_evidence": ["Delivery confirmation"],
        "message_to_buyer": "We shipped your order with DHL. Here is what we know so far.",
        "note_to_paypal": "Order shipped with DHL tracking 1234567890 before the dispute.",
        "offer": None,
    }
    rec.update(overrides)
    return rec


def offer(amount=40.0, **overrides) -> dict:
    return fight(decision="OFFER", offer={"type": "REFUND", "amount": amount},
                 reasons=[{"text": "Small amount, weak evidence.", "fact_keys": ["dispute.amount"]}],
                 **overrides)


def ai_reply(rec: dict | str) -> httpx.Response:
    content = rec if isinstance(rec, str) else json.dumps(rec)
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                              "content": content}}]})


def shipped_order(client, carrier="DHL", number="1234567890"):
    """ORDER1 captured as CAP1 and marked shipped (Day 1 flow)."""
    respx.post(f"{API}/v2/checkout/orders/ORDER1/track").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1"}))
    capture_order(client)
    client.post("/api/orders/ORDER1/tracking", json={"tracking_number": number, "carrier": carrier})


def open_dispute(client, event_id="WH-EVT-1"):
    """Deliver a verified CUSTOMER.DISPUTE.CREATED webhook for the dispute."""
    return post_event(client, {"id": event_id, "event_type": "CUSTOMER.DISPUTE.CREATED",
                               "resource": {"dispute_id": DISPUTE_ID}})


def setup_paypal():
    mock_token()
    mock_verify()


def kinds(client, order_id="ORDER1") -> list[str]:
    return [e["kind"] for e in client.get(f"/api/orders/{order_id}/timeline").json()]


def events(client, order_id, kind) -> list[dict]:
    resp = client.get(f"/api/orders/{order_id}/timeline")
    return [e for e in resp.json() if e["kind"] == kind] if resp.status_code == 200 else []


def multipart_json(request: httpx.Request, part: str = "input") -> dict:
    """The JSON inside one named part of a multipart/form-data request."""
    ctype = request.headers["content-type"]
    assert ctype.startswith("multipart/form-data"), ctype
    boundary = ctype.split("boundary=")[1].encode()
    for chunk in request.content.split(b"--" + boundary):
        if f'name="{part}"'.encode() in chunk:
            head, body = chunk.split(b"\r\n\r\n", 1)
            assert b"application/json" in head
            return json.loads(body.rstrip(b"\r\n-"))
    raise AssertionError(f"no part named {part!r}")
