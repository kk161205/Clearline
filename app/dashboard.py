"""Money in flight (Day 7): one read-only summary of every order and invoice for /dashboard.

Computed from the stored timelines in one pass, with the same rules the seller page uses
(latest dispute state by PayPal's update_time, refund totals, the "answered before a dispute"
counter, invoice display status). No PII leaves this module: ids are masked, there are no
emails, names, addresses or free-text messages, and history rows keep only whitelisted fields.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .buyer import _money, count_answered, refund_totals
from .invoicing import days_overdue, display_status
from .policy import TRIGGER
from .store import DISPUTE_KINDS, latest_state

STATES = ("settled", "held", "disputed", "unpaid")
STATE_LABELS = {"settled": "Settled", "held": "Waiting", "disputed": "Disputed", "unpaid": "Unpaid"}
REASONS = {
    "MERCHANDISE_OR_SERVICE_NOT_RECEIVED": "Item not received",
    "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED": "Item not as described",
    "UNAUTHORISED": "Unauthorised payment", "CREDIT_NOT_PROCESSED": "Refund not processed",
    "DUPLICATE_TRANSACTION": "Charged twice", "INCORRECT_AMOUNT": "Wrong amount charged",
    "PAYMENT_BY_OTHER_MEANS": "Paid by other means",
    "CANCELED_RECURRING_BILLING": "Cancelled subscription still billed",
    "PROBLEM_WITH_REMITTANCE": "Problem with remittance", "OTHER": "Other",
}
STAGES = {"INQUIRY": "Inquiry", "CHARGEBACK": "Claim", "PRE_ARBITRATION": "Pre-arbitration",
          "ARBITRATION": "Arbitration"}
OUTCOMES = {"RESOLVED_SELLER_FAVOUR": "You won", "RESOLVED_BUYER_FAVOUR": "Buyer won",
            "RESOLVED_WITH_PAYOUT": "PayPal covered it", "CANCELED_BY_BUYER": "Buyer cancelled",
            "ACCEPTED": "You accepted", "DENIED": "Denied"}
ASKS = {"refund": "Buyer asked for a refund", "not_arrived": "Buyer says it hasn't arrived",
        "unrecognised": "Buyer doesn't recognise the charge", "message": "Buyer sent a message"}

# History fields the dashboard may show (no emails, names, messages or buyer text).
SAFE_FIELDS = {"amount", "currency", "status", "mode", "trigger", "rule", "carrier", "reason",
               "dispute_life_cycle_stage", "dispute_amount", "seller_response_due_date",
               "update_time", "request_id", "type", "via", "endpoint", "action", "run_id",
               "pending_reason", "event_type", "number", "due_date", "auto", "enabled",
               "max_refund_per_order", "daily_cap", "provider", "outcome", "from", "to",
               "payer_country", "error", "paypal_issue", "paypal_status", "debug_id"}


def mask(ref: str) -> str:
    """"…38815W": enough to recognise an order, not the full PayPal id."""
    return f"…{ref[-6:]}"


def short_day(d: date, today: date) -> str:
    """"30 Oct" this year, "30 Oct 2025" otherwise (day first, like the pages)."""
    return f"{d.day} {d.strftime('%b')}" + ("" if d.year == today.year else f" {d.year}")


def _date(ts: float) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def _iso_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def safe_history(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        p = e["payload"]
        safe = {k: v for k, v in p.items() if k in SAFE_FIELDS}
        if e["kind"] == "ai_recommendation":
            r = p.get("recommendation") or {}
            safe["recommendation"] = {"decision": r.get("decision"), "confidence": r.get("confidence")}
        if "dispute_id" in p:
            safe["dispute_id"] = mask(str(p["dispute_id"]))
        out.append({"kind": e["kind"], "created_at": e["created_at"], "payload": safe})
    return out


def _amount(value) -> str:
    return f"{_money(value):.2f}"


def order_row(ref: str, events: list[dict], today: date, item_names: dict) -> dict | None:
    """One money item for a paid PayPal checkout order (None if it never got paid)."""
    kinds = {e["kind"] for e in events}
    captured_evt = next((e for e in events if e["kind"] == "payment_captured"), None)
    if not captured_evt:
        return None
    created = next((e for e in events if e["kind"] == "order_created"), {"payload": {}})
    captured, refunded, currency = refund_totals(events)
    states = [e for e in events if e["kind"] in DISPUTE_KINDS]
    dispute = latest_state(states)
    open_dispute = bool(dispute) and dispute["kind"] != "dispute_resolved" \
        and dispute["payload"].get("status") != "RESOLVED"
    # A buyer request is open until the seller replies to it or refunds it.
    handled = {e["payload"].get("request_id") for e in events
               if e["kind"] in ("seller_reply", "refund_sent") and e["payload"].get("request_id")}
    asks = [e for e in events if e["kind"] == "buyer_request" and e["payload"].get("request_id") not in handled]
    last_pending = max((e["created_at"] for e in events if e["kind"] == "webhook_payment_capture_pending"), default=None)
    last_done = max((e["created_at"] for e in events if e["kind"] == "webhook_payment_capture_completed"), default=None)
    pending = last_pending is not None and (last_done is None or last_done < last_pending)
    reversed_ = "webhook_payment_capture_reversed" in kinds

    # Same order as the seller page's line colour: disputed, then waiting, then settled.
    if open_dispute or reversed_:
        state = "disputed"
    elif pending or asks:
        state = "held"
    else:
        state = "settled"

    # needs_you: the seller has something to do. Waiting on the buyer or on PayPal doesn't count.
    deadline, needs_you = None, True
    if open_dispute:
        p = dispute["payload"]
        due = _iso_day(p.get("seller_response_due_date"))
        status = p.get("status")
        if status == "WAITING_FOR_SELLER_RESPONSE":
            deadline = due
            nxt = (f"Respond by {short_day(due, today)}" if due and due >= today
                   else "Response overdue" if due else "Respond to the dispute")
        elif status == "WAITING_FOR_BUYER_RESPONSE":
            nxt, needs_you = "Waiting for the buyer", False
        else:
            nxt, needs_you = "PayPal is reviewing", False
    elif asks:
        nxt = ASKS.get(asks[-1]["payload"].get("type"), "Buyer sent a request")
    elif pending:
        nxt, needs_you = "PayPal is holding the payment", False
    elif "tracking_added" not in kinds and refunded < captured:
        nxt = "Add tracking"
    else:
        nxt, needs_you = "Nothing to do", False

    item_id = created["payload"].get("item_id")
    dp = dispute["payload"] if dispute else {}
    return {
        "kind": "order", "ref": mask(ref), "date": _date(captured_evt["created_at"]).isoformat(),
        "item": item_names.get(item_id, item_id or "Order"),
        "country": captured_evt["payload"].get("payer_country"),
        "amount": f"{captured:.2f}", "refunded": f"{refunded:.2f}", "currency": currency,
        "state": state, "status": STATE_LABELS[state],
        "dispute": ({"stage": STAGES.get(dp.get("dispute_life_cycle_stage"), "Dispute"),
                     "reason": REASONS.get(dp.get("reason"), "Other"),
                     "open": open_dispute,
                     "due": dp.get("seller_response_due_date") if open_dispute else None,
                     "amount": _amount((dp.get("dispute_amount") or {}).get("value")),
                     "outcome": None if open_dispute else OUTCOMES.get(dp.get("outcome"), "Closed, outcome not recorded"),
                     "responded": any(e["kind"] == "dispute_action_sent" for e in events)}
                    if dispute else None),
        "next": nxt, "needs_you": needs_you, "deadline": deadline.isoformat() if deadline else None,
        "auto_refunds": [_amount(e["payload"].get("amount")) for e in events
                         if e["kind"] == "refund_sent" and e["payload"].get("trigger") == TRIGGER],
        "history": safe_history(events),
    }


def invoice_row(ref: str, events: list[dict], today: date) -> dict | None:
    """One money item for an invoice that was sent (paid or not); drafts and cancelled ones
    aren't money in flight."""
    statuses = [e for e in events if e["kind"] == "invoice_status"]
    if not statuses:
        return None
    s = statuses[-1]["payload"]
    shown = display_status(s, today)
    paid = next((e for e in events if e["kind"] == "invoice_paid"), None)
    if not paid and shown in ("Draft", "Cancelled"):
        return None
    _, refunded, _ = refund_totals(events)
    overdue = days_overdue(s, today)
    due = _iso_day(s.get("due_date"))
    if paid or shown in ("Paid", "Refunded"):
        state, nxt = "settled", "Nothing to do"
    else:
        state = "unpaid"
        nxt = (f"Invoice overdue {overdue} day{'s' if overdue != 1 else ''}" if overdue
               else f"Due {short_day(due, today)}" if due else "Waiting for payment")
    items = [i.get("name") for i in s.get("items") or [] if i.get("name")]
    when = paid["created_at"] if paid else statuses[0]["created_at"]
    return {
        "kind": "invoice", "ref": f"Invoice {s.get('number')}" if s.get("number") else mask(ref),
        "date": _date(when).isoformat(), "item": ", ".join(items) or "Invoice",
        "country": s.get("recipient_country"),
        "amount": _amount(s.get("amount")), "refunded": f"{refunded:.2f}", "currency": s.get("currency") or "",
        "state": state, "status": STATE_LABELS[state] if state != "unpaid" or not overdue else "Overdue",
        "dispute": None, "next": nxt, "overdue_days": overdue, "auto_refunds": [],
        # An invoice needs the seller once it's overdue (a reminder); before that it's waiting.
        "needs_you": bool(state == "unpaid" and overdue),
        "deadline": due.isoformat() if state == "unpaid" and due else None,
        "history": safe_history(events),
    }


