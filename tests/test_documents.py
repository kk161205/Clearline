"""Seller-provided document evidence in the claim stage (no network; PayPal mocked)."""

import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.assistant import Recommendation, build_facts, check_guardrails, PROMPT_VERSION, SYSTEM_PROMPT
from app.evidence import PAYPAL_NAME, EvidenceFileError, parse_upload, sanitize_name
from app.paypal import PayPalError
from app.store import TimelineStore
from tests.support import (API, DISPUTE_ID, GEMINI, DisputeOnPayPal, ai_reply, events, fight,
                           make_client, open_dispute, setup_paypal, shipped_order)

DISPUTES = f"{API}/v1/customer/disputes/{DISPUTE_ID}"
MARKER = b"CLEARLINE-TEST-MARKER-7f3a"  # must never end up in SQLite
PDF = b"%PDF-1.4\n" + MARKER + b"\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + MARKER
B64 = lambda data: base64.b64encode(data).decode()  # noqa: E731


def claim_stage(tmp_path):
    """ORDER1 shipped, then disputed and escalated to a claim that wants documents."""
    setup_paypal()
    paypal = DisputeOnPayPal(
        dispute_life_cycle_stage="CHARGEBACK",
        evidences=[{"evidence_type": t, "source": "REQUESTED_FROM_SELLER"}
                   for t in ("PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND", "PROOF_OF_DELIVERY_SIGNATURE")],
        links=[{"rel": r.replace("-", "_"), "method": "POST", "href": f"{DISPUTES}/{r}"}
               for r in ("accept-claim", "provide-evidence")])
    respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, settings = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    return client, settings, paypal


def act(client, evidence=None, **extra):
    body = {"action": "FIGHT", "note_to_paypal": "Signed delivery receipt attached.", **extra}
    if evidence:
        body["evidence"] = evidence
    return client.post(f"/api/disputes/{DISPUTE_ID}/act", json=body)


def parts(request: httpx.Request) -> dict[str, tuple[bytes, bytes]]:
    """multipart/form-data parts by name: {name: (headers, body)}."""
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    out = {}
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, body = chunk[2:].split(b"\r\n\r\n", 1)
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        out[name] = (head, body[:-2])  # drop the CRLF before the next boundary
    return out


# ---- Happy path: sent exactly like the Sandbox experiment's V6 ----------------------

