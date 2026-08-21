"""
Phase 2 — Workflow A: external risk assessment, end to end.

    External Event → Orchestrator → Event Interpretation → Explicit P
      → Affected Node Mapping → REI Lookup → Snapshot Validation
      → RF Calculation → Reasoning → Numeric Grounding → Action Governance
      → Final Risk Assessment

Every number here is produced by the real MILP, the real REI engine and the real
RF calculator. The acceptance figures are arithmetic on the fixture network:

    C0 = 1,200 ; PI(DELHI) = 400 ; max EI = 500 ⇒ REI = 0.80
    P  = 0.70  ⇒ RF = 0.7 + 0.8 − 0.56 = 0.94
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator.audit import events
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.schemas.requests import (
    EventSeverity,
    Intent,
    OrchestratorRequest,
)
from netgravity.orchestrator.schemas.risk import RFNotComputableReason, RFStatus

from .conftest import flood_signal

TOL = 1e-9


def _run(orch, signal, actor=None, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input=kwargs.pop("input", "Flood warning issued for the Delhi NCR region."),
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=signal,
        actor=actor,
        disable_llm=True,
        **kwargs,
    ))


# ===========================================================================
# §5 / §18 — the normal path
# ===========================================================================

class TestExternalRiskHappyPath:

    def test_rf_is_exactly_0_94(self, orch, planner_actor):
        response = _run(orch, flood_signal(probability=0.7, nodes=["DC_DELHI"]),
                        planner_actor)

        assert response.risk is not None
        rows = response.risk["results"]
        assert len(rows) == 1, "only the mapped node is assessed"

        row = rows[0]
        assert row["facility_id"] == "DC_DELHI"
        assert row["likelihood"] == pytest.approx(0.7, abs=TOL)
        assert row["rei"] == pytest.approx(0.8, abs=TOL)
        assert row["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert row["status"] == RFStatus.COMPUTED.value
        assert row["formula"] == "RF = P + REI - P*REI"

    def test_workflow_reaches_a_terminal_state_with_a_verdict(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)

        # RF = 0.94 is above the 0.8 human-decision threshold, so the governed
        # outcome is REQUIRES_HUMAN. That IS the workflow completing correctly —
        # a high-risk finding that auto-approved would be the failure.
        assert response.status == ExecutionState.REQUIRES_HUMAN.value
        assert response.governance is not None
        assert response.governance.classification.value == "HUMAN_ONLY"
        assert "R6_RISK_FACTOR_HUMAN" in response.governance.triggered_rules

    def test_every_provenance_field_is_retained(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)
        risk = response.risk
        row = risk["results"][0]

        assert response.execution_id
        assert response.network_snapshot_id == orch.snapshots.current_id
        assert response.intent == Intent.EXTERNAL_EVENT.value
        assert risk["network_id"] == "PHASE2_DELHI"
        assert risk["data_version"]
        assert row["provenance"]["likelihood"].startswith("external_signal:")
        assert "india_met_department" in row["provenance"]["likelihood"]
        assert row["provenance"]["rei"].startswith("rei_registry:")
        assert orch.snapshots.current_id in row["provenance"]["rei"]

    def test_rei_and_rf_agree_with_the_registry(self, orch, planner_actor):
        """RF's REI is the SAME number the registry published, not a re-derivation."""
        response = _run(orch, flood_signal(), planner_actor)
        registry = response.results["resilience"]

        assert registry["rei_by_facility"]["DC_DELHI"] == pytest.approx(0.8, abs=TOL)
        assert registry["max_rei"] == pytest.approx(1.0, abs=TOL)
        assert registry["highest_exposure_facility"] == "DC_MUMBAI"
        assert response.risk["results"][0]["rei"] == registry["rei_by_facility"]["DC_DELHI"]

    def test_baseline_business_cost_is_the_hand_calculated_value(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)
        assert response.results["resilience"]["baseline_business_cost"] == pytest.approx(
            1200.0, abs=1e-6
        )

    def test_reasoning_ran_and_cites_the_deterministic_figures(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)
        assert response.reasoning is not None
        assert response.reasoning.source == "template"      # LLM disabled
        assert "0.940" in response.reasoning.summary
        assert response.reasoning.grounding_status in ("GROUNDED", "NO_CLAIMS")

    def test_reasoning_did_not_recalculate_rf(self, orch, planner_actor):
        """
        The narrative may quote RF; it may not produce one.

        Proven structurally: the only RF in the response comes from
        `ctx.risk_results`, which the reasoning agent receives read-only and
        cannot write to.
        """
        response = _run(orch, flood_signal(), planner_actor)
        rf = response.risk["results"][0]["risk_factor"]
        assert rf == pytest.approx(0.94, abs=TOL)
        # And the narrative's figure is grounded against that same value.
        assert response.reasoning.grounding_status != "GROUNDING_FAILED"


# ===========================================================================
# §5 — node mapping is explicit, never a broadcast
# ===========================================================================

class TestNodeMapping:

    def test_an_event_at_delhi_does_not_assess_mumbai(self, orch, planner_actor):
        response = _run(orch, flood_signal(nodes=["DC_DELHI"]), planner_actor)
        assessed = {r["facility_id"] for r in response.risk["results"]}
        assert assessed == {"DC_DELHI"}, (
            "a flood in Delhi says nothing about Mumbai's probability"
        )

    def test_location_string_resolves_when_no_entity_is_named(self, orch, planner_actor):
        response = _run(orch, flood_signal(nodes=[], location="DELHI"), planner_actor)
        assessed = {r["facility_id"] for r in response.risk["results"]}
        assert assessed == {"DC_DELHI"}

    def test_two_named_nodes_both_get_their_own_rf(self, orch, planner_actor):
        response = _run(
            orch, flood_signal(probability=0.5, nodes=["DC_DELHI", "DC_KOLKATA"]),
            planner_actor,
        )
        by_node = {r["facility_id"]: r for r in response.risk["results"]}
        assert set(by_node) == {"DC_DELHI", "DC_KOLKATA"}
        # RF = 0.5 + 0.8 − 0.40 = 0.90 ; 0.5 + 0.4 − 0.20 = 0.70
        assert by_node["DC_DELHI"]["risk_factor"] == pytest.approx(0.90, abs=TOL)
        assert by_node["DC_KOLKATA"]["risk_factor"] == pytest.approx(0.70, abs=TOL)


