"""GET /api/seller/summary: /seller's one request per refresh, and its 304 when nothing changed."""

from app.store import TimelineStore
from tests.support import make_client


def shop(tmp_path):
    client, settings = make_client(tmp_path)
    store = TimelineStore(settings.database_target)
    store.add("ORDER1", "order_created", {"item_id": "brand-kit", "amount": "120.00", "currency": "USD"})
    store.add("ORDER1", "payment_captured", {"capture_id": "CAP1", "status": "COMPLETED"})
    store.add("ORDER1", "refund_sent", {"amount": "20.00", "currency": "USD", "status": "COMPLETED"})
    store.add("ORDER1", "ai_recommendation", {"dispute_id": "D1", "recommendation": {"decision": "FIGHT"}})
    store.add("INV2-1", "invoice_status", {"invoice_id": "INV2-1", "number": "0007", "status": "SENT",
                                           "amount": "50.00", "currency": "USD", "due_date": "2026-01-01"})
    store.add("other", "auto_policy_updated", {"enabled": False})
    return client, store


def test_one_answer_has_what_the_list_and_today_need(tmp_path):
    client, _ = shop(tmp_path)
    body = client.get("/api/seller/summary").json()
    [order] = body["orders"]  # an unpaid invoice isn't an order yet
    assert order["order_id"] == "ORDER1" and order["last_id"] > 0
    assert order["buyer"] == {"refundable": "100.00", "currency": "USD"}
    assert [e["kind"] for e in order["events"]] == ["order_created", "payment_captured", "refund_sent"]
    assert [i["number"] for i in body["invoices"]] == ["0007"] and body["invoices"][0]["display_status"] == "Overdue"
    assert body["side"] == []  # its only event is an app-wide setting change
    assert set(body["stats"]) == {"questions_answered_before_dispute", "auto_resolved"}
    assert "auto_sync_minutes" in body["sync"]


