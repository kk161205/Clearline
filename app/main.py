import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from urllib.parse import urlparse
from contextlib import asynccontextmanager, suppress
from importlib.metadata import version as package_version
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ValidationError
from starlette.routing import Route

from .agents import BUYER_TOOLS, AgentDeps, McpGuard, build_mcp
from .ai import AIClient, AIError
from .assistant import Assistant, strip_pii
from .accounts import (GENERIC_LOGIN_ERROR, SESSION_ABSOLUTE, SESSION_COOKIE, Accounts,
                       new_setup_code, setup_code_matches)
from .auth import RateLimiter, is_public
from .buyer import (REFUND_REASONS, SummaryCache, answered_before_dispute, buyer_token,
                    buyer_view, order_for_token, refund_totals)
from .config import Settings, get_settings
from .dashboard import build as build_dashboard
from .logsafe import configure_logging, safe
from .insights_ai import (ALREADY_HAVE, TOO_MANY_TURNS, InsightsTurn, SignatureCache,
                          canonical_call, chat_messages, chat_tools, earlier_results,
                          for_provider, turn_cap, turn_summary, turns_used)
from .insights_ai import reply as insights_reply
from .invoicing import (EXTRACT_PROMPT, EXTRACT_PROMPT_VERSION, EXTRACT_SCHEMA,
                        OFFICIAL_SOURCES, PAID, PURPOSE_CODES, PURPOSE_PROMPT,
                        PURPOSE_PROMPT_VERSION, PURPOSE_SCHEMA, REMINDER_PROMPT,
                        REMINDER_PROMPT_VERSION, REMINDER_SCHEMA, InvoiceDraft,
                        PurposeSuggestion, ReminderDraft, days_overdue, display_status,
                        due_date_from, extract_problems, invoice_body, invoice_summary,
                        reminder_facts, reminder_problems, state_signature, text_currency)
from .evidence import (DOCUMENT_TYPES, MAX_BASE64_CHARS, EvidenceFile, EvidenceFileError,
                       document_evidence_types, parse_upload)
from .policy import POLICY_KEY, TRIGGER, NotInPolicy, Policy, day_start, decide, spent_today
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
    # Day 7: more printed products, so the demo dashboard has varied amounts. The checkout
    # page still sells the brand kit.
    "poster-set": {"description": "Set of three art prints, shipped from India",
                   "amount": "60.00", "currency": "USD"},
    "logo-refresh": {"description": "Logo refresh with printed style sheet, shipped from India",
                     "amount": "85.00", "currency": "USD"},
    "brand-guide": {"description": "Bound brand guidelines book, shipped from India",
                    "amount": "240.00", "currency": "USD"},
}


# Insights layouts are kept per seller account (insights_layouts.user_key = the user's id).
MAX_LAYOUT_BYTES = 256 * 1024
MAX_AI_TURN_BYTES = 1024 * 1024

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


class EvidenceIn(BaseModel):
    """A document the seller attached (claim stage). Checked on the server, never stored."""
    type: str
    file_name: str = Field(max_length=255)
    content_base64: str = Field(max_length=MAX_BASE64_CHARS)


class ActIn(BaseModel):
    """The seller's approved action. Texts default to the latest AI draft if left out."""
    action: Literal["FIGHT", "REFUND", "OFFER"]
    message_to_buyer: str | None = None
    note_to_paypal: str | None = None
    offer: OfferIn | None = None
    evidence: EvidenceIn | None = None


class BuyerRequestIn(BaseModel):
    """One tap on the buyer page. The message is stored PII-stripped."""
    type: Literal["unrecognised", "not_arrived", "refund"]
    reason: Literal["not_received", "damaged", "not_as_described", "changed_mind", "other"] | None = None
    message: str = Field(default="", max_length=1000)


class ReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


class RefundIn(BaseModel):
    """Seller-approved refund. "full" refunds whatever is left; "partial" needs an amount."""
    mode: Literal["full", "partial"]
    amount: str | None = None
    request_id: str | None = None


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class SetupIn(BaseModel):
    code: str = Field(max_length=40)
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)
    confirm: str = Field(max_length=200)


class PasswordChangeIn(BaseModel):
    current: str = Field(max_length=200)
    password: str = Field(max_length=200)
    confirm: str = Field(max_length=200)


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class ExtractIn(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class InvoiceItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1_000_000)
    unit_amount: float = Field(gt=0, le=10_000_000)


class CreateInvoiceIn(BaseModel):
    """The seller-reviewed draft. The client email is always typed by the seller."""
    client_name: str = Field(min_length=1, max_length=300)
    email: str = Field(min_length=3, max_length=254)
    currency: Literal["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"]
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=20)
    due_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    note: str | None = Field(default=None, max_length=1000)


class ReminderIn(BaseModel):
    subject: str = Field(min_length=3, max_length=100)
    note: str = Field(min_length=10, max_length=600)


class PurposeIn(BaseModel):
    code: Literal["P0104", "P0301", "P0801", "P0802", "P0803", "P0805", "P0806", "P0902",
                  "P1004", "P1005", "P1006", "P1007", "P1008", "P1009"]


class FiraIn(BaseModel):
    downloaded: bool


