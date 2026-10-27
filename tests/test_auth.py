"""Seller login: protected vs public routes, sessions, logout, rate limits (no network)."""

import httpx
import pytest
import respx

from app.auth import PUBLIC_ROUTES, SellerAuth, is_public
from tests.support import (API, DISPUTE_ID, DisputeOnPayPal, make_client, open_dispute,
                           setup_paypal, shipped_order)

SELLER_ROUTES = [
    ("GET", "/seller"), ("GET", "/api/orders"), ("GET", "/api/orders/ORDER1/timeline"),
    ("POST", "/api/orders/ORDER1/tracking"), ("GET", "/api/orders/ORDER1/buyer"),
    ("POST", "/api/orders/ORDER1/refund"), ("POST", "/api/orders/ORDER1/requests/R/reply"),
    ("GET", "/api/stats"), ("POST", "/api/disputes/sync"), ("GET", "/api/disputes/sync/status"),
    ("POST", f"/api/disputes/{DISPUTE_ID}/act"), ("GET", f"/api/disputes/{DISPUTE_ID}/recommendation"),
    ("POST", f"/api/disputes/{DISPUTE_ID}/sandbox/require-seller-evidence"),
    ("GET", "/api/ai/status"), ("POST", "/api/ai/reset"), ("GET", "/docs"), ("GET", "/openapi.json"),
]


@pytest.mark.parametrize("method,path", SELLER_ROUTES)
def test_seller_routes_need_login(tmp_path, method, path):
    client, _ = make_client(tmp_path, login=False)
    resp = client.request(method, path, follow_redirects=False, json={})
    if method == "GET" and not path.startswith("/api/"):
        assert resp.status_code == 303 and resp.headers["location"] == "/login"  # pages redirect
    else:
        assert resp.status_code == 401 and resp.json()["detail"] == "Log in as the seller first."


def test_public_routes_are_exactly_the_agreed_ones():
    assert len(PUBLIC_ROUTES) == 13  # adding a public route should be a deliberate change
    for method, path in [("GET", "/"), ("GET", "/api/config"), ("POST", "/api/orders"),
                         ("POST", "/api/orders/X/capture"), ("GET", "/o/abc"), ("GET", "/api/o/abc"),
                         ("GET", "/api/o/abc/status"), ("POST", "/api/o/abc/requests"),
                         ("POST", "/webhooks/paypal"), ("GET", "/login"), ("HEAD", "/")]:
        assert is_public(method, path), (method, path)
    for method, path in SELLER_ROUTES + [("GET", "/api/orders/X"), ("POST", "/api/o/abc"),
                                         ("GET", "/o/abc/extra"), ("DELETE", "/api/orders")]:
        assert not is_public(method, path), (method, path)


@respx.mock
def test_checkout_buyer_page_and_webhook_work_without_login(tmp_path):
    setup_paypal()
    DisputeOnPayPal()
    seller, _ = make_client(tmp_path)  # logged in: sets up the order
    shipped_order(seller)
    link = seller.get("/api/orders/ORDER1/buyer").json()["link"]

    public, _ = make_client(tmp_path, login=False)  # same DB, no session
    assert public.get("/").status_code == 200
    assert public.get("/api/config").status_code == 200
    assert public.get(link).status_code == 200
    assert public.get(f"/api/o{link[2:]}").json()["order_ref"] == "ORDER1"[-6:]
    assert open_dispute(public).json() == {"status": "accepted"}  # webhook: no session needed


def test_wrong_password_rejected_and_rate_limited(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    # Locked out now, even with the right password, until the window passes.
    assert client.post("/api/login", json={"password": "test-pass"}).status_code == 429
    assert client.get("/api/orders").status_code == 401


def test_login_cookie_is_httponly_lax_and_logout_ends_session(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    resp = client.post("/api/login", json={"password": "test-pass"})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie  # http
    assert client.get("/api/orders").status_code == 200
    stolen = client.cookies.get("clearline_seller")

    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/orders").status_code == 401
    client.cookies.set("clearline_seller", stolen)  # an old copy of the cookie is dead too
    assert client.get("/api/orders").status_code == 401


def test_cookie_is_secure_over_https(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    resp = client.post("/api/login", json={"password": "test-pass"},
                       headers={"X-Forwarded-Proto": "https"})  # what ngrok sends
    assert "secure" in resp.headers["set-cookie"].lower()


def test_forged_session_cookie_rejected():
    auth = SellerAuth("secret", "pw")
    good = auth.new_session()
    session_id, sig = good.rsplit(".", 1)
    assert auth.valid(good)
    assert not auth.valid(f"{session_id}.{'0' * len(sig)}")
    assert not auth.valid(SellerAuth("other-secret", "pw").new_session())
    assert not auth.valid("garbage") and not auth.valid(None)
    assert auth.check_password("pw") and not auth.check_password("pw ") and not auth.check_password("")


def test_missing_seller_password_locks_seller_side(tmp_path):
    client, _ = make_client(tmp_path, seller_password="")
    page = client.get("/seller")
    assert page.status_code == 503 and "SELLER_PASSWORD" in page.text
    api = client.get("/api/orders")
    assert api.status_code == 503 and "SELLER_PASSWORD" in api.json()["detail"]
    assert client.post("/api/login", json={"password": ""}).status_code == 503
    assert client.get("/").status_code == 200  # checkout still works