def build(all_events: dict[str, list[dict]], *, item_names: dict, now: float | None = None) -> dict:
    """KPIs, grid rows and chart series for the dashboard."""
    now = now or datetime.now(timezone.utc).timestamp()
    today = _date(now)
    rows = []
    for ref, events in all_events.items():
        if ref == "other" or ref.startswith("unmatched:"):
            continue
        kinds = {e["kind"] for e in events}
        row = invoice_row(ref, events, today) if "invoice_status" in kinds else order_row(ref, events, today, item_names)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: r["date"], reverse=True)

    def money_by_currency(pairs) -> dict[str, str]:
        totals: dict[str, Decimal] = defaultdict(Decimal)
        for value, currency in pairs:
            totals[currency] += _money(value)
        return {c: f"{v:.2f}" for c, v in sorted(totals.items(), key=lambda kv: -kv[1]) if v}

    orders = [r for r in rows if r["kind"] == "order"]
    invoices = [r for r in rows if r["kind"] == "invoice"]
    disputes = [r["dispute"] | {"currency": r["currency"]} for r in orders if r["dispute"]]
    unpaid = [r for r in invoices if r["state"] == "unpaid"]
    auto = [(a, r["currency"]) for r in rows for a in r["auto_refunds"]]
    outcomes = Counter(d["outcome"] for d in disputes if not d["open"])
    kpis = {
        "captured": money_by_currency((r["amount"], r["currency"]) for r in rows if r["state"] != "unpaid"),
        "refunded": money_by_currency((r["refunded"], r["currency"]) for r in rows),
        "disputed_open": money_by_currency((d["amount"], d["currency"]) for d in disputes if d["open"]),
        "disputes_open": sum(1 for d in disputes if d["open"]),
        "invoiced_unpaid": money_by_currency((r["amount"], r["currency"]) for r in unpaid),
        "invoices_unpaid": len(unpaid),
        "overdue": money_by_currency((r["amount"], r["currency"]) for r in unpaid if r["overdue_days"]),
        "invoices_overdue": sum(1 for r in unpaid if r["overdue_days"]),
        "auto_resolved": {"count": len(auto), "amount": money_by_currency(auto)},
        "answered_before_dispute": count_answered({e["kind"] for e in ev} for ev in all_events.values()),
        "disputes": {"total": len(disputes), "responded": sum(1 for d in disputes if d["responded"]),
                     "open": sum(1 for d in disputes if d["open"]),
                     "outcomes": dict(outcomes.most_common())},
    }

    # Charts are in the main currency (the one most money is in); others are counted, not mixed.
    currency = Counter(r["currency"] for r in rows).most_common(1)[0][0] if rows else "USD"
    main = [r for r in rows if r["currency"] == currency]
    by_state = defaultdict(Decimal)
    for r in main:
        by_state[r["state"]] += _money(r["amount"]) - (_money(r["refunded"]) if r["state"] == "settled" else 0)
    days = [today - timedelta(days=n) for n in range(29, -1, -1)]
    per_day = {d.isoformat(): defaultdict(Decimal) for d in days}
    for r in main:
        if r["date"] in per_day:
            per_day[r["date"]][r["state"]] += _money(r["amount"])
    ours = [ev for ref, ev in all_events.items() if ref != "other" and not ref.startswith("unmatched:")]
    asked = sum(1 for ev in ours if any(e["kind"] == "buyer_request" for e in ev))
    disputed_orders = sum(1 for ev in ours if any(e["kind"] in DISPUTE_KINDS for e in ev))
    charts = {
        "currency": currency,
        "other_currencies": sorted({r["currency"] for r in rows} - {currency}),
        "by_state": [{"state": s, "label": STATE_LABELS[s], "amount": float(by_state[s])} for s in STATES],
        "funnel": [{"step": "Buyer questions", "count": asked},
                   {"step": "Resolved without a dispute", "count": kpis["answered_before_dispute"]},
                   {"step": "Disputes", "count": disputed_orders}],
        "per_day": [{"day": d, **{s: float(v[s]) for s in STATES}} for d, v in per_day.items()],
        "reasons": [{"reason": k, "count": v} for k, v in Counter(d["reason"] for d in disputes).most_common()],
    }
    return {"generated_at": now, "kpis": kpis, "rows": rows, "charts": charts}
