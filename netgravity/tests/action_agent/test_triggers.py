"""
Tests for netgravity.action_agent.triggers — the five entry points.

These exercise the dispatcher end to end (build email -> stub-send ->
record dispatch) against a tmp-path storage root, without touching the
repo's real ./data directory. Fake approval/context/decision objects stand
in for the real orchestrator/ingestion types — only the attributes triggers.py
actually reads are populated (record_key style, matching the repo's
_Fake... convention rather than unittest.mock).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from netgravity.action_agent import triggers
from netgravity.action_agent.dispatch_log import DispatchLogStore
from netgravity.action_agent.recipients import NotificationRecipientStore, SourceContactStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.storage import get_storage


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    """Every action_agent store built inside triggers.py goes through
    get_storage(IngestionConfig()) — redirect NETGRAVITY_DATA_ROOT so tests
    never touch the repo's real ./data directory."""
    monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _fake_session(run_id="ing_1", client_id="client_a", required=None, optional=None):
    return SimpleNamespace(
        run_id=run_id, client_id=client_id, source="/uploads/whatever",
        created_at="2026-08-30T00:00:00", client_id_=client_id,
        report={"missing_required": required or [], "missing_optional": optional or []},
    )


def _fake_context(execution_id="exec_1", headline="Something happened", narrative="Details."):
    # Mirrors the REAL ExecutiveBriefing/KPIInsight shape: headline/narrative
    # live on kpi_insights[i], not on the briefing itself.
    insight = SimpleNamespace(headline=headline, narrative=narrative)
    briefing = SimpleNamespace(kpi_insights=[insight], opening="", context="",
                               recommendation="")
    reasoning = SimpleNamespace(briefing=briefing, summary="", recommendation="")
    return SimpleNamespace(execution_id=execution_id, reasoning=reasoning,
                           baseline_snapshot_id="snap_abc")


def test_on_completeness_failure_required_sends_and_logs(_isolated_data_root):
    storage = get_storage(IngestionConfig())
    SourceContactStore(storage).set("client_a", "owner@clienta.com", contact_name="Owner")

    session = _fake_session(required=[
        {"entity_type": "Candidate DC", "entity_name": "Pune DC",
         "display_label": "DC Annual Fixed Cost (₹ lakh/year)"},
    ])

    triggers.on_completeness_failure(session, kind="required")

    log = DispatchLogStore(storage)
    assert log.already_dispatched("required_data", "ing_1") is True


def test_on_completeness_failure_optional_sends_and_logs(_isolated_data_root):
    storage = get_storage(IngestionConfig())
    SourceContactStore(storage).set("client_a", "owner@clienta.com")

    session = _fake_session(optional=[
        {"display_label": "Carbon Emission Factor (kg CO₂/unit)",
         "what_it_unlocks": "would let us include a carbon-impact KPI"},
    ])

    triggers.on_completeness_failure(session, kind="optional")

    log = DispatchLogStore(storage)
    assert log.already_dispatched("optional_data", "ing_1") is True


def test_on_completeness_failure_no_contact_sends_nothing(_isolated_data_root):
    storage = get_storage(IngestionConfig())
    session = _fake_session(required=[{"entity_type": "Candidate DC", "entity_name": "X",
                                       "display_label": "DC Annual Fixed Cost"}])

    triggers.on_completeness_failure(session, kind="required")

    log = DispatchLogStore(storage)
    assert log.already_dispatched("required_data", "ing_1") is False


def test_on_recommendation_card_created_sends_and_dedups(_isolated_data_root):
    storage = get_storage(IngestionConfig())
    NotificationRecipientStore(storage).add("planner@example.com")

    approval = SimpleNamespace(approval_id="appr_1", execution_id="exec_1",
                               baseline_snapshot_id="snap_abc")
    context = _fake_context(execution_id="exec_1", headline="Close Pune DC")

    triggers.on_recommendation_card_created(approval, context)
    triggers.on_recommendation_card_created(approval, context)  # must not double-send

    log = DispatchLogStore(storage)
    records = [r for r in log.list_all()
              if r.trigger_type == "recommendation" and r.reference_id == "appr_1"]
    assert len(records) == 1
    assert records[0].result == "stubbed"


def test_on_investigate_card_created_sends_and_dedups(_isolated_data_root):
    storage = get_storage(IngestionConfig())
    NotificationRecipientStore(storage).add("planner@example.com")

    decision = SimpleNamespace(reason="required risk evidence unresolved")
    context = _fake_context(execution_id="exec_2", headline="Investigate lane disruption")

    triggers.on_investigate_card_created("exec_2", decision, context)
    triggers.on_investigate_card_created("exec_2", decision, context)

    log = DispatchLogStore(storage)
    records = [r for r in log.list_all()
              if r.trigger_type == "investigate" and r.reference_id == "exec_2"]
    assert len(records) == 1


def test_recommendation_and_investigate_do_not_collide_on_same_id(_isolated_data_root):
    """A recommendation for execution X and a later investigate case that
    happens to reuse the same execution_id as its approval's execution_id
    must be tracked as separate dispatches — trigger_type is part of the
    dedup key, not just reference_id."""
    storage = get_storage(IngestionConfig())
    NotificationRecipientStore(storage).add("planner@example.com")

    approval = SimpleNamespace(approval_id="shared_id", execution_id="shared_id",
                               baseline_snapshot_id="snap_abc")
    context = _fake_context(execution_id="shared_id")
    decision = SimpleNamespace(reason="moderate risk")

    triggers.on_recommendation_card_created(approval, context)
    triggers.on_investigate_card_created("shared_id", decision, context)

    log = DispatchLogStore(storage)
    assert log.already_dispatched("recommendation", "shared_id") is True
    assert log.already_dispatched("investigate", "shared_id") is True