# ===========================================================================
# §7 — severity is not probability
# ===========================================================================

class TestMissingEventProbability:

    def test_severe_without_probability_yields_not_computable(self, orch, planner_actor):
        response = _run(
            orch,
            flood_signal(probability=None, severity=EventSeverity.SEVERE),
            planner_actor,
        )

        assert response.risk["results"] == []
        rows = response.risk["not_computable"]
        assert len(rows) == 1
        assert rows[0]["facility_id"] == "DC_DELHI"
        assert rows[0]["not_computable_reason"] == (
            RFNotComputableReason.NO_EVENT_PROBABILITY.value
        )
        assert rows[0]["risk_factor"] is None
        assert rows[0]["likelihood"] is None, "P is UNKNOWN, never 0"

    def test_no_probability_is_inferred_from_severity(self, orch, planner_actor):
        """The classic failure: SEVERE quietly becoming P = 0.7 via a lookup table."""
        for severity in (EventSeverity.MODERATE, EventSeverity.HIGH,
                         EventSeverity.SEVERE, EventSeverity.CRITICAL):
            response = _run(
                orch, flood_signal(probability=None, severity=severity), planner_actor,
                request_id=f"sev-{severity.value}",
            )
            assert response.risk["max_risk_factor"] is None
            assert all(r["likelihood"] is None for r in response.risk["not_computable"])

    def test_the_narrative_says_p_is_unavailable(self, orch, planner_actor):
        response = _run(orch, flood_signal(probability=None), planner_actor)
        summary = response.reasoning.summary
        assert "NOT calculated" in summary
        assert RFNotComputableReason.NO_EVENT_PROBABILITY.value in summary
        assert "NO defensible probability" in summary

    def test_rei_is_still_reported_when_p_is_missing(self, orch, planner_actor):
        """Losing P costs us RF, not the exposure analysis."""
        response = _run(orch, flood_signal(probability=None), planner_actor)
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)


# ===========================================================================
# §9 — node mapping failure
# ===========================================================================

class TestNodeMappingFailure:

    def test_unknown_facility_is_refused_explicitly(self, orch, planner_actor):
        response = _run(
            orch, flood_signal(nodes=["DC_ATLANTIS"], location="Atlantis"),
            planner_actor,
        )
        rows = response.risk["not_computable"]
        assert len(rows) == 1
        assert rows[0]["not_computable_reason"] == (
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE.value
        )
        assert rows[0]["facility_id"] is None, "no arbitrary node is substituted"
        assert rows[0]["risk_factor"] is None

    def test_no_arbitrary_facility_is_selected(self, orch, planner_actor):
        response = _run(orch, flood_signal(nodes=["XYZ"], location="XYZ"), planner_actor)
        assert response.risk["results"] == []
        assert response.risk["max_risk_factor"] is None
        assert response.risk["highest_risk_entity"] is None

    def test_a_partially_resolvable_event_uses_only_the_real_node(self, orch, planner_actor):
        response = _run(
            orch, flood_signal(nodes=["DC_DELHI", "DC_ATLANTIS"]), planner_actor,
        )
        assert {r["facility_id"] for r in response.risk["results"]} == {"DC_DELHI"}
        assert any("DC_ATLANTIS" in w for w in response.risk["warnings"])


# ===========================================================================
# §26 — observability of the risk chain
# ===========================================================================

class TestRiskChainObservability:

    def test_the_chain_emits_its_canonical_events(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)
        trace = orch.get_trace(response.execution_id)
        emitted = {e.event_type for e in trace.events}

        for required in (
            events.WORKFLOW_STARTED, events.STEP_STARTED, events.STEP_COMPLETED,
            events.REI_LOOKUP, events.RF_CALCULATED, events.REASONING_COMPLETED,
            events.GROUNDING_COMPLETED, events.GOVERNANCE_DECISION,
            events.WORKFLOW_COMPLETED,
        ):
            assert required in emitted, f"missing observability event: {required}"

    def test_rf_calculated_event_carries_the_inputs_and_the_output(self, orch, planner_actor):
        response = _run(orch, flood_signal(), planner_actor)
        trace = orch.get_trace(response.execution_id)
        [event] = trace.events_of(events.RF_CALCULATED)

        assert event.detail["node_id"] == "DC_DELHI"
        assert event.detail["event_probability"] == pytest.approx(0.7, abs=TOL)
        assert event.detail["rei"] == pytest.approx(0.8, abs=TOL)
        assert event.detail["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert event.detail["execution_id"] == response.execution_id
        assert event.detail["workflow_id"] == "wf_external_event"
        assert event.detail["snapshot_id"] == orch.snapshots.current_id

    def test_rf_not_computable_event_names_the_reason(self, orch, planner_actor):
        response = _run(orch, flood_signal(probability=None), planner_actor)
        trace = orch.get_trace(response.execution_id)
        [event] = trace.events_of(events.RF_NOT_COMPUTABLE)
        assert event.detail["reason"] == RFNotComputableReason.NO_EVENT_PROBABILITY.value
