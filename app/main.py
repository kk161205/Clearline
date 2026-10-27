import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .ai import AIClient
from .assistant import Assistant
from .config import Settings, get_settings
from .paypal import (PayPalClient, PayPalError, allowed_actions, capture_id_from,
                     dispute_summary, disputed_capture_ids, parent_capture_id)
from .scheduler import run_every
from .store import TimelineStore

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("clearline")

# Prices live on the server, never in the browser, so a buyer can't change them.
CATALOGUE = {
    "brand-kit": {
        "description": "Printed brand kit (logo + business cards), shipped from India",
        "amount": "120.00",
        "currency": "USD",
    },
}


# Webhook event type -> timeline event kind.
DISPUTE_EVENTS = {
    "CUSTOMER.DISPUTE.CREATED": "dispute_created",
    "CUSTOMER.DISPUTE.UPDATED": "dispute_updated",
    "CUSTOMER.DISPUTE.RESOLVED": "dispute_resolved",
}
CAPTURE_EVENTS = {
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.PENDING",
    "PAYMENT.CAPTURE.REFUNDED",
    "PAYMENT.CAPTURE.REVERSED",
}


class CreateOrderIn(BaseModel):
    item_id: str


class TrackingIn(BaseModel):
    tracking_number: str
    carrier: str = "DHL"


class OfferIn(BaseModel):
    type: str
    amount: float


class ActIn(BaseModel):
    """The seller's approved action. Texts default to the latest AI draft if left out."""
    action: Literal["FIGHT", "REFUND", "OFFER"]
    message_to_buyer: str | None = None
    note_to_paypal: str | None = None
    offer: OfferIn | None = None


class ActionNotAllowed(Exception):
    """PayPal doesn't currently allow this action on the dispute (-> 409)."""


PAYPAL_TEXT_LIMIT = 2000  # maxLength of message/note fields in the Disputes schema


STATE_CHANGED_ISSUE = "ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE"


def step_blocked(endpoint: str, dispute: dict) -> str | None:
    """Why PayPal won't accept this action endpoint on the dispute right now, in plain words
    (None = allowed). Uses the HATEOAS link plus the status/stage rules from PayPal's docs;
    the link alone isn't enough (provide-evidence stays linked while waiting for the buyer)."""
    status = dispute.get("status")
    stage = dispute.get("dispute_life_cycle_stage")
    if status == "RESOLVED":
        return "dispute is resolved"
    if status == "WAITING_FOR_BUYER_RESPONSE" and endpoint != "accept-claim":
        return "dispute is waiting for the buyer"
    if endpoint == "provide-evidence" and status != "WAITING_FOR_SELLER_RESPONSE":
        return f"evidence needs status WAITING_FOR_SELLER_RESPONSE (now {status})"
    if endpoint in ("send-message", "make-offer") and stage != "INQUIRY":
        return f"only allowed in the INQUIRY stage (now {stage})"
    if endpoint not in allowed_actions(dispute):
        return f"PayPal doesn't offer {endpoint} on this dispute right now"
    return None


