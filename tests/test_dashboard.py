"""Day 7: the money in flight dashboard. Numbers must match the per-order logic the seller page
uses, the payload must carry no PII, and the demo seeder must never touch the real database."""

import json
import re
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.dashboard import build
from app.main import CATALOGUE
from app.policy import TRIGGER
from app.seed_demo import MARKER, main as seed_main, seed
from app.store import TimelineStore
from tests.support import make_client
from tests.test_seller_page import RAW_CODE, visible_text

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
ITEMS = {k: v["description"] for k, v in CATALOGUE.items()}
EMAIL = "jane.buyer@example.com"
CLIENT = "Jane Client Ltd"
NOTE = "Call me on 555 0100, I'm Jane"
DAY = 86400


def iso(days_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def order(store, oid, amount="120.00", country="US"):
    store.add(oid, "order_created", {"item_id": "brand-kit", "amount": amount, "currency": "USD"})
    store.add(oid, "payment_captured", {"capture_id": f"CAP-{oid}", "status": "COMPLETED",
                                         "payer_email": EMAIL, "payer_country": country})


def dispute(store, oid, kind, status, stage, update_time, **extra):
    store.add(oid, kind, {"dispute_id": f"PP-D-{oid}", "status": status, "dispute_life_cycle_stage": stage,
                          "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "update_time": update_time,
                          "dispute_amount": {"value": "120.00", "currency_code": "USD"},
                          "buyer_messages": [{"content": NOTE}], **extra})


def invoice(store, iid, number, status, due, amount="900.00"):
    store.add(iid, "invoice_status", {"invoice_id": iid, "number": number, "status": status, "client": CLIENT,
                                      "email": "billing@client.example", "amount": amount, "currency": "USD",
                                      "due_date": due, "items": [{"name": "Logo design"}]})


@pytest.fixture
def mixed(tmp_path):
    """Captured, shipped; partial refund; inquiry and claim disputes (one with rows out of
    order); a resolved dispute; auto-resolved refund; paid, overdue and not-yet-due invoices."""
    client, settings = make_client(tmp_path)
    store = TimelineStore(settings.database_path)
    order(store, "ORDER0000000PLAIN")
    store.add("ORDER0000000PLAIN", "tracking_added", {"carrier": "DHL", "tracking_number": "999"})
    order(store, "ORDER00000PARTIAL", country="GB")
    store.add("ORDER00000PARTIAL", "buyer_request", {"request_id": "R1", "type": "refund", "message": NOTE})
    store.add("ORDER00000PARTIAL", "seller_reply", {"request_id": "R1", "message": "Sorry"})
    store.add("ORDER00000PARTIAL", "refund_sent", {"request_id": "R1", "mode": "partial", "amount": "40.00",
                                                   "currency": "USD", "status": "COMPLETED", "trigger": "seller"})
    order(store, "ORDER00000INQUIRY")
    dispute(store, "ORDER00000INQUIRY", "dispute_created", "WAITING_FOR_SELLER_RESPONSE", "INQUIRY", iso(-1),
            seller_response_due_date=iso(5))
    order(store, "ORDER0000000CLAIM")
    # The newer state (PayPal reviewing) arrives first, the older one second: newest update_time wins.
    dispute(store, "ORDER0000000CLAIM", "dispute_updated", "UNDER_REVIEW", "CHARGEBACK", iso(-1))
    dispute(store, "ORDER0000000CLAIM", "dispute_created", "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", iso(-3),
            seller_response_due_date=iso(2))
    store.add("ORDER0000000CLAIM", "dispute_action_sent", {"dispute_id": "PP-D-ORDER0000000CLAIM", "action": "FIGHT",
                                                           "endpoint": "provide-evidence"})
    order(store, "ORDER000000CLOSED")
    dispute(store, "ORDER000000CLOSED", "dispute_created", "WAITING_FOR_SELLER_RESPONSE", "INQUIRY", iso(-6))
    dispute(store, "ORDER000000CLOSED", "dispute_resolved", "RESOLVED", "INQUIRY", iso(-2),
            outcome="RESOLVED_SELLER_FAVOUR")
    order(store, "ORDER000000AUTOED", amount="60.00")
    store.add("ORDER000000AUTOED", "buyer_request", {"request_id": "R2", "type": "refund", "reason": "not_received"})
    store.add("ORDER000000AUTOED", "refund_sent", {"request_id": "R2", "mode": "partial", "amount": "25.00",
                                                   "currency": "USD", "status": "COMPLETED", "trigger": TRIGGER,
                                                   "rule": "Item not received up to 25.00"})
    store.add("ORDER000000AUTOED", "seller_reply", {"request_id": "R2", "message": "Refunded", "auto": True})
    order(store, "ORDER0000000ASKED")
    store.add("ORDER0000000ASKED", "buyer_request", {"request_id": "R3", "type": "not_arrived", "message": NOTE})
    today = date.today()
    invoice(store, "INV2-PAID", "0001", "SENT", (today + timedelta(days=9)).isoformat())
    invoice(store, "INV2-PAID", "0001", "PAID", (today + timedelta(days=9)).isoformat())
    store.add("INV2-PAID", "invoice_paid", {"invoice_id": "INV2-PAID", "number": "0001", "amount": "900.00",
                                             "currency": "USD", "items": [{"name": "Logo design"}]})
    invoice(store, "INV2-LATE", "0002", "SENT", (today - timedelta(days=3)).isoformat(), amount="300.00")
    invoice(store, "INV2-SOON", "0003", "SENT", (today + timedelta(days=4)).isoformat(), amount="450.00")
    invoice(store, "INV2-DRAFT", "0004", "DRAFT", None)
    return client, store


def rows_by_ref(data):
    return {r["ref"]: r for r in data["rows"]}


def test_dashboard_numbers_match_the_per_order_logic(mixed):
    client, store = mixed
    data = client.get("/api/dashboard").json()
    rows = rows_by_ref(data)
    k = data["kpis"]

    # Refund totals: the same as the seller page's refundable amount for every paid order.
    for o in store.orders():
        if o["order_id"].startswith("ORDER"):
            seller_side = client.get(f"/api/orders/{o['order_id']}/buyer").json()
            r = rows[f"…{o['order_id'][-6:]}"]
            assert f"{float(r['amount']) - float(r['refunded']):.2f}" == seller_side["refundable"]
    assert k["refunded"] == {"USD": "65.00"} and k["captured"] == {"USD": "1680.00"}  # 6 x 120 + 60, plus the 900 paid invoice

    # The counter and the auto-resolved number are the seller page's.
    stats = client.get("/api/stats").json()
    assert k["answered_before_dispute"] == stats["questions_answered_before_dispute"] == 3
    assert k["auto_resolved"] == {"count": stats["auto_resolved"], "amount": {"USD": "25.00"}}

    # Latest dispute state: newest PayPal update_time, like store.latest_dispute_state.
    for oid in ("ORDER00000INQUIRY", "ORDER0000000CLAIM", "ORDER000000CLOSED"):
        latest = store.latest_dispute_state(f"PP-D-{oid}")
        d = rows[f"…{oid[-6:]}"]["dispute"]
        assert d["open"] == (latest["payload"]["status"] != "RESOLVED"), oid
    claim = rows["…0CLAIM"]
    assert claim["state"] == "disputed" and claim["next"] == "PayPal is reviewing" and claim["dispute"]["stage"] == "Claim"
    inquiry = rows["…NQUIRY"]
    due = date.fromisoformat(iso(5)[:10])
    assert inquiry["next"] == f"Respond by {due.day} {due.strftime('%b')}" and inquiry["dispute"]["stage"] == "Inquiry"
    assert rows["…CLOSED"]["dispute"]["outcome"] == "You won" and rows["…CLOSED"]["state"] == "settled"
    assert k["disputes"] == {"total": 3, "responded": 1, "open": 2, "outcomes": {"You won": 1}}
    assert k["disputed_open"] == {"USD": "240.00"} and k["disputes_open"] == 2

    # Invoices: the seller page's display status and days overdue.
    listed = {i["number"]: i for i in client.get("/api/invoices").json()}
    assert rows["Invoice 0002"]["status"] == "Overdue" == listed["0002"]["display_status"]
    assert rows["Invoice 0002"]["overdue_days"] == listed["0002"]["days_overdue"] == 3
    assert rows["Invoice 0002"]["next"] == "Invoice overdue 3 days"
    assert rows["Invoice 0003"]["state"] == "unpaid" and rows["Invoice 0003"]["next"].startswith("Due ")
    assert rows["Invoice 0001"]["state"] == "settled" and "Invoice 0004" not in rows  # drafts aren't in flight
    assert k["invoiced_unpaid"] == {"USD": "750.00"} and k["invoices_unpaid"] == 2
    assert k["overdue"] == {"USD": "300.00"} and k["invoices_overdue"] == 1

    # Every row's next action, in plain words.
    assert rows["…0PLAIN"]["next"] == "Nothing to do" and rows["…0ASKED"]["next"] == "Buyer says it hasn't arrived"
    assert rows["…0ASKED"]["state"] == "held" and rows["…ARTIAL"]["state"] == "settled"
    assert {c["state"]: c["amount"] for c in data["charts"]["by_state"]} == {
        "settled": 1255.0, "held": 120.0, "disputed": 240.0, "unpaid": 750.0}
    assert data["charts"]["funnel"] == [{"step": "Buyer questions", "count": 3},
                                        {"step": "Resolved without a dispute", "count": 3},
                                        {"step": "Disputes", "count": 3}]


def test_dashboard_payload_has_no_pii(mixed):
    client, _ = mixed
    raw = client.get("/api/dashboard").text
    for secret in (EMAIL, CLIENT, "billing@client.example", NOTE, "555 0100", "ORDER0000000PLAIN", "PP-D-ORDER"):
        assert secret not in raw, secret
    assert "…0PLAIN" in raw  # masked ids only


def test_dashboard_needs_the_seller_login(tmp_path):
    client, _ = make_client(tmp_path, login=False)
    assert client.get("/api/dashboard").status_code == 401
    page = client.get("/dashboard", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert client.get("/static/history.js", follow_redirects=False).status_code == 303  # seller-only script
    seller, _ = make_client(tmp_path)
    assert seller.get("/dashboard").status_code == 200
    lib = seller.get("/static/vendor/ag-grid-community-36.2.0.min.js")
    assert lib.status_code == 200 and "max-age" in lib.headers["cache-control"]


def test_dashboard_is_fast_with_hundreds_of_orders(tmp_path):
    db = tmp_path / "many.db"
    seed(db, 400)
    events = TimelineStore(str(db)).all_events()
    start = time.perf_counter()
    data = build(events, item_names=ITEMS)
    assert time.perf_counter() - start < 1.0 and len(data["rows"]) > 400


def test_seed_never_touches_the_default_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    real = tmp_path / "clearline.db"
    real.write_bytes(b"the seller's real timeline")
    with pytest.raises(SystemExit, match="default database"):
        seed_main(["--db", "clearline.db"])
    with pytest.raises(SystemExit, match="default database"):
        seed_main(["--db", str(real)])
    other = tmp_path / "mine.db"
    other.write_bytes(b"not a demo file")
    with pytest.raises(SystemExit, match="wasn't made by the demo seeder"):
        seed_main(["--db", "mine.db"])
    seed_main(["--orders", "12"])  # default output: clearline-demo.db, and again over its own file
    seed_main(["--orders", "12"])
    assert real.read_bytes() == b"the seller's real timeline" and other.read_bytes() == b"not a demo file"
    demo = TimelineStore(str(tmp_path / "clearline-demo.db"))
    assert demo.get_setting(MARKER)["orders"] == 12
    kinds = {e["kind"] for ev in demo.all_events().values() for e in ev}
    assert {"payment_captured", "refund_sent", "dispute_created", "dispute_resolved", "buyer_request",
            "invoice_paid", "invoice_status", "webhook_payment_capture_pending"} <= kinds
    emails = {e["payload"].get("payer_email") for ev in demo.all_events().values() for e in ev} - {None}
    assert emails and all(e.endswith("@example.com") for e in emails)  # clearly fake


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dashboard_page_renders_kpis_rows_and_side_panel(mixed):
    client, _ = mixed
    data = client.get("/api/dashboard").json()
    out = json.loads(subprocess.run([NODE, str(ROOT / "tests/js/dashboard_harness.js"), str(ROOT / "app/static/dashboard.html")],
                                    input=json.dumps(data), capture_output=True, text=True, encoding="utf-8",
                                    timeout=30, check=True).stdout)
    kpis = visible_text(out["kpis"])
    assert "1680.00 USD" in kpis and "1 of 3" in kpis and "Closed: You won 1" in kpis
    rows = {r["ref"]: r for r in out["rows"]}
    assert rows["…0CLAIM"]["disputeText"] == "Claim, open"  # PayPal is reviewing: no due date to show
    assert rows["…CLOSED"]["disputeText"] == "Inquiry, closed: You won"
    assert rows["…0PLAIN"]["last"] == "Shipped with DHL"
    for html in out["drawers"]:
        text = visible_text(html)
        assert not RAW_CODE.search(text), RAW_CODE.findall(text)
        assert '<ol class="cl"' in html and "Open on Orders" in html
        assert not re.search(r"data-(refund|approve|reply|invoice-send)", html)  # read-only
    plain = next(h for h in out["drawers"] if "…0PLAIN" in h)
    assert 'href="/seller#order-0PLAIN"' in plain
    late = next(h for h in out["drawers"] if "Invoice 0002" in h)
    assert 'href="/seller#invoices"' in late and "Invoice overdue 3 days" in late


def test_needs_you_means_the_seller_has_something_to_do(mixed):
    client, store = mixed
    order(store, "ORDER00WAITBUYER")  # a dispute waiting on the buyer doesn't need the seller
    dispute(store, "ORDER00WAITBUYER", "dispute_created", "WAITING_FOR_BUYER_RESPONSE", "INQUIRY", iso(-1))
    rows = rows_by_ref(client.get("/api/dashboard").json())
    due_in_5 = date.fromisoformat(iso(5)[:10]).isoformat()
    expected = {
        "…NQUIRY": (True, due_in_5),             # dispute, the seller's turn: respond-by date
        "…0ASKED": (True, None),                 # a buyer is waiting for a reply
        "…0CLAIM": (False, None),                # PayPal is reviewing
        "…TBUYER": (False, None),                # waiting for the buyer
        "…0PLAIN": (False, None),                # shipped, nothing to do
        "…CLOSED": (True, None),                 # dispute closed, but it was never shipped: Add tracking
        "Invoice 0002": (True, (date.today() - timedelta(days=3)).isoformat()),   # overdue
        "Invoice 0003": (False, (date.today() + timedelta(days=4)).isoformat()),  # sent, not due yet
        "Invoice 0001": (False, None),           # paid
    }
    assert {ref: (rows[ref]["needs_you"], rows[ref]["deadline"]) for ref in expected} == expected


def test_a_refund_answers_a_buyer_request_too(mixed):
    client, store = mixed
    order(store, "ORDER0000REFUNDED")  # shipped, asked for a refund, got it, no written reply
    store.add("ORDER0000REFUNDED", "tracking_added", {"carrier": "DHL", "tracking_number": "1"})
    store.add("ORDER0000REFUNDED", "buyer_request", {"request_id": "R9", "type": "refund", "reason": "not_received"})
    store.add("ORDER0000REFUNDED", "refund_sent", {"request_id": "R9", "mode": "partial", "amount": "20.00",
                                                   "currency": "USD", "status": "COMPLETED", "trigger": "seller"})
    order(store, "ORDER000STILLOPEN")  # asked, the auto policy said no: still the seller's turn
    store.add("ORDER000STILLOPEN", "buyer_request", {"request_id": "R10", "type": "refund", "reason": "changed_mind"})
    store.add("ORDER000STILLOPEN", "auto_policy_skipped", {"request_id": "R10", "reason": "not allowed"})
    rows = rows_by_ref(client.get("/api/dashboard").json())
    assert rows["…FUNDED"]["needs_you"] is False and rows["…FUNDED"]["next"] != "Buyer asked for a refund"
    assert rows["…LLOPEN"]["needs_you"] is True and rows["…LLOPEN"]["next"] == "Buyer asked for a refund"
    assert rows["…LLOPEN"]["deadline"] is None  # no deadline, still needs the seller


def test_act_by_is_the_date_to_filter_needs_me_questions_on(tmp_path):
    """The live case: a dispute due this week, an open buyer request with no deadline, a
    refunded request and a dispute waiting for the buyer."""
    client, settings = make_client(tmp_path)
    store = TimelineStore(settings.database_path)
    today = datetime.now(timezone.utc).date()
    end_of_week = today + timedelta(days=6 - today.weekday())
    order(store, "ORDER0000053815W")
    dispute(store, "ORDER0000053815W", "dispute_created", "WAITING_FOR_SELLER_RESPONSE", "CHARGEBACK", iso(-1),
            seller_response_due_date=f"{end_of_week.isoformat()}T12:00:00.000Z")
    order(store, "ORDER00000551845")  # refund request, the auto policy said no: open
    store.add("ORDER00000551845", "buyer_request", {"request_id": "R1", "type": "refund", "reason": "changed_mind"})
    store.add("ORDER00000551845", "auto_policy_skipped", {"request_id": "R1", "reason": "not allowed"})
    order(store, "ORDER0000040493N")  # shipped, asked for a refund and got it: answered
    store.add("ORDER0000040493N", "tracking_added", {"carrier": "DHL", "tracking_number": "1"})
    store.add("ORDER0000040493N", "buyer_request", {"request_id": "R2", "type": "refund", "reason": "not_received"})
    store.add("ORDER0000040493N", "refund_sent", {"request_id": "R2", "mode": "partial", "amount": "20.00",
                                                  "currency": "USD", "status": "COMPLETED", "trigger": "seller"})
    order(store, "ORDER00WAITBUYER")  # shipped, dispute waiting on the buyer
    store.add("ORDER00WAITBUYER", "tracking_added", {"carrier": "DHL", "tracking_number": "2"})
    dispute(store, "ORDER00WAITBUYER", "dispute_created", "WAITING_FOR_BUYER_RESPONSE", "INQUIRY", iso(-1))

    rows = rows_by_ref(client.get("/api/dashboard").json())
    assert {ref: (rows[ref]["act_by"], rows[ref]["deadline"]) for ref in ("…53815W", "…551845", "…40493N", "…TBUYER")} == {
        "…53815W": (end_of_week.isoformat(), end_of_week.isoformat()),  # the respond-by date
        "…551845": (today.isoformat(), None),                            # waiting on the seller now
        "…40493N": (None, None),                                         # answered by the refund
        "…TBUYER": (None, None),                                         # the buyer's turn
    }
    # The filter a model writes for "this week" returns exactly the two that need the seller.
    this_week = sorted(ref for ref, r in rows.items() if r["act_by"] and r["act_by"] <= end_of_week.isoformat())
    assert this_week == ["…53815W", "…551845"]
