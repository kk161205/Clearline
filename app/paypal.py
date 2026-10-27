"""Thin async client for the PayPal REST APIs Clearline uses.

We call REST directly with httpx instead of the official Python Server SDK,
because that SDK only covers Orders, Payments, Vault, Transaction Search and
Subscriptions. Clearline also needs Disputes, Invoicing, Tracking and Webhooks,
so one client for everything keeps the code consistent.
"""

import json as jsonlib
import logging
import time
import uuid
from collections.abc import Mapping

import httpx

from .config import Settings

log = logging.getLogger("clearline.paypal")


class PayPalError(Exception):
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        super().__init__(f"PayPal API error {status}: {body}")

    @property
    def issues(self) -> list[str]:
        """PayPal's machine-readable issue codes, e.g. ACTION_NOT_ALLOWED_IN_CURRENT_DISPUTE_STATE."""
        details = self.body.get("details", []) if isinstance(self.body, dict) else []
        return [d.get("issue") for d in details if isinstance(d, dict) and d.get("issue")]

    @property
    def debug_id(self) -> str | None:
        return self.body.get("debug_id") if isinstance(self.body, dict) else None


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self.settings = settings
        self.http = http or httpx.AsyncClient(base_url=settings.api_base, timeout=30)
        self._token: str | None = None
        self._token_expires_at = 0.0

    async def _access_token(self) -> str:
        # Reuse the token until 60s before it expires.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = await self.http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self.settings.paypal_client_id, self.settings.paypal_client_secret),
        )
        if resp.status_code != 200:
            raise PayPalError(resp.status_code, _body(resp))
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 0))
        return self._token

    async def _request(self, method: str, path: str, *, json: dict | None = None,
                       idempotent: bool = False, params: dict | None = None,
                       json_part: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        if idempotent:
            # PayPal-Request-Id makes retries safe for calls that move money.
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        files = None
        if json_part is not None:
            # Some dispute actions only accept multipart/form-data: the JSON goes in a part
            # named "input" (no file attached).
            files = {"input": (None, jsonlib.dumps(json_part), "application/json")}
        resp = await self.http.request(method, path, json=json, headers=headers,
                                       params=params, files=files)
        if resp.status_code >= 400:
            raise PayPalError(resp.status_code, _body(resp))
        return resp.json() if resp.content else {}

    # ---- Orders -----------------------------------------------------------

    async def create_order(self, *, amount: str, currency: str, description: str,
                           reference_id: str) -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id,
                "description": description,
                "amount": {"currency_code": currency, "value": amount},
            }],
        }
        return await self._request("POST", "/v2/checkout/orders", json=body, idempotent=True)

    async def capture_order(self, order_id: str) -> dict:
        return await self._request("POST", f"/v2/checkout/orders/{order_id}/capture",
                                   json={}, idempotent=True)

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/v2/checkout/orders/{order_id}")

    async def add_tracking(self, order_id: str, *, capture_id: str, tracking_number: str,
                           carrier: str, notify_payer: bool = False) -> dict:
        body = {
            "capture_id": capture_id,
            "tracking_number": tracking_number,
            "carrier": carrier,
            "notify_payer": notify_payer,
        }
        return await self._request("POST", f"/v2/checkout/orders/{order_id}/track",
                                   json=body, idempotent=True)

    # ---- Webhooks & disputes ----------------------------------------------

    async def verify_webhook_signature(self, headers: Mapping[str, str], event: dict) -> bool:
        """Ask PayPal whether a webhook really came from PayPal for our webhook ID.

        `headers` are the incoming request headers (names matched case-insensitively),
        `event` is the parsed JSON body. Returns True only on verification_status SUCCESS;
        missing headers or a PayPal error count as not verified.
        """
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": self.settings.paypal_webhook_id,
        }
        if not all(body.values()):
            log.warning("Webhook is missing PayPal signature headers or webhook ID")
            return False
        body["webhook_event"] = event
        try:
            result = await self._request("POST", "/v1/notifications/verify-webhook-signature",
                                         json=body)
        except (PayPalError, httpx.HTTPError) as err:
            log.warning("Webhook signature verification call failed: %s", err)
            return False
        return result.get("verification_status") == "SUCCESS"

    async def get_dispute(self, dispute_id: str) -> dict:
        """Fetch the full dispute record (reason, amount, deadline, disputed transactions)."""
        return await self._request("GET", f"/v1/customer/disputes/{dispute_id}")

    async def list_disputes(self, *, max_pages: int = 10) -> list[dict]:
        """Dispute summaries from the last 180 days (PayPal's default window), all pages."""
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            data = await self._request("GET", "/v1/customer/disputes",
                                       params={"page_size": 50, "page": page})
            items += data.get("items", [])
            if not any(link.get("rel") == "next" for link in data.get("links", [])):
                break
        return items

    # Dispute actions. Only ever called after the seller clicks Approve.

    async def send_dispute_message(self, dispute_id: str, message: str) -> dict:
        """Message the buyer (INQUIRY stage only)."""
        return await self._request("POST", f"/v1/customer/disputes/{dispute_id}/send-message",
                                   json_part={"message": message}, idempotent=True)

    async def provide_fulfillment_evidence(self, dispute_id: str, *, carrier: str,
                                           tracking_number: str, notes: str) -> dict:
        """Submit proof of fulfillment (shipment tracking) for the dispute."""
        evidence = {
            "evidence_type": "PROOF_OF_FULFILLMENT",
            "evidence_info": {"tracking_info": [{"carrier_name": carrier,
                                                 "tracking_number": tracking_number}]},
        }
        if notes:
            evidence["notes"] = notes
        return await self._request("POST", f"/v1/customer/disputes/{dispute_id}/provide-evidence",
                                   json_part={"evidences": [evidence]}, idempotent=True)

    async def make_dispute_offer(self, dispute_id: str, *, note: str, offer_type: str,
                                 amount: dict) -> dict:
        """Offer the buyer a resolution, e.g. a partial refund (INQUIRY stage only)."""
        body = {"note": note, "offer_type": offer_type, "offer_amount": amount}
        return await self._request("POST", f"/v1/customer/disputes/{dispute_id}/make-offer",
                                   json=body, idempotent=True)

    async def accept_dispute_claim(self, dispute_id: str, *, note: str) -> dict:
        """Accept the claim with a full refund. PayPal refunds the whole disputed amount;
        refund_amount must be omitted for accept_claim_type REFUND."""
        body = {"accept_claim_type": "REFUND"}
        if note:
            body["note"] = note
        return await self._request("POST", f"/v1/customer/disputes/{dispute_id}/accept-claim",
                                   json_part=body, idempotent=True)