def plan_action(action: str, dispute: dict, *, message: str, note: str,
                offer: dict | None, tracking: dict | None) -> list[tuple[str, dict]]:
    """The PayPal calls an approved action needs, in order, as (endpoint, body) steps.

    Planned against a freshly fetched dispute; each step is checked again right before it
    runs, because one step can change what the next is allowed to do (sending a message moves
    the dispute to WAITING_FOR_BUYER_RESPONSE). Raises ActionNotAllowed rather than guessing,
    and ValueError for missing or invalid seller input.
    """
    options = dispute.get("allowed_response_options") or {}

    if action == "FIGHT":
        # Evidence first: messaging the buyer hands the turn to them and blocks evidence.
        steps = []
        evidence_blocked = step_blocked("provide-evidence", dispute)
        if tracking and not evidence_blocked:
            steps.append(("provide-evidence", {"carrier": tracking["carrier"],
                                               "tracking_number": tracking["tracking_number"],
                                               "notes": note}))
        message_blocked = step_blocked("send-message", dispute)
        if not message_blocked:
            if not message:
                raise ValueError("A message to the buyer is required.")
            steps.append(("send-message", {"message": message}))
        if not steps:
            why = evidence_blocked if tracking else "no tracking number is stored for this order"
            raise ActionNotAllowed(f"Nothing PayPal allows right now: evidence ({why}), "
                                   f"message ({message_blocked}).")
        return steps

    if action == "OFFER":
        if blocked := step_blocked("make-offer", dispute):
            raise ActionNotAllowed(f"Can't make an offer: {blocked}.")
        if not offer:
            raise ValueError("An offer (type and amount) is required.")
        offer_types = options.get("make_offer", {}).get("offer_types")
        if offer_types and offer["type"] not in offer_types:
            raise ActionNotAllowed(f"Offer type {offer['type']} isn't allowed; PayPal allows "
                                   f"{', '.join(offer_types)}.")
        amount = dispute.get("dispute_amount") or {}
        if not 0 < offer["amount"] <= float(amount.get("value") or 0):
            raise ValueError("The offer amount must be more than 0 and at most the disputed "
                             "amount.")
        if not note:
            raise ValueError("A note for the offer is required.")
        return [("make-offer", {"note": note, "offer_type": offer["type"],
                                "offer_amount": {"currency_code": amount.get("currency_code"),
                                                 "value": f"{offer['amount']:.2f}"}})]

    # REFUND
    if blocked := step_blocked("accept-claim", dispute):
        raise ActionNotAllowed(f"Can't accept the claim: {blocked}.")
    claim_types = options.get("accept_claim", {}).get("accept_claim_types")
    if claim_types and "REFUND" not in claim_types:
        raise ActionNotAllowed("PayPal doesn't currently allow accepting this claim with a "
                               "full refund.")
    return [("accept-claim", {"accept_claim_type": "REFUND", "note": note})]


def readable_error(err: Exception) -> str:
    """Short sentence for the seller page (PayPal's raw body stays in the logs)."""
    if isinstance(err, PayPalError):
        return err.readable
    return "Could not reach PayPal (network error)"


def error_details(err: Exception) -> dict:
    """What support needs to trace a PayPal failure; shown collapsed on /seller.
    `paypal_error` is PayPal's full error (every details[] entry), not just the first issue."""
    if isinstance(err, PayPalError):
        return {"paypal_status": err.status, "paypal_issue": next(iter(err.issues), None),
                "debug_id": err.debug_id, "paypal_error": err.record}
    return {"paypal_status": None, "paypal_issue": None, "debug_id": None, "paypal_error": None}


def allowed_decisions(dispute: dict) -> list[str]:
    """Decisions the seller could approve right now; drives the Approve button."""
    decisions = []
    if not step_blocked("provide-evidence", dispute) or not step_blocked("send-message", dispute):
        decisions.append("FIGHT")
    if not step_blocked("make-offer", dispute):
        decisions.append("OFFER")
    if not step_blocked("accept-claim", dispute):
        decisions.append("REFUND")
    return decisions


STEP_NAMES = {"provide-evidence": "Evidence", "send-message": "Message",
              "make-offer": "Offer", "accept-claim": "Refund"}


def step_summary(results: list[dict]) -> str:
    """One line for the seller, e.g. "Evidence sent ✓ · Message skipped (dispute is waiting
    for the buyer)"."""
    parts = []
    for r in results:
        name = STEP_NAMES.get(r["endpoint"], r["endpoint"])
        if r["outcome"] == "sent":
            parts.append(f"{name} sent ✓")
        else:
            parts.append(f"{name} {r['outcome']} ({r.get('reason') or r.get('error')})")
    return " · ".join(parts)


