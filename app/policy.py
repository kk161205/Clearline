"""Auto-resolve policy (Day 6): refunds the seller has approved in advance.

Off by default. When the seller turns it on, a buyer refund request (from the buyer page or a
buyer's AI agent) that fits every rule is refunded straight away through the normal refund
path; anything else goes to the seller exactly as before. All checks run on Clearline's own
records before any PayPal call, and the refund always names an explicit amount, so it can never
exceed what the checks allowed (captured minus refunded, the per-order maximum, the daily cap).
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .buyer import REFUND_REASONS, refund_totals
from .store import DISPUTE_KINDS

POLICY_KEY = "auto_resolve_policy"
TRIGGER = "auto-policy"
Reason = Literal["not_received", "damaged", "not_as_described", "changed_mind", "other"]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    max_refund_per_order: Decimal = Field(default=Decimal("0.00"), ge=0, le=100000, decimal_places=2)
    allowed_reasons: list[Reason] = Field(default_factory=lambda: ["not_received", "not_as_described"])
    require_no_dispute: bool = True
    daily_cap: Decimal = Field(default=Decimal("0.00"), ge=0, le=1000000, decimal_places=2)
    only_if_not_shipped: bool = True
    currency: Literal["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"] = "USD"


class NotInPolicy(Exception):
    """The request doesn't fit the policy (the message says which rule); it goes to the seller."""


def day_start(now: float) -> float:
    """Start of the current UTC day, for the daily cap."""
    d = datetime.fromtimestamp(now, timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def spent_today(auto_refunds: list[dict]) -> Decimal:
    """Total of today's refund_sent rows that the policy sent (the caller passes today's rows)."""
    return sum((Decimal(r["payload"]["amount"]) for r in auto_refunds
                if r["payload"].get("trigger") == TRIGGER), Decimal("0.00"))


def decide(policy: Policy, events: list[dict], *, reason: str | None,
           requested: Decimal | None, spent: Decimal) -> tuple[Decimal, str]:
    """(amount to refund, the rule that matched), or NotInPolicy with the first rule that
    failed. `events` is the order's timeline, `spent` what the policy refunded today."""
    if not policy.enabled:
        raise NotInPolicy("auto-resolve is off")
    if reason not in policy.allowed_reasons:
        raise NotInPolicy(f"reason \"{REFUND_REASONS.get(reason or '', reason)}\" isn't allowed")
    kinds = {e["kind"] for e in events}
    if "invoice_paid" in kinds:
        raise NotInPolicy("invoice payments are never refunded automatically")
    captured_rows = [e for e in events if e["kind"] == "payment_captured"]
    if not captured_rows or not captured_rows[-1]["payload"].get("capture_id"):
        raise NotInPolicy("no captured payment")
    captured, refunded, currency = refund_totals(events)
    if currency != policy.currency:
        raise NotInPolicy(f"the order is in {currency}, the policy in {policy.currency}")
    if policy.require_no_dispute and kinds & set(DISPUTE_KINDS):
        raise NotInPolicy("the order has a dispute")
    if policy.only_if_not_shipped and "tracking_added" in kinds:
        raise NotInPolicy("the order has shipped")
    remaining = captured - refunded
    if remaining <= 0:
        raise NotInPolicy("already fully refunded")
    amount = remaining if requested is None else requested
    if not 0 < amount <= remaining:
        raise NotInPolicy(f"{amount} is more than the {remaining} {currency} left to refund")
    if refunded + amount > policy.max_refund_per_order:
        raise NotInPolicy(f"{amount} {currency} (plus {refunded} refunded before) is over the "
                          f"per-order maximum of {policy.max_refund_per_order}")
    if spent + amount > policy.daily_cap:
        raise NotInPolicy(f"{amount} {currency} would pass today's cap of {policy.daily_cap} "
                          f"({spent} used)")
    rule = (f"reason \"{REFUND_REASONS[reason]}\" allowed · {amount} ≤ {policy.max_refund_per_order} "
            f"per order · today {spent + amount} of {policy.daily_cap} {currency}"
            + (" · no dispute" if policy.require_no_dispute else "")
            + (" · not shipped" if policy.only_if_not_shipped else ""))
    return amount, rule
