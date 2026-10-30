"""End-to-end flow against a mocked PayPal API (no credentials or network needed)."""

import json

import httpx
import respx
from fastapi.testclient import TestClient

from app.accounts import Accounts
from app.config import Settings
from app.main import create_app
from app.store import TimelineStore

API = "https://api-m.sandbox.paypal.com"


def make_client(tmp_path):
    settings = Settings(paypal_client_id="cid", paypal_client_secret="secret",
                        database_path=str(tmp_path / "test.db"),
                        app_secret="test-secret")
    http = httpx.AsyncClient(base_url=settings.api_base)
    client = TestClient(create_app(settings, http))
    # Seller APIs need a session: a test-only account, signed in through the real login.
    Accounts(TimelineStore(settings.database_target)).create_user("seller@example.com", "test-only-password-1")
    client.post("/api/login", json={"email": "seller@example.com", "password": "test-only-password-1"})
    return client


@respx.mock
def test_order_capture_tracking_flow(tmp_path):
    token = respx.post(f"{API}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 32400}))
    create = respx.post(f"{API}/v2/checkout/orders").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1", "status": "CREATED"}))
    capture = respx.post(f"{API}/v2/checkout/orders/ORDER1/capture").mock(
        return_value=httpx.Response(201, json={
            "id": "ORDER1", "status": "COMPLETED",
            "payer": {"email_address": "buyer@us.example.com", "address": {"country_code": "US"}},
            "purchase_units": [{"payments": {"captures": [{"id": "CAP1"}]}}],
        }))
    track = respx.post(f"{API}/v2/checkout/orders/ORDER1/track").mock(
        return_value=httpx.Response(201, json={"id": "ORDER1"}))

    client = make_client(tmp_path)

    # Buyer page config exposes the public Client ID and sandbox SDK URL, never the secret.
    cfg = client.get("/api/config").json()
    assert cfg["clientId"] == "cid" and "sandbox" in cfg["sdkUrl"]
    assert "secret" not in json.dumps(cfg)

    # Price comes from the server catalogue, not the request.
    assert client.post("/api/orders", json={"item_id": "brand-kit"}).json() == {"orderId": "ORDER1"}
    sent = json.loads(create.calls[0].request.content)
    assert sent["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "120.00"}
    assert "PayPal-Request-Id" in create.calls[0].request.headers

    # Tracking is refused before capture.
    assert client.post("/api/orders/ORDER1/tracking", json={"tracking_number": "X"}).status_code == 409

    captured = client.post("/api/orders/ORDER1/capture").json()
    assert {k: captured[k] for k in ("status", "captureId")} == {"status": "COMPLETED", "captureId": "CAP1"}
    assert captured["buyerLink"].startswith("/o/")  # Day 4: the buyer's private order page

    assert client.post("/api/orders/ORDER1/tracking",
                       json={"tracking_number": "1234567890", "carrier": "DHL"}).json() == {"ok": True}
    assert json.loads(track.calls[0].request.content)["capture_id"] == "CAP1"

    kinds = [e["kind"] for e in client.get("/api/orders/ORDER1/timeline").json()]
    assert kinds == ["order_created", "payment_captured", "tracking_added"]
    assert token.call_count == 1  # token reused across calls
    assert capture.called


@respx.mock
def test_unknown_item_and_paypal_errors(tmp_path):
    respx.post(f"{API}/v1/oauth2/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 32400}))
    respx.post(f"{API}/v2/checkout/orders").mock(
        return_value=httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"}))
    client = make_client(tmp_path)

    assert client.post("/api/orders", json={"item_id": "nope"}).status_code == 400
    resp = client.post("/api/orders", json={"item_id": "brand-kit"})
    assert resp.status_code == 502
    assert resp.json()["detail"]["paypal_error"]["name"] == "UNPROCESSABLE_ENTITY"