def _body(resp: httpx.Response) -> dict | str:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def capture_id_from(order: dict) -> str | None:
    """Pull the capture ID out of a captured order (needed for tracking, refunds, disputes)."""
    for unit in order.get("purchase_units", []):
        for capture in unit.get("payments", {}).get("captures", []):
            return capture.get("id")
    return None


def dispute_summary(dispute: dict) -> dict:
    """The dispute fields the seller page needs, from a dispute record or webhook resource."""
    amount = dispute.get("dispute_amount") or {}
    return {
        "dispute_id": dispute.get("dispute_id"),
        "reason": dispute.get("reason"),
        "status": dispute.get("status"),
        "dispute_life_cycle_stage": dispute.get("dispute_life_cycle_stage"),
        "dispute_amount": {"value": amount.get("value"),
                           "currency_code": amount.get("currency_code")},
        "seller_response_due_date": dispute.get("seller_response_due_date"),
        "update_time": dispute.get("update_time"),
        "allowed_actions": allowed_actions(dispute),
        "allowed_response_options": dispute.get("allowed_response_options") or {},
        "requested_evidence": [e.get("evidence_type") for e in dispute.get("evidences", [])
                               if e.get("source") == "REQUESTED_FROM_SELLER"],
        "buyer_messages": [{"time_posted": m.get("time_posted"), "content": m.get("content")}
                           for m in dispute.get("messages", []) if m.get("posted_by") == "BUYER"],
    }


def allowed_actions(dispute: dict) -> list[str]:
    """Actions PayPal currently offers on a dispute, from its HATEOAS links
    (e.g. ["send-message", "make-offer", "accept-claim", "provide-evidence"])."""
    return sorted({link["href"].rstrip("/").rsplit("/", 1)[-1]
                   for link in dispute.get("links", [])
                   if link.get("method") == "POST" and link.get("href")})


def disputed_capture_ids(dispute: dict) -> list[str]:
    """Capture IDs a dispute is about (PayPal calls them seller_transaction_id)."""
    return [t["seller_transaction_id"] for t in dispute.get("disputed_transactions", [])
            if t.get("seller_transaction_id")]


def parent_capture_id(resource: dict) -> str | None:
    """Capture a refund/reversal belongs to, taken from its rel="up" link."""
    for link in resource.get("links", []):
        href = link.get("href", "")
        if link.get("rel") == "up" and "/captures/" in href:
            return href.rstrip("/").rsplit("/", 1)[-1]
    return None