def test_an_unchanged_shop_answers_304_with_no_body(tmp_path):
    client, _ = shop(tmp_path)
    first = client.get("/api/seller/summary")
    tag = first.headers["etag"]
    again = client.get("/api/seller/summary", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == tag
    assert first.headers["cache-control"] == "no-cache"


def test_a_weak_tag_from_a_compressing_proxy_still_matches(tmp_path):
    client, _ = shop(tmp_path)
    tag = client.get("/api/seller/summary").headers["etag"]
    assert client.get("/api/seller/summary", headers={"If-None-Match": f"W/{tag}"}).status_code == 304
    assert client.get("/api/seller/summary", headers={"If-None-Match": f'"other", W/{tag}'}).status_code == 304
    assert client.get("/api/seller/summary", headers={"If-None-Match": 'W/"other"'}).status_code == 200


def test_any_new_event_changes_the_tag(tmp_path):
    client, store = shop(tmp_path)
    tag = client.get("/api/seller/summary").headers["etag"]
    store.add("ORDER1", "tracking_added", {"carrier": "DHL", "tracking_number": "1"})
    res = client.get("/api/seller/summary", headers={"If-None-Match": tag})
    assert res.status_code == 200 and res.headers["etag"] != tag
    assert "tracking_added" in [e["kind"] for e in res.json()["orders"][0]["events"]]


def test_a_new_day_changes_the_tag(tmp_path, monkeypatch):
    import time_machine
    from datetime import datetime, timedelta, timezone
    client, _ = shop(tmp_path)
    tag = client.get("/api/seller/summary").headers["etag"]
    with time_machine.travel(datetime.now(timezone.utc) + timedelta(days=1)):
        assert client.get("/api/seller/summary", headers={"If-None-Match": tag}).status_code == 200


def test_the_order_list_follows_the_newest_activity(tmp_path):
    client, store = shop(tmp_path)
    store.add("ORDER2", "payment_captured", {"capture_id": "CAP2"})
    assert [o["order_id"] for o in client.get("/api/seller/summary").json()["orders"]] == ["ORDER2", "ORDER1"]
    store.add("ORDER1", "seller_reply", {"request_id": "R1", "message": "hi"})
    assert [o["order_id"] for o in client.get("/api/seller/summary").json()["orders"]] == ["ORDER1", "ORDER2"]


def test_the_summary_needs_a_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get("/api/seller/summary").status_code == 401


# ---- Archiving (the demo reset) ---------------------------------------------------------------

def test_archive_hides_everything_but_what_it_keeps(tmp_path):
    client, store = shop(tmp_path)
    store.add("ORDER2", "payment_captured", {"capture_id": "CAP2"})
    assert client.post("/api/archive", json={"keep": ["ORDER2"]}).json() == {"archived": 2}  # ORDER1, INV2-1
    body = client.get("/api/seller/summary").json()
    assert [o["order_id"] for o in body["orders"]] == ["ORDER2"] and body["invoices"] == []
    assert body["side"] == []  # app-wide setting changes never show there
    assert [r["ref"] for r in client.get("/api/dashboard").json()["rows"]] == ["…ORDER2"]
    assert client.get("/api/invoices").json() == []
    assert "archived" in [e["kind"] for e in store.timeline("ORDER1")]  # nothing deleted
    assert client.post("/api/archive", json={"keep": ["ORDER2"]}).json() == {"archived": 0}  # again: nothing


def test_archive_only_some(tmp_path):
    client, store = shop(tmp_path)
    store.add("ORDER2", "payment_captured", {"capture_id": "CAP2"})
    assert client.post("/api/archive", json={"only": ["INV2-1"]}).json() == {"archived": 1}
    body = client.get("/api/seller/summary").json()
    assert body["invoices"] == [] and {o["order_id"] for o in body["orders"]} == {"ORDER1", "ORDER2"}


def test_include_archived_shows_them_again_marked(tmp_path):
    client, _ = shop(tmp_path)
    client.post("/api/archive", json={})
    plain = client.get("/api/seller/summary")
    shown = client.get("/api/seller/summary?include_archived=true")
    assert plain.json()["orders"] == [] and plain.headers["etag"] != shown.headers["etag"]
    [order] = shown.json()["orders"]
    assert order["archived"] is True and shown.json()["include_archived"] is True


def test_a_demo_account_cant_archive_or_cancel(tmp_path):
    client, store = shop(tmp_path)
    store.update_user(store.user_by_email("seller@example.com")["id"], demo=1)
    assert client.post("/api/archive", json={}).status_code == 403
    assert client.post("/api/invoices/INV2-1/cancel").status_code == 403
    assert len(client.get("/api/seller/summary").json()["orders"]) == 1


# ---- The side panel and the header numbers ------------------------------------------------------

def test_the_side_panel_has_recent_paypal_events_only(tmp_path):
    import time
    client, store = shop(tmp_path)
    store.add("other", "ai_provider_switched", {"from": "gemini", "to": "groq", "reason": "timeout"})
    store.add("unmatched:D9", "dispute_created", {"dispute_id": "D9", "status": "OPEN"})
    side = {s["order_id"]: [e["kind"] for e in s["events"]] for s in client.get("/api/seller/summary").json()["side"]}
    assert side == {"unmatched:D9": ["dispute_created"]}  # the policy change and the AI switch aren't here
    with store.db.connect() as conn:
        conn.execute("UPDATE events SET created_at = ?", (time.time() - 8 * 86400,))
    assert client.get("/api/seller/summary").json()["side"] == []


def test_header_numbers_leave_archived_orders_out_like_the_dashboard(tmp_path):
    client, store = shop(tmp_path)
    store.add("ORDER2", "payment_captured", {"capture_id": "CAP2"})
    store.add("ORDER2", "buyer_request", {"request_id": "R1", "type": "refund"})
    store.add("ORDER2", "refund_sent", {"request_id": "R1", "amount": "5.00", "trigger": "auto_policy"})
    assert client.get("/api/stats").json()["questions_answered_before_dispute"] == 1
    client.post("/api/archive", json={})
    stats = client.get("/api/stats").json()
    kpis = client.get("/api/dashboard").json()["kpis"]
    assert stats == {"questions_answered_before_dispute": 0, "auto_resolved": 0}
    assert kpis["answered_before_dispute"] == 0 and kpis["auto_resolved"]["count"] == 0
