"""Day 2: PayPal webhooks and disputes against a mocked PayPal API (no network)."""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.store import TimelineStore

API = "https://api-m.sandbox.paypal.com"
VERIFY = f"{API}/v1/notifications/verify-webhook-signature"

# Headers PayPal sends with every webhook (mixed case on purpose).
SIG_HEADERS = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "Paypal-Transmission-Id": "tx-1",
    "PAYPAL-TRANSMISSION-SIG": "sig==",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-26T10:00:00Z",
}


def make_client(tmp_path, *, webhook_id="WH-1", allow_unverified=False):
    # Webhook settings are passed explicitly so a real .env can't change the result.
    settings = Settings(paypal_client_id="cid", paypal_client_secret="secret",
                        database_path=str(tmp_path / "test.db"),
                        paypal_webhook_id=webhook_id,
                        allow_unverified_webhooks=allow_unverified)
    http = httpx.AsyncClient(base_url=settings.api_base)
    return TestClient(create_app(settings, http))


def mock_token():
    respx.post(f"{API}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 32400}))


def mock_verify(status="SUCCESS"):
    return respx.post(VERIFY).mock(
        return_value=httpx.Response(200, json={"verification_status": status}))


def capture_order(client, order_id="ORDER1", capture_id="CAP1"):
    """Run the Day 1 create + capture flow so the store knows ORDER1 <-> CAP1."""
    respx.post(f"{API}/v2/checkout/orders").mock(
        return_value=httpx.Response(201, json={"id": order_id, "status": "CREATED"}))
    respx.post(f"{API}/v2/checkout/orders/{order_id}/capture").mock(
        return_value=httpx.Response(201, json={
            "id": order_id, "status": "COMPLETED",
            "purchase_units": [{"payments": {"captures": [{"id": capture_id}]}}],
        }))
    client.post("/api/orders", json={"item_id": "brand-kit"})
    client.post(f"/api/orders/{order_id}/capture")


def dispute_record(dispute_id="PP-D-1", capture_id="CAP1"):
    return {
        "dispute_id": dispute_id,
        "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
        "status": "WAITING_FOR_SELLER_RESPONSE",
        "dispute_life_cycle_stage": "INQUIRY",
        "dispute_amount": {"currency_code": "USD", "value": "120.00"},
        "seller_response_due_date": "2026-11-05T10:00:00.000Z",
        "disputed_transactions": [{"seller_transaction_id": capture_id}],
    }


def dispute_event(event_id="WH-EVT-1", dispute_id="PP-D-1", event_type="CUSTOMER.DISPUTE.CREATED"):
    # Webhook resources can be thinner than the full record; only dispute_id is relied on.
    return {"id": event_id, "event_type": event_type, "resource_type": "dispute",
            "resource": {"dispute_id": dispute_id, "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                         "status": "OPEN"}}


def post_event(client, event):
    return client.post("/webhooks/paypal", json=event, headers=SIG_HEADERS)


@respx.mock
def test_verified_dispute_lands_on_captured_order(tmp_path):
    mock_token()
    verify = mock_verify()
    respx.get(f"{API}/v1/customer/disputes/PP-D-1").mock(
        return_value=httpx.Response(200, json=dispute_record()))
    client = make_client(tmp_path)
    capture_order(client)

    resp = post_event(client, dispute_event())
    assert resp.status_code == 200 and resp.json() == {"status": "accepted"}

    # Verification request is built from the headers (case-insensitive) + webhook ID + event.
    sent = json.loads(verify.calls[0].request.content)
    assert sent["auth_algo"] == "SHA256withRSA"
    assert sent["transmission_id"] == "tx-1"
    assert sent["cert_url"].endswith("CERT-1")
    assert sent["webhook_id"] == "WH-1"
    assert sent["webhook_event"]["id"] == "WH-EVT-1"

    events = client.get("/api/orders/ORDER1/timeline").json()
    assert [e["kind"] for e in events] == ["order_created", "payment_captured", "dispute_created"]
    p = events[-1]["payload"]
    assert p["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
    assert p["seller_response_due_date"] == "2026-11-05T10:00:00.000Z"
    assert p["dispute_amount"] == {"value": "120.00", "currency_code": "USD"}
    assert p["status"] == "WAITING_FOR_SELLER_RESPONSE"
    assert p["dispute_life_cycle_stage"] == "INQUIRY"
    assert p["dispute_id"] == "PP-D-1" and p["event_id"] == "WH-EVT-1"
    assert "enrichment_error" not in p


@respx.mock
def test_duplicate_event_is_recorded_once(tmp_path):
    mock_token()
    mock_verify()
    respx.get(f"{API}/v1/customer/disputes/PP-D-1").mock(
        return_value=httpx.Response(200, json=dispute_record()))
    client = make_client(tmp_path)
    capture_order(client)

    assert post_event(client, dispute_event()).json() == {"status": "accepted"}
    second = post_event(client, dispute_event())
    assert second.status_code == 200 and second.json() == {"status": "duplicate"}

    kinds = [e["kind"] for e in client.get("/api/orders/ORDER1/timeline").json()]
    assert kinds.count("dispute_created") == 1


@respx.mock
def test_failed_verification_rejected_unless_allowed(tmp_path):
    mock_token()
    mock_verify("FAILURE")
    respx.get(f"{API}/v1/customer/disputes/PP-D-1").mock(
        return_value=httpx.Response(200, json=dispute_record()))

    (tmp_path / "strict").mkdir()
    (tmp_path / "lenient").mkdir()

    strict = make_client(tmp_path / "strict")
    assert post_event(strict, dispute_event()).status_code == 400
    assert strict.get("/api/orders").json() == []

    lenient = make_client(tmp_path / "lenient", allow_unverified=True)
    resp = post_event(lenient, dispute_event())
    assert resp.status_code == 200 and resp.json() == {"status": "accepted"}
    assert lenient.get("/api/orders/unmatched:PP-D-1/timeline").status_code == 200


def test_missing_webhook_id_returns_503(tmp_path):
    client = make_client(tmp_path, webhook_id=None)
    resp = post_event(client, dispute_event())
    assert resp.status_code == 503
    assert "PAYPAL_WEBHOOK_ID" in resp.json()["detail"]


@respx.mock
def test_dispute_for_unknown_capture_is_unmatched(tmp_path):
    mock_token()
    mock_verify()
    respx.get(f"{API}/v1/customer/disputes/PP-D-9").mock(
        return_value=httpx.Response(200, json=dispute_record("PP-D-9", capture_id="CAP-UNKNOWN")))
    client = make_client(tmp_path)

    post_event(client, dispute_event(dispute_id="PP-D-9"))
    events = client.get("/api/orders/unmatched:PP-D-9/timeline").json()
    assert [e["kind"] for e in events] == ["dispute_created"]
    assert events[0]["payload"]["dispute_id"] == "PP-D-9"


@respx.mock
def test_get_dispute_failure_still_records_event(tmp_path):
    mock_token()
    mock_verify()
    respx.get(f"{API}/v1/customer/disputes/PP-D-1").mock(
        return_value=httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"}))
    client = make_client(tmp_path)
    capture_order(client)

    assert post_event(client, dispute_event()).status_code == 200
    # Without the full record there's no capture ID to match, so it goes to unmatched.
    events = client.get("/api/orders/unmatched:PP-D-1/timeline").json()
    p = events[0]["payload"]
    assert events[0]["kind"] == "dispute_created"
    assert p["reason"] == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"  # from the webhook resource
    assert "500" in p["enrichment_error"]


@respx.mock
@pytest.mark.parametrize("event_type,resource,kind", [
    ("PAYMENT.CAPTURE.PENDING",
     {"id": "CAP1", "status": "PENDING", "amount": {"value": "120.00", "currency_code": "USD"},
      "status_details": {"reason": "RECEIVING_PREFERENCE_MANDATES_MANUAL_ACTION"}},
     "webhook_payment_capture_pending"),
    ("PAYMENT.CAPTURE.REFUNDED",  # resource is the refund; capture is in its "up" link
     {"id": "REF1", "status": "COMPLETED", "amount": {"value": "120.00", "currency_code": "USD"},
      "links": [{"rel": "up", "href": f"{API}/v2/payments/captures/CAP1"}]},
     "webhook_payment_capture_refunded"),
])
def test_capture_events_map_to_order(tmp_path, event_type, resource, kind):
    mock_token()
    mock_verify()
    client = make_client(tmp_path)
    capture_order(client)

    post_event(client, {"id": "WH-CAP", "event_type": event_type, "resource": resource})
    last = client.get("/api/orders/ORDER1/timeline").json()[-1]
    assert last["kind"] == kind
    assert last["payload"]["amount"] == {"value": "120.00", "currency_code": "USD"}
    if event_type.endswith("PENDING"):
        assert last["payload"]["pending_reason"] == "RECEIVING_PREFERENCE_MANDATES_MANUAL_ACTION"


@respx.mock
def test_processing_crash_is_recorded(tmp_path, monkeypatch):
    mock_token()
    mock_verify()
    respx.get(f"{API}/v1/customer/disputes/PP-D-1").mock(
        return_value=httpx.Response(200, json=dispute_record()))

    def boom(self, capture_id):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(TimelineStore, "find_order_by_capture", boom)
    client = make_client(tmp_path)

    resp = post_event(client, dispute_event())
    assert resp.status_code == 200 and resp.json() == {"status": "accepted"}
    events = client.get("/api/orders/other/timeline").json()
    assert [e["kind"] for e in events] == ["webhook_processing_failed"]
    assert events[0]["payload"] == {"event_id": "WH-EVT-1",
                                    "event_type": "CUSTOMER.DISPUTE.CREATED",
                                    "error": "store exploded"}


@respx.mock
def test_other_event_types_are_kept(tmp_path):
    mock_token()
    mock_verify()
    client = make_client(tmp_path)

    post_event(client, {"id": "WH-X", "event_type": "CHECKOUT.ORDER.APPROVED", "resource": {}})
    events = client.get("/api/orders/other/timeline").json()
    assert events[0]["payload"]["event_type"] == "CHECKOUT.ORDER.APPROVED"