@respx.mock
def test_claim_document_is_sent_as_multipart_with_both_parts(tmp_path):
    client, settings, _ = claim_stage(tmp_path)
    evidence = respx.post(f"{DISPUTES}/provide-evidence").mock(
        return_value=httpx.Response(200, json={"links": [{"rel": "detail"}]}))
    message = respx.post(f"{DISPUTES}/send-message").mock(return_value=httpx.Response(200, json={}))

    resp = act(client, {"type": "PROOF_OF_DELIVERY_SIGNATURE",
                        "file_name": "Signed receipt (DHL) #1.pdf", "content_base64": B64(PDF)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"] == "Evidence sent ✓"
    assert not message.called  # no messaging in the claim stage

    request = evidence.calls[0].request
    assert request.headers["PayPal-Request-Id"]
    p = parts(request)
    assert list(p) == ["input", "evidence-file"]
    head, body = p["input"]
    assert b"Content-Type: application/json" in head
    assert json.loads(body) == {"evidences": [{
        "evidence_type": "PROOF_OF_DELIVERY_SIGNATURE",
        "documents": [{"name": "Signed receipt _DHL_ _1.pdf"}],
        "notes": "Signed delivery receipt attached."}]}
    head, body = p["evidence-file"]
    assert b'filename="Signed receipt _DHL_ _1.pdf"' in head
    assert b"Content-Type: application/pdf" in head
    assert body == PDF

    # The timeline knows about the file, but never holds its contents.
    [sent] = events(client, "ORDER1", "dispute_action_sent")
    assert sent["payload"]["sent"]["document"] == {
        "name": "Signed receipt _DHL_ _1.pdf", "content_type": "application/pdf",
        "size": len(PDF), "sha256": hashlib.sha256(PDF).hexdigest()}
    db = Path(settings.database_path).read_bytes()
    assert MARKER not in db and B64(PDF).encode() not in db


@respx.mock
def test_paypal_refusal_of_document_is_recorded_without_retry(tmp_path):
    client, _, _ = claim_stage(tmp_path)
    route = respx.post(f"{DISPUTES}/provide-evidence").mock(return_value=httpx.Response(400, json={
        "name": "INVALID_REQUEST", "debug_id": "dbg-1",
        "details": [{"issue": "EVIDENCE_TYPE_IS_NOT_ALLOWED"}]}))
    resp = act(client, {"type": "PROOF_OF_FULFILLMENT", "file_name": "receipt.pdf",
                        "content_base64": B64(PDF)})
    assert resp.status_code == 502 and route.call_count == 1
    [failed] = events(client, "ORDER1", "dispute_action_failed")
    assert failed["payload"]["error"].startswith("PayPal won't accept this kind of evidence")
    assert failed["payload"]["sent"]["document"]["name"] == "receipt.pdf"


# ---- Rejected before any PayPal call -------------------------------------------------

BAD_FILES = [
    ("not a document", "receipt.pdf", B64(b"MZ\x90\x00 pretend exe"), "Only PDF, JPG, PNG or GIF"),
    ("fake extension", "receipt.pdf", B64(PNG), "content is a PNG"),
    ("wrong extension", "receipt.txt", B64(PDF), "name says .txt"),
    ("too big", "big.pdf", B64(b"%PDF-" + b"0" * (5 * 1024 * 1024)), "too big"),
    ("not base64", "receipt.pdf", "%%%not-base64%%%", "couldn't be read"),
    ("empty", "receipt.pdf", "", "empty"),
]


@respx.mock
@pytest.mark.parametrize("case,name,content,message", BAD_FILES, ids=[c[0] for c in BAD_FILES])
def test_bad_files_rejected_before_any_paypal_call(tmp_path, case, name, content, message):
    client, _, paypal = claim_stage(tmp_path)
    route = respx.post(f"{DISPUTES}/provide-evidence").mock(return_value=httpx.Response(200, json={}))
    gets = paypal.route.call_count
    resp = act(client, {"type": "PROOF_OF_DELIVERY_SIGNATURE", "file_name": name,
                        "content_base64": content})
    assert resp.status_code == 400 and message in resp.json()["detail"]
    assert paypal.route.call_count == gets and not route.called  # not even a GET


@respx.mock
def test_far_too_big_upload_is_refused_by_the_request_model(tmp_path):
    client, _, paypal = claim_stage(tmp_path)
    gets = paypal.route.call_count
    resp = act(client, {"type": "PROOF_OF_DELIVERY_SIGNATURE", "file_name": "huge.pdf",
                        "content_base64": "A" * (8 * 1024 * 1024)})
    assert resp.status_code == 422 and paypal.route.call_count == gets


@respx.mock
def test_claim_needs_a_document_of_a_requested_type(tmp_path):
    client, _, _ = claim_stage(tmp_path)
    route = respx.post(f"{DISPUTES}/provide-evidence").mock(return_value=httpx.Response(200, json={}))
    no_file = act(client)
    assert no_file.status_code == 400 and "needs a document" in no_file.json()["detail"]
    wrong_type = act(client, {"type": "PROOF_OF_REFUND", "file_name": "r.pdf",
                              "content_base64": B64(PDF)})
    assert wrong_type.status_code == 400 and "isn't asking for PROOF_OF_REFUND" in wrong_type.json()["detail"]
    assert not route.called


@respx.mock
def test_inquiry_stage_flow_is_unchanged_and_refuses_documents(tmp_path):
    setup_paypal()
    DisputeOnPayPal()  # INQUIRY stage, tracking evidence + message
    respx.post(GEMINI).mock(return_value=ai_reply(fight()))
    client, _ = make_client(tmp_path)
    shipped_order(client)
    open_dispute(client)
    assert events(client, "ORDER1", "dispute_created")[0]["payload"]["document_evidence"] == []
    resp = act(client, {"type": "PROOF_OF_FULFILLMENT", "file_name": "r.pdf",
                        "content_base64": B64(PDF)})
    assert resp.status_code == 400 and "only sent when PayPal asks" in resp.json()["detail"]


# ---- Names, types, facts, AI rules -----------------------------------------------------

@pytest.mark.parametrize("raw,kind,expected", [
    ("Signed receipt (DHL) #1.pdf", "pdf", "Signed receipt _DHL_ _1.pdf"),
    ("C:\\Users\\me\\Photos\\My Receipt.JPEG", "jpg", "My Receipt.jpg"),
    ("../../etc/passwd.pdf", "pdf", "passwd.pdf"),
    ("Quittung_é.pdf", "pdf", "Quittung.pdf"),
    ("....pdf", "pdf", "evidence.pdf"),
    ("receipt", "png", "receipt.png"),
])
def test_file_names_are_sanitized_to_paypal_pattern(raw, kind, expected):
    name = sanitize_name(raw, kind)
    assert name == expected and PAYPAL_NAME.match(name)


def test_types_are_detected_by_content():
    assert parse_upload("a.jpeg", B64(b"\xff\xd8\xff\xe0" + MARKER)).content_type == "image/jpeg"
    assert parse_upload("a.gif", B64(b"GIF89a" + MARKER)).content_type == "image/gif"
    assert parse_upload("a.PNG", B64(PNG)).name == "a.png"
    with pytest.raises(EvidenceFileError):
        parse_upload("a.gif", B64(PDF))


@respx.mock
def test_facts_and_prompt_cover_the_claim_stage(tmp_path):
    client, settings, _ = claim_stage(tmp_path)
    facts = build_facts(TimelineStore(settings.database_target), "ORDER1", DISPUTE_ID)
    assert facts["dispute.stage"] == "CHARGEBACK"
    assert facts["dispute.requested_evidence"] == ["PROOF_OF_FULFILLMENT", "PROOF_OF_REFUND",
                                                   "PROOF_OF_DELIVERY_SIGNATURE"]
    assert PROMPT_VERSION == "dispute-v2"
    prompt = " ".join(SYSTEM_PROMPT.split())  # the prompt wraps lines
    assert "never suggest creating, editing or altering a document" in prompt
    assert "Attach the signed delivery receipt" in prompt


@pytest.mark.parametrize("text", ["Create a delivery receipt showing the date",
                                  "You could edit the invoice to match",
                                  "Make a shipping label for the claim"])
def test_guardrail_rejects_suggestions_to_fabricate(text):
    rec = Recommendation.model_validate(fight(missing_evidence=[text]))
    facts = {"tracking.carrier": "DHL", "tracking.number": "1"}
    assert any("never suggest creating" in e for e in check_guardrails(rec, facts))


def test_guardrail_allows_asking_for_existing_documents():
    rec = Recommendation.model_validate(fight(missing_evidence=[
        "Attach the signed delivery receipt", "Make sure the receipt shows the delivery date"]))
    assert check_guardrails(rec, {"tracking.carrier": "DHL", "tracking.number": "1"}) == []


def test_evidence_type_not_allowed_reads_as_a_sentence():
    err = PayPalError(400, {"details": [{"issue": "EVIDENCE_TYPE_IS_NOT_ALLOWED"}]})
    assert err.readable == ("PayPal won't accept this kind of evidence at this stage. Attach a "
                            "document and choose the matching type.")
