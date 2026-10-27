"""Buyer clarity page: answer "what is this charge / where is my order" before it becomes a dispute.

Buyers get a private, unguessable link per captured order (/o/{token}). The page shows only
facts we already hold, never the buyer's email, name or address. The short AI summary is
generated in the background and cached per order state, so the page never waits on the AI;
until it's ready (or if the AI fails) a plain template sentence is shown.
"""

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field

from .ai import AIClient, AIError
from .assistant import EMAIL_RE, URL_RE
from .store import DISPUTE_KINDS, TimelineStore

log = logging.getLogger("clearline.buyer")

SUMMARY_PROMPT_VERSION = "buyer-summary-v1"
SUMMARY_PROMPT = """You write one or two short, friendly sentences for a buyer who opened their
PayPal order page. Use only the FACTS given (JSON). Say who they paid, for what, and the
shipping status (carrier, tracking number, ship date) or refund if present. Never invent or
estimate a delivery date, and never promise anything. No links, emails or phone numbers.
Write numbers and dates exactly as they appear in FACTS. Reply with JSON only."""
SUMMARY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["summary"],
    "properties": {"summary": {"type": "string"}},
}

REQUEST_TYPES = {"unrecognised": "I don't recognise this charge",
                 "not_arrived": "My order hasn't arrived",
                 "refund": "I want a refund"}
REFUND_REASONS = {"not_received": "Item not received", "damaged": "Item arrived damaged",
                  "not_as_described": "Not as described", "changed_mind": "Changed my mind",
                  "other": "Other"}


class BuyerSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=10, max_length=400)


def buyer_token(secret: str, order_id: str) -> str:
    """Unguessable link token: HMAC-SHA256 of the order ID (128 bits, URL-safe)."""
    digest = hmac.new(secret.encode(), f"buyer-link:{order_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def order_for_token(store: TimelineStore, secret: str, token: str) -> str | None:
    """The captured order this token belongs to, or None (unknown, forged or from an old
    secret). Compares in constant time against every captured order."""
    if not token or len(token) > 64:
        return None
    found = None
    for o in store.orders():
        if "payment_captured" in o["kinds"]:
            if hmac.compare_digest(buyer_token(secret, o["order_id"]), token):
                found = o["order_id"]
    return found


def _day(ts: float | None) -> str | None:
    """e.g. "27 Oct 2026" (UTC)."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y").lstrip("0") if ts else None


def _money(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return Decimal("0.00")


def _last(events: list[dict], kind: str) -> dict | None:
    matches = [e for e in events if e["kind"] == kind]
    return matches[-1] if matches else None


def refund_totals(events: list[dict]) -> tuple[Decimal, Decimal, str]:
    """(captured amount, refunded so far by Clearline, currency) for an order's events."""
    created = _last(events, "order_created") or {"payload": {}}
    captured = _money(created["payload"].get("amount"))
    refunded = sum((_money(e["payload"].get("amount")) for e in events
                    if e["kind"] == "refund_sent"), Decimal("0.00"))
    return captured, refunded, created["payload"].get("currency") or ""


def order_facts(events: list[dict], seller_name: str, item_names: dict[str, str]) -> dict:
    """Facts the buyer page (and its AI summary) may use. No buyer PII by construction:
    only order, payment, shipping and refund facts, plus the seller's display name."""
    created = _last(events, "order_created") or {"payload": {}, "created_at": None}
    captured = _last(events, "payment_captured")
    tracked = _last(events, "tracking_added")
    _, refunded, currency = refund_totals(events)
    item_id = created["payload"].get("item_id")
    facts = {
        "seller": seller_name,
        "item": item_names.get(item_id, item_id or "your order"),
        "amount": created["payload"].get("amount"),
        "currency": currency,
        "paid_with": "PayPal",
        "paid_on": _day(captured["created_at"]) if captured else None,
        "country": captured["payload"].get("payer_country") if captured else None,
        "shipped": bool(tracked),
    }
    if tracked:
        facts.update(carrier=tracked["payload"].get("carrier"),
                     tracking_number=tracked["payload"].get("tracking_number"),
                     shipped_on=_day(tracked["created_at"]))
    if refunded:
        facts.update(refunded=f"{refunded:.2f}",
                     refunded_on=_day(_last(events, "refund_sent")["created_at"]))
    return {k: v for k, v in facts.items() if v not in (None, "")}


def template_summary(facts: dict) -> str:
    """Plain sentence used until the AI summary is ready, or if the AI fails."""
    text = (f"You paid {facts.get('amount')} {facts.get('currency')} to {facts['seller']} for "
            f"{facts['item']} with PayPal" + (f" on {facts['paid_on']}." if facts.get("paid_on") else "."))
    if facts.get("shipped"):
        text += (f" Your order shipped with {facts.get('carrier')}"
                 + (f" on {facts['shipped_on']}" if facts.get("shipped_on") else "")
                 + f" (tracking number {facts.get('tracking_number')}).")
    else:
        text += " It hasn't shipped yet; the seller adds tracking when it does."
    if facts.get("refunded"):
        text += f" A refund of {facts['refunded']} {facts.get('currency')} was sent."
    return text


def explain_charge(facts: dict) -> str:
    """Plain-words answer to "I don't recognise this charge"."""
    return (f"This is a payment of {facts.get('amount')} {facts.get('currency')} to "
            f"{facts['seller']} for \"{facts['item']}\", paid with PayPal"
            + (f" on {facts['paid_on']}" if facts.get("paid_on") else "")
            + ". It may appear on your statement under the seller's PayPal name.")


def state_key(facts: dict) -> str:
    """Changes whenever anything the summary depends on changes."""
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]


NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    return {Decimal(n) for n in NUMBER.findall(text)}


def summary_problems(text: str, facts: dict) -> list[str]:
    """Guardrails for the AI summary: every number must be one of the facts' numbers, compared
    whole and by value ("120" = "120.00", but "2" isn't in "2026"), so no invented dates or
    amounts; and no emails or links."""
    problems = []
    known = _numbers(json.dumps(facts))
    invented = [n for n in NUMBER.findall(text) if Decimal(n) not in known]
    if invented:
        problems.append(f"numbers not in the facts: {invented}")
    if EMAIL_RE.search(text) or URL_RE.search(text):
        problems.append("no emails or links")
    if len(text.split()) > 60:
        problems.append("keep it under 60 words")
    return problems


class SummaryCache:
    """Background AI summaries, cached per order state. Never awaited by the page."""

    def __init__(self, store: TimelineStore, ai: AIClient):
        self.store = store
        self.ai = ai
        self.in_flight: set[str] = set()
        self.failed: set[tuple[str, str]] = set()  # (order, state) not to retry until it changes

    def cached(self, order_id: str, facts: dict) -> dict | None:
        """The cached AI summary if it matches the current state, else None."""
        row = self.store.buyer_summary(order_id)
        return row if row and row["state_key"] == state_key(facts) else None

    def needs_refresh(self, order_id: str, facts: dict) -> bool:
        key = state_key(facts)
        return (self.ai.configured and order_id not in self.in_flight
                and (order_id, key) not in self.failed and not self.cached(order_id, facts))

    async def refresh(self, order_id: str, facts: dict) -> None:
        """Generate and cache the summary for this state; on any failure keep the template."""
        key = state_key(facts)
        if not self.needs_refresh(order_id, facts):
            return
        self.in_flight.add(order_id)
        try:
            messages = [{"role": "system", "content": SUMMARY_PROMPT},
                        {"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)}]
            for _ in range(2):  # one corrective retry, like the dispute assistant
                result = await self.ai.complete(messages, schema_name="buyer_summary",
                                                schema=SUMMARY_SCHEMA, model_cls=BuyerSummary,
                                                prompt_version=SUMMARY_PROMPT_VERSION)
                problems = summary_problems(result.data.summary, facts)
                if not problems:
                    self.store.set_buyer_summary(order_id, key, result.data.summary,
                                                 result.provider, result.model)
                    return
                messages = messages + [
                    {"role": "assistant", "content": result.data.model_dump_json()},
                    {"role": "user", "content": "Fix these problems: " + "; ".join(problems)}]
            self.failed.add((order_id, key))
            log.warning("Buyer summary for %s rejected by guardrails: %s", order_id, problems)
        except AIError as err:
            self.failed.add((order_id, key))
            log.warning("Buyer summary for %s failed (%s); showing the template", order_id, err)
        except Exception:
            self.failed.add((order_id, key))
            log.exception("Buyer summary for %s crashed; showing the template", order_id)
        finally:
            self.in_flight.discard(order_id)


def buyer_view(store: TimelineStore, cache: SummaryCache, order_id: str, *, seller_name: str,
               item_names: dict[str, str]) -> tuple[dict, dict]:
    """(page data, summary facts) for one order. Page data has no buyer PII."""
    events = store.timeline(order_id)
    facts = order_facts(events, seller_name, item_names)
    cached = cache.cached(order_id, facts)
    replies: dict[str, list] = {}
    for e in events:
        if e["kind"] == "seller_reply":
            replies.setdefault(e["payload"]["request_id"], []).append(
                {"message": e["payload"]["message"], "at": e["created_at"]})
    requests = [{"id": e["payload"]["request_id"], "type": e["payload"]["type"],
                 "type_label": REQUEST_TYPES.get(e["payload"]["type"], e["payload"]["type"]),
                 "reason": REFUND_REASONS.get(e["payload"].get("reason") or "", None),
                 "message": e["payload"].get("message") or "", "at": e["created_at"],
                 "replies": replies.get(e["payload"]["request_id"], [])}
                for e in events if e["kind"] == "buyer_request"]
    refunds = [{"amount": e["payload"]["amount"], "currency": e["payload"]["currency"],
                "status": e["payload"].get("status"), "at": e["created_at"]}
               for e in events if e["kind"] == "refund_sent"]
    page = {
        "order_ref": order_id[-6:],
        "seller": facts["seller"], "item": facts["item"],
        "amount": facts.get("amount"), "currency": facts.get("currency"),
        "paid_on": facts.get("paid_on"), "paid_with": "PayPal", "country": facts.get("country"),
        "shipping": {"shipped": facts["shipped"], "carrier": facts.get("carrier"),
                     "tracking_number": facts.get("tracking_number"),
                     "shipped_on": facts.get("shipped_on")},
        "summary": ({"text": cached["text"], "source": "ai"} if cached
                    else {"text": template_summary(facts), "source": "template"}),
        "charge_explanation": explain_charge(facts),
        "requests": requests,
        "refunds": refunds,
        "request_types": REQUEST_TYPES,
        "refund_reasons": REFUND_REASONS,
    }
    return page, facts


def answered_before_dispute(store: TimelineStore) -> int:
    """Orders where the buyer asked us something and no dispute was ever opened."""
    return sum(1 for o in store.orders()
               if "buyer_request" in o["kinds"]
               and not any(k in DISPUTE_KINDS for k in o["kinds"]))
