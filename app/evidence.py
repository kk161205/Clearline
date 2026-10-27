"""Seller-provided evidence documents: validate, name and describe them, never store them.

Clearline only uploads files the seller provides; it never generates proof. The browser sends
the file base64-encoded in the /act JSON (no extra upload dependency). Everything here is
checked on the server; the browser's own checks are only a convenience.
"""

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

MAX_BYTES = 5 * 1024 * 1024  # PayPal: each file under 5 MB
MAX_BASE64_CHARS = 4 * (MAX_BYTES // 3 + 1) + 16  # rejects oversized uploads before decoding

# Detected from the content (magic bytes), never from the name or the browser's claim.
SIGNATURES = [
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
]
EXTENSIONS = {"pdf": "pdf", "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif"}

# PayPal Disputes schema, documents[].name
PAYPAL_NAME = re.compile(r"^[A-Za-z0-9-_,\s]+[.]{1}[A-Za-z]+$")
UNSAFE = re.compile(r"[^A-Za-z0-9_,\s-]")

# Evidence types a document can satisfy, in plain words for the seller. PROOF_OF_REFUND is
# left out: it needs a refund ID, not a document, and it isn't a way to fight a claim.
DOCUMENT_TYPES = {
    "PROOF_OF_DELIVERY_SIGNATURE": "Signed delivery receipt",
    "PROOF_OF_FULFILLMENT": "Proof of shipment (e.g. shipping receipt)",
    "PROOF_OF_RECEIPT_COPY": "Copy of the receipt",
    "PROOF_OF_SHIPMENT_POSTAGE": "Postage receipt",
    "ONLINE_TRACKING_INFORMATION": "Tracking page printout",
    "DELIVERY_RECEIPT": "Delivery receipt",
    "OTHER": "Other document",
}


class EvidenceFileError(ValueError):
    """The uploaded file can't be sent to PayPal (-> 400, before any PayPal call)."""


@dataclass
class EvidenceFile:
    name: str           # sanitized, matches PayPal's pattern
    content: bytes
    content_type: str

    @property
    def meta(self) -> dict:
        """What the timeline stores about the file: never its contents."""
        return {"name": self.name, "content_type": self.content_type, "size": len(self.content),
                "sha256": hashlib.sha256(self.content).hexdigest()}


def document_evidence_types(dispute: dict) -> list[str]:
    """Evidence PayPal requests from the seller that needs a document: in the claim stage
    (anything after INQUIRY) tracking alone isn't accepted, so these need a file."""
    if dispute.get("dispute_life_cycle_stage") in (None, "INQUIRY"):
        return []
    requested = [e.get("evidence_type") for e in dispute.get("evidences", [])
                 if e.get("source") == "REQUESTED_FROM_SELLER"]
    return [t for t in requested if t and t != "PROOF_OF_REFUND"]


def sanitize_name(name: str, kind: str) -> str:
    """A file name PayPal accepts: path dropped, unsafe characters replaced with "_", and the
    extension of the detected type (e.g. "Receipt (signed) #2.PDF" -> "Receipt _signed_ _2.pdf")."""
    stem = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = re.sub(r"_+", "_", UNSAFE.sub("_", stem)).strip(" _")[:100] or "evidence"
    safe = f"{stem}.{kind}"
    if not PAYPAL_NAME.match(safe):  # can't happen with the rules above; fail closed anyway
        safe = f"evidence.{kind}"
    return safe


def parse_upload(file_name: str, content_base64: str) -> EvidenceFile:
    """Decode and check one upload. Raises EvidenceFileError with a sentence for the seller."""
    if len(content_base64 or "") > MAX_BASE64_CHARS:
        raise EvidenceFileError("The file is too big. PayPal accepts files under 5 MB.")
    try:
        content = base64.b64decode(content_base64 or "", validate=True)
    except (binascii.Error, ValueError):
        raise EvidenceFileError("The file couldn't be read. Please choose it again.")
    if not content:
        raise EvidenceFileError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise EvidenceFileError("The file is too big. PayPal accepts files under 5 MB.")
    detected = next(((kind, ctype) for magic, kind, ctype in SIGNATURES
                     if content.startswith(magic)), None)
    if not detected:
        raise EvidenceFileError("Only PDF, JPG, PNG or GIF files can be sent to PayPal.")
    kind, ctype = detected
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in (file_name or "") else ""
    if EXTENSIONS.get(ext) != kind:
        raise EvidenceFileError(f"The file's name says .{ext or '?'} but its content is a "
                                f"{kind.upper()} file. Please choose the right file.")
    return EvidenceFile(name=sanitize_name(file_name, kind), content=content, content_type=ctype)
