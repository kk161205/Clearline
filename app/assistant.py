"""AI dispute assistant: code gathers the facts, the AI only reasons and drafts.

build_facts() turns an order's timeline into a flat dict of facts with stable keys. The AI
must cite those keys for every reason, and server-side guardrails reject answers that cite
unknown facts, offer more than the disputed amount, fight a non-receipt claim without
tracking, or put emails/links in drafted text. Nothing here talks to PayPal: dispute actions
only happen when the seller clicks Approve (see /api/disputes/{id}/act).
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .ai import AIClient, AIError
from .store import DISPUTE_KINDS, TimelineStore

log = logging.getLogger("clearline.assistant")

PROMPT_VERSION = "dispute-v1"

SYSTEM_PROMPT = """You are Clearline's dispute assistant for a small online seller on PayPal.
You receive FACTS about one disputed order as JSON, keyed by fact key. Recommend one decision:
- FIGHT: contest the dispute with proof (e.g. shipment tracking).
- REFUND: accept the claim and refund the full disputed amount.
- OFFER: propose a resolution, usually a partial refund.

Rules:
- Use only the given FACTS. Never invent tracking numbers, carriers, delivery events, dates,
  amounts or policies. Anything not in FACTS is unknown: list it in missing_evidence.
- Every reason must cite, in fact_keys, the exact fact keys it relies on.
- Prefer FIGHT only when the facts contain proof. For an item-not-received claim, FIGHT needs
  tracking facts (tracking.carrier and tracking.number).
- Prefer OFFER for small amounts with weak evidence. offer.amount must be more than 0 and no
  more than dispute.amount; offer.type must be one of dispute.allowed_offer_types if given.
- Set offer to null unless the decision is OFFER.
- message_to_buyer: courteous, factual, under 120 words. Call the people "you" and "we";
  no names, emails, phone numbers or links.
- note_to_paypal: factual summary of the seller's position for PayPal, under 120 words,
  no emails or links.
- confidence is a number from 0 to 1.
- The seller reviews and approves everything; you never take actions yourself.
Reply with JSON only, matching the schema."""

OFFER_TYPES = ["REFUND", "REFUND_WITH_RETURN", "REFUND_WITH_REPLACEMENT",
               "REPLACEMENT_WITHOUT_REFUND"]  # PayPal Disputes schema: offer_type

# One strict schema for both providers: every field required, optional ones nullable,
# additionalProperties false (Groq strict mode requires exactly this).
RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "confidence", "reasons", "missing_evidence",
                 "message_to_buyer", "note_to_paypal", "offer"],
    "properties": {
        "decision": {"type": "string", "enum": ["FIGHT", "REFUND", "OFFER"]},
        "confidence": {"type": "number"},
        "reasons": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "fact_keys"],
            "properties": {"text": {"type": "string"},
                           "fact_keys": {"type": "array", "items": {"type": "string"}}}}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "message_to_buyer": {"type": "string"},
        "note_to_paypal": {"type": "string"},
        "offer": {"anyOf": [
            {"type": "object", "additionalProperties": False, "required": ["type", "amount"],
             "properties": {"type": {"type": "string", "enum": OFFER_TYPES},
                            "amount": {"type": "number"}}},
            {"type": "null"}]},
    },
}


class Reason(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    fact_keys: list[str]


class Offer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["REFUND", "REFUND_WITH_RETURN", "REFUND_WITH_REPLACEMENT",
                  "REPLACEMENT_WITHOUT_REFUND"]
    amount: float


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["FIGHT", "REFUND", "OFFER"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[Reason] = Field(min_length=1)
    missing_evidence: list[str]
    message_to_buyer: str = Field(min_length=1)
    note_to_paypal: str = Field(min_length=1)
    offer: Offer | None


class GuardrailError(Exception):
    """The AI's answer was well-formed but broke one of our rules, twice."""


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
MAX_WORDS = 120


