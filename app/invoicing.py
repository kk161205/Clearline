"""Invoices (PayPal Invoicing v2), smart reminders, and the India export checklist.

The AI only drafts: it turns the seller's sentence into a structured invoice, drafts a polite
reminder, or suggests a purpose code with one line of reasoning. Code checks every draft
against the seller's own words (no invented amounts, emails, fees or threats), and nothing is
created, sent, reminded or set without a separate click by the seller. None of these AI calls
may switch the app's AI provider (only the dispute assistant can).

Field names follow PayPal's Invoicing v2 schema (verified): detail.currency_code (required),
detail.payment_term.due_date, primary_recipients[].billing_info.{business_name,email_address},
items[].{name, quantity, unit_amount}; send/remind take a `notification` object.
"""

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .assistant import EMAIL_RE, URL_RE

CURRENCIES = ["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"]
CURRENCY_HINTS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "rs": "INR", "rupees": "INR",
                  "dollars": "USD", "euros": "EUR", "pounds": "GBP"}
UNPAID = {"SENT", "UNPAID", "PARTIALLY_PAID", "SCHEDULED", "SHARED", "PAYMENT_PENDING"}
PAID = {"PAID", "MARKED_AS_PAID", "PAID_EXTERNAL"}

# ---- Draft extraction ----------------------------------------------------------------

EXTRACT_PROMPT_VERSION = "invoice-extract-v1"
EXTRACT_PROMPT = """Turn the seller's sentence into an invoice draft (JSON only).
- client_name: the client exactly as written.
- items: each billed thing with a short name, quantity (use 1 if not stated) and unit_amount.
  Use only amounts written in the text; never compute, guess or add taxes or fees.
- currency: the ISO code if the text states one (or a clear symbol like $), else null.
- due_in_days: a number of days if the text says "due in N days", else null.
- note: null unless the text asks for a note to the client.
Never include an email address; the seller types it separately."""

EXTRACT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["client_name", "items", "currency", "due_in_days", "note"],
    "properties": {
        "client_name": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "quantity", "unit_amount"],
            "properties": {"name": {"type": "string"}, "quantity": {"type": "number"},
                           "unit_amount": {"type": "number"}}}},
        "currency": {"anyOf": [{"type": "string", "enum": CURRENCIES}, {"type": "null"}]},
        "due_in_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
}


class DraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1_000_000)
    unit_amount: float = Field(gt=0)


class InvoiceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str = Field(min_length=1, max_length=300)
    items: list[DraftItem] = Field(min_length=1, max_length=20)
    currency: Literal["USD", "EUR", "GBP", "INR", "AUD", "CAD", "SGD", "AED", "JPY", "CHF"] | None
    due_in_days: int | None = Field(default=None, ge=0, le=365)
    note: str | None = Field(default=None, max_length=1000)


