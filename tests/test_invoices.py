"""Day 5: AI invoice drafts, create vs send, reminders, invoice sync/webhooks, paid invoices on
the buyer page, the India export checklist, and refund buttons on every request (no network)."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.invoicing import InvoiceDraft, ReminderDraft, extract_problems, reminder_problems
from app.store import TimelineStore
from tests.support import API, GEMINI, GROQ, make_client, setup_paypal
from tests.test_seller_page import NODE, render
from tests.test_webhooks import SIG_HEADERS, capture_order

INVOICES = f"{API}/v2/invoicing/invoices"
INV = "INV2-ABCD-1234-EFGH-5678"
TEXT = "Bill Acme Corp 1200 USD for logo design, due in 14 days"
TODAY = datetime.now(timezone.utc).date()


def ai(content: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


def draft(**overrides) -> dict:
    d = {"client_name": "Acme Corp", "items": [{"name": "Logo design", "quantity": 1, "unit_amount": 1200}],
         "currency": "USD", "due_in_days": 14, "note": None}
    d.update(overrides)
    return d


def paypal_invoice(status="DRAFT", due=None, *, number="0042", country=None, paid=False) -> dict:
    """An invoice as PayPal returns it (GET /v2/invoicing/invoices/{id})."""
    billing = {"business_name": "Acme Corp", "email_address": "billing@acme.example"}
    if country:
        billing["address"] = {"country_code": country}
    inv = {"id": INV, "status": status,
           "detail": {"invoice_number": number, "currency_code": "USD",
                      "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED",
                                       "due_date": (due or TODAY + timedelta(days=14)).isoformat()}},
           "primary_recipients": [{"billing_info": billing}],
           "items": [{"name": "Logo design", "quantity": "1",
                      "unit_amount": {"currency_code": "USD", "value": "1200.00"}}],
           "amount": {"currency_code": "USD", "value": "1200.00"}}
    if paid:
        inv["payments"] = {"transactions": [{"payment_id": "TX1", "payment_date": TODAY.isoformat()}]}
    return inv


class InvoiceOnPayPal:
    """A mutable invoice served by the mocked GET endpoint."""

    def __init__(self, **kwargs):
        self.data = paypal_invoice(**kwargs)
        self.route = respx.get(f"{INVOICES}/{INV}").mock(
            side_effect=lambda request: httpx.Response(200, json=json.loads(json.dumps(self.data))))


def created_invoice(client, paypal: InvoiceOnPayPal):
    """Create the draft through Clearline (POST /api/invoices)."""
    create = respx.post(INVOICES).mock(side_effect=lambda r: httpx.Response(201, json=paypal.data))
    resp = client.post("/api/invoices", json={
        "client_name": "Acme Corp", "email": "billing@acme.example", "currency": "USD",
        "items": [{"name": "Logo design", "quantity": 1, "unit_amount": 1200}],
        "due_date": (TODAY + timedelta(days=14)).isoformat(), "note": None})
    assert resp.status_code == 200, resp.text
    return create, resp.json()


# ---- AI draft extraction ------------------------------------------------------------

@respx.mock
def test_extraction_fills_a_draft_from_the_sellers_words(tmp_path):
    setup_paypal()
    gemini = respx.post(GEMINI).mock(return_value=ai(draft()))
    client, _ = make_client(tmp_path)
    resp = client.post("/api/invoices/extract", json={"text": TEXT + ". Email ap@acme.example"})
    assert resp.status_code == 200, resp.text
    d = resp.json()
    assert d["client_name"] == "Acme Corp" and d["currency"] == "USD"
    assert d["items"] == [{"name": "Logo design", "quantity": 1.0, "unit_amount": 1200.0}]
    assert d["due_date"] == (TODAY + timedelta(days=14)).isoformat()
    assert "email" not in d  # the email is never part of the AI draft
    assert "ap@acme.example" not in gemini.calls[0].request.content.decode()  # stripped first
    sent = json.loads(gemini.calls[0].request.content)
    assert sent["response_format"]["json_schema"]["strict"] is True


@respx.mock
@pytest.mark.parametrize("bad", [
    draft(items=[{"name": "Logo design", "quantity": 1, "unit_amount": 1500}]),  # invented amount
    draft(note="Please pay to billing@acme.example"),                          # invented email
    draft(client_name="Acme Corporation Ltd"),                                  # not in the text
])
def test_extraction_guardrails_reject_and_never_switch(tmp_path, bad):
    setup_paypal()
    gemini = respx.post(GEMINI).mock(return_value=ai(bad))
    groq = respx.post(GROQ).mock(return_value=ai(draft()))
    client, settings = make_client(tmp_path)
    resp = client.post("/api/invoices/extract", json={"text": TEXT})
    assert resp.status_code == 422 and "Fill in the form yourself" in resp.json()["detail"]
    assert gemini.call_count == 2 and not groq.called  # one corrective retry, same provider
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "gemini"


@respx.mock
def test_extraction_ai_outage_does_not_switch_provider(tmp_path):
    setup_paypal()
    respx.post(GEMINI).mock(return_value=httpx.Response(503))
    groq = respx.post(GROQ).mock(return_value=ai(draft()))
    client, settings = make_client(tmp_path)
    assert client.post("/api/invoices/extract", json={"text": TEXT}).status_code == 502
    assert not groq.called
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "gemini"


def test_extract_problems_unit():
    ok = InvoiceDraft.model_validate(draft())
    assert extract_problems(ok, TEXT) == []
    assert extract_problems(ok, "Bill Acme Corp 1,200 USD for logo design, due in 14 days") == []
    assert any("currency" in p for p in extract_problems(ok, "Bill Acme Corp 1200 for logo, due in 14 days"))
    assert any("quantity" in p for p in extract_problems(
        InvoiceDraft.model_validate(draft(items=[{"name": "Logo", "quantity": 3, "unit_amount": 1200}])), TEXT))
    assert any("due_in_days" in p for p in extract_problems(
        InvoiceDraft.model_validate(draft(due_in_days=30)), TEXT))


# ---- Create vs send ------------------------------------------------------------------

@respx.mock
def test_create_makes_a_draft_and_never_sends(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal()
    send = respx.post(f"{INVOICES}/{INV}/send").mock(return_value=httpx.Response(200, json={}))
    client, _ = make_client(tmp_path)
    create, row = created_invoice(client, paypal)

    request = create.calls[0].request
    assert request.headers["Prefer"] == "return=representation"
    assert request.headers["PayPal-Request-Id"]
    assert json.loads(request.content) == {
        "detail": {"currency_code": "USD", "invoice_date": TODAY.isoformat(),
                   "payment_term": {"term_type": "DUE_ON_DATE_SPECIFIED",
                                    "due_date": (TODAY + timedelta(days=14)).isoformat()}},
        "primary_recipients": [{"billing_info": {"business_name": "Acme Corp",
                                                 "email_address": "billing@acme.example"}}],
        "items": [{"name": "Logo design", "quantity": "1",
                   "unit_amount": {"currency_code": "USD", "value": "1200.00"}}]}
    assert row["display_status"] == "Draft" and not send.called
    assert [r["display_status"] for r in client.get("/api/invoices").json()] == ["Draft"]


@respx.mock
def test_create_needs_a_typed_email(tmp_path):
    setup_paypal()
    create = respx.post(INVOICES).mock(return_value=httpx.Response(201, json={}))
    client, _ = make_client(tmp_path)
    body = {"client_name": "Acme Corp", "email": "not-an-email", "currency": "USD",
            "items": [{"name": "Logo", "quantity": 1, "unit_amount": 1200}]}
    assert client.post("/api/invoices", json=body).status_code == 400
    assert client.post("/api/invoices", json={**body, "email": ""}).status_code == 422
    past = (TODAY - timedelta(days=1)).isoformat()
    assert client.post("/api/invoices", json={**body, "email": "a@b.example", "due_date": past}).status_code == 400
    assert not create.called


@respx.mock
def test_send_is_a_separate_click_and_only_for_drafts(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal()
    client, _ = make_client(tmp_path)
    created_invoice(client, paypal)
    send = respx.post(f"{INVOICES}/{INV}/send").mock(
        side_effect=lambda r: paypal.data.update(status="SENT") or httpx.Response(200, json={"links": []}))

    resp = client.post(f"/api/invoices/{INV}/send")
    assert resp.status_code == 200 and resp.json()["display_status"] == "Sent"
    assert json.loads(send.calls[0].request.content) == {"send_to_recipient": True, "send_to_invoicer": True}
    assert client.post(f"/api/invoices/{INV}/send").status_code == 409  # already sent
    assert send.call_count == 1
    kinds = [e["kind"] for e in client.get(f"/api/orders/{INV}/timeline").json()]
    assert kinds.count("invoice_send_sent") == 1 and "invoice_status" in kinds


# ---- Reminders -------------------------------------------------------------------------

@respx.mock
def test_reminders_only_for_overdue_invoices(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal(status="SENT")  # due in 14 days: not overdue
    remind = respx.post(f"{INVOICES}/{INV}/remind").mock(return_value=httpx.Response(204))
    client, _ = make_client(tmp_path)
    created_invoice(client, paypal)
    assert client.post(f"/api/invoices/{INV}/reminder-draft").status_code == 409
    assert client.post(f"/api/invoices/{INV}/remind",
                       json={"subject": "Reminder", "note": "Kindly pay invoice 0042."}).status_code == 409
    paypal.data["status"] = "PAID"
    assert client.post(f"/api/invoices/{INV}/reminder-draft").status_code == 409
    assert not remind.called


@respx.mock
def test_overdue_reminder_drafted_edited_and_sent_by_the_seller(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal(status="SENT")
    client, _ = make_client(tmp_path)
    created_invoice(client, paypal)
    paypal.data["detail"]["payment_term"]["due_date"] = (TODAY - timedelta(days=10)).isoformat()
    gemini = respx.post(GEMINI).mock(return_value=ai({
        "subject": "Invoice 0042 is now 10 days past due",
        "note": "Hello Acme Corp, a courteous reminder that invoice 0042 for 1200.00 USD is 10 days past due. Thank you!"}))
    remind = respx.post(f"{INVOICES}/{INV}/remind").mock(return_value=httpx.Response(204))

    d = client.post(f"/api/invoices/{INV}/reminder-draft").json()
    assert d["source"] == "ai" and d["days_overdue"] == 10 and not remind.called  # drafting sends nothing
    facts = json.loads(json.loads(gemini.calls[0].request.content)["messages"][1]["content"].split("FACTS:\n")[1])
    assert facts["days_overdue"] == 10 and "email" not in json.dumps(facts)

    resp = client.post(f"/api/invoices/{INV}/remind", json={"subject": d["subject"], "note": "Edited by me: please pay 0042."})
    assert resp.status_code == 200
    assert json.loads(remind.calls[0].request.content) == {
        "subject": "Invoice 0042 is now 10 days past due", "note": "Edited by me: please pay 0042.",
        "send_to_recipient": True, "send_to_invoicer": True}
    assert remind.calls[0].request.headers["PayPal-Request-Id"]
    [sent] = [e for e in client.get(f"/api/orders/{INV}/timeline").json() if e["kind"] == "invoice_reminder_sent"]
    assert sent["payload"]["days_overdue"] == 10


@respx.mock
def test_threatening_or_invented_reminder_falls_back_to_template(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal(status="SENT", due=TODAY - timedelta(days=40))
    client, settings = make_client(tmp_path)
    created_invoice(client, paypal)
    gemini = respx.post(GEMINI).mock(return_value=ai({
        "subject": "Final notice for invoice 0042",
        "note": "Pay 1200.00 USD now or a late fee of 50 USD applies and we will take legal action."}))
    d = client.post(f"/api/invoices/{INV}/reminder-draft").json()
    assert d["source"] == "template" and "legal" not in d["note"] and gemini.call_count == 2
    assert TimelineStore(settings.database_path).ai_state()["active_provider"] == "gemini"


def test_reminder_guardrail_unit():
    facts = {"seller": "S", "client": "Acme Corp", "invoice_number": "0042", "amount": "1200.00",
             "currency": "USD", "due_date": "2026-10-01", "days_overdue": 26}
    ok = ReminderDraft(subject="Invoice 0042 reminder", note="Invoice 0042 for 1200.00 USD was due on 2026-10-01.")
    assert reminder_problems(ok, facts) == []
    assert reminder_problems(ReminderDraft(subject="Reminder", note="Please pay within 3 days, thanks."), facts)
    assert reminder_problems(ReminderDraft(subject="Reminder", note="Interest will be charged on 0042."), facts)
    assert reminder_problems(ReminderDraft(subject="Reminder", note="We may go to court over 0042."), facts)
    courteous = ReminderDraft(subject="A courteous reminder", note="A courteous note about invoice 0042.")
    assert reminder_problems(courteous, facts) == []  # whole words only


# ---- Status sync: webhook and auto-sync ---------------------------------------------------

@respx.mock
def test_invoice_webhook_and_sync_update_status(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal(status="SENT")
    client, _ = make_client(tmp_path)
    created_invoice(client, paypal)

    paypal.data.update(status="PAID", payments={"transactions": [{"payment_date": TODAY.isoformat()}]})
    resp = client.post("/webhooks/paypal", headers=SIG_HEADERS, json={
        "id": "WH-INV-1", "event_type": "INVOICING.INVOICE.PAID",
        "resource": {"invoice": {"id": INV, "status": "PAID"}}})
    assert resp.json() == {"status": "accepted"}
    [row] = client.get("/api/invoices").json()
    assert row["display_status"] == "Paid"
    kinds = [e["kind"] for e in client.get(f"/api/orders/{INV}/timeline").json()]
    assert kinds.count("invoice_paid") == 1

    # Auto-sync picks up later changes, and adds nothing when nothing changed.
    respx.get(f"{API}/v1/customer/disputes").mock(return_value=httpx.Response(200, json={"items": [], "links": []}))
    listing = respx.get(INVOICES).mock(side_effect=lambda r: httpx.Response(200, json={
        "items": [paypal.data], "total_pages": 1}))
    rows_before = len(client.get(f"/api/orders/{INV}/timeline").json())
    with client:
        client.portal.call(client.app.state.auto_sync)
    assert len(client.get(f"/api/orders/{INV}/timeline").json()) == rows_before
    paypal.data["status"] = "PARTIALLY_REFUNDED"
    client.post("/api/disputes/sync")
    assert client.get("/api/disputes/sync/status").json()["invoices"] == {"listed": 1, "changed": 1, "error": None}
    assert listing.calls[0].request.url.params["page_size"] == "100"


@respx.mock
def test_invoice_sync_failure_never_breaks_dispute_sync(tmp_path):
    setup_paypal()
    respx.get(f"{API}/v1/customer/disputes").mock(return_value=httpx.Response(200, json={"items": [], "links": []}))
    respx.get(INVOICES).mock(return_value=httpx.Response(403, json={"name": "NOT_AUTHORIZED"}))
    client, _ = make_client(tmp_path)
    assert client.post("/api/disputes/sync").status_code == 200
    assert "refused access" in client.get("/api/disputes/sync/status").json()["invoices"]["error"]


# ---- Paid invoice -> buyer page ------------------------------------------------------------

@respx.mock
def test_paid_invoice_gets_a_buyer_link_with_masked_number(tmp_path):
    setup_paypal()
    paypal = InvoiceOnPayPal(status="SENT", number="INV-2026-000123")
    client, _ = make_client(tmp_path, gemini_key=None, groq_key=None)
    created_invoice(client, paypal)
    paypal.data.update(status="PAID")
    client.post(f"/api/invoices/{INV}/send")  # 409 (not a draft) but refreshes the status
    link = client.get(f"/api/orders/{INV}/buyer").json()["link"]
    page = client.get(f"/api/o/{link.split('/')[-1]}").json()
    assert page["kind"] == "invoice" and page["order_ref"] == "000123"
    assert page["item"] == "Logo design" and page["amount"] == "1200.00" and page["shipping"] is None
    assert "not_arrived" not in page["request_types"]
    assert "billing@acme.example" not in json.dumps(page) and "INV-2026-000123" not in json.dumps(page)
    assert "invoice …000123 (Logo design)" in page["summary"]["text"]


# ---- India export checklist ------------------------------------------------------------------

@respx.mock
def test_purpose_code_suggestion_is_never_auto_applied(tmp_path):
    setup_paypal()
    gemini = respx.post(GEMINI).mock(return_value=ai({"code": "P1007", "reason": "Logo design is advertising work."}))
    client, _ = make_client(tmp_path)
    capture_order(client)  # captured without a payer country -> not an international payment
    store = TimelineStore(str(tmp_path / "test.db"))
    store.add("ORDER9", "order_created", {"item_id": "brand-kit", "amount": "120.00", "currency": "USD"})
    store.add("ORDER9", "payment_captured", {"capture_id": "C9", "payer_country": "US"})

    assert client.get("/api/export/ORDER1").status_code == 404  # no foreign payer
    x = client.get("/api/export/ORDER9").json()
    assert x["purpose_code"] is None and x["disclaimer"] == "Guidance, not legal or tax advice."
    assert len(x["purpose_codes"]) == 14 and x["sources"][0]["url"].startswith("https://www.paypal.com/in/")

    s = client.post("/api/export/ORDER9/suggest").json()
    assert s["code"] == "P1007" and gemini.call_count == 1
    x = client.get("/api/export/ORDER9").json()
    assert x["purpose_code"] is None and x["suggestion"]["code"] == "P1007"  # suggestion only

    assert client.post("/api/export/ORDER9/purpose", json={"code": "P9999"}).status_code == 422
    assert client.post("/api/export/ORDER9/purpose", json={"code": "P0802"}).status_code == 200
    assert client.post("/api/export/ORDER9/fira", json={"downloaded": True}).status_code == 200
    x = client.get("/api/export/ORDER9").json()
    assert x["purpose_code"] == "P0802" and x["fira_downloaded"] is True


@respx.mock
def test_export_checklist_only_for_india_sellers_and_foreign_payments(tmp_path):
    setup_paypal()
    client, _ = make_client(tmp_path, seller_country="US")
    store = TimelineStore(str(tmp_path / "test.db"))
    store.add("ORDER9", "payment_captured", {"capture_id": "C9", "payer_country": "GB"})
    assert client.get("/api/export/ORDER9").status_code == 404
    (tmp_path / "in").mkdir()
    client, _ = make_client(tmp_path / "in")
    store = TimelineStore(str(tmp_path / "in" / "test.db"))
    store.add("DOMESTIC", "payment_captured", {"capture_id": "C1", "payer_country": "IN"})
    assert client.get("/api/export/DOMESTIC").status_code == 404


# ---- Seller page: refund buttons on every request; export section (node harness) ------------

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_refund_buttons_on_every_buyer_request():
    buyer = {"link": "/o/tok", "refundable": "120.00", "currency": "USD", "refunds": [],
             "requests": [{"id": "R1", "type": "not_arrived", "reason": None, "message": "Where is it?",
                           "at": 1, "replies": []},
                          {"id": "R2", "type": "unrecognised", "reason": None, "message": "", "at": 2, "replies": []}]}
    [page] = render({"env": "sandbox", "events": [], "buyer": buyer})
    html = page["buyer"]
    for rid in ("R1", "R2"):
        assert f'data-refund="full" data-request="{rid}"' in html
        assert f'data-refund="partial" data-request="{rid}"' in html
    [none_left] = render({"env": "sandbox", "events": [], "buyer": {**buyer, "refundable": "0.00"}})
    assert "data-refund" not in none_left["buyer"]


@needs_node
def test_export_section_is_labelled_guidance_with_sources():
    data = {"payment": {"country": "US", "amount": "120.00", "currency": "USD", "description": "Logo"},
            "purpose_code": None, "suggestion": {"code": "P1007", "reason": "Design work"},
            "fira_downloaded": False, "purpose_codes": {"P0802": "Software consulting.", "P1007": "Advertising and market research."},
            "sources": [{"label": "PayPal India", "url": "https://www.paypal.com/in/x"}],
            "disclaimer": "Guidance, not legal or tax advice."}
    [page] = render({"env": "sandbox", "events": [], "exportData": data})
    html = page["export"]
    assert "Guidance, not legal or tax advice." in html and "https://www.paypal.com/in/x" in html
    assert "AI suggests <strong>P1007</strong>" in html and "not applied" in html
    assert 'value="P1007" selected' not in html  # the suggestion doesn't pre-select itself