def strip_pii(text: str) -> str:
    """Remove emails, links and phone-like numbers from free text before it goes to the AI."""
    text = EMAIL_RE.sub("[email removed]", text or "")
    text = URL_RE.sub("[link removed]", text)
    return PHONE_RE.sub("[number removed]", text)


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _latest(events: list[dict], kind: str) -> dict | None:
    matches = [e for e in events if e["kind"] == kind]
    return matches[-1] if matches else None


def build_facts(store: TimelineStore, order_id: str, dispute_id: str | None = None) -> dict:
    """Facts about one disputed order, keyed by stable dotted keys. Missing facts are absent.
    Contains no names, emails, addresses or account IDs (only "buyer"/"seller" roles)."""
    events = store.timeline(order_id)
    facts: dict = {"today": datetime.now(timezone.utc).date().isoformat()}

    if created := _latest(events, "order_created"):
        p = created["payload"]
        facts["order.item"] = p.get("item_id")
        facts["order.amount"] = p.get("amount")
        facts["order.currency"] = p.get("currency")
        facts["order.created_at"] = _iso(created["created_at"])
    if captured := _latest(events, "payment_captured"):
        p = captured["payload"]
        facts["payment.status"] = p.get("status")
        facts["payment.captured_at"] = _iso(captured["created_at"])
        facts["payment.payer_country"] = p.get("payer_country")
    if tracked := _latest(events, "tracking_added"):
        p = tracked["payload"]
        facts["tracking.carrier"] = p.get("carrier")
        facts["tracking.number"] = p.get("tracking_number")
        facts["tracking.added_at"] = _iso(tracked["created_at"])

    disputes = [e for e in events if e["kind"] in DISPUTE_KINDS
                and (dispute_id is None or e["payload"].get("dispute_id") == dispute_id)]
    if disputes:
        p = max(reversed(disputes), key=lambda e: e["payload"].get("update_time") or "")["payload"]
        amount = p.get("dispute_amount") or {}
        facts["dispute.reason"] = p.get("reason")
        facts["dispute.stage"] = p.get("dispute_life_cycle_stage")
        facts["dispute.status"] = p.get("status")
        facts["dispute.amount"] = amount.get("value")
        facts["dispute.currency"] = amount.get("currency_code")
        facts["dispute.due_date"] = p.get("seller_response_due_date")
        facts["dispute.buyer_messages"] = [strip_pii(m.get("content") or "")
                                           for m in p.get("buyer_messages", [])
                                           if m.get("content")]
        offer_types = (p.get("allowed_response_options") or {}).get("make_offer", {})
        facts["dispute.allowed_offer_types"] = offer_types.get("offer_types")
        facts["dispute.requested_evidence"] = p.get("requested_evidence")
    return {k: v for k, v in facts.items() if v not in (None, "", [])}


