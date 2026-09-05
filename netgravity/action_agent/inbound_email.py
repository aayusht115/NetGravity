"""
NetGravity — Inbound Email Handling
======================================
Handles a data owner replying directly to a missing-data email with a
corrected file, instead of using the resume link.

PROVIDER CHOICE: SendGrid Inbound Parse. The spec calls out three
equivalent options (SendGrid Inbound Parse, Mailgun Routes, Postmark) and
says to pick one — SendGrid is the most commonly documented and is what
this module's payload parsing assumes. Swapping providers later means
rewriting `parse_payload()` only; nothing downstream of it (session lookup,
sender verification, re-entering the ingestion pipeline) is provider-shaped.

INFRASTRUCTURE PREREQUISITE (outside this codebase): a domain configured
with SendGrid Inbound Parse, an MX record pointed at it, and its webhook
pointed at POST /api/inbound-email (see action_agent/api.py). That is a
DNS/domain setup step for whoever owns DNS, not something this module can
do or fake — flagged, not guessed at.

IDEMPOTENCY: inbound webhook providers retry on failure. Every processed
message id is recorded so a retried delivery is a no-op, not a duplicate
ingestion run.
"""

from __future__ import annotations

import email.utils
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from netgravity.ingestion.storage.base import StorageBackend

ZONE = "standardized"
PROCESSED_IDS_PREFIX = "action_agent/processed_email_ids"

_SESSION_ID_RE = re.compile(r"ingest-([A-Za-z0-9_-]+)@")


@dataclass
class InboundAttachment:
    filename: str
    content: bytes


@dataclass
class InboundEmail:
    message_id: str
    from_address: str
    to_address: str
    subject: str
    session_id: Optional[str]
    attachments: List[InboundAttachment] = field(default_factory=list)


def extract_session_id(to_address: str) -> Optional[str]:
    """
    Pull the session id straight out of the reply-to address itself
    (ingest-{session_id}@domain) — no parsing of email body/subject needed,
    exactly the point of setting a unique Reply-To per session.
    """
    match = _SESSION_ID_RE.search(to_address or "")
    return match.group(1) if match else None


def _extract_message_id(headers_blob: str) -> str:
    """
    SendGrid's Inbound Parse payload carries the raw email headers as one
    string field. Message-ID lives in there, not as its own form field.
    Falls back to a generated id when the sender's mail client omitted one
    (rare, but not worth failing the whole webhook over) — a fallback id
    means a genuine duplicate delivery from that specific sender might not
    be deduplicated, which is an acceptable degradation, not a silent
    correctness bug.
    """
    for line in (headers_blob or "").splitlines():
        if line.lower().startswith("message-id:"):
            return line.split(":", 1)[1].strip()
    return f"generated-{uuid.uuid4().hex}"


def parse_sendgrid_payload(form: Mapping[str, str], files: Mapping[str, Any]) -> InboundEmail:
    """
    `form` is the webhook's form fields (from/to/subject/headers/...).
    `files` is filename -> file-like object with .filename and .read(),
    exactly the shape Flask's request.files provides — kept duck-typed here
    so tests can pass plain stand-ins without a real Flask request.
    """
    from_header = form.get("from", "")
    from_address = email.utils.parseaddr(from_header)[1] or from_header
    to_address = form.get("to", "")
    message_id = _extract_message_id(form.get("headers", ""))

    attachments: List[InboundAttachment] = []
    for _, file_obj in files.items():
        filename = getattr(file_obj, "filename", "") or "attachment"
        content = file_obj.read()
        attachments.append(InboundAttachment(filename=filename, content=content))

    return InboundEmail(
        message_id=message_id,
        from_address=from_address,
        to_address=to_address,
        subject=form.get("subject", ""),
        session_id=extract_session_id(to_address),
        attachments=attachments,
    )


class ProcessedEmailStore:
    """Idempotency guard: has this provider message id already been handled."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def _key(self, message_id: str) -> str:
        safe = "".join(ch for ch in message_id if ch.isalnum() or ch in "_-.@")
        return f"{PROCESSED_IDS_PREFIX}/{safe}.json"

    def already_processed(self, message_id: str) -> bool:
        return self.storage.exists(ZONE, self._key(message_id))

    def mark_processed(self, message_id: str, outcome: str) -> None:
        self.storage.save_text(
            ZONE, self._key(message_id),
            json.dumps({"message_id": message_id, "outcome": outcome}, indent=2))
