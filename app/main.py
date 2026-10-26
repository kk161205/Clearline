import logging
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import Settings, get_settings
from .paypal import (PayPalClient, PayPalError, capture_id_from, dispute_summary,
                     disputed_capture_ids, parent_capture_id)
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


def create_app(settings: Settings | None = None,
               http: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    paypal = PayPalClient(settings, http)
    store = TimelineStore(settings.database_path)
    app = FastAPI(title="Clearline", version="0.1.0")

    def fail(err: PayPalError) -> HTTPException:
        return HTTPException(status_code=502, detail={"paypal_status": err.status,
                                                       "paypal_error": err.body})

    @app.get("/", include_in_schema=False)
    async def buyer_page():
        return FileResponse(STATIC / "buyer.html")

    @app.get("/seller", include_in_schema=False)
    async def seller_page():
        return FileResponse(STATIC / "seller.html")

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
        payload = {**dispute_summary(dispute), "event_id": event.get("id")}
        payload["dispute_id"] = payload["dispute_id"] or dispute_id
        if error:
            payload["enrichment_error"] = error
        order_id = next((o for o in map(store.find_order_by_capture,
                                        disputed_capture_ids(dispute)) if o), None)
        store.add(order_id or f"unmatched:{dispute_id}", kind, payload)

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
