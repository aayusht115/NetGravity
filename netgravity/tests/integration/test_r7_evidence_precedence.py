"""
R7/R7B governance precedence — missing critical evidence must not increase autonomy.

THE POLICY, formally
────────────────────
For an action A that requires evidence E to justify autonomous execution:

    E.state ∈ {UNAVAILABLE, FAILED, STALE, NOT_COMPUTABLE, GROUNDING_FAILED}
        ⟹  AUTO_ACTION is prohibited.

The verdict falls to the next conservative tier the action's own classification
permits — APPROVAL_REQUIRED, or HUMAN_ONLY where a stricter rule already applies.

What the policy does NOT say, and what these tests pin down:

    evidence unavailable  ⇒  autonomy cannot be justified      ✓
    evidence unavailable  ⇒  risk is high                      ✗
    evidence unavailable  ⇒  REI = 0 / RF = 0                  ✗
    evidence unavailable  ⇒  information may not be delivered  ✗

The rule is action-aware. It constrains actions that would have pointed at risk
evidence to justify running unattended; it does not escalate everything that
happens to be missing a number.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.governance.action_classifier import (
    ACTIONS_REQUIRING_RISK_EVIDENCE,
    ActionClassifier,
    EvidenceState,
    GovernancePolicy,
)
from netgravity.orchestrator.schemas.actions import ActionClassification, ActionType
from netgravity.orchestrator.schemas.plans import StepStatus
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

from .conftest import build_delhi_network, flood_signal
from .test_failure_propagation import (
    FailingOptimizationClient,
    StaleREIClient,
    TimingOutREIClient,
)

TOL = 1e-9

REI_DOWN = {"resilience.assess": "UNAVAILABLE: REI engine timed out"}


def _risk_run(orch, signal=None, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning for Delhi NCR.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=signal if signal is not None else flood_signal(),
        disable_llm=True, **kwargs,
    ))


@pytest.fixture
def classifier():
    return ActionClassifier()


# ===========================================================================
# §13 — the precedence regression test
# ===========================================================================

class TestR7PrecedenceRegression:
    """
    Reproduces the original defect directly. These fail if anyone reintroduces
    the short-circuit.

    Before the fix:  R7 → AUTO_ACTION, R7B never evaluated.
    After the fix:   R7 → candidate, R7B evaluated, conservative result settles.
    """

    def test_r7_does_not_short_circuit_r7b(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )

        assert decision.classification != ActionClassification.AUTO_ACTION, (
            "R7 short-circuited R7B: an analytical action reached AUTO_ACTION "
            "while required risk evidence was unavailable"
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R7B_MISSING_CRITICAL_EVIDENCE" in decision.triggered_rules

    def test_r7_is_recorded_as_a_candidate_that_was_overridden(self, classifier):
        """
        The audit trail must show BOTH: R7 matched, and the evidence constraint
        overrode it. A verdict that hid the candidate would be harder to explain.
        """
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.triggered_rules == [
            "R7_ANALYTICAL_ONLY", "R7B_MISSING_CRITICAL_EVIDENCE",
        ]
        assert decision.governing_rule == "R7B_MISSING_CRITICAL_EVIDENCE"

    def test_missing_evidence_never_produces_more_autonomy_than_present_evidence(
        self, classifier,
    ):
        """
        The invariant stated as a comparison, which is what "must not increase
        autonomy" actually means: for every action type, degrading the evidence
        can only move the verdict to an equal or stricter tier.
        """
        strictness = {
            ActionClassification.AUTO_ACTION: 0,
            ActionClassification.APPROVAL_REQUIRED: 1,
            ActionClassification.HUMAN_ONLY: 2,
            ActionClassification.NO_ACTION: 2,
        }

        for action in ActionType:
            with_evidence = classifier.classify(
                action_type=action, confidence="HIGH", rei=0.2, risk_factor=0.2,
                cost_impact_pct=0.5, unserved_demand_rate=0.0,
            )
            without_evidence = classifier.classify(
                action_type=action, confidence="HIGH", rei=None, risk_factor=None,
                cost_impact_pct=0.5, unserved_demand_rate=0.0,
                missing_evidence=REI_DOWN,
            )
            assert strictness[without_evidence.classification] >= \
                strictness[with_evidence.classification], (
                f"{action.value}: losing evidence RELAXED governance from "
                f"{with_evidence.classification.value} to "
                f"{without_evidence.classification.value}"
            )


# ===========================================================================
# §12 — the required test matrix
# ===========================================================================

class TestPolicyMatrix:

    def test_1_existing_r7_auto_case_is_preserved(self, classifier):
        """Valid evidence: R7 behaves exactly as before."""
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            rei=0.3, risk_factor=0.3, cost_impact_pct=0.0,
        )
        assert decision.classification == ActionClassification.AUTO_ACTION
        assert decision.governing_rule == "R7_ANALYTICAL_ONLY"
        assert decision.blocked_by_missing_evidence is False

    def test_2_r7_plus_missing_rei_prohibits_auto(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert decision.blocked_by_missing_evidence is True
        assert decision.evidence_status["resilience.assess"] == \
            EvidenceState.UNAVAILABLE.value

    def test_3_r7_plus_stale_rei_prohibits_auto(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            unresolved_evidence={"risk.compute_rf": EvidenceState.STALE.value},
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert decision.blocked_by_missing_evidence is True
        assert decision.evidence_status["risk.compute_rf"] == EvidenceState.STALE.value

    def test_4_r7_plus_rf_not_computable_prohibits_auto(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            unresolved_evidence={"risk.compute_rf": EvidenceState.NOT_COMPUTABLE.value},
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert decision.blocked_by_missing_evidence is True

    def test_5_r7_plus_grounding_failure_prohibits_auto(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            rei=0.3, risk_factor=0.3, grounding_failed=True,
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R7C_GROUNDING_FAILED" in decision.triggered_rules
        assert decision.evidence_status["reasoning.grounding"] == \
            EvidenceState.GROUNDING_FAILED.value

    def test_6_informational_report_is_still_produced(self):
        """
        §10 — do not overcorrect. The rule constrains ACTION AUTONOMY, not
        INFORMATION DELIVERY. With REI down, the run still completes and the
        narrative still says what it knows.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert response.status != ExecutionState.FAILED.value
        assert response.reasoning is not None
        assert response.reasoning.summary.strip(), "the report still has content"
        assert "resilience.assess" in response.reasoning.summary
        assert "UNKNOWN" in response.reasoning.summary
        # Reasoning ran; only autonomy was withdrawn.
        by_step = {s["step_id"]: s["status"] for s in response.steps}
        assert by_step["reason"] == StepStatus.COMPLETED.value

    def test_7_policy_can_explicitly_permit_autonomy_without_rei(self):
        """
        §7 / §3.B — a genuinely low-stakes informational action stays AUTO if the
        policy explicitly says so. The exemption is declared in policy, not
        obtained by accident of rule ordering, which is the whole difference.
        """
        permissive = GovernancePolicy(
            actions_requiring_risk_evidence=[
                ActionType.REROUTE_FLOW, ActionType.CHANGE_CAPACITY,
                ActionType.OPEN_FACILITY, ActionType.CLOSE_FACILITY,
            ],
        )
        decision = ActionClassifier(permissive).classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.classification == ActionClassification.AUTO_ACTION

        # And the default policy does NOT grant that exemption.
        assert ActionClassifier().classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        ).classification == ActionClassification.APPROVAL_REQUIRED

    def test_7b_a_workflow_that_never_needed_rei_keeps_its_autonomy(self):
        """
        The natural, un-configured case for §3.B. A network-state query plans no
        REI step, so no REI evidence is missing — there is no gap, and nothing
        escalates. Evidence is only "missing" when the workflow asked for it.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))

        assert response.governance.evaluated["missing_critical_evidence"] == []
        assert response.governance.evaluated["unresolved_evidence"] == {}
        assert response.governance.classification == ActionClassification.AUTO_ACTION
        assert response.governance.blocked_by_missing_evidence is False

    def test_8_facility_closure_with_low_rei_is_human_only(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.CLOSE_FACILITY, confidence="HIGH",
            rei=0.01, risk_factor=0.01, cost_impact_pct=0.1,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert decision.governing_rule == "R2_STRUCTURAL_ACTION"

    def test_9_facility_closure_with_missing_rei_is_human_only(self, classifier):
        """
        Still HUMAN_ONLY, and still via R2 — the structural rule outranks the
        evidence rule. Escalating to APPROVAL_REQUIRED here would be a
        RELAXATION, which is exactly what the fix must not cause.
        """
        decision = classifier.classify(
            action_type=ActionType.CLOSE_FACILITY, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert decision.governing_rule == "R2_STRUCTURAL_ACTION"

    def test_10_network_restructuring_with_low_rf_is_human_only(self, classifier):
        for action in (ActionType.OPEN_FACILITY, ActionType.CLOSE_FACILITY):
            decision = classifier.classify(
                action_type=action, confidence="HIGH",
                rei=0.0, risk_factor=0.0, cost_impact_pct=0.0,
            )
            assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_11_valid_evidence_restores_autonomy(self, classifier):
        """
        Proves the rule is a constraint, not a permanent disablement of AUTO.
        Same action, same policy, evidence restored ⇒ original verdict returns.
        """
        blocked = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        restored = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            rei=0.3, risk_factor=0.3,
        )
        assert blocked.classification == ActionClassification.APPROVAL_REQUIRED
        assert restored.classification == ActionClassification.AUTO_ACTION
        assert restored.governing_rule == "R7_ANALYTICAL_ONLY"

        # The same round trip for an operational action.
        reroute_blocked = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH",
            cost_impact_pct=0.5, unserved_demand_rate=0.0, missing_evidence=REI_DOWN,
        )
        reroute_restored = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH",
            cost_impact_pct=0.5, unserved_demand_rate=0.0, rei=0.1, risk_factor=0.1,
        )
        assert reroute_blocked.classification == ActionClassification.APPROVAL_REQUIRED
        assert reroute_restored.classification == ActionClassification.AUTO_ACTION
        assert reroute_restored.governing_rule == "R11_REVERSIBLE_LOW_IMPACT"

    def test_12_missing_evidence_does_not_become_zero(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.evaluated["rei"] is None
        assert decision.evaluated["risk_factor"] is None
        assert decision.evaluated["rei"] != 0.0
        assert decision.evaluated["risk_factor"] != 0.0

        # And the recorded reason says EVIDENCE, not RISK.
        assert "evidence is unavailable" in decision.reason
        assert "UNKNOWN rather than zero" in decision.reason
        assert "risk is high" not in decision.reason.lower()


# ===========================================================================
# §15 — action-awareness, not a blanket override
# ===========================================================================

class TestActionAwareness:

    def test_hypothetical_scenarios_are_exempt(self, classifier):
        """
        A scenario cannot touch observed state, so no measurement is load-bearing
        for its safety. Escalating it would be the overcorrection §3 warns about.
        """
        decision = classifier.classify(
            action_type=ActionType.CREATE_SCENARIO, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        assert decision.classification == ActionClassification.AUTO_ACTION
        assert ActionType.CREATE_SCENARIO not in ACTIONS_REQUIRING_RISK_EVIDENCE

    def test_nothing_proposed_stays_no_action(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.NONE, missing_evidence=REI_DOWN,
        )
        assert decision.classification == ActionClassification.NO_ACTION

    def test_non_critical_missing_evidence_does_not_escalate(self, classifier):
        """A lost narrative is not lost risk evidence."""
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH", rei=0.2, risk_factor=0.2,
            missing_evidence={"reasoning.synthesise": "UNAVAILABLE: llm down"},
        )
        assert decision.classification == ActionClassification.AUTO_ACTION
        assert decision.evaluated["missing_critical_evidence"] == []

    def test_the_requirement_set_is_explicit_and_narrow(self):
        assert ACTIONS_REQUIRING_RISK_EVIDENCE == {
            ActionType.REPORT, ActionType.REROUTE_FLOW, ActionType.CHANGE_CAPACITY,
            ActionType.OPEN_FACILITY, ActionType.CLOSE_FACILITY,
        }
        assert ActionType.NONE not in ACTIONS_REQUIRING_RISK_EVIDENCE
        assert ActionType.CREATE_SCENARIO not in ACTIONS_REQUIRING_RISK_EVIDENCE


# ===========================================================================
# §7 / §8 — end to end through the real orchestrator
# ===========================================================================

class TestEndToEndEvidencePrecedence:

    def test_rei_failure_withdraws_autonomy_from_the_risk_report(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert response.governance.classification != ActionClassification.AUTO_ACTION
        assert response.governance.classification == \
            ActionClassification.APPROVAL_REQUIRED
        assert response.governance.blocked_by_missing_evidence is True
        assert "resilience.assess" in response.governance.evidence_status

    def test_stale_rei_withdraws_autonomy_even_though_no_step_failed(self):
        """
        §8's subtle case. `resilience.assess` SUCCEEDS and returns valid numbers;
        RF refuses them because they belong to another snapshot. No failed step
        exists for `missing_evidence` to notice, so without `unresolved_evidence`
        governance would treat this run as fully evidenced.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = StaleREIClient("snap_V17")
        response = _risk_run(orch)

        by_step = {s["step_id"]: s["status"] for s in response.steps}
        assert by_step["rei"] == StepStatus.COMPLETED.value, "no step failed"
        assert response.governance.evaluated["missing_critical_evidence"] == []

        assert response.governance.evaluated["unresolved_evidence"] == {
            "risk.compute_rf": EvidenceState.STALE.value
        }
        assert response.governance.classification != ActionClassification.AUTO_ACTION
        assert response.governance.blocked_by_missing_evidence is True

    def test_p_available_but_rei_missing_blocks_auto(self):
        """§8 verbatim: P available, REI missing, RF NOT_COMPUTABLE ⇒ no AUTO."""
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch, flood_signal(probability=0.7))

        assert response.risk["not_computable"][0]["likelihood"] == pytest.approx(0.7)
        assert response.risk["not_computable"][0]["rei"] is None
        assert response.risk["max_risk_factor"] is None
        assert response.governance.classification != ActionClassification.AUTO_ACTION

    def test_no_event_probability_alone_does_not_withdraw_autonomy(self):
        """
        The deliberate non-escalation. `NO_EVENT_PROBABILITY` means nobody
        asserted an event — nothing is missing. Treating it as an evidence
        failure would penalise every ordinary resilience query with no live
        incident attached, which is the overcorrection §10 forbids.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="Why is DC_DELHI considered high risk?", disable_llm=True,
        ))

        assert response.intent == Intent.EXPLANATION.value
        assert response.risk["max_risk_factor"] is None

        # Two non-assertion refusals appear, and neither escalates:
        #   NO_EVENT_PROBABILITY — the assessed DCs have REI but no event.
        #   NO_INPUTS            — PLANT_N is infeasible so has no REI either;
        #                          with no P asserted, that is still "nothing was
        #                          claimed", not "evidence went missing".
        assert {r["not_computable_reason"] for r in response.risk["not_computable"]} == {
            "NO_EVENT_PROBABILITY", "NO_INPUTS",
        }
        assert response.governance.evaluated["unresolved_evidence"] == {}
        assert response.governance.action_type == ActionType.REPORT
        assert response.governance.classification == ActionClassification.AUTO_ACTION
        assert response.governance.blocked_by_missing_evidence is False

    def test_valid_evidence_restores_the_original_verdict_end_to_end(self):
        """§19 Trace A vs B, on one orchestrator: degrade, then restore."""
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)

        healthy = _risk_run(orch, flood_signal(probability=0.1, nodes=["DC_KOLKATA"]),
                            request_id="healthy")
        assert healthy.governance.classification == ActionClassification.AUTO_ACTION

        orch.services["rei"] = TimingOutREIClient()
        degraded = _risk_run(orch, flood_signal(probability=0.1, nodes=["DC_KOLKATA"]),
                             request_id="degraded")
        assert degraded.governance.classification == \
            ActionClassification.APPROVAL_REQUIRED

        # Restore the real service; the original verdict comes back.
        orch.services["rei"] = build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
        ).services["rei"]
        restored = _risk_run(orch, flood_signal(probability=0.1, nodes=["DC_KOLKATA"]),
                             request_id="restored")
        assert restored.governance.classification == ActionClassification.AUTO_ACTION
        assert restored.governance.blocked_by_missing_evidence is False

    def test_grounding_failure_withdraws_autonomy_from_a_report(self):
        """§9 — governance respects the grounding verdict; grounding is untouched."""
        import json
        from .conftest import FakeGateway, reasoning_json

        gateway = FakeGateway({
            "reasoning": reasoning_json(
                summary="Exposure rose by 50% this period.",
                claims=[{"type": "max_rei", "value": 50, "unit": "percent",
                         "text": "50%"}],
            ),
        })
        orch = build_orchestrator(network=build_delhi_network(), gateway=gateway)
        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi NCR.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(probability=0.1, nodes=["DC_KOLKATA"]),
        ))

        assert response.reasoning.grounding_status == "GROUNDING_FAILED"
        assert response.governance.classification != ActionClassification.AUTO_ACTION


# ===========================================================================
# §14 — settle precedence is preserved
# ===========================================================================

class TestSettlePrecedenceUnchanged:

    def test_a_required_step_failure_still_outranks_the_governance_verdict(self):
        """
        A verdict about an analysis that did not complete would imply a usable
        result exists. The run must read FAILED, not REQUIRES_APPROVAL, even
        though governance produced a verdict.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["optimization"] = FailingOptimizationClient()
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))

        assert response.governance is not None
        assert response.status == ExecutionState.FAILED.value
        assert "network" not in response.results

    def test_a_soft_failure_yields_a_usable_result_plus_a_conservative_verdict(self):
        """
        The distinction §14 asks to preserve: an optional-step failure produces a
        usable result governed conservatively, which is a materially different
        outcome from a required-step failure.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert response.status == ExecutionState.REQUIRES_APPROVAL.value
        assert response.status != ExecutionState.FAILED.value
        assert response.reasoning is not None and response.reasoning.summary.strip()
        assert response.governance.blocked_by_missing_evidence is True

    def test_human_only_does_not_mask_a_required_failure(self):
        """HUMAN_ONLY must not be reported when the analysis itself broke."""
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["optimization"] = FailingOptimizationClient()
        response = orch.run_sync(OrchestratorRequest(
            input="Close DC_KOLKATA.",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY,
                facility_ids=["DC_KOLKATA"])],
            disable_llm=True,
        ))

        assert response.governance.classification == ActionClassification.HUMAN_ONLY
        assert response.status == ExecutionState.FAILED.value, (
            "the required-failure check must run before the governance verdict"
        )


# ===========================================================================
# §16 — the output contract is reconstructable
# ===========================================================================

class TestOutputContract:

    def test_the_decision_exposes_every_required_field(self, classifier):
        decision = classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )

        assert decision.action_type == ActionType.REPORT          # action_tier
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert decision.governing_rule == "R7B_MISSING_CRITICAL_EVIDENCE"
        assert decision.evidence_status == {
            "resilience.assess": EvidenceState.UNAVAILABLE.value
        }
        assert decision.blocked_by_missing_evidence is True
        assert decision.reason.startswith(
            "Autonomous execution is not permitted because required risk evidence"
        )

    def test_the_reason_describes_evidence_not_risk(self, classifier):
        """§16 — do not claim 'risk is high' when the issue is missing evidence."""
        decision = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH",
            missing_evidence=REI_DOWN,
        )
        lowered = decision.reason.lower()
        assert "evidence is unavailable" in lowered
        assert "risk is high" not in lowered
        assert "high risk" not in lowered

    def test_a_high_risk_verdict_still_describes_risk(self, classifier):
        """The mirror: when risk IS measured and high, say so."""
        decision = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH", risk_factor=0.94,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert "Risk factor 0.940" in decision.reason
        assert decision.blocked_by_missing_evidence is False

    def test_the_governance_event_carries_the_evidence_contract(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        [event] = orch.get_trace(response.execution_id).events_of(
            events.GOVERNANCE_DECISION
        )
        assert event.detail["blocked_by_missing_evidence"] is True
        assert event.detail["governing_rule"] == "R7B_MISSING_CRITICAL_EVIDENCE"
        assert "resilience.assess" in event.detail["evidence_status"]
        assert event.detail["rei"] is None
        assert event.detail["risk_factor"] is None