def text_numbers(text: str) -> set[Decimal]:
    """Numbers written in the seller's text ("1,200.50" -> 1200.50)."""
    found = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            found.add(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            pass
    return found


def text_currency(text: str) -> str | None:
    """The currency the text states, if it states exactly one."""
    lowered = text.lower()
    codes = {c for c in CURRENCIES if re.search(rf"\b{c.lower()}\b", lowered)}
    codes |= {code for hint, code in CURRENCY_HINTS.items()
              if (hint in lowered if not hint.isalpha() else re.search(rf"\b{hint}\b", lowered))}
    return codes.pop() if len(codes) == 1 else None


def extract_problems(draft: InvoiceDraft, text: str) -> list[str]:
    """Guardrails: every amount and quantity must be written in the text (quantity 1 is the
    default), the client must be named in it, no emails anywhere, and the currency and due
    days must come from the text too."""
    problems = []
    numbers = text_numbers(text)
    for i, item in enumerate(draft.items):
        if Decimal(str(item.unit_amount)).normalize() not in {n.normalize() for n in numbers}:
            problems.append(f"items[{i}].unit_amount {item.unit_amount} is not written in the text")
        if item.quantity != 1 and Decimal(str(item.quantity)).normalize() not in {n.normalize() for n in numbers}:
            problems.append(f"items[{i}].quantity {item.quantity} is not written in the text")
    if draft.client_name.strip().lower() not in text.lower():
        problems.append("client_name must be written exactly as in the text")
    if draft.due_in_days is not None and Decimal(draft.due_in_days) not in numbers:
        problems.append("due_in_days is not written in the text")
    stated = text_currency(text)
    if draft.currency and draft.currency != stated:
        problems.append(f"currency {draft.currency} is not stated in the text")
    fields = [draft.client_name, draft.note or "", *(i.name for i in draft.items)]
    if any(EMAIL_RE.search(f) or URL_RE.search(f) for f in fields):
        problems.append("never include an email address or link")
    return problems


def money(value, currency: str) -> dict:
    return {"currency_code": currency, "value": f"{Decimal(str(value)).quantize(Decimal('0.01'))}"}


def invoice_body(*, client_name: str, email: str, currency: str, items: list[dict],
                 due_date: str | None, note: str | None, today: date) -> dict:
    """PayPal Invoicing v2 create-invoice body for a DRAFT (nothing is sent)."""
    detail = {"currency_code": currency, "invoice_date": today.isoformat()}
    if due_date:
        detail["payment_term"] = {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due_date}
    if note:
        detail["note"] = note
    return {
        "detail": detail,
        "primary_recipients": [{"billing_info": {"business_name": client_name,
                                                 "email_address": email}}],
        "items": [{"name": i["name"], "quantity": f"{Decimal(str(i['quantity'])).normalize():f}",
                   "unit_amount": money(i["unit_amount"], currency)} for i in items],
    }


# ---- Invoice state -------------------------------------------------------------------

def invoice_summary(inv: dict) -> dict:
    """The invoice fields Clearline stores and shows (from a PayPal invoice resource)."""
    detail = inv.get("detail") or {}
    billing = ((inv.get("primary_recipients") or [{}])[0].get("billing_info") or {})
    amount = (inv.get("amount") or {})
    name = billing.get("business_name") or " ".join(
        filter(None, [(billing.get("name") or {}).get("given_name"),
                      (billing.get("name") or {}).get("surname")])) or None
    paid = (inv.get("payments") or {}).get("transactions") or []
    return {
        "invoice_id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "status": inv.get("status"),
        "client": name,
        "email": billing.get("email_address"),  # seller side only, never on the buyer page
        "amount": amount.get("value"),
        "currency": amount.get("currency_code") or detail.get("currency_code"),
        "due_date": (detail.get("payment_term") or {}).get("due_date"),
        "items": [{"name": i.get("name"), "quantity": i.get("quantity"),
                   "unit_amount": (i.get("unit_amount") or {}).get("value")}
                  for i in inv.get("items") or []],
        "paid_on": next((t.get("payment_date") for t in paid if t.get("payment_date")), None),
        "recipient_country": ((billing.get("address") or {}).get("country_code")),
    }


def days_overdue(summary: dict, today: date) -> int:
    """Days past the due date for an unpaid, sent invoice (0 if not overdue)."""
    if summary.get("status") not in UNPAID or not summary.get("due_date"):
        return 0
    try:
        due = date.fromisoformat(summary["due_date"])
    except ValueError:
        return 0
    return max(0, (today - due).days)


def display_status(summary: dict, today: date) -> str:
    """Draft / Sent / Paid / Overdue / Cancelled / Refunded (PayPal has no "overdue" status:
    it's an unpaid sent invoice past its due date)."""
    status = summary.get("status") or ""
    if status == "DRAFT":
        return "Draft"
    if status in PAID or status == "PARTIALLY_REFUNDED":
        return "Paid"
    if status in ("CANCELLED", "AUTO_CANCELLED"):
        return "Cancelled"
    if status in ("REFUNDED", "MARKED_AS_REFUNDED", "REFUNDED_EXTERNAL"):
        return "Refunded"
    if days_overdue(summary, today):
        return "Overdue"
    return "Sent" if status in UNPAID else status.title()


def state_signature(summary: dict) -> tuple:
    """What has to change for a new invoice_status row (no duplicates on every sync)."""
    return (summary.get("status"), summary.get("amount"), summary.get("due_date"),
            summary.get("number"))


# ---- Reminders -----------------------------------------------------------------------

REMINDER_PROMPT_VERSION = "invoice-reminder-v1"
REMINDER_PROMPT = """Draft a short, polite payment reminder for an overdue invoice (JSON only).
Use only the FACTS. Tone by days_overdue: up to 7 days friendly and light; 8 to 30 days
clear and courteous; over 30 days direct but respectful. Never threaten, never mention late
fees, interest, penalties, legal action or collections, and never invent dates or amounts.
Write numbers exactly as in FACTS. No links or emails. subject under 100 characters, note
under 600 characters."""
REMINDER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["subject", "note"],
    "properties": {"subject": {"type": "string"}, "note": {"type": "string"}},
}
# Whole words only ("courteous" is fine, "court" isn't).
THREATS = re.compile(r"\b(late fees?|fees?|interest|penalty|penalties|legal|lawyers?|attorneys?|"
                     r"courts?|sue|suing|sued|collections?|debt collectors?|consequences?|"
                     r"blacklist(ed)?|report you)\b", re.IGNORECASE)


class ReminderDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=3, max_length=100)
    note: str = Field(min_length=10, max_length=600)


def reminder_facts(summary: dict, seller_name: str, today: date) -> dict:
    return {"seller": seller_name, "client": summary.get("client"),
            "invoice_number": summary.get("number"), "amount": summary.get("amount"),
            "currency": summary.get("currency"), "due_date": summary.get("due_date"),
            "days_overdue": days_overdue(summary, today)}


def reminder_problems(draft: ReminderDraft, facts: dict) -> list[str]:
    """No threats or invented fees, every number from the facts, no emails or links."""
    problems = []
    text = f"{draft.subject} {draft.note}"
    if THREATS.search(text):
        problems.append("no threats, fees, interest, penalties or legal language")
    known = text_numbers(" ".join(str(v) for v in facts.values() if v is not None))
    known |= {Decimal(p) for v in facts.values() if isinstance(v, str)
              for p in re.findall(r"\d+", v)}  # parts of dates and invoice numbers
    invented = [n for n in text_numbers(text) if n not in known]
    if invented:
        problems.append(f"numbers not in the facts: {[str(n) for n in invented]}")
    if EMAIL_RE.search(text) or URL_RE.search(text):
        problems.append("no emails or links")
    return problems


# ---- India export checklist ------------------------------------------------------------

# PayPal's accepted purpose codes, verbatim from PayPal's official spec
# (Partner Referrals v2, purpose_code_enum: "Required only for India").
PURPOSE_CODES = {
    "P0104": "Cross border delivery of goods and services.",
    "P0301": "Business related travel purchase.",
    "P0801": "Hardware consulting.",
    "P0802": "Software consulting.",
    "P0803": "Data processing consulting.",
    "P0805": "Freelance journalism.",
    "P0806": "Other information services.",
    "P0902": "Licensing revenues.",
    "P1004": "Legal.",
    "P1005": "Accounting and tax.",
    "P1006": "Business and management consultancy.",
    "P1007": "Advertising and market research.",
    "P1008": "Research and development.",
    "P1009": "Architectural services.",
}
OFFICIAL_SOURCES = [
    {"label": "PayPal India: What is the purpose code?",
     "url": "https://www.paypal.com/in/cshelp/article/what-is-the-purpose-code-help978"},
    {"label": "PayPal India: How do I change my purpose code?",
     "url": "https://www.paypal.com/in/cshelp/article/how-do-i-change-my-purpose-code-help977"},
    {"label": "PayPal India: Monthly Digital FIRA",
     "url": "https://www.paypal.com/in/brc/article/paypal-monthly-digital-firc"},
    {"label": "PayPal developer docs: purpose code list",
     "url": "https://developer.paypal.com/api/partner-referrals/v2/definitions/purpose_code_enum"},
    {"label": "RBI: purpose codes for reporting forex receipts",
     "url": "https://www.rbi.org.in/upload/notification/pdfs/52220.pdf"},
]
PURPOSE_PROMPT_VERSION = "purpose-suggest-v1"
PURPOSE_PROMPT = ("Suggest the single best matching purpose code for this export payment from "
                  "the allowed list, with a one-line reason based only on the description. "
                  "If none fits well, still pick the closest and say so. JSON only.")
PURPOSE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["code", "reason"],
    "properties": {"code": {"type": "string", "enum": list(PURPOSE_CODES)},
                   "reason": {"type": "string"}},
}


class PurposeSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Literal["P0104", "P0301", "P0801", "P0802", "P0803", "P0805", "P0806", "P0902",
                  "P1004", "P1005", "P1006", "P1007", "P1008", "P1009"]
    reason: str = Field(min_length=5, max_length=160)


def due_date_from(days: int | None, today: date) -> str | None:
    return (today + timedelta(days=days)).isoformat() if days is not None else None
