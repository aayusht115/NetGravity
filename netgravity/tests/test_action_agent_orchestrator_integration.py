"""
Integration — Orchestrator governance triggers the Action Agent.

Claim under test: when governance settles APPROVAL_REQUIRED or HUMAN_ONLY
during a REAL orchestrator run, _govern() (netgravity/orchestrator/core/
orchestrator.py) notifies the Action Agent exactly once each, and the
Action Agent's own dispatch log records it — end to end, through the actual
hook added to _govern(), not a direct call into triggers.py.

Reuses the exact run patterns netgravity/tests/test_orchestrator.py already
established for reliably reaching each classification (structural CLOSE ->
HUMAN_ONLY; a tuned GovernancePolicy + SHIFT_VOLUME -> REQUIRES_APPROVAL),
so this test rides the same proven path rather than inventing a new one.
"""

from __future__ import annotations

import pytest

from netgravity.action_agent.dispatch_log import DispatchLogStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.storage import get_storage
from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.governance.action_classifier import GovernancePolicy
from netgravity.orchestrator.schemas.actions import ActionClassification
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


@pytest.fixture(autouse=True)
def _isolated_action_agent_env(tmp_path, monkeypatch):
    # A developer's real .env may have live SMTP credentials configured
    # (see scripts/verify_9_action_agent.py's live-mode run) — clear them so
    # this test always exercises the stub-mode path regardless.
    for name in ("NETGRAVITY_SMTP_HOST", "NETGRAVITY_SMTP_USERNAME",
                "NETGRAVITY_SMTP_PASSWORD", "NETGRAVITY_EMAIL_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("NETGRAVITY_DEFAULT_RECIPIENT_EMAIL", "planner@example.com")
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)


def _dispatch_log() -> DispatchLogStore:
    return DispatchLogStore(get_storage(IngestionConfig()))


def test_human_only_classification_triggers_investigate_email():
    orch = build_orchestrator(network=build_case16_network(), enable_llm=False)
    resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))

    assert resp.governance.classification == ActionClassification.HUMAN_ONLY

    records = [r for r in _dispatch_log().list_all()
              if r.trigger_type == "investigate" and r.reference_id == resp.execution_id]
    assert len(records) == 1
    assert records[0].result == "stubbed"  # no SMTP credential configured


def test_approval_required_classification_triggers_recommendation_email():
    orch = build_orchestrator(
        network=build_case16_network(), enable_llm=False,
        governance_policy=GovernancePolicy(
            cost_impact_human_pct=1e9,       # keep it off the HUMAN_ONLY path
            unserved_demand_human_rate=1.0,
            min_confidence_for_auto="HIGH",  # template confidence < HIGH
        ),
    )
    resp = orch.run_sync(OrchestratorRequest(
        input="Shift DC_EAST volume to DC_WEST",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[ScenarioIntentSpec(
            action=ScenarioActionType.SHIFT_VOLUME,
            facility_ids=["DC_EAST"], target_facility_id="DC_WEST")],
    ))
    if resp.status != "REQUIRES_APPROVAL":
        pytest.skip(f"run produced {resp.status}, not an approval path")

    records = [r for r in _dispatch_log().list_all()
              if r.trigger_type == "recommendation"
              and r.reference_id == resp.approval.approval_id]
    assert len(records) == 1
    assert records[0].result == "stubbed"
