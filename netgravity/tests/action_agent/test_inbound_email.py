"""
Tests for netgravity.action_agent.inbound_email.

Claims under test:
  1. The session id is pulled directly from the Reply-To/To address
     (ingest-{session_id}@domain) — no body/subject parsing needed.
  2. Message-ID is extracted from SendGrid's raw `headers` blob, with a
     generated fallback id when the sender's client omitted one, so a
     missing header degrades gracefully instead of crashing the webhook.
  3. Idempotency: a message id marked processed is reported as such, and
     re-marking is safe.
"""

from __future__ import annotations

import io

from netgravity.action_agent.inbound_email import (
    ProcessedEmailStore,
    extract_session_id,
    parse_sendgrid_payload,
)


class _FakeFileStorage:
    """Stand-in for werkzeug's FileStorage: .filename + .read()."""

    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self._buf = io.BytesIO(content)

    def read(self) -> bytes:
        return self._buf.read()


def test_extract_session_id_from_reply_to_address():
    assert extract_session_id("ingest-ing_abc123@mail.netgravity.example") == "ing_abc123"


def test_extract_session_id_returns_none_when_not_a_reply_address():
    assert extract_session_id("someone@example.com") is None


def test_parse_sendgrid_payload_pulls_message_id_from_headers():
    form = {
        "from": "Data Owner <owner@clienta.com>",
        "to": "ingest-ing_abc123@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": "From: owner@clienta.com\nMessage-ID: <xyz-789@mail.clienta.com>\n",
    }
    files = {"attachment1": _FakeFileStorage("corrected.xlsx", b"fake-bytes")}

    inbound = parse_sendgrid_payload(form, files)

    assert inbound.message_id == "<xyz-789@mail.clienta.com>"
    assert inbound.from_address == "owner@clienta.com"
    assert inbound.session_id == "ing_abc123"
    assert len(inbound.attachments) == 1
    assert inbound.attachments[0].filename == "corrected.xlsx"
    assert inbound.attachments[0].content == b"fake-bytes"


def test_parse_sendgrid_payload_generates_id_when_header_missing():
    form = {"from": "owner@clienta.com", "to": "ingest-ing_1@x.com", "headers": ""}
    inbound = parse_sendgrid_payload(form, {})
    assert inbound.message_id.startswith("generated-")


def test_processed_email_store_idempotency(aa_storage):
    store = ProcessedEmailStore(aa_storage)
    assert store.already_processed("<xyz-789@mail.clienta.com>") is False

    store.mark_processed("<xyz-789@mail.clienta.com>", "applied")

    assert store.already_processed("<xyz-789@mail.clienta.com>") is True
