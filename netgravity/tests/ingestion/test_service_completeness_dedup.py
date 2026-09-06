"""
Tests for IngestionService._maybe_notify_completeness (service.py).

Claim under test: the required/optional missing-data emails fire at most
once per session per kind, using required_notified_at/optional_notified_at
as the dedup flag — a second call with the same missing data must not
trigger a second send.
"""

from __future__ import annotations

from netgravity.action_agent import triggers as action_agent_triggers
from netgravity.ingestion.service import IngestionService
from netgravity.ingestion.session import IngestionSession


def _session_with_gaps(missing_required=None, missing_optional=None) -> IngestionSession:
    return IngestionSession(
        run_id="ing_test123",
        source="/tmp/does-not-matter",
        client_id="test_client",
        report={
            "missing_required": missing_required or [],
            "missing_optional": missing_optional or [],
        },
    )


def test_required_gap_notifies_exactly_once(tmp_config, monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_agent_triggers, "on_completeness_failure",
        lambda session, kind: calls.append(kind),
    )

    service = IngestionService(tmp_config)
    session = _session_with_gaps(missing_required=[{"display_label": "DC Annual Fixed Cost"}])

    service._maybe_notify_completeness(session)
    service._maybe_notify_completeness(session)

    assert calls == ["required"]
    assert session.required_notified_at is not None


def test_optional_gap_notifies_exactly_once(tmp_config, monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_agent_triggers, "on_completeness_failure",
        lambda session, kind: calls.append(kind),
    )

    service = IngestionService(tmp_config)
    session = _session_with_gaps(missing_optional=[{"display_label": "Carbon Emission Factor"}])

    service._maybe_notify_completeness(session)
    service._maybe_notify_completeness(session)

    assert calls == ["optional"]
    assert session.optional_notified_at is not None


def test_required_and_optional_gaps_fire_independently(tmp_config, monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_agent_triggers, "on_completeness_failure",
        lambda session, kind: calls.append(kind),
    )

    service = IngestionService(tmp_config)
    session = _session_with_gaps(
        missing_required=[{"display_label": "DC Annual Fixed Cost"}],
        missing_optional=[{"display_label": "Carbon Emission Factor"}],
    )

    service._maybe_notify_completeness(session)

    assert set(calls) == {"required", "optional"}
    assert session.required_notified_at is not None
    assert session.optional_notified_at is not None


def test_no_gaps_never_notifies(tmp_config, monkeypatch):
    calls = []
    monkeypatch.setattr(
        action_agent_triggers, "on_completeness_failure",
        lambda session, kind: calls.append(kind),
    )

    service = IngestionService(tmp_config)
    session = _session_with_gaps()

    service._maybe_notify_completeness(session)

    assert calls == []
    assert session.required_notified_at is None
    assert session.optional_notified_at is None