def create_app(settings: Settings | None = None,
               http: httpx.AsyncClient | None = None,
               ai_http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    paypal = PayPalClient(settings, http)
    store = TimelineStore(settings.database_path)
    ai = AIClient(settings, store, ai_http)
    assistant = Assistant(store, ai)
    sync_lock = asyncio.Lock()
    sync_state = {"running": False, "last_synced_at": None, "trigger": None, "counts": None,
                  "error": None, "auto_sync_minutes": settings.auto_sync_minutes}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start the auto-sync safety net with the app and stop it cleanly on shutdown."""
        task = None
        if settings.auto_sync_minutes > 0:
            task = asyncio.create_task(run_every(settings.auto_sync_minutes * 60, auto_sync))
        app.state.auto_sync_task = task
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Clearline", version="0.1.0", lifespan=lifespan)

    def fail(err: PayPalError) -> HTTPException:
        return HTTPException(status_code=502, detail={"paypal_status": err.status,
                                                       "paypal_error": err.body})

    # no-cache: browsers revalidate every load, so a new page version shows up right away.
    @app.get("/", include_in_schema=False)
    async def buyer_page():
        return FileResponse(STATIC / "buyer.html", headers={"Cache-Control": "no-cache"})

    @app.get("/seller", include_in_schema=False)
    async def seller_page():
        return FileResponse(STATIC / "seller.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config")
    async def config():
        # The Client ID is public by design; the secret never leaves the server.
        return {"clientId": settings.paypal_client_id, "sdkUrl": settings.sdk_url,
                "env": settings.paypal_env, "catalogue": CATALOGUE}

    @app.post("/api/orders")
    async def create_order(body: CreateOrderIn):
        item = CATALOGUE.get(body.item_id)
        if not item:
            raise HTTPException(400, "Unknown item")
        try:
            order = await paypal.create_order(amount=item["amount"], currency=item["currency"],
                                              description=item["description"],
                                              reference_id=body.item_id)
        except PayPalError as err:
            raise fail(err)
        store.add(order["id"], "order_created",
                  {"item_id": body.item_id, "amount": item["amount"],
                   "currency": item["currency"], "status": order.get("status")})
        return {"orderId": order["id"]}

    @app.post("/api/orders/{order_id}/capture")
    async def capture_order(order_id: str):
        try:
            order = await paypal.capture_order(order_id)
        except PayPalError as err:
            store.add(order_id, "capture_failed", {"paypal_status": err.status})
            raise fail(err)
        capture_id = capture_id_from(order)
        payer = order.get("payer", {})
        store.add(order_id, "payment_captured",
                  {"status": order.get("status"), "capture_id": capture_id,
                   "payer_email": payer.get("email_address"),
                   "payer_country": payer.get("address", {}).get("country_code")})
        return {"status": order.get("status"), "captureId": capture_id}

    @app.post("/api/orders/{order_id}/tracking")
    async def add_tracking(order_id: str, body: TrackingIn):
        captured = [e for e in store.timeline(order_id) if e["kind"] == "payment_captured"]
        if not captured:
            raise HTTPException(409, "Order has not been captured yet")
        capture_id = captured[-1]["payload"]["capture_id"]
        try:
            await paypal.add_tracking(order_id, capture_id=capture_id,
                                      tracking_number=body.tracking_number,
                                      carrier=body.carrier)
        except PayPalError as err:
            raise fail(err)
        store.add(order_id, "tracking_added",
                  {"tracking_number": body.tracking_number, "carrier": body.carrier})
        return {"ok": True}

    # ---- Webhooks (Day 2) --------------------------------------------------

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request, background: BackgroundTasks):
        if not settings.paypal_webhook_id:
            raise HTTPException(503, "PAYPAL_WEBHOOK_ID is not set. Add the Webhook ID from the "
                                     "Developer Dashboard to .env and restart the app.")
        try:
            event = await request.json()
        except ValueError:
            raise HTTPException(400, "Body is not JSON")
        if not isinstance(event, dict) or not event.get("id"):
            raise HTTPException(400, "Not a PayPal webhook event")

        if not await paypal.verify_webhook_signature(request.headers, event):
            if not settings.allow_unverified_webhooks:
                raise HTTPException(400, "Webhook signature verification failed")
            log.warning("!!! UNVERIFIED WEBHOOK ACCEPTED: %s %s. ALLOW_UNVERIFIED_WEBHOOKS=true "
                        "is for local testing only; never enable it in production. !!!",
                        event.get("event_type"), event["id"])

        if not store.mark_webhook_processed(event["id"]):
            return {"status": "duplicate"}
        # Answer PayPal fast; enrichment calls happen after the response is sent.
        background.add_task(process_webhook, event)
        return {"status": "accepted"}

    async def process_webhook(event: dict) -> None:
        """Put one verified, de-duplicated webhook event on the right timeline."""
        event_type = event.get("event_type", "")
        resource = event.get("resource") or {}
        try:
            if event_type in DISPUTE_EVENTS:
                await record_dispute(DISPUTE_EVENTS[event_type], event, resource)
            elif event_type in CAPTURE_EVENTS:
                record_capture_event(event_type, event, resource)
            else:
                store.add("other", "webhook_other",
                          {"event_type": event_type, "event_id": event.get("id"),
                           "summary": event.get("summary")})
        except Exception as err:
            # The event is already marked processed and PayPal got 200, so never drop it silently.
            log.exception("Processing webhook %s (%s) failed", event.get("id"), event_type)
            store.add("other", "webhook_processing_failed",
                      {"event_id": event.get("id"), "event_type": event_type, "error": str(err)})

    async def record_dispute(kind: str, event: dict, resource: dict) -> None:
        """Fetch the full dispute and record it on the disputed order (or under unmatched:)."""
        dispute_id = resource.get("dispute_id")
        dispute, error = resource, None
        if not dispute_id:
            error = "Webhook resource has no dispute_id"
        else:
            try:
                dispute = await paypal.get_dispute(dispute_id)
            except (PayPalError, httpx.HTTPError) as err:
                # Keep what the webhook itself told us rather than losing the event.
                error = str(err)
        store_dispute(kind, dispute, dispute_id=dispute_id,
                      extra={"event_id": event.get("id")}, error=error)
        if kind == "dispute_created" and dispute_id:
            await assistant.auto_recommend(dispute_id)

    def store_dispute(kind: str, dispute: dict, *, dispute_id: str | None, extra: dict,
                      error: str | None = None) -> str:
        """Record one dispute snapshot (webhook, sync or post-action re-fetch) on its order.
        The order comes from the disputed capture ID, else from earlier events for the same
        dispute, else "unmatched:<dispute_id>". Returns the order_id used."""
        payload = {**dispute_summary(dispute), **extra,
                   "allowed_decisions": allowed_decisions(dispute)}
        payload["dispute_id"] = payload["dispute_id"] or dispute_id
        if error:
            payload["enrichment_error"] = error
        order_id = next((o for o in map(store.find_order_by_capture,
                                        disputed_capture_ids(dispute)) if o), None)
        if not order_id and (known := store.latest_dispute_state(payload["dispute_id"] or "")):
            order_id = known["order_id"]
        order_id = order_id or f"unmatched:{dispute_id}"
        store.add(order_id, kind, payload)
        return order_id

    def is_new_state(known: dict | None, dispute: dict) -> bool:
        """True if PayPal's update_time or status differs from what we stored last."""
        if not known:
            return True
        return (known["payload"].get("update_time") != dispute.get("update_time")
                or known["payload"].get("status") != dispute.get("status"))

    def snapshot_kind(known: dict | None, dispute: dict) -> str:
        """Timeline kind for a dispute snapshot that didn't come from a webhook."""
        if dispute.get("status") == "RESOLVED":
            return "dispute_resolved"
        return "dispute_updated" if known else "dispute_created"

    def record_capture_event(event_type: str, event: dict, resource: dict) -> None:
        """Record a PAYMENT.CAPTURE.* webhook on its order (or under unmatched:)."""
        amount = resource.get("amount") or {}
        payload = {"event_id": event.get("id"), "resource_id": resource.get("id"),
                   "status": resource.get("status"),
                   "amount": {"value": amount.get("value"),
                              "currency_code": amount.get("currency_code")}}
        reason = (resource.get("status_details") or {}).get("reason")
        if event_type == "PAYMENT.CAPTURE.PENDING" and reason:
            payload["pending_reason"] = reason
        kind = "webhook_" + event_type.lower().replace(".", "_")
        store.add(order_for_capture_resource(resource) or f"unmatched:{resource.get('id')}",
                  kind, payload)

    def order_for_capture_resource(resource: dict) -> str | None:
        """Our order for a capture resource, or for a refund resource via its parent capture."""
        for capture_id in (resource.get("id"), parent_capture_id(resource)):
            if capture_id and (order_id := store.find_order_by_capture(capture_id)):
                return order_id
        order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
        return order_id if order_id and store.timeline(order_id) else None

    # ---- AI dispute assistant (Day 3) ---------------------------------------

    @app.get("/api/ai/status")
    async def ai_status():
        return ai.status()

    @app.post("/api/ai/reset")
    async def ai_reset():
        ai.reset()
        return ai.status()

    @app.get("/api/disputes/{dispute_id}/recommendation")
    async def get_recommendation(dispute_id: str):
        result = assistant.latest_result(dispute_id)
        if not result:
            raise HTTPException(404, "No recommendation for this dispute yet")
        return {"kind": result["kind"], "payload": result["payload"]}

    @app.post("/api/disputes/{dispute_id}/recommendation")
    async def regenerate_recommendation(dispute_id: str):
        if not store.latest_dispute_state(dispute_id):
            raise HTTPException(404, "Unknown dispute")
        if not ai.configured:
            raise HTTPException(503, "No AI key configured. Set GEMINI_API_KEY and/or "
                                     "GROQ_API_KEY in .env and restart the app.")
        return await assistant.recommend(dispute_id, force=True, trigger="regenerate")

    @app.post("/api/disputes/{dispute_id}/act")
    async def act_on_dispute(dispute_id: str, body: ActIn):
        """Run the seller-approved action against PayPal. The only place Clearline changes a
        dispute; it is triggered by the seller's Approve click, never by the AI."""
        known = store.latest_dispute_state(dispute_id)
        if not known:
            raise HTTPException(404, "Unknown dispute")
        order_id = known["order_id"]
        draft = {}
        if (latest := assistant.latest_result(dispute_id)) and latest["kind"] == "ai_recommendation":
            draft = latest["payload"]["recommendation"]
        message = (body.message_to_buyer or draft.get("message_to_buyer") or "").strip()
        note = (body.note_to_paypal or draft.get("note_to_paypal") or "").strip()
        offer = body.offer.model_dump() if body.offer else draft.get("offer")
        if len(message) > PAYPAL_TEXT_LIMIT or len(note) > PAYPAL_TEXT_LIMIT:
            raise HTTPException(400, f"Texts must be at most {PAYPAL_TEXT_LIMIT} characters")
        tracked = [e for e in store.timeline(order_id) if e["kind"] == "tracking_added"]
        tracking = tracked[-1]["payload"] if tracked else None

        try:
            dispute = await paypal.get_dispute(dispute_id)  # never act on stale state
        except PayPalError as err:
            raise fail(err)
        try:
            steps = plan_action(body.action, dispute, message=message, note=note,
                                offer=offer, tracking=tracking)
        except ActionNotAllowed as err:
            raise HTTPException(409, str(err))
        except ValueError as err:
            raise HTTPException(400, str(err))

        run_id = uuid.uuid4().hex[:12]  # groups the steps of one Approve click
        results, failed = [], False
        for endpoint, sent in steps:
            base = {"dispute_id": dispute_id, "action": body.action, "endpoint": endpoint,
                    "run_id": run_id}
            try:
                current = await paypal.get_dispute(dispute_id)  # re-check before EACH step
            except (PayPalError, httpx.HTTPError) as err:
                error = f"Could not re-check the dispute: {readable_error(err)}"
                store.add(order_id, "dispute_action_failed",
                          {**base, "sent": sent, "error": error, **error_details(err)})
                results.append({"endpoint": endpoint, "outcome": "failed", "error": error})
                failed = True
                break
            if reason := step_blocked(endpoint, current):
                store.add(order_id, "dispute_action_skipped",
                          {**base, "reason": reason, "dispute_status": current.get("status")})
                results.append({"endpoint": endpoint, "outcome": "skipped", "reason": reason})
                continue
            try:
                response = await run_step(dispute_id, endpoint, sent)
            except (PayPalError, httpx.HTTPError) as err:
                details = error_details(err)
                if details["paypal_status"] == 422 and details["paypal_issue"] == STATE_CHANGED_ISSUE:
                    # The dispute changed between our check and the call: not a failure.
                    reason = readable_error(err)
                    store.add(order_id, "dispute_action_skipped",
                              {**base, "reason": reason, "dispute_status": current.get("status"),
                               **details})
                    results.append({"endpoint": endpoint, "outcome": "skipped", "reason": reason})
                    continue
                # No automatic retry: PayPal rejected it, so a human looks at it first.
                error = readable_error(err)
                store.add(order_id, "dispute_action_failed",
                          {**base, "sent": sent, "error": error, **details})
                results.append({"endpoint": endpoint, "outcome": "failed", "error": error})
                failed = True
                break
            store.add(order_id, "dispute_action_sent",
                      {**base, "sent": sent,
                       "response": {"links": [link.get("rel")
                                              for link in response.get("links", [])]}})
            results.append({"endpoint": endpoint, "outcome": "sent"})

        try:
            fresh = await paypal.get_dispute(dispute_id)
            current = store.latest_dispute_state(dispute_id)
            if is_new_state(current, fresh):
                store_dispute(snapshot_kind(current, fresh), fresh, dispute_id=dispute_id,
                              extra={"source": "action"})
        except (PayPalError, httpx.HTTPError) as err:
            log.warning("Could not re-fetch dispute %s after action: %s", dispute_id, err)

        summary = step_summary(results)
        if failed:
            raise HTTPException(502, {"steps": results, "summary": summary})
        return {"ok": True, "steps": results, "summary": summary}

    async def run_step(dispute_id: str, endpoint: str, sent: dict) -> dict:
        """Call the PayPal endpoint for one planned step."""
        if endpoint == "send-message":
            return await paypal.send_dispute_message(dispute_id, sent["message"])
        if endpoint == "provide-evidence":
            return await paypal.provide_fulfillment_evidence(
                dispute_id, carrier=sent["carrier"], tracking_number=sent["tracking_number"],
                notes=sent["notes"])
        if endpoint == "make-offer":
            return await paypal.make_dispute_offer(dispute_id, note=sent["note"],
                                                   offer_type=sent["offer_type"],
                                                   amount=sent["offer_amount"])
        return await paypal.accept_dispute_claim(dispute_id, note=sent["note"])

    @app.post("/api/disputes/sync")
    async def sync_disputes(background: BackgroundTasks):
        """The Sync disputes button. AI recommendations for new/changed disputes run after
        the response is sent."""
        try:
            result = await sync_once("manual")
        except PayPalError as err:
            raise fail(err)
        if result is None:
            raise HTTPException(409, "A sync is already running")
        counts, changed = result
        for dispute_id in changed:
            background.add_task(assistant.auto_recommend, dispute_id)
        return counts

    @app.get("/api/disputes/sync/status")
    async def sync_status():
        return sync_state

    async def auto_sync() -> None:
        """One auto-sync run (the scheduler catches and logs any error)."""
        result = await sync_once("auto")
        for dispute_id in (result[1] if result else []):
            await assistant.auto_recommend(dispute_id)

    app.state.auto_sync = auto_sync  # lets tests run one scheduler tick without sleeping

    async def sync_once(trigger: str) -> tuple[dict, list[str]] | None:
        """Run one dispute sync unless one is already running (then return None). Returns the
        counts and the IDs of disputes that are new or changed."""
        if sync_lock.locked():
            log.info("Dispute sync (%s) skipped: another sync is still running", trigger)
            return None
        async with sync_lock:
            sync_state["running"] = True
            try:
                counts, changed = await pull_disputes()
            except Exception as err:
                sync_state.update(error=err.readable if isinstance(err, PayPalError) else str(err),
                                  trigger=trigger)
                raise
            finally:
                sync_state["running"] = False
            sync_state.update(last_synced_at=time.time(), trigger=trigger, counts=counts,
                              error=None)
            return counts, changed

    async def pull_disputes() -> tuple[dict, list[str]]:
        """Pull disputes from PayPal and record the ones that are new or changed since we last
        stored them (same recording path as webhooks; unchanged ones add no rows)."""
        items = await paypal.list_disputes()
        counts = {"listed": len(items), "new": 0, "changed": 0, "unchanged": 0, "failed": 0}
        changed = []
        for item in items:
            dispute_id = item.get("dispute_id")
            known = store.latest_dispute_state(dispute_id) if dispute_id else None
            if not dispute_id or not is_new_state(known, item):
                counts["unchanged"] += 1
                continue
            try:
                dispute = await paypal.get_dispute(dispute_id)
            except (PayPalError, httpx.HTTPError) as err:
                log.warning("Sync: could not fetch dispute %s: %s", dispute_id, err)
                counts["failed"] += 1
                continue
            if not is_new_state(known, dispute):
                counts["unchanged"] += 1
                continue
            store_dispute(snapshot_kind(known, dispute), dispute, dispute_id=dispute_id,
                          extra={"source": "sync"})
            counts["changed" if known else "new"] += 1
            changed.append(dispute_id)
        return counts, changed

    # ---- Sandbox-only test helper --------------------------------------------

    @app.post("/api/disputes/{dispute_id}/sandbox/require-seller-evidence")
    async def sandbox_require_seller_evidence(dispute_id: str, background: BackgroundTasks):
        """SANDBOX ONLY: simulate PayPal asking the seller for evidence (UNDER_REVIEW ->
        WAITING_FOR_SELLER_RESPONSE). Doesn't exist at all in live mode."""
        if settings.paypal_env != "sandbox":
            raise HTTPException(404, "Not Found")
        known = store.latest_dispute_state(dispute_id)
        if not known:
            raise HTTPException(404, "Unknown dispute")
        try:
            dispute = await paypal.get_dispute(dispute_id)
        except PayPalError as err:
            raise fail(err)
        if dispute.get("status") != "UNDER_REVIEW":
            raise HTTPException(409, "Only disputes that PayPal is reviewing (UNDER_REVIEW) can "
                                     f"be moved; this one is {dispute.get('status')}.")
        order_id = known["order_id"]
        base = {"dispute_id": dispute_id, "action": "SELLER_EVIDENCE",
                "from_status": dispute.get("status")}
        try:
            await paypal.sandbox_require_seller_evidence(dispute_id)
        except PayPalError as err:
            store.add(order_id, "sandbox_status_change_failed",
                      {**base, "error": err.readable, **error_details(err)})
            raise HTTPException(502, err.readable)
        store.add(order_id, "sandbox_status_changed", base)
        try:
            fresh = await paypal.get_dispute(dispute_id)
        except PayPalError as err:
            raise fail(err)
        current = store.latest_dispute_state(dispute_id)
        if is_new_state(current, fresh):
            store_dispute(snapshot_kind(current, fresh), fresh, dispute_id=dispute_id,
                          extra={"source": "sandbox"})
        # The normal rules decide whether an AI recommendation runs for the new state.
        background.add_task(assistant.auto_recommend, dispute_id)
        return {"status": fresh.get("status")}

    @app.get("/api/orders")
    async def list_orders():
        return store.orders()

    @app.get("/api/orders/{order_id}/timeline")
    async def timeline(order_id: str):
        events = store.timeline(order_id)
        if not events:
            raise HTTPException(404, "No such order")
        return events

    return app
# Run with: uvicorn app.main:create_app --factory --reload
