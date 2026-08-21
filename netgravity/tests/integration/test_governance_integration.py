"""
Phase 2 — §15: real risk outputs reaching the existing governance layer.

The governance RULES are not redesigned here and no new action category is
introduced. What is tested is the wiring: that genuine RF, REI, evidence
availability, confidence and grounding state arrive at `ActionClassifier`, and
that the verdict is one of the existing states.

The load-bearing assertion is §15.B — a facility closure is HUMAN_ONLY even when
its REI is the lowest in the network. Irreversibility governs, not exposure. A
system that automated a closure because the numbers looked benign would be
dangerous in exactly the way this project is meant to avoid.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.governance.action_classifier import (
    ActionClassifier,
    GovernancePolicy,
)
from netgravity.orchestrator.schemas.actions import ActionClassification, ActionType
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

from .conftest import build_delhi_network, flood_signal

TOL = 1e-9

VALID_CLASSIFICATIONS = {c.value for c in ActionClassification}


def _risk_run(orch, signal=None, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning for Delhi NCR.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=signal if signal is not None else flood_signal(),
        disable_llm=True, **kwargs,
    ))


def _scenario_run(orch, spec, actor, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Evaluate this change.",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[spec], actor=actor, disable_llm=True, **kwargs,
    ))


# ===========================================================================
# §15 — real evidence reaches the classifier
# ===========================================================================

class TestGovernanceConsumesRealEvidence:

    def test_the_measured_rf_drives_the_verdict(self, orch):
        response = _risk_run(orch)

        assert response.governance.evaluated["risk_factor"] == pytest.approx(
            0.94, abs=TOL
        )
        assert response.governance.classification == ActionClassification.HUMAN_ONLY
        assert response.governance.triggered_rules == ["R6_RISK_FACTOR_HUMAN"]

    def test_the_measured_rei_reaches_the_classifier(self, orch):
        response = _risk_run(orch)
        # The network's maximum exposure, from the real registry.
        assert response.governance.evaluated["rei"] == pytest.approx(1.0, abs=TOL)

    def test_cost_impact_and_service_come_from_the_real_milp(self, orch, planner_actor):
        response = _scenario_run(orch, ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY,
            facility_ids=["DC_DELHI"], capacity_delta_units=-4_950.0,
        ), planner_actor)

        evaluated = response.governance.evaluated
        assert evaluated["cost_impact_pct"] == pytest.approx(16.6667, abs=1e-3)
        assert evaluated["unserved_demand_rate"] == pytest.approx(0.0, abs=TOL)
        assert evaluated["is_feasible"] is True

    def test_the_verdict_is_always_an_existing_category(self, orch, planner_actor):
        """§15: no new action states are introduced by Phase 2."""
        runs = [
            _risk_run(orch, request_id="r1"),
            _risk_run(orch, flood_signal(probability=None), request_id="r2"),
            _risk_run(orch, flood_signal(probability=0.1), request_id="r3"),
            _scenario_run(orch, ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_KOLKATA"],
            ), planner_actor, request_id="r4"),
        ]
        for response in runs:
            assert response.governance is not None
            assert response.governance.classification.value in VALID_CLASSIFICATIONS

    def test_every_verdict_names_the_rule_that_produced_it(self, orch):
        response = _risk_run(orch)
        assert response.governance.triggered_rules
        assert response.governance.reason
        assert response.governance.evaluated


# ===========================================================================
# §15.B / §15.C — structural actions are HUMAN_ONLY regardless of the numbers
# ===========================================================================

class TestStructuralActionsAreAlwaysHuman:

    def test_closing_the_least_exposed_facility_is_still_human_only(
        self, orch, planner_actor,
    ):
        """
        DC_KOLKATA carries the LOWEST exposure in the network (REI 0.40) and
        closing it is cheap. Both signals point at "safe". It is HUMAN_ONLY
        anyway, because closure is effectively irreversible.
        """
        response = _scenario_run(orch, ScenarioIntentSpec(
            action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_KOLKATA"],
        ), planner_actor)

        assert response.governance.classification == ActionClassification.HUMAN_ONLY
        assert "R2_STRUCTURAL_ACTION" in response.governance.triggered_rules
        assert response.status == ExecutionState.REQUIRES_HUMAN.value

    def test_the_rule_fires_before_any_rei_or_cost_threshold_is_read(self):
        """
        Rule ordering is the mechanism, so it is asserted directly: even with
        perfect evidence and zero impact, a closure never reaches a threshold
        rule that could authorise it.
        """
        decision = ActionClassifier().classify(
            action_type=ActionType.CLOSE_FACILITY,
            is_feasible=True, cost_impact_pct=0.0, unserved_demand_rate=0.0,
            rei=0.0, risk_factor=0.0, confidence="HIGH", data_quality_ok=True,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert decision.triggered_rules == ["R2_STRUCTURAL_ACTION"]

    def test_a_permissive_policy_cannot_unlock_a_closure(self):
        """Thresholds are configurable; irreversibility is not."""
        permissive = GovernancePolicy(
            cost_impact_approval_pct=100.0, cost_impact_human_pct=1000.0,
            risk_factor_approval=1.1, risk_factor_human=1.1,
            unserved_demand_human_rate=1.0, min_confidence_for_auto="LOW",
        )
        decision = ActionClassifier(permissive).classify(
            action_type=ActionType.CLOSE_FACILITY, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_opening_a_facility_is_also_structural(self, orch, planner_actor):
        response = _scenario_run(orch, ScenarioIntentSpec(
            action=ScenarioActionType.OPEN_FACILITY, facility_ids=["DC_MUMBAI"],
        ), planner_actor)
        assert response.governance.classification == ActionClassification.HUMAN_ONLY

    def test_no_facility_was_actually_opened_or_closed(self, orch, planner_actor):
        """
        Analysing a closure must not perform one. The observed network is
        unchanged and the change lives only in the scenario overlay.
        """
        before = orch.snapshots.current().network.model_dump_json()
        _scenario_run(orch, ScenarioIntentSpec(
            action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_KOLKATA"],
        ), planner_actor)

        assert orch.snapshots.current().network.model_dump_json() == before
        kolkata = next(f for f in orch.snapshots.current().network.facilities
                       if f.id == "DC_KOLKATA")
        assert kolkata.status.value == "EXISTING"


# ===========================================================================
# §15.A / §15.D — the other categories
# ===========================================================================

class TestOtherClassifications:

    def test_an_analytical_report_may_be_auto(self, orch):
        """§15.A: low-risk informational output, where the rules permit it."""
        response = _risk_run(orch, flood_signal(probability=0.1, nodes=["DC_KOLKATA"]))
        # RF = 0.1 + 0.4 − 0.04 = 0.46, below every escalation threshold.
        assert response.risk["results"][0]["risk_factor"] == pytest.approx(0.46, abs=TOL)
        assert response.governance.classification == ActionClassification.AUTO_ACTION
        assert "R7_ANALYTICAL_ONLY" in response.governance.triggered_rules

    def test_a_reroute_at_moderate_risk_requires_approval(self):
        """§15.D: an operational change the planner must sign off."""
        decision = ActionClassifier().classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.6, rei=0.6,
            confidence="HIGH",
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R8_RISK_FACTOR_APPROVAL" in decision.triggered_rules

    def test_approval_required_raises_a_pinned_approval_request(self, orch,
                                                                planner_actor):
        response = _scenario_run(orch, ScenarioIntentSpec(
            action=ScenarioActionType.SHIFT_VOLUME,
            facility_ids=["DC_KOLKATA"], target_facility_id="DC_MUMBAI",
        ), planner_actor)

        if response.governance.classification == ActionClassification.APPROVAL_REQUIRED:
            assert response.approval is not None
            assert response.approval.baseline_snapshot_id == orch.snapshots.current_id
            assert response.approval.scenario_id == response.scenario_id

    def test_rf_alone_does_not_decide_autonomy(self, orch):
        """
        §15: the same RF produces different verdicts for different action types.
        Risk informs the decision; it does not constitute it.
        """
        classifier = ActionClassifier()
        verdicts = {
            action: classifier.classify(
                action_type=action, risk_factor=0.3, rei=0.3, confidence="HIGH",
            ).classification
            for action in (ActionType.REPORT, ActionType.REROUTE_FLOW,
                           ActionType.CLOSE_FACILITY)
        }
        assert verdicts[ActionType.REPORT] == ActionClassification.AUTO_ACTION
        assert verdicts[ActionType.REROUTE_FLOW] == ActionClassification.AUTO_ACTION
        assert verdicts[ActionType.CLOSE_FACILITY] == ActionClassification.HUMAN_ONLY

    def test_no_new_action_category_was_introduced(self):
        assert VALID_CLASSIFICATIONS == {
            "AUTO_ACTION", "APPROVAL_REQUIRED", "HUMAN_ONLY", "NO_ACTION",
        }


# ===========================================================================
# §15 — missing evidence and grounding both reach governance
# ===========================================================================

class TestEvidenceStateReachesGovernance:

    def test_missing_critical_evidence_is_recorded_on_the_decision(self, orch):
        from .test_failure_propagation import TimingOutREIClient

        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)
        evaluated = response.governance.evaluated

        assert "resilience.assess" in evaluated["missing_critical_evidence"]
        assert evaluated["rei"] is None
        assert evaluated["risk_factor"] is None

    def test_absent_risk_information_is_never_recorded_as_zero(self, orch):
        """
        The distinction the whole design rests on: `rei = None` means "we do not
        know", `rei = 0.0` would mean "we measured, and there is no exposure".
        """
        from .test_failure_propagation import TimingOutREIClient

        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert response.governance.evaluated["rei"] is None
        assert response.governance.evaluated["risk_factor"] is None
        # Explicitly: not the falsy-but-present value that would read as "measured".
        assert response.governance.evaluated["rei"] != 0.0
        assert response.governance.evaluated["risk_factor"] != 0.0

    def test_infeasibility_forces_a_human(self):
        from .conftest import build_infeasible_network

        orch = build_orchestrator(network=build_infeasible_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))
        assert response.status == ExecutionState.INFEASIBLE.value
        assert response.governance.classification in (
            ActionClassification.HUMAN_ONLY, ActionClassification.NO_ACTION,
        )


# ===========================================================================
# §25 / §26 — the verdict is auditable
# ===========================================================================

class TestGovernanceObservability:

    def test_the_decision_event_carries_the_evidence_it_weighed(self, orch):
        response = _risk_run(orch)
        [event] = orch.get_trace(response.execution_id).events_of(
            events.GOVERNANCE_DECISION
        )

        assert event.detail["classification"] == "HUMAN_ONLY"
        assert event.detail["action_type"] == "REPORT"
        assert event.detail["rules"] == ["R6_RISK_FACTOR_HUMAN"]
        assert event.detail["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert event.detail["grounding_failed"] is False
        assert event.detail["execution_id"] == response.execution_id

    def test_the_verdict_is_reconstructable_from_the_sealed_trace(self, orch):
        response = _risk_run(orch)
        decision = orch.get_trace(response.execution_id).governance_decision

        assert decision["classification"] == "HUMAN_ONLY"
        assert decision["triggered_rules"] == ["R6_RISK_FACTOR_HUMAN"]
        assert decision["evaluated"]["risk_factor"] == pytest.approx(0.94, abs=TOL)
