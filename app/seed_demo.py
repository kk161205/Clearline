"""Fill a SEPARATE demo database with clearly fake, sandbox-style orders and invoices.

    python -m app.seed_demo --orders 40                # writes clearline-demo.db
    python -m app.seed_demo --orders 80 --db demo2.db --seed 3

Then run the app on it with DATABASE_PATH=clearline-demo.db (see README, "Demo data").
It refuses to write to the app's default database (clearline.db) or to any existing file it
didn't create itself, so the real timeline can't be touched. Nothing here calls PayPal.
"""

import argparse
import json
import random
import secrets
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .accounts import Accounts, password_problem
from .config import Settings
from .main import CATALOGUE
from .policy import TRIGGER
from .store import TimelineStore

DEFAULT_OUT = "clearline-demo.db"
MARKER = "demo_database"
DEMO_EMAIL = "demo-seller@example.com"  # a reserved, clearly fake domain  # app_settings key: this file was made by the seeder
DAY = 86400
COUNTRIES = ["US", "GB", "DE", "CA", "AU", "FR", "NL", "SG", "AE", "JP", "IE", "SE"]
CARRIERS = ["DHL", "FEDEX", "UPS", "USPS"]
REASONS = ["MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED",
           "UNAUTHORISED", "DUPLICATE_TRANSACTION"]
CLIENTS = ["Demo Client Ltd", "Example Studio", "Sample Co", "Test Agency GmbH", "Placeholder Inc"]
SERVICES = ["Logo design", "Brand workshop", "Packaging design", "Website illustrations", "Social media kit"]
# (scenario, weight): every money state the dashboard shows.
SCENARIOS = [("settled", 30), ("unshipped", 8), ("asked_open", 9), ("asked_answered", 6),
             ("partial_refund", 7), ("full_refund", 3), ("auto_refund", 6), ("pending", 4),
             ("inquiry_seller", 5), ("claim_seller", 4), ("under_review", 3), ("waiting_buyer", 2),
             ("won", 3), ("lost", 2)]


def default_db() -> Path:
    """The app's default database file, from the Settings default (never reads .env)."""
    return Path(Settings.model_fields["database_path"].default).resolve()


def check_target(path: Path) -> None:
    if path.resolve() == default_db():
        raise SystemExit(f"Refusing to write demo data to the app's default database ({default_db().name}). "
                         f"Pick another file with --db.")
    if path.exists():
        try:
            with closing(sqlite3.connect(path)) as conn:
                made_here = conn.execute("SELECT 1 FROM app_settings WHERE key = ?", (MARKER,)).fetchone()
        except sqlite3.Error:
            made_here = None
        if not made_here:
            raise SystemExit(f"{path} exists and wasn't made by the demo seeder; not touching it.")


