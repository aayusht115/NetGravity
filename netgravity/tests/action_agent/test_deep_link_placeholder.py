"""
Tests for netgravity.action_agent.deep_link_placeholder.

Claim under test: the two placeholder routes give an Action Agent email's
link somewhere real to land — a known session/card renders its actual
state (not invented content), and an unknown one 404s honestly rather than
pretending to be a valid page.
"""

from __future__ import annotations

import pytest
from flask import Flask

from netgravity.action_agent.deep_link_placeholder import create_deep_link_placeholder_blueprint
from netgravity.action_agent.dispatch_log import DispatchLogStore, DispatchRecord
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService
from netgravity.ingestion.storage import get_storage


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    app.register_blueprint(create_deep_link_placeholder_blueprint())
    return app.test_client()


def test_ingestion_review_renders_a_real_session(app_client, tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    (upload_dir / "facilities.csv").write_text("facility_id,facility_name,role\nDC1,Test DC,DC\n")
    session = IngestionService(IngestionConfig()).start(upload_dir, client_id="client_a")

    resp = app_client.get(f"/ingestion/{session.run_id}/review")

    assert resp.status_code == 200
    assert session.run_id.encode() in resp.data
    assert b"Placeholder" in resp.data


def test_ingestion_review_404s_for_unknown_session(app_client):
    resp = app_client.get("/ingestion/ing_does_not_exist/review")
    assert resp.status_code == 404


def test_insight_card_renders_dispatch_history(app_client):
    storage = get_storage(IngestionConfig())
    DispatchLogStore(storage).record(DispatchRecord(
        trigger_type="recommendation", reference_id="appr_123",
        recipients=["planner@example.com"], subject="Test subject", result="stubbed"))

    resp = app_client.get("/insights/appr_123")

    assert resp.status_code == 200
    assert b"recommendation" in resp.data
    assert b"Test subject" in resp.data


def test_insight_card_404s_for_unknown_id(app_client):
    resp = app_client.get("/insights/does_not_exist")
    assert resp.status_code == 404