def _amount(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def check_guardrails(rec: Recommendation, facts: dict) -> list[str]:
    """Server-side rules the AI's answer must satisfy. Returns the problems (empty = OK)."""
    errors = []
    for i, reason in enumerate(rec.reasons):
        unknown = [k for k in reason.fact_keys if k not in facts]
        if unknown:
            errors.append(f"reasons[{i}] cites fact keys that don't exist: {unknown}")
    if rec.decision == "OFFER":
        limit = _amount(facts.get("dispute.amount"))
        allowed = facts.get("dispute.allowed_offer_types")
        if rec.offer is None:
            errors.append("decision OFFER needs an offer")
        elif limit is None:
            errors.append("cannot make an offer: dispute.amount is unknown")
        elif not 0 < rec.offer.amount <= limit:
            errors.append(f"offer.amount must be more than 0 and at most {limit}")
        if rec.offer and allowed and rec.offer.type not in allowed:
            errors.append(f"offer.type must be one of {allowed}")
    elif rec.offer is not None:
        errors.append("offer must be null unless decision is OFFER")
    if (rec.decision == "FIGHT"
            and facts.get("dispute.reason") == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
            and not ("tracking.carrier" in facts and "tracking.number" in facts)):
        errors.append("FIGHT on an item-not-received claim needs tracking facts, and there are none")
    for field in ("message_to_buyer", "note_to_paypal"):
        text = getattr(rec, field)
        if EMAIL_RE.search(text) or URL_RE.search(text):
            errors.append(f"{field} must not contain emails or links")
        if len(text.split()) >= MAX_WORDS:
            errors.append(f"{field} must be under {MAX_WORDS} words")
    return errors


class Assistant:
    def __init__(self, store: TimelineStore, ai: AIClient):
        self.store = store
        self.ai = ai

    def latest_result(self, dispute_id: str) -> dict | None:
        """Most recent ai_recommendation or ai_failed event for a dispute."""
        events = [e for e in self.store.dispute_events(dispute_id)
                  if e["kind"] in ("ai_recommendation", "ai_failed")]
        return events[-1] if events else None

    async def auto_recommend(self, dispute_id: str) -> None:
        """Background hook after a new/changed dispute: only for matched orders that wait on
        the seller, only when an AI key is configured, and never twice per PayPal update."""
        state = self.store.latest_dispute_state(dispute_id)
        if (not self.ai.configured or not state or state["order_id"].startswith("unmatched:")
                or state["payload"].get("status") != "WAITING_FOR_SELLER_RESPONSE"):
            return
        await self.recommend(dispute_id)

    async def recommend(self, dispute_id: str, *, force: bool = False) -> dict | None:
        """Generate and store a recommendation for the dispute's current state. Returns the
        stored event ({kind, payload}), or None if one already exists for this update_time
        (unless force=True, i.e. the seller clicked Regenerate)."""
        state = self.store.latest_dispute_state(dispute_id)
        if not state:
            raise LookupError(dispute_id)
        order_id, update_time = state["order_id"], state["payload"].get("update_time")
        if not force and any(e["kind"] == "ai_recommendation"
                             and e["payload"].get("dispute_update_time") == update_time
                             for e in self.store.dispute_events(dispute_id)):
            return None

        base = {"dispute_id": dispute_id, "dispute_update_time": update_time,
                "prompt_version": PROMPT_VERSION}
        facts = build_facts(self.store, order_id, dispute_id)
        try:
            result = await self._ask(facts)
        except Exception as err:  # AIError, GuardrailError, or a bug: never lose the attempt
            if not isinstance(err, (AIError, GuardrailError)):
                log.exception("Recommendation for %s crashed", dispute_id)
            kind, payload = "ai_failed", {**base, "error": str(err)}
        else:
            kind, payload = "ai_recommendation", {
                **base, "recommendation": result.data.model_dump(), "provider": result.provider,
                "model": result.model, "latency_ms": result.latency_ms}
        self.store.add(order_id, kind, payload)
        return {"kind": kind, "payload": payload}

    async def _ask(self, facts: dict):
        """Ask the AI, then enforce guardrails; one corrective retry, then GuardrailError."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)
             + "\n\nRecommend FIGHT, REFUND or OFFER. Cite fact keys exactly as written."},
        ]
        errors: list[str] = []
        for _ in range(2):
            result = await self.ai.complete(messages, schema_name="dispute_recommendation",
                                            schema=RECOMMENDATION_SCHEMA,
                                            model_cls=Recommendation,
                                            prompt_version=PROMPT_VERSION)
            errors = check_guardrails(result.data, facts)
            if not errors:
                return result
            messages = messages + [
                {"role": "assistant", "content": result.data.model_dump_json()},
                {"role": "user", "content": "Your answer broke these rules: "
                 + "; ".join(errors) + ". Fix them and answer again."},
            ]
        raise GuardrailError("guardrails rejected the answer twice: " + "; ".join(errors))