EXPORT_DISCLAIMER = "Guidance, not legal or tax advice."
EMAIL_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")
INVOICE_EVENTS = {"INVOICING.INVOICE.CREATED", "INVOICING.INVOICE.UPDATED",
                  "INVOICING.INVOICE.SENT", "INVOICING.INVOICE.SCHEDULED",
                  "INVOICING.INVOICE.PAID", "INVOICING.INVOICE.UNPAID",
                  "INVOICING.INVOICE.CANCELLED", "INVOICING.INVOICE.REFUNDED",
                  "INVOICING.INVOICE.AUTOMATIC-REMINDER-SENT"}


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
                offer: dict | None, tracking: dict | None,
                document: tuple[str, EvidenceFile] | None = None) -> list[tuple[str, dict]]:
    """The PayPal calls an approved action needs, in order, as (endpoint, body) steps.

    Planned against a freshly fetched dispute; each step is checked again right before it
    runs, because one step can change what the next is allowed to do (sending a message moves
    the dispute to WAITING_FOR_BUYER_RESPONSE). Raises ActionNotAllowed rather than guessing,
    and ValueError for missing or invalid seller input.
    """
    options = dispute.get("allowed_response_options") or {}

    if action == "FIGHT" and (doc_types := document_evidence_types(dispute)):
        # Claim stage: PayPal wants a document; tracking alone is rejected
        # (EVIDENCE_TYPE_IS_NOT_ALLOWED in our Sandbox experiment).
        if blocked := step_blocked("provide-evidence", dispute):
            raise ActionNotAllowed(f"Can't send evidence: {blocked}.")
        if not document:
            raise ValueError("PayPal needs a document for this claim. Attach one (PDF, JPG, PNG "
                             "or GIF under 5 MB) and choose the matching evidence type.")
        evidence_type, file = document
        if evidence_type not in doc_types:
            raise ValueError(f"PayPal isn't asking for {evidence_type}. Choose one of: "
                             f"{', '.join(doc_types)}.")
        return [("provide-evidence", {"evidence_type": evidence_type, "notes": note,
                                      "document": file.meta})]
    if document:
        raise ValueError("Documents are only sent when PayPal asks for them (claim stage).")

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
    configure_logging(settings.log_level)
    paypal = PayPalClient(settings, http)
    store = TimelineStore(settings.database_path)
    ai = AIClient(settings, store, ai_http)
    assistant = Assistant(store, ai)
    secret = settings.app_secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        log.warning("APP_SECRET is not set: using a random one for now. Buyer links (/o/...) "
                    "will stop working after a restart. Set APP_SECRET in .env.")
    for name, value in (("SELLER_PASSWORD", settings.legacy_seller_password),
                        ("MERCHANT_API_KEY", settings.legacy_merchant_api_key)):
        if value is not None:  # by name only, never the value
            log.warning("%s is no longer used; remove it from your environment.", name)
    accounts = Accounts(store)
    # The first account is made at /setup with a one-time code from this log line. Memory only,
    # new on every start, useless once an account exists.
    setup = {"code": None}
    if store.count_users() == 0:
        setup["code"] = new_setup_code()
        log.warning("No seller account yet. Open /setup and use setup code: %s", setup["code"])
    setup_lock = asyncio.Lock()

    def needs_setup() -> bool:
        """True while there's no account yet. An account made any other way (the CLI) ends
        setup too: the code is dropped and never valid again."""
        if setup["code"] and store.count_users():
            setup["code"] = None
        return bool(setup["code"])
    login_limit = RateLimiter(limit=20, window=15 * 60)  # login attempts per IP
    setup_limit = RateLimiter(limit=10, window=15 * 60)  # setup attempts per IP
    summaries = SummaryCache(store, ai)
    buyer_limits = RateLimiter(limit=5, window=3600)  # buyer submissions per link per hour
    policy_lock = asyncio.Lock()  # one auto-resolve decision at a time, so caps hold
    item_names = {k: v["description"] for k, v in CATALOGUE.items()}
    tracking_lock = asyncio.Lock()  # one "Mark shipped" at a time, so repeats are caught
    sync_lock = asyncio.Lock()
    sync_state = {"running": False, "last_synced_at": None, "trigger": None, "counts": None,
                  "error": None, "invoices": None, "auto_sync_minutes": settings.auto_sync_minutes}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Start the auto-sync safety net with the app and stop it cleanly on shutdown."""
        task = None
        if settings.auto_sync_minutes > 0:
            task = asyncio.create_task(run_every(settings.auto_sync_minutes * 60, auto_sync))
        app.state.auto_sync_task = task

        async def clean_sessions():
            removed = accounts.cleanup()
            if removed:
                log.info("Removed %d expired seller sessions", removed)

        cleanup = asyncio.create_task(run_every(3600, clean_sessions))  # hourly
        try:
            async with mcp_server.session_manager.run():  # the MCP endpoint needs this running
                yield
        finally:
            for t in (task, cleanup):
                if t:
                    t.cancel()
                    with suppress(asyncio.CancelledError):
                        await t

    app = FastAPI(title="Clearline", version="0.1.0", lifespan=lifespan)

    def fail(err: PayPalError) -> HTTPException:
        return HTTPException(status_code=502, detail={"paypal_status": err.status,
                                                       "paypal_error": err.body})

    # ---- Seller accounts: everything not public needs a signed-in seller ------------

    def client_ip(request: Request) -> str:
        """The client's IP: the first X-Forwarded-For hop only behind a trusted proxy."""
        if settings.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            if forwarded:
                return forwarded
        return request.client.host if request.client else "unknown"

    def is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return settings.trust_proxy and request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"

    def request_host(request: Request) -> str:
        if settings.trust_proxy and request.headers.get("x-forwarded-host"):
            return request.headers["x-forwarded-host"].split(",")[0].strip()
        return request.headers.get("host", "")

    def foreign_origin(request: Request) -> bool:
        """A state-changing request whose Origin names another site (the browser always sends
        Origin on cross-site POST/PUT/PATCH/DELETE). No Origin header: not a browser, allowed."""
        origin = request.headers.get("origin")
        if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not origin:
            return False
        return urlparse(origin).netloc.lower() != request_host(request).lower()

    def start_session(response: Response, request: Request, user_id: int) -> None:
        accounts.end_session(request.cookies.get(SESSION_COOKIE))  # rotate: the old one ends
        response.set_cookie(SESSION_COOKIE, accounts.start_session(user_id), max_age=SESSION_ABSOLUTE,
                            httponly=True, samesite="lax", secure=is_https(request), path="/")

    @app.middleware("http")
    async def require_seller(request: Request, call_next):
        """Deny by default. Public: checkout, buyer page + API, webhooks, login, setup, the MCP
        endpoint (its own Bearer and order-link checks). Everything else needs a live session."""
        path = request.url.path
        if is_public(request.method, path):
            if path in ("/api/login", "/api/setup", "/api/logout") and foreign_origin(request):
                return JSONResponse({"detail": "Request blocked: wrong origin."}, status_code=403)
            return await call_next(request)
        found = accounts.session_user(request.cookies.get(SESSION_COOKIE))
        if not found:
            if request.method == "GET" and not path.startswith("/api/"):
                return RedirectResponse("/setup" if needs_setup() else "/login", status_code=303)
            return JSONResponse({"detail": "Log in as the seller first."}, status_code=401)
        if foreign_origin(request):
            return JSONResponse({"detail": "Request blocked: wrong origin."}, status_code=403)
        request.state.user, request.state.session_id = found
        return await call_next(request)

    def current_user(request: Request) -> dict:
        return request.state.user

    @app.get("/login", include_in_schema=False)
    async def login_page():
        if needs_setup():
            return RedirectResponse("/setup", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})

    @app.post("/api/login")
    async def login(body: LoginIn, request: Request, response: Response):
        ip = client_ip(request)
        if login_limit.blocked(ip):
            log.warning("Login blocked: too many attempts from %s", ip)
            raise HTTPException(429, "Too many attempts, try again later.")
        login_limit.hit(ip)
        user, outcome = accounts.authenticate(body.email, body.password)
        who = user["id"] if user else "unknown"
        if outcome != "ok":
            if outcome == "locked":
                log.warning("Login failed: user %s is locked (from %s)", who, ip)
            else:
                log.warning("Login failed: user %s (%s) from %s", who, outcome, ip)
            raise HTTPException(401, GENERIC_LOGIN_ERROR)
        start_session(response, request, user["id"])
        log.info("Login ok: user %s from %s", who, ip)
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        accounts.end_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @app.get("/setup", include_in_schema=False)
    async def setup_page():
        if not needs_setup():
            raise HTTPException(404, "Not Found")
        return FileResponse(STATIC / "setup.html", headers={"Cache-Control": "no-cache"})

    @app.post("/api/setup")
    async def create_first_account(body: SetupIn, request: Request, response: Response):
        """The first seller account, with the setup code from the server log. Only while there
        are no accounts; afterwards this doesn't exist (404)."""
        ip = client_ip(request)
        async with setup_lock:
            if not needs_setup():
                raise HTTPException(404, "Not Found")
            if setup_limit.blocked(ip):
                raise HTTPException(429, "Too many attempts, try again later.")
            setup_limit.hit(ip)
            if not setup_code_matches(body.code, setup["code"]):
                log.warning("Setup failed: wrong setup code from %s", ip)
                raise HTTPException(403, "That setup code isn't right. Check the server log.")
            if body.password != body.confirm:
                raise HTTPException(422, "The passwords don't match.")
            try:
                user_id = accounts.create_user(body.email, body.password)
            except ValueError as err:
                raise HTTPException(422, str(err))
            setup["code"] = None  # used: never valid again, never logged again
        start_session(response, request, user_id)
        log.info("Setup: first seller account created, user %s from %s", user_id, ip)
        return {"ok": True}

    @app.get("/api/account")
    async def account(request: Request):
        user = current_user(request)
        return {"id": user["id"], "email": user["email"]}

    @app.post("/api/account/password")
    async def change_password(body: PasswordChangeIn, request: Request):
        """New password, with the current one. Every other session of this account ends."""
        user = current_user(request)
        if not accounts.check_password(user, body.current):
            log.warning("Password change refused: wrong current password, user %s", user["id"])
            raise HTTPException(403, "Your current password isn't right.")
        if body.password != body.confirm:
            raise HTTPException(422, "The new passwords don't match.")
        try:
            accounts.set_password(user["id"], body.password, keep_session=request.state.session_id)
        except ValueError as err:
            raise HTTPException(422, str(err))
        log.info("Password changed: user %s; other sessions ended", user["id"])
        return {"ok": True}

    @app.post("/api/account/sign-out-everywhere")
    async def sign_out_everywhere(request: Request, response: Response):
        user = current_user(request)
        ended = store.delete_user_sessions(user["id"])
        response.delete_cookie(SESSION_COOKIE, path="/")
        log.info("Signed out everywhere: user %s, %d sessions ended", user["id"], ended)
        return {"ok": True}

    # ---- Agent access: API keys for the read-only merchant MCP tools ----------------

    @app.get("/api/agent-keys")
    async def list_agent_keys(request: Request):
        return [{**k, "revoked": bool(k["revoked_at"])} for k in store.api_keys(current_user(request)["id"])]

    @app.post("/api/agent-keys")
    async def create_agent_key(body: ApiKeyIn, request: Request):
        """A new key, shown once: only its SHA-256 is stored."""
        user = current_user(request)
        created = accounts.create_api_key(user["id"], body.name)
        log.info("Agent key %s created by user %s", created["id"], user["id"])
        return created

    @app.delete("/api/agent-keys/{key_id}")
    async def revoke_agent_key(key_id: int, request: Request):
        user = current_user(request)
        if not store.revoke_api_key(user["id"], key_id, time.time()):
            raise HTTPException(404, "No such key")
        log.info("Agent key %s revoked by user %s", key_id, user["id"])
        return {"ok": True}

    # no-cache: browsers revalidate every load, so a new page version shows up right away.
    @app.get("/", include_in_schema=False)
    async def buyer_page():
        return FileResponse(STATIC / "buyer.html", headers={"Cache-Control": "no-cache"})

    @app.get("/seller", include_in_schema=False)
    async def seller_page():
        return FileResponse(STATIC / "seller.html", headers={"Cache-Control": "no-cache"})

    # The shared design system (Day 7) and its self-hosted font. Only these files are served.
    static_files = {"ui.css": None, "ui.js": None, "history.js": None,
                    "vendor/ag-grid-community-36.2.0.min.js": None,
                    "vendor/ag-charts-community-14.2.0.min.js": None,
                    "fonts/instrument-sans-latin.woff2": "font/woff2",
                    "fonts/instrument-sans-latin-ext.woff2": "font/woff2"}

    @app.get("/static/{name:path}", include_in_schema=False)
    async def static_file(name: str):
        if name not in static_files:
            raise HTTPException(404, "Not Found")
        # Fonts and the versioned libraries never change under the same name.
        cache = "public, max-age=604800" if name.startswith(("fonts/", "vendor/")) else "no-cache"
        return FileResponse(STATIC / name, media_type=static_files[name],
                            headers={"Cache-Control": cache})

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
        return {"status": order.get("status"), "captureId": capture_id,
                "buyerLink": buyer_link(order_id)}

    @app.post("/api/orders/{order_id}/tracking")
    async def add_tracking(order_id: str, body: TrackingIn):
        async with tracking_lock:  # a double click waits here, then sees the first one's row
            events = store.timeline(order_id)
            captured = [e for e in events if e["kind"] == "payment_captured"]
            if not captured:
                raise HTTPException(409, "Order has not been captured yet")
            if any(e["kind"] == "tracking_added"
                   and e["payload"].get("carrier") == body.carrier
                   and e["payload"].get("tracking_number") == body.tracking_number
                   for e in events):
                return {"ok": True, "duplicate": True}  # same shipment again: nothing to do
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

    # ---- Buyer clarity page (Day 4): public, by unguessable link ------------

    def buyer_link(order_id: str) -> str:
        return f"/o/{buyer_token(secret, order_id)}"

    def order_from_token(token: str) -> str:
        order_id = order_for_token(store, secret, token)
        if not order_id:
            raise HTTPException(404, "Not Found")  # unknown, forged and stale links look the same
        return order_id

    @app.get("/o/{token}", include_in_schema=False)
    async def buyer_order_page(token: str):
        order_from_token(token)
        return FileResponse(STATIC / "order.html",
                            headers={"Cache-Control": "no-cache", "Referrer-Policy": "no-referrer",
                                     "X-Robots-Tag": "noindex"})

    @app.get("/api/o/{token}")
    async def buyer_order(token: str, background: BackgroundTasks):
        """Everything the buyer page shows. Answers from stored facts only: never waits on the
        AI. The AI summary is made lazily, in the background, the first time the buyer looks
        at a given order state (so only orders whose buyers actually visit cost an AI call)."""
        order_id = order_from_token(token)
        page, facts = buyer_view(store, summaries, order_id,
                                 seller_name=settings.seller_display_name, item_names=item_names)
        if page["summary"]["source"] == "template" and summaries.needs_refresh(order_id, facts):
            background.add_task(summaries.refresh, order_id, facts)
        return page

    @app.get("/api/o/{token}/status")
    async def buyer_order_status(token: str):
        """Minimal "is it paid?" for the checkout page's restore (no timeline needed)."""
        order_id = order_from_token(token)
        return {"paid": True, "orderRef": order_id[-6:], "buyerLink": buyer_link(order_id)}

    @app.post("/api/o/{token}/requests")
    async def buyer_request(token: str, body: BuyerRequestIn):
        order_id = order_from_token(token)
        result = await submit_request(order_id, token=token, type=body.type, reason=body.reason,
                                      message=body.message, amount=None, via="page")
        return {"ok": True, **result}

    async def submit_request(order_id: str, *, token: str, type: str, reason: str | None,
                             message: str, amount: Decimal | None, via: str) -> dict:
        """Store one buyer request (from the buyer page or a buyer's AI agent) and, for a
        refund request, apply the seller's auto-resolve policy. The message is untrusted text:
        stored PII-stripped, shown escaped, never acted on."""
        if buyer_limits.blocked(token):
            raise HTTPException(429, "You've sent several messages already. Please wait for the "
                                     "seller's reply.")
        if type == "refund" and not reason:
            raise HTTPException(400, "Choose a reason for the refund.")
        buyer_limits.hit(token)
        request_id = uuid.uuid4().hex[:12]
        text = strip_pii(message.strip())
        payload = {"request_id": request_id, "type": type,
                   "reason": reason if type == "refund" else None, "message": text, "via": via}
        if amount is not None:
            payload["amount"] = f"{amount:.2f}"
        store.add(order_id, "buyer_request", payload)
        result = {"request_id": request_id, "status": "sent_to_seller", "message": text}
        if type == "refund":
            if refund := await auto_resolve(order_id, request_id, reason, amount):
                result.update(status="refunded", refund=refund)
        return result

    # ---- Auto-resolve policy (Day 6): off by default, set by the seller -------------

    def load_policy() -> Policy:
        saved = store.get_setting(POLICY_KEY)
        return Policy.model_validate(saved) if saved else Policy()

    def auto_spent_today() -> Decimal:
        return spent_today(store.events_since("refund_sent", day_start(time.time())))

    async def auto_resolve(order_id: str, request_id: str, reason: str | None,
                           requested: Decimal | None) -> dict | None:
        """Refund right away if the request fits the seller's policy; None = the seller decides.
        Every check runs before the PayPal call, under a lock so two requests can't both fit
        the daily cap; a PayPal failure is recorded and the request stays with the seller."""
        policy = load_policy()
        if not policy.enabled:
            return None
        async with policy_lock:
            try:
                amount, rule = decide(policy, store.timeline(order_id), reason=reason,
                                      requested=requested, spent=auto_spent_today())
            except NotInPolicy as why:
                store.add(order_id, "auto_policy_skipped", {"request_id": request_id,
                                                            "reason": str(why)})
                return None
            try:
                sent = await send_refund(order_id, mode="full" if requested is None else "partial",
                                         amount=amount, request_id=request_id, trigger=TRIGGER,
                                         rule=rule, explicit=True)
            except HTTPException:
                return None  # refund_failed is on the timeline; the seller takes it from here
        store.add(order_id, "seller_reply",
                  {"request_id": request_id, "auto": True,
                   "message": f"Your refund of {sent['amount']} {sent['currency']} has been sent "
                              f"through PayPal. It can take a few days to show on your statement."})
        return {k: sent[k] for k in ("amount", "currency", "status")}

    @app.get("/api/policy")
    async def get_policy():
        return {**load_policy().model_dump(mode="json"), "spent_today": f"{auto_spent_today():.2f}",
                "reasons": REFUND_REASONS}

    @app.put("/api/policy")
    async def set_policy(body: Policy):
        saved = body.model_dump(mode="json")
        store.set_setting(POLICY_KEY, saved)
        store.add("other", "auto_policy_updated", saved)
        return await get_policy()

    # Seller side of buyer requests (login required)

    @app.post("/api/orders/{order_id}/requests/{request_id}/reply")
    async def reply_to_buyer(order_id: str, request_id: str, body: ReplyIn):
        if not any(e["kind"] == "buyer_request" and e["payload"].get("request_id") == request_id
                   for e in store.timeline(order_id)):
            raise HTTPException(404, "No such buyer request")
        store.add(order_id, "seller_reply", {"request_id": request_id,
                                             "message": body.message.strip()})
        return {"ok": True}

    @app.post("/api/orders/{order_id}/refund")
    async def refund_order(order_id: str, body: RefundIn):
        """Seller-approved refund of the order's capture."""
        amount = None
        if body.mode == "partial":
            try:
                amount = Decimal(body.amount or "").quantize(Decimal("0.01"))
            except InvalidOperation:
                raise HTTPException(400, "Enter the refund amount, e.g. 40.00")
        sent = await send_refund(order_id, mode=body.mode, amount=amount,
                                 request_id=body.request_id, trigger="seller")
        return {"ok": True, **{k: sent[k] for k in ("status", "amount", "currency")}}

    async def send_refund(order_id: str, *, mode: str, amount: Decimal | None,
                          request_id: str | None, trigger: str, rule: str | None = None,
                          explicit: bool = False) -> dict:
        """Refund the order's capture (the seller's click, or the auto-resolve policy). Amount
        checks happen before any PayPal call; PayPal's answer is recorded either way and
        nothing is retried. `explicit` always names the amount, even for a full refund, so
        PayPal can never refund more than was checked here."""
        events = store.timeline(order_id)
        captured_events = [e for e in events if e["kind"] == "payment_captured"]
        if not captured_events or not captured_events[-1]["payload"].get("capture_id"):
            raise HTTPException(409, "This order has no captured payment to refund.")
        capture_id = captured_events[-1]["payload"]["capture_id"]
        captured, refunded, currency = refund_totals(events)
        remaining = captured - refunded
        if remaining <= 0:
            raise HTTPException(409, "This order has already been fully refunded.")
        if mode == "full" and not explicit:
            amount, money = remaining, None  # empty body = "captured minus previous refunds"
        else:
            amount = remaining if mode == "full" and amount is None else amount
            if not 0 < amount <= remaining:
                raise HTTPException(400, f"The refund must be more than 0 and at most "
                                         f"{remaining} {currency} (captured {captured}, already "
                                         f"refunded {refunded}).")
            money = {"currency_code": currency, "value": f"{amount:.2f}"}
        base = {"request_id": request_id, "mode": mode, "capture_id": capture_id,
                "amount": f"{amount:.2f}", "currency": currency, "trigger": trigger}
        if rule:
            base["rule"] = rule
        try:
            result = await paypal.refund_capture(capture_id, amount=money)
        except (PayPalError, httpx.HTTPError) as err:
            store.add(order_id, "refund_failed",
                      {**base, "error": readable_error(err), **error_details(err)})
            raise HTTPException(502, readable_error(err))
        store.add(order_id, "refund_sent", {**base, "refund_id": result.get("id"),
                                            "status": result.get("status")})
        return {"status": result.get("status"), "amount": f"{amount:.2f}", "currency": currency}

    @app.get("/api/stats")
    async def stats():
        """Auto-resolved requests are buyer requests too, so they're in the first number."""
        auto = {e["order_id"] for e in store.events_since("refund_sent", 0)
                if e["payload"].get("trigger") == TRIGGER}
        return {"questions_answered_before_dispute": answered_before_dispute(store),
                "auto_resolved": len(auto)}

    # ---- Invoices (Day 5): AI drafts, the seller creates, sends and reminds ----

    def today() -> date:
        return datetime.now(timezone.utc).date()

    def latest_invoice(invoice_id: str) -> dict | None:
        rows = [e for e in store.timeline(invoice_id) if e["kind"] == "invoice_status"]
        return rows[-1]["payload"] if rows else None

    def record_invoice(summary: dict, *, source: str, event_id: str | None = None) -> bool:
        """Store an invoice snapshot on its own timeline (keyed by invoice id) if anything
        changed, and record invoice_paid once when it gets paid. Returns True if stored."""
        invoice_id = summary.get("invoice_id")
        if not invoice_id:
            return False
        known = latest_invoice(invoice_id)
        changed = not known or state_signature(known) != state_signature(summary)
        if changed:
            store.add(invoice_id, "invoice_status", {**summary, "source": source,
                                                     "event_id": event_id})
        events = store.timeline(invoice_id)
        if summary.get("status") in PAID and not any(e["kind"] == "invoice_paid" for e in events):
            store.add(invoice_id, "invoice_paid", {k: summary.get(k) for k in (
                "invoice_id", "number", "amount", "currency", "items", "paid_on",
                "recipient_country")})
        return changed

    async def record_invoice_webhook(event: dict, resource: dict) -> None:
        """INVOICING.INVOICE.* webhook: re-fetch the invoice (the webhook may be stale or
        partial) and record its current state."""
        invoice = resource.get("invoice") if isinstance(resource.get("invoice"), dict) else resource
        invoice_id = invoice.get("id")
        try:
            invoice = await paypal.get_invoice(invoice_id)
        except (PayPalError, httpx.HTTPError) as err:
            log.warning("Invoice %s webhook: re-fetch failed (%s); using the webhook copy",
                        invoice_id, safe(err))
        record_invoice(invoice_summary(invoice), source="webhook", event_id=event.get("id"))

    def invoice_row(summary: dict) -> dict:
        """One line of the seller's invoice list."""
        return {**{k: summary.get(k) for k in ("invoice_id", "number", "client", "email",
                                               "amount", "currency", "due_date", "status")},
                "display_status": display_status(summary, today()),
                "days_overdue": days_overdue(summary, today())}

    @app.get("/api/invoices")
    async def list_invoices():
        rows = [invoice_row(s) for o in store.orders() if "invoice_status" in o["kinds"]
                if (s := latest_invoice(o["order_id"]))]
        return sorted(rows, key=lambda r: r.get("number") or "", reverse=True)

    @app.post("/api/invoices/extract")
    async def extract_invoice(body: ExtractIn):
        """AI turns the seller's sentence into a draft to review. Nothing is created here.
        Emails, links and phone numbers are stripped before the AI sees the text."""
        if not ai.configured:
            raise HTTPException(503, "No AI key configured: fill in the invoice form yourself.")
        text = strip_pii(body.text)
        messages = [{"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": text}]
        problems: list[str] = []
        try:
            for _ in range(2):
                result = await ai.complete(messages, schema_name="invoice_draft",
                                           schema=EXTRACT_SCHEMA, model_cls=InvoiceDraft,
                                           prompt_version=EXTRACT_PROMPT_VERSION,
                                           allow_switch=False)
                problems = extract_problems(result.data, text)
                if not problems:
                    draft = result.data.model_dump()
                    return {**draft, "currency": draft["currency"] or text_currency(text),
                            "due_date": due_date_from(draft["due_in_days"], today()),
                            "provider": result.provider, "model": result.model}
                messages = messages + [
                    {"role": "assistant", "content": result.data.model_dump_json()},
                    {"role": "user", "content": "Fix these problems: " + "; ".join(problems)}]
        except AIError as err:
            raise HTTPException(502, f"AI unavailable ({err.reason}): fill in the form yourself.")
        raise HTTPException(422, "The AI draft didn't match your text (" + "; ".join(problems)
                                 + "). Fill in the form yourself.")

    @app.post("/api/invoices")
    async def create_invoice(body: CreateInvoiceIn):
        """Create a PayPal DRAFT invoice from the seller-reviewed form. Doesn't send it."""
        if not EMAIL_ADDRESS.fullmatch(body.email.strip()):
            raise HTTPException(400, "Enter the client's email address.")
        if body.due_date and body.due_date < today().isoformat():
            raise HTTPException(400, "The due date is in the past.")
        payload = invoice_body(client_name=body.client_name.strip(), email=body.email.strip(),
                               currency=body.currency, items=[i.model_dump() for i in body.items],
                               due_date=body.due_date, note=body.note, today=today())
        try:
            invoice = await paypal.create_draft_invoice(payload)
        except PayPalError as err:
            store.add("other", "invoice_action_failed",
                      {"action": "create", "error": err.readable, **error_details(err)})
            raise HTTPException(502, err.readable)
        summary = invoice_summary(invoice)
        record_invoice(summary, source="create")
        return invoice_row(summary)

    async def fresh_invoice(invoice_id: str) -> dict:
        if not latest_invoice(invoice_id):
            raise HTTPException(404, "Unknown invoice")
        try:
            summary = invoice_summary(await paypal.get_invoice(invoice_id))
        except PayPalError as err:
            raise fail(err)
        record_invoice(summary, source="refresh")
        return summary

    async def invoice_action(invoice_id: str, action: str, call, sent: dict) -> dict:
        """Run one seller-approved invoice call; record sent/failed; never retry."""
        try:
            await call()
        except PayPalError as err:
            store.add(invoice_id, "invoice_action_failed",
                      {"invoice_id": invoice_id, "action": action, "sent": sent,
                       "error": err.readable, **error_details(err)})
            raise HTTPException(502, err.readable)
        store.add(invoice_id, f"invoice_{action}_sent", {"invoice_id": invoice_id, **sent})
        try:
            record_invoice(invoice_summary(await paypal.get_invoice(invoice_id)), source=action)
        except PayPalError as err:
            log.warning("Invoice %s: re-fetch after %s failed: %s", invoice_id, action, safe(err))
        return invoice_row(latest_invoice(invoice_id))

    @app.post("/api/invoices/{invoice_id}/send")
    async def send_invoice(invoice_id: str):
        """Send a draft to the client (a separate click from creating it)."""
        summary = await fresh_invoice(invoice_id)
        if summary["status"] != "DRAFT":
            raise HTTPException(409, f"Only draft invoices can be sent; this one is "
                                     f"{display_status(summary, today())}.")
        return await invoice_action(invoice_id, "send",
                                    lambda: paypal.send_invoice(invoice_id), {})

    @app.post("/api/invoices/{invoice_id}/reminder-draft")
    async def draft_reminder(invoice_id: str):
        """A reminder for the seller to edit: AI-drafted (tone by days overdue), or a plain
        template if the AI is unavailable or its draft breaks the rules. Sends nothing."""
        summary = await fresh_invoice(invoice_id)
        overdue = days_overdue(summary, today())
        if not overdue:
            raise HTTPException(409, "Reminders are only for overdue invoices.")
        facts = reminder_facts(summary, settings.seller_display_name, today())
        template = {"subject": f"Reminder: invoice {summary.get('number')} is overdue",
                    "note": (f"Hello {summary.get('client') or 'there'}, this is a friendly "
                             f"reminder that invoice {summary.get('number')} for "
                             f"{summary.get('amount')} {summary.get('currency')} was due on "
                             f"{summary.get('due_date')}. Thank you!"),
                    "source": "template", "days_overdue": overdue}
        if not ai.configured:
            return template
        messages = [{"role": "system", "content": REMINDER_PROMPT},
                    {"role": "user", "content": "FACTS:\n" + json.dumps(facts, indent=1)}]
        try:
            for _ in range(2):
                result = await ai.complete(messages, schema_name="invoice_reminder",
                                           schema=REMINDER_SCHEMA, model_cls=ReminderDraft,
                                           prompt_version=REMINDER_PROMPT_VERSION,
                                           allow_switch=False)
                problems = reminder_problems(result.data, facts)
                if not problems:
                    return {**result.data.model_dump(), "source": "ai",
                            "provider": result.provider, "days_overdue": overdue}
                messages = messages + [
                    {"role": "assistant", "content": result.data.model_dump_json()},
                    {"role": "user", "content": "Fix these problems: " + "; ".join(problems)}]
            log.warning("Reminder draft for %s rejected by guardrails (%d problems)", invoice_id, len(problems))
        except AIError as err:
            log.warning("Reminder draft for %s failed (%s); using the template", invoice_id, safe(err))
        return template

    @app.post("/api/invoices/{invoice_id}/remind")
    async def remind_invoice(invoice_id: str, body: ReminderIn):
        """Send the seller-edited reminder through PayPal (overdue invoices only)."""
        summary = await fresh_invoice(invoice_id)
        overdue = days_overdue(summary, today())
        if not overdue:
            raise HTTPException(409, "Reminders are only for overdue invoices.")
        sent = {"subject": body.subject.strip(), "note": body.note.strip(), "days_overdue": overdue}
        return await invoice_action(invoice_id, "reminder", lambda: paypal.remind_invoice(
            invoice_id, subject=sent["subject"], note=sent["note"]), sent)

    # ---- India export paperwork (Day 5): guidance only, the seller decides --------

    def export_payment(order_id: str) -> dict | None:
        """A received international payment, or None (not paid, or not from abroad)."""
        events = store.timeline(order_id)
        if paid := next((e for e in reversed(events) if e["kind"] == "invoice_paid"), None):
            p = paid["payload"]
            country = p.get("recipient_country")
            if country == settings.seller_country or (not country and p.get("currency") == "INR"):
                return None
            return {"description": ", ".join(i.get("name") or "" for i in p.get("items") or []),
                    "amount": p.get("amount"), "currency": p.get("currency"), "country": country}
        captured = next((e for e in reversed(events) if e["kind"] == "payment_captured"), None)
        if not captured or captured["payload"].get("payer_country") in (None, settings.seller_country):
            return None
        created = next((e for e in events if e["kind"] == "order_created"), {"payload": {}})
        return {"description": item_names.get(created["payload"].get("item_id"), ""),
                "amount": created["payload"].get("amount"),
                "currency": created["payload"].get("currency"),
                "country": captured["payload"].get("payer_country")}

    @app.get("/api/export/{order_id}")
    async def export_checklist(order_id: str):
        """The export paperwork checklist for one received international payment."""
        if settings.seller_country != "IN":
            raise HTTPException(404, "The export checklist is for India-based sellers.")
        payment = export_payment(order_id)
        if not payment:
            raise HTTPException(404, "Not a received international payment")
        events = store.timeline(order_id)

        def last(kind: str) -> dict | None:
            return next((e["payload"] for e in reversed(events) if e["kind"] == kind), None)

        return {"payment": payment, "purpose_code": (last("export_purpose_set") or {}).get("code"),
                "suggestion": last("export_purpose_suggested"),
                "fira_downloaded": bool((last("export_fira") or {}).get("downloaded")),
                "purpose_codes": PURPOSE_CODES, "sources": OFFICIAL_SOURCES,
                "disclaimer": EXPORT_DISCLAIMER}

    @app.post("/api/export/{order_id}/suggest")
    async def suggest_purpose(order_id: str):
        """AI suggests a purpose code with a one-line reason. It's stored as a suggestion
        only; the code is never set until the seller picks it."""
        if settings.seller_country != "IN" or not (payment := export_payment(order_id)):
            raise HTTPException(404, "Not a received international payment")
        if not ai.configured:
            raise HTTPException(503, "No AI key configured: pick the code yourself.")
        facts = {"description": payment["description"], "allowed": PURPOSE_CODES}
        try:
            result = await ai.complete(
                [{"role": "system", "content": PURPOSE_PROMPT},
                 {"role": "user", "content": json.dumps(facts, indent=1)}],
                schema_name="purpose_code", schema=PURPOSE_SCHEMA, model_cls=PurposeSuggestion,
                prompt_version=PURPOSE_PROMPT_VERSION, allow_switch=False)
        except AIError as err:
            raise HTTPException(502, f"AI unavailable ({err.reason}): pick the code yourself.")
        suggestion = {**result.data.model_dump(), "provider": result.provider}
        store.add(order_id, "export_purpose_suggested", suggestion)
        return suggestion

    @app.post("/api/export/{order_id}/purpose")
    async def set_purpose(order_id: str, body: PurposeIn):
        """The seller's own choice of purpose code for this payment (a checklist record in
        Clearline; PayPal's API has no per-payment purpose code)."""
        if settings.seller_country != "IN" or not export_payment(order_id):
            raise HTTPException(404, "Not a received international payment")
        store.add(order_id, "export_purpose_set", {"code": body.code,
                                                   "label": PURPOSE_CODES[body.code]})
        return {"ok": True}

    @app.post("/api/export/{order_id}/fira")
    async def set_fira(order_id: str, body: FiraIn):
        if settings.seller_country != "IN" or not export_payment(order_id):
            raise HTTPException(404, "Not a received international payment")
        store.add(order_id, "export_fira", {"downloaded": body.downloaded})
        return {"ok": True}

    @app.get("/api/orders/{order_id}/buyer")
    async def seller_buyer_view(order_id: str):
        """The seller's view of an order's buyer page: link, requests, refunds, refundable."""
        events = store.timeline(order_id)
        if not any(e["kind"] in ("payment_captured", "invoice_paid") for e in events):
            raise HTTPException(404, "Order has not been captured yet")
        page, _ = buyer_view(store, summaries, order_id,
                             seller_name=settings.seller_display_name, item_names=item_names)
        captured, refunded, currency = refund_totals(events)
        auto = {e["payload"].get("request_id"): {k: e["payload"].get(k) for k in (
                    "amount", "currency", "rule")}
                for e in events if e["kind"] == "refund_sent"
                and e["payload"].get("trigger") == TRIGGER}
        for r in page["requests"]:
            r["auto_refund"] = auto.get(r["id"])
        return {"link": buyer_link(order_id), "requests": page["requests"],
                "refunds": page["refunds"], "summary": page["summary"],
                "refundable": f"{captured - refunded:.2f}", "currency": currency}

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
            elif event_type in INVOICE_EVENTS:
                await record_invoice_webhook(event, resource)
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
                   "allowed_decisions": allowed_decisions(dispute),
                   # Evidence types that need a seller-provided document (claim stage), with
                   # plain-word labels for the page's dropdown.
                   "document_evidence": [{"type": t, "label": DOCUMENT_TYPES.get(t, t)}
                                         for t in document_evidence_types(dispute)]}
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
        document = None
        if body.evidence:
            # Checked before any PayPal call; the bytes live only in memory for this request.
            try:
                document = (body.evidence.type, parse_upload(body.evidence.file_name,
                                                             body.evidence.content_base64))
            except EvidenceFileError as err:
                raise HTTPException(400, str(err))

        try:
            dispute = await paypal.get_dispute(dispute_id)  # never act on stale state
        except PayPalError as err:
            raise fail(err)
        try:
            steps = plan_action(body.action, dispute, message=message, note=note,
                                offer=offer, tracking=tracking, document=document)
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
                response = await run_step(dispute_id, endpoint, sent, document)
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
            log.warning("Could not re-fetch dispute %s after action: %s", dispute_id, safe(err))

        summary = step_summary(results)
        if failed:
            raise HTTPException(502, {"steps": results, "summary": summary})
        return {"ok": True, "steps": results, "summary": summary}

    async def run_step(dispute_id: str, endpoint: str, sent: dict,
                       document: tuple[str, EvidenceFile] | None = None) -> dict:
        """Call the PayPal endpoint for one planned step. `sent` is what the timeline stores;
        the document's bytes come separately so they're never stored."""
        if endpoint == "send-message":
            return await paypal.send_dispute_message(dispute_id, sent["message"])
        if endpoint == "provide-evidence" and "document" in sent:
            file = document[1]
            return await paypal.provide_document_evidence(
                dispute_id, evidence_type=sent["evidence_type"], notes=sent["notes"],
                file_name=file.name, content=file.content, content_type=file.content_type)
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
                              error=None, invoices=await pull_invoices())
            return counts, changed

    async def pull_invoices() -> dict:
        """Sync invoice statuses from PayPal (safety net for missed INVOICING webhooks).
        Never breaks the dispute sync: errors are reported in the sync status instead."""
        try:
            invoices = await paypal.list_invoices()
        except Exception as err:  # e.g. Invoicing not enabled for the app
            log.warning("Invoice sync failed: %s", safe(err))
            return {"error": err.readable if isinstance(err, PayPalError) else str(err)}
        changed = sum(record_invoice(invoice_summary(inv), source="sync") for inv in invoices)
        return {"listed": len(invoices), "changed": changed, "error": None}

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
                log.warning("Sync: could not fetch dispute %s: %s", dispute_id, safe(err))
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

    # ---- MCP server for AI agents (Day 6) ---------------------------------------------

    mcp_server = build_mcp(AgentDeps(settings=settings, store=store, secret=secret,
                                     summaries=summaries, assistant=assistant,
                                     item_names=item_names, submit_request=submit_request,
                                     check_key=accounts.api_key_ok))
    app.state.mcp = mcp_server
    # Stateless + JSON replies: one POST in, one JSON reply out, for 2026-07-28 and older
    # clients alike. The SDK's localhost-only Host check is off because the app is reached
    # through ngrok; McpGuard does the Origin check the spec requires instead.
    mcp_http = mcp_server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    app.router.routes.append(Route("/mcp", McpGuard(mcp_http, accounts.api_key_ok)))

    @app.get("/agents", include_in_schema=False)
    async def agents_page():
        return FileResponse(STATIC / "agents.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/agents")
    async def agents_info():
        """For the /agents page: the tools agents can use and the last 20 calls (no PII)."""
        tools = [{"name": t.name, "title": t.title,
                  "role": "buyer" if t.name in BUYER_TOOLS else "merchant",
                  "description": (t.description or "").strip().split("\n\n")[0]}
                 for t in await mcp_server.list_tools()]
        return {"mcp_path": "/mcp", "transport": "Streamable HTTP (stateless, JSON replies)",
                "sdk": f"mcp {package_version('mcp')}",
                "merchant_enabled": store.active_api_keys() > 0, "tools": tools,
                "calls": store.agent_calls(20)}

    # ---- Insights (Day 8): the seller's own AG Studio report over the same data ----

    @app.get("/insights", include_in_schema=False)
    async def insights_page():
        return FileResponse(STATIC / "insights.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/insights/license")
    async def insights_license():
        """AG Studio's licence key for the seller's browser (AG Studio checks it client-side).
        Never logged, never cached; {"key": null} means Studio shows its watermark."""
        return JSONResponse({"key": settings.ag_studio_license_key or None},
                            headers={"Cache-Control": "no-store"})

    def owner(request: Request) -> str:
        """Whose layout (and AI quota) this is: the signed-in seller's account id."""
        return str(request.state.user["id"])

    @app.get("/api/insights/layout")
    async def get_insights_layout(request: Request):
        saved = store.insights_layout(owner(request))
        return saved or {"state": None, "updated_at": None}

    @app.put("/api/insights/layout")
    async def put_insights_layout(request: Request):
        """Save the seller's AG Studio report state: a JSON object of at most 256 KB."""
        raw = await request.body()
        if len(raw) > MAX_LAYOUT_BYTES:
            raise HTTPException(422, "Layout is too large (at most 256 KB).")
        try:
            state = json.loads(raw)
        except ValueError:
            raise HTTPException(422, "Layout must be JSON.")
        if not isinstance(state, dict):
            raise HTTPException(422, "Layout must be a JSON object.")
        return {"ok": True, "updated_at": store.set_insights_layout(owner(request), state)}

    @app.delete("/api/insights/layout")
    async def reset_insights_layout(request: Request):
        store.delete_insights_layout(owner(request))
        return {"ok": True}

    insights_ai_limit = RateLimiter(60, 3600)  # model turns per seller per hour
    signatures = SignatureCache()  # Gemini's raw tool-call messages, by tool-call id

    @app.post("/api/insights/ai")
    async def insights_ai(request: Request):
        """One model turn for AG Studio's analyst agent (see app/insights_ai.py). Read and
        arrange the report only; Gemini first, Groq for this request only, never the sticky
        switch."""
        raw = await request.body()
        if len(raw) > MAX_AI_TURN_BYTES:
            raise HTTPException(422, "This conversation is too long. Start a new one.")
        try:
            turn = InsightsTurn.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "Not an Insights assistant turn.")
        if insights_ai_limit.blocked(owner(request)):
            raise HTTPException(429, "That's a lot of questions this hour. Try again later.")
        insights_ai_limit.hit(owner(request))
        if not ai.configured:
            raise HTTPException(503, "No AI key configured, so the Insights assistant is off.")
        rounds, cap = turns_used(turn), turn_cap(turn)
        called, issues = turn_summary(turn)
        messages = chat_messages(turn)
        known = earlier_results(turn)

        def prepare(provider: str):
            # Gemini gets its own raw tool-call messages back (thought signatures) and every
            # allowed tool; Groq gets standard fields and the reduced, trimmed tool set.
            return for_provider(messages, provider, signatures), chat_tools(turn.tools, provider)

        while True:
            if rounds >= cap:
                log.info("Insights assistant: turn cap reached (%d of %d); tools so far: %s",
                         rounds, cap, ", ".join(called) or "none")
                return insights_reply(TOO_MANY_TURNS, [], None, None)
            try:
                out = await ai.chat_tools(prepare, turn.tool_choice)
            except AIError as err:
                if err.busy:
                    raise HTTPException(503, "The assistant is busy, try again in a minute.")
                raise HTTPException(502, "The assistant couldn't answer just now. Try again in a moment.")
            rounds += 1
            # Names and counts only: never arguments, results or text.
            log.info("Insights assistant: turn %d of %d on %s; tools so far: %s; issues in the "
                     "last results: %d; next: %s", rounds, cap, out["provider"], ", ".join(called) or "none",
                     issues, ", ".join(c["name"] for c in out["tool_calls"]) or "answer")
            if out["tool_calls"]:
                signatures.put(out["provider"], out["raw"])  # stays on the server, never sent to the browser
            repeats = [known.get(canonical_call(c["name"], c["arguments"])) for c in out["tool_calls"]]
            if not out["tool_calls"] or None in repeats:
                return insights_reply(out["text"], out["tool_calls"], out["provider"], out["model"])
            # Every call repeats one already made for this question: answer it from the earlier
            # result instead of running it again, and ask the model once more (a turn, too).
            log.info("Insights assistant: %d repeated tool call(s) answered from earlier results", len(repeats))
            messages.append({"role": "assistant", "content": out["text"] or None,
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["name"], "arguments": c["arguments"]}}
                                            for c in out["tool_calls"]]})
            messages.extend({"role": "tool", "tool_call_id": c["id"], "content": result + ALREADY_HAVE}
                            for c, result in zip(out["tool_calls"], repeats))

    # ---- Money in flight dashboard (Day 7): read-only, no PII ------------------

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard_page():
        return FileResponse(STATIC / "dashboard.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/dashboard")
    async def dashboard_data():
        return build_dashboard(store.all_events(), item_names=item_names)

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