def fake_id(rng: random.Random, n: int = 17) -> str:
    return "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(n))


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_rows(n_orders: int, rng: random.Random, now: float) -> list[tuple]:
    rows: list[tuple] = []

    def add(order: str, kind: str, ts: float, **payload):
        rows.append((order, kind, json.dumps(payload), min(ts, now - 60)))

    names = list(CATALOGUE)
    for i in range(n_orders):
        scenario = rng.choices([s for s, _ in SCENARIOS], [w for _, w in SCENARIOS])[0]
        oid = fake_id(rng)
        item = rng.choice(names)
        price = CATALOGUE[item]["amount"]
        t = now - rng.uniform(0.2, 28) * DAY
        country = rng.choice(COUNTRIES)
        add(oid, "order_created", t, item_id=item, amount=price, currency="USD", status="CREATED")
        add(oid, "payment_captured", t + 40, capture_id=f"CAP-{fake_id(rng, 12)}", status="COMPLETED",
            payer_email=f"sandbox-buyer{i + 1:02d}@example.com", payer_country=country)
        if scenario == "pending":
            add(oid, "webhook_payment_capture_pending", t + 60, status="PENDING", pending_reason="PENDING_REVIEW",
                amount={"value": price, "currency_code": "USD"})
            continue
        add(oid, "webhook_payment_capture_completed", t + 90, status="COMPLETED", amount={"value": price, "currency_code": "USD"})
        shipped = scenario not in ("unshipped", "auto_refund", "full_refund") and rng.random() > 0.05
        ship_t = t + rng.uniform(0.3, 2) * DAY
        if shipped:
            add(oid, "tracking_added", ship_t, carrier=rng.choice(CARRIERS), tracking_number=str(rng.randint(10**11, 10**12 - 1)))
        rid = f"req-{fake_id(rng, 8).lower()}"
        ask_t = min(ship_t + rng.uniform(0.5, 4) * DAY, now - 3600)
        if scenario == "asked_open":
            add(oid, "buyer_request", ask_t, request_id=rid, type=rng.choice(["not_arrived", "unrecognised", "refund", "message"]),
                via=rng.choice(["page", "page", "agent"]), message="Demo message from a sandbox buyer.")
        elif scenario == "asked_answered":
            add(oid, "buyer_request", ask_t, request_id=rid, type=rng.choice(["not_arrived", "unrecognised"]), via="page",
                message="Demo question from a sandbox buyer.")
            add(oid, "seller_reply", ask_t + 3 * 3600, request_id=rid, message="Demo reply: it's on its way.")
        elif scenario in ("partial_refund", "full_refund"):
            amount = price if scenario == "full_refund" else f"{float(price) * rng.choice([0.25, 0.33, 0.5]):.2f}"
            add(oid, "buyer_request", ask_t, request_id=rid, type="refund", reason="damaged", via="page",
                message="Demo: one item arrived damaged.")
            add(oid, "seller_reply", ask_t + 2 * 3600, request_id=rid, message="Demo reply: sorry, refunding now.")
            add(oid, "refund_sent", ask_t + 2 * 3600 + 30, request_id=rid, mode="full" if scenario == "full_refund" else "partial",
                amount=amount, currency="USD", status="COMPLETED", trigger="seller")
        elif scenario == "auto_refund":
            amount = f"{min(float(price), 40.0):.2f}"
            add(oid, "buyer_request", ask_t, request_id=rid, type="refund", reason="not_received",
                via=rng.choice(["page", "agent"]), message="Demo: still waiting.", amount=amount)
            add(oid, "refund_sent", ask_t + 5, request_id=rid, mode="partial" if amount != price else "full",
                amount=amount, currency="USD", status="COMPLETED", trigger=TRIGGER,
                rule="Item not received, up to 40.00 USD per order")
            add(oid, "seller_reply", ask_t + 6, request_id=rid, auto=True,
                message=f"Demo: a refund of {amount} USD was sent automatically.")
        elif scenario in ("inquiry_seller", "claim_seller", "under_review", "waiting_buyer", "won", "lost"):
            did = f"PP-D-{rng.randint(10000, 99999)}"
            open_t = min(t + rng.uniform(3, 12) * DAY, now - 2 * 3600)
            if rng.random() < 0.5:  # some buyers asked first and opened a dispute anyway
                add(oid, "buyer_request", open_t - DAY, request_id=rid, type="not_arrived", via="page",
                    message="Demo: where is my order?")
            stage = "CHARGEBACK" if scenario in ("claim_seller", "under_review", "lost") else "INQUIRY"
            reason = rng.choice(REASONS)
            due = iso(open_t + rng.uniform(2, 12) * DAY) if scenario in ("inquiry_seller", "claim_seller") else None
            status = {"inquiry_seller": "WAITING_FOR_SELLER_RESPONSE", "claim_seller": "WAITING_FOR_SELLER_RESPONSE",
                      "under_review": "UNDER_REVIEW", "waiting_buyer": "WAITING_FOR_BUYER_RESPONSE"}.get(scenario, "WAITING_FOR_SELLER_RESPONSE")
            base = {"dispute_id": did, "reason": reason, "dispute_life_cycle_stage": stage,
                    "dispute_amount": {"value": price, "currency_code": "USD"}}
            add(oid, "dispute_created", open_t, **base, status=status, update_time=iso(open_t),
                seller_response_due_date=due, allowed_decisions=["FIGHT", "REFUND"] if status == "WAITING_FOR_SELLER_RESPONSE" else [])
            add(oid, "ai_recommendation", open_t + 30, dispute_id=did, provider="gemini", model="demo-model", latency_ms=1500,
                recommendation={"decision": "FIGHT" if shipped else "REFUND", "confidence": round(rng.uniform(0.6, 0.95), 2),
                                "reasons": [{"text": "Demo reason.", "fact_keys": ["tracking.number"]}],
                                "missing_evidence": [], "message_to_buyer": "Demo.", "note_to_paypal": "Demo.", "offer": None})
            if scenario in ("under_review", "waiting_buyer", "won", "lost"):
                add(oid, "dispute_action_sent", open_t + 3600, dispute_id=did, action="FIGHT", endpoint="provide-evidence", run_id=fake_id(rng, 6))
            if scenario in ("won", "lost"):
                close_t = min(open_t + rng.uniform(2, 6) * DAY, now - 3600)
                add(oid, "dispute_resolved", close_t, **base, status="RESOLVED", update_time=iso(close_t),
                    outcome="RESOLVED_SELLER_FAVOUR" if scenario == "won" else "RESOLVED_BUYER_FAVOUR")

    # Invoices (services, no shipping): paid, sent and not yet due, overdue, and a draft.
    for i in range(max(3, n_orders // 6)):
        inv = f"INV2-{fake_id(rng, 4)}-{fake_id(rng, 4)}-{fake_id(rng, 4)}"
        number = f"{100 + i:04d}"
        amount = f"{rng.choice([350, 480, 650, 900, 1200, 1500]):.2f}"
        kind = rng.choices(["paid", "sent", "overdue", "draft"], [4, 2, 3, 1])[0]
        t = now - rng.uniform(2, 40) * DAY
        due_days = rng.randint(7, 21)
        due = datetime.fromtimestamp(t + due_days * DAY, timezone.utc).date()
        if kind == "overdue":
            due = datetime.fromtimestamp(now - rng.randint(2, 12) * DAY, timezone.utc).date()
        elif kind == "sent" and due <= datetime.fromtimestamp(now, timezone.utc).date():
            due = datetime.fromtimestamp(now + rng.randint(3, 14) * DAY, timezone.utc).date()
        summary = {"invoice_id": inv, "number": number, "client": rng.choice(CLIENTS),
                   "email": f"billing{i + 1:02d}@example.com", "amount": amount, "currency": "USD",
                   "due_date": due.isoformat(), "items": [{"name": rng.choice(SERVICES), "quantity": "1", "unit_amount": amount}],
                   "recipient_country": rng.choice(COUNTRIES), "paid_on": None}
        add(inv, "invoice_status", t, **summary, status="DRAFT", source="seller")
        if kind == "draft":
            continue
        add(inv, "invoice_status", t + 600, **summary, status="SENT", source="seller")
        add(inv, "invoice_send_sent", t + 600, invoice_id=inv)
        if kind == "overdue" and rng.random() < 0.5:
            add(inv, "invoice_reminder_sent", now - DAY, subject="Demo: friendly reminder")
        if kind == "paid":
            paid_t = min(t + rng.uniform(1, 10) * DAY, now - 3600)
            add(inv, "invoice_status", paid_t, **summary, status="PAID", source="webhook")
            add(inv, "invoice_paid", paid_t, **{k: summary[k] for k in ("invoice_id", "number", "amount", "currency", "items",
                                                                         "recipient_country")}, paid_on=None)
    return rows


def seed(path: Path, n_orders: int, seed_value: int = 7, now: float | None = None) -> dict:
    """Write the demo database: {"events": count, "email": ..., "password": ...}. The demo
    seller's password is random every time and exists only in this return value."""
    check_target(path)
    path.unlink(missing_ok=True)
    store = TimelineStore(str(path))
    store.set_setting(MARKER, {"made_by": "app.seed_demo", "orders": n_orders})
    rows = build_rows(n_orders, random.Random(seed_value), now or time.time())
    rows.sort(key=lambda r: r[3])  # insert in time order, like real events
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany("INSERT INTO events (order_id, kind, payload, created_at) VALUES (?, ?, ?, ?)", rows)
    password = secrets.token_urlsafe(18)
    while password_problem(password, DEMO_EMAIL):
        password = secrets.token_urlsafe(18)
    Accounts(store).create_user(DEMO_EMAIL, password)
    return {"events": len(rows), "email": DEMO_EMAIL, "password": password}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.seed_demo", description=__doc__.split("\n\n")[0])
    ap.add_argument("--orders", type=int, default=40, help="how many checkout orders (default 40)")
    ap.add_argument("--db", default=DEFAULT_OUT, help=f"demo database file (default {DEFAULT_OUT})")
    ap.add_argument("--seed", type=int, default=7, help="random seed, for the same data every time")
    args = ap.parse_args(argv)
    if not 1 <= args.orders <= 5000:
        raise SystemExit("--orders must be between 1 and 5000")
    path = Path(args.db)
    result = seed(path, args.orders, args.seed)
    print(f"Wrote {result['events']} demo events ({args.orders} orders plus invoices) to {path.resolve()}")
    print(f"Run the app on it with DATABASE_PATH={args.db}")
    # Shown once: it isn't stored anywhere but as a hash in the demo database.
    print(f"Demo seller login: {result['email']} / {result['password']}")


if __name__ == "__main__":
    main(sys.argv[1:])
