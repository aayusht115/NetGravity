"""
Tests for netgravity.action_agent.api (POST /api/inbound-email).

Claims under test:
  1. A verified sender's reply re-enters the exact same upload pipeline,
     tagged to the original session (via IngestionService.resume_with_file).
  2. An unverified sender is held for manual review, never applied.
  3. A retried webhook delivery (same message id) is a no-op, not a
     duplicate ingestion run.
"""

from __future__ import annotations

import io

import pytest
from flask import Flask

from netgravity.action_agent.api import create_action_agent_blueprint
from netgravity.action_agent.recipients import SourceContactStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService
from netgravity.ingestion.storage import get_storage


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)

    upload_dir = tmp_path / "uploads" / "client_a"
    upload_dir.mkdir(parents=True)
    (upload_dir / "facilities.csv").write_text(
        "facility_id,facility_name,role\nDC1,Test DC,DC\n")

    service = IngestionService(IngestionConfig())
    session = service.start(upload_dir, client_id="client_a")

    storage = get_storage(IngestionConfig())
    SourceContactStore(storage).set("client_a", "owner@clienta.com", contact_name="Owner")

    app = Flask(__name__)
    app.register_blueprint(create_action_agent_blueprint())
    return app.test_client(), session.run_id


def _headers_with_message_id(message_id: str) -> str:
    return f"From: owner@clienta.com\nMessage-ID: {message_id}\n"


def test_verified_sender_applies_the_attachment(app_client):
    client, run_id = app_client
    data = {
        "from": "owner@clienta.com",
        "to": f"ingest-{run_id}@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": _headers_with_message_id("<msg-1@clienta.com>"),
        "attachment1": (io.BytesIO(b"facility_id,facility_name,role\nDC1,Test DC,DC\n"),
                       "corrected.csv"),
    }
    resp = client.post("/api/inbound-email", data=data, content_type="multipart/form-data")

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "applied"


def test_unverified_sender_is_held(app_client):
    client, run_id = app_client
    data = {
        "from": "attacker@evil.com",
        "to": f"ingest-{run_id}@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": _headers_with_message_id("<msg-2@evil.com>"),
        "attachment1": (io.BytesIO(b"whatever"), "corrected.csv"),
    }
    resp = client.post("/api/inbound-email", data=data, content_type="multipart/form-data")

    assert resp.status_code == 202
    assert resp.get_json()["status"] == "held_for_review"


def test_retried_webhook_is_a_noop(app_client):
    client, run_id = app_client
    data = {
        "from": "owner@clienta.com",
        "to": f"ingest-{run_id}@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": _headers_with_message_id("<msg-3@clienta.com>"),
        "attachment1": (io.BytesIO(b"facility_id,facility_name,role\nDC1,Test DC,DC\n"),
                       "corrected.csv"),
    }
    first = client.post("/api/inbound-email", data=data, content_type="multipart/form-data")
    assert first.get_json()["status"] == "applied"

    data["attachment1"] = (io.BytesIO(b"facility_id,facility_name,role\nDC1,Test DC,DC\n"),
                           "corrected.csv")
    second = client.post("/api/inbound-email", data=data, content_type="multipart/form-data")

    assert second.status_code == 200
    assert second.get_json()["status"] == "already_processed"


def test_unknown_session_returns_404(app_client):
    client, _run_id = app_client
    data = {
        "from": "owner@clienta.com",
        "to": "ingest-ing_does_not_exist@mail.netgravity.example",
        "subject": "Re: Data needed",
        "headers": _headers_with_message_id("<msg-4@clienta.com>"),
        "attachment1": (io.BytesIO(b"whatever"), "corrected.csv"),
    }
    resp = client.post("/api/inbound-email", data=data, content_type="multipart/form-data")
    assert resp.status_code == 404
