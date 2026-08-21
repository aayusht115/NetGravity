"""
Phase 2 — §6, §16, §17, §19: the failure matrix.

Every major component is failed deliberately and the whole system's response is
checked: step status, dependency behaviour, evidence state, final workflow
status, and — the point of the exercise — that nothing is fabricated to paper
over the gap.

The four prohibitions asserted throughout:

    no fabricated zero        missing REI never becomes REI = 0
    no fabricated value       missing P never becomes an inferred P
    no silent failure         every absence is named in the response
    no baseline corruption    observed state survives every failure path

Failures are injected at the SERVICE boundary (swapping a client on
`orch.services`), not by monkeypatching internals, so the orchestrator's real
error taxonomy, retry policy and dependency resolution all execute.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.engines.deterministic import REIClient
from netgravity.orchestrator.exceptions import EngineFailureError
from netgravity.orchestrator.schemas.plans import StepStatus
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.schemas.risk import RFNotComputableReason

from .conftest import build_delhi_network, build_infeasible_network, flood_signal

TOL = 1e-9


# ---------------------------------------------------------------------------
# Injectable failures
# ---------------------------------------------------------------------------

class TimingOutREIClient(REIClient):
    """REI that never returns in time. Models a genuine engine timeout."""

    async def assess_registry(self, *args: Any, **kwargs: Any):
        raise EngineFailureError(
            "REI assessment failed: TimeoutError: the REI engine exceeded its "
            "600s budget while re-optimising facility disruptions.",
            context={"engine": "rei"},
        )


class ExplodingREIClient(REIClient):
    """REI that raises something unexpected."""

    async def assess_registry(self, *args: Any, **kwargs: Any):
        raise RuntimeError("segfault in the exposure kernel")


class StaleREIClient(REIClient):
    """
    REI that returns a perfectly valid batch — computed against the WRONG
    snapshot. This is the dangerous failure: nothing errors, the numbers look
    fine, and combining them would be silently wrong.
    """

    def __init__(self, stale_snapshot_id: str = "snap_v17") -> None:
        super().__init__()
        self.stale_snapshot_id = stale_snapshot_id

    async def assess_registry(self, network, *, snapshot_id=None, **kwargs: Any):
        registry = await super().assess_registry(
            network, snapshot_id=self.stale_snapshot_id, **kwargs
        )
        return registry


class FailingOptimizationClient:
    """MILP that raises. Wraps nothing — the step must fail, not degrade."""

    async def solve(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise EngineFailureError("MILP engine raised OSError: solver binary missing")

    async def solve_scenario(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise EngineFailureError("MILP engine raised OSError: solver binary missing")


class FailingReasoningAgent:
    """Reasoning that blows up. Advisory, so the run must survive it."""

    def reason(self, *args: Any, **kwargs: Any):
        raise RuntimeError("reasoning agent crashed")


def _event_run(orch, signal=None, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning for Delhi NCR.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=signal if signal is not None else flood_signal(),
        disable_llm=True,
        **kwargs,
    ))


# ===========================================================================
# 1–4. MILP outcomes
# ===========================================================================

class TestMILPFailures:

    def test_1_milp_success(self, orch):
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))
        assert response.results["network"]["is_feasible"] is True
        assert response.results["network"]["business_network_cost"] == pytest.approx(
            1200.0, abs=1e-6
        )

    def test_2_milp_infeasible_is_an_outcome_not_an_error(self):
        orch = build_orchestrator(network=build_infeasible_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))

        assert response.status == ExecutionState.INFEASIBLE.value
        assert "no feasible solution" in response.summary.lower()
        # No cost is reported, because none exists.
        assert "network" not in response.results
        # And it is never retried: infeasibility is a property of the model.
        optimize = next(s for s in response.steps if s["capability"] == "optimization.solve")
        assert optimize["attempts"] == 1

    def test_3_infeasible_never_authorises_an_action(self):
        orch = build_orchestrator(network=build_infeasible_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))
        assert response.governance is not None
        assert response.governance.classification.value in ("HUMAN_ONLY", "NO_ACTION")

    def test_4_milp_exception_fails_the_run_and_blocks_downstream(self, orch):
        orch.services["optimization"] = FailingOptimizationClient()
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))

        assert response.status == ExecutionState.FAILED.value
        by_step = {s["step_id"]: s for s in response.steps}
        assert by_step["optimize"]["status"] == StepStatus.FAILED.value
        # KPI depends HARD on the solve, so it must not run on absent data.
        assert by_step["kpi"]["status"] == StepStatus.BLOCKED.value
        # And no cost figure is invented for the response.
        assert "network" not in response.results

    def test_required_failure_outranks_the_governance_verdict(self, orch):
        """§16: a verdict about an analysis that did not complete is meaningless."""
        orch.services["optimization"] = FailingOptimizationClient()
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))
        assert response.governance is not None       # a verdict WAS produced
        assert response.status == ExecutionState.FAILED.value, (
            "but the run is FAILED, not REQUIRES_APPROVAL — the analysis is broken"
        )


# ===========================================================================
# 5–8. REI outcomes
# ===========================================================================

class TestREIFailures:

    def test_5_rei_success(self, orch):
        response = _event_run(orch)
        registry = response.results["resilience"]

        # COMPLETED_WITH_ERRORS, not COMPLETE: PLANT_N is the network's only
        # plant, so its disruption is genuinely INFEASIBLE. The batch says so
        # rather than quietly dropping the node.
        assert registry["batch_status"] == "COMPLETED_WITH_ERRORS"
        assert registry["n_successful"] == 3
        assert registry["n_infeasible"] == 1
        assert response.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

    def test_5b_a_failed_node_reports_its_real_status_not_a_default(self, orch):
        """
        Regression: the flattened REI projection used to omit `calculation_status`
        and `failure_reason`, and the RF layer rebuilt a typed registry from it,
        defaulting every node to OK. A node that was INFEASIBLE therefore appeared
        in the audit trail as OK-with-no-REI. The typed registry is now passed
        through directly, so the real status survives.
        """
        response = _event_run(orch)
        rows = {r["facility_id"]: r for r in response.results["resilience"]["facilities"]}

        plant = rows["PLANT_N"]
        assert plant["rei"] is None
        assert plant["calculation_status"] == "INFEASIBLE"
        assert plant["solver_status"] == "INFEASIBLE"
        assert plant["risk_classification"] == "CRITICAL"
        assert rows["DC_DELHI"]["calculation_status"] == "OK"

    def test_5c_rf_sees_the_real_node_status_when_rei_is_absent(self, orch):
        """An event on the infeasible node refuses with its ACTUAL status quoted."""
        response = _event_run(orch, flood_signal(nodes=["PLANT_N"], location="PLANT_N"))
        row = response.risk["not_computable"][0]

        assert row["not_computable_reason"] == RFNotComputableReason.NO_REI.value
        assert row["facility_id"] == "PLANT_N"
        notes = " ".join(row["notes"])
        assert "calculation_status=INFEASIBLE" in notes, (
            "the refusal must quote the node's real status, not a fabricated OK"
        )

    def test_6_rei_timeout_yields_rf_not_computable(self, orch):
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)

        assert response.risk["results"] == []
        rows = response.risk["not_computable"]
        assert len(rows) == 1
        assert rows[0]["not_computable_reason"] == RFNotComputableReason.NO_REI.value
        assert rows[0]["risk_factor"] is None

    def test_6b_rei_is_reported_unavailable_never_as_zero(self, orch):
        """§6: the response must not say REI = 0. It must say REI is unavailable."""
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)

        row = response.risk["not_computable"][0]
        assert row["rei"] is None, "REI must stay UNKNOWN, not become 0"
        assert "resilience" not in response.results

        text = " ".join([response.reasoning.summary, *response.risk["warnings"]])
        assert "UNKNOWN, not zero" in text or "not zero" in text
        assert "resilience.assess" in " ".join(response.warnings)

    def test_6c_the_available_probability_survives_the_rei_failure(self, orch):
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)
        # P was available and is retained on the refusal row — we lost exposure,
        # not the whole picture.
        assert response.risk["not_computable"][0]["likelihood"] == pytest.approx(
            0.7, abs=TOL
        )

    def test_7_reasoning_still_completes_because_the_dependency_is_soft(self, orch):
        """§19: REI FAILED, RF NOT_COMPUTABLE, but reasoning COMPLETED."""
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)

        by_step = {s["step_id"]: s for s in response.steps}
        assert by_step["rei"]["status"] == StepStatus.FAILED.value
        assert by_step["risk"]["status"] == StepStatus.COMPLETED.value
        assert by_step["reason"]["status"] == StepStatus.COMPLETED.value
        assert by_step["govern"]["status"] == StepStatus.COMPLETED.value
        assert response.reasoning is not None
        assert response.reasoning.summary.strip()

    def test_7b_the_narrative_names_the_missing_resilience_evidence(self, orch):
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)

        summary = response.reasoning.summary
        assert "resilience.assess" in summary
        assert "UNKNOWN" in summary
        assert "resilience.assess" in response.reasoning.unavailable_evidence

    def test_7c_governance_records_and_acts_on_the_missing_critical_evidence(self, orch):
        """
        The missing evidence reaches the governance layer, is recorded on the
        decision, and withdraws autonomy.

        Historical note: R7 ("analytical output is always safe") once returned
        before R7B ("missing critical evidence") could be evaluated, so this run
        was classified AUTO_ACTION — losing evidence made the system MORE
        autonomous. R7 now yields a candidate verdict that the evidence rules
        settle. See `integration/test_r7_evidence_precedence.py`.
        """
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)

        evaluated = response.governance.evaluated
        assert "resilience.assess" in evaluated["missing_evidence"]
        assert "resilience.assess" in evaluated["missing_critical_evidence"]
        assert evaluated["rei"] is None, "REI is recorded as UNKNOWN, not 0"
        assert evaluated["risk_factor"] is None
        assert response.governance.action_type.value == "REPORT"

        assert response.governance.classification.value != "AUTO_ACTION"
        assert response.governance.blocked_by_missing_evidence is True

    def test_7c_conservatism_applies_where_an_action_is_governed(self):
        """
        R7B in isolation: for an action that COULD be automated, losing REI
        withholds automation rather than permitting it.
        """
        from netgravity.orchestrator.governance.action_classifier import ActionClassifier
        from netgravity.orchestrator.schemas.actions import ActionType

        classifier = ActionClassifier()
        with_rei = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, rei=0.2, risk_factor=0.1,
            confidence="HIGH",
        )
        without_rei = classifier.classify(
            action_type=ActionType.REROUTE_FLOW, rei=None, risk_factor=None,
            confidence="HIGH",
            missing_evidence={"resilience.assess": "UNAVAILABLE: REI engine timed out"},
        )

        assert with_rei.classification.value == "AUTO_ACTION"
        assert without_rei.classification.value == "APPROVAL_REQUIRED"
        assert "R7B_MISSING_CRITICAL_EVIDENCE" in without_rei.triggered_rules

    def test_7d_rei_exception_is_handled_the_same_way(self, orch):
        orch.services["rei"] = ExplodingREIClient()
        response = _event_run(orch)
        assert response.risk["not_computable"][0]["rei"] is None
        assert response.reasoning is not None

    def test_8_stale_rei_is_detected_and_refused(self, orch):
        orch.services["rei"] = StaleREIClient(stale_snapshot_id="snap_v17")
        response = _event_run(orch)

        rows = response.risk["not_computable"]
        assert len(rows) == 1
        assert rows[0]["not_computable_reason"] == RFNotComputableReason.STALE_REI.value
        assert rows[0]["risk_factor"] is None
        assert rows[0]["facility_id"] == "DC_DELHI"

    def test_8b_stale_detection_is_not_weakened_to_let_the_run_pass(self, orch):
        """
        The REI batch here is numerically perfect — same network, same maths.
        Only its snapshot label differs. RF must still refuse.
        """
        orch.services["rei"] = StaleREIClient(stale_snapshot_id="snap_v17")
        response = _event_run(orch)

        registry = response.results["resilience"]
        assert registry["rei_by_facility"]["DC_DELHI"] == pytest.approx(0.8, abs=TOL)
        assert response.risk["max_risk_factor"] is None, (
            "a correct-looking REI from the wrong snapshot is still refused"
        )
        detail = response.risk["not_computable"][0]["notes"]
        assert any("snap_v17" in n for n in detail)


# ===========================================================================
# 9–12. RF outcomes
# ===========================================================================

class TestRFOutcomes:

    def test_9_rf_success(self, orch):
        response = _event_run(orch)
        assert response.risk["max_risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert response.risk["highest_risk_entity"] == "DC_DELHI"

    def test_10_rf_missing_p(self, orch):
        response = _event_run(orch, flood_signal(probability=None))
        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.NO_EVENT_PROBABILITY.value

    def test_11_rf_missing_rei(self, orch):
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch)
        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.NO_REI.value

    def test_11b_rf_missing_both(self, orch):
        orch.services["rei"] = TimingOutREIClient()
        response = _event_run(orch, flood_signal(probability=None))
        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.NO_INPUTS.value

    def test_12_rf_node_mapping_failure(self, orch):
        response = _event_run(orch, flood_signal(nodes=["DC_NOWHERE"], location="Nowhere"))
        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE.value

    def test_12b_p_equals_zero_computes_rather_than_refusing(self, orch):
        """
        P = 0 is a measurement, not an absence. RF = 0 + 0.8 − 0 = 0.8.

        This is the distinction the whole NOT_COMPUTABLE machinery exists to
        preserve: a stated zero and a missing value are different facts.
        """
        response = _event_run(orch, flood_signal(probability=0.0))
        row = response.risk["results"][0]
        assert row["likelihood"] == pytest.approx(0.0, abs=TOL)
        assert row["risk_factor"] == pytest.approx(0.8, abs=TOL)


# ===========================================================================
# 13–16. Reasoning, grounding, governance
# ===========================================================================

class TestAdvisoryLayerFailures:

    def test_13_reasoning_success(self, orch):
        response = _event_run(orch)
        assert response.reasoning.source == "template"
        assert response.reasoning.summary.strip()

    def test_14_reasoning_failure_does_not_fail_the_run(self, orch):
        orch.services["reasoning_agent"] = FailingReasoningAgent()
        response = _event_run(orch)

        by_step = {s["step_id"]: s for s in response.steps}
        assert by_step["reason"]["status"] == StepStatus.FAILED.value
        assert by_step["govern"]["status"] == StepStatus.COMPLETED.value
        # The deterministic result is intact and still reported.
        assert response.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert response.status != ExecutionState.FAILED.value

    def test_14b_a_lost_narrative_does_not_lose_the_numbers(self, orch):
        orch.services["reasoning_agent"] = FailingReasoningAgent()
        response = _event_run(orch)
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)

    def test_16_governance_always_produces_a_verdict(self, orch):
        """Even when everything upstream degrades, no response leaves ungoverned."""
        orch.services["rei"] = TimingOutREIClient()
        orch.services["reasoning_agent"] = FailingReasoningAgent()
        response = _event_run(orch)

        assert response.governance is not None
        assert response.governance.classification.value in (
            "AUTO_ACTION", "APPROVAL_REQUIRED", "HUMAN_ONLY", "NO_ACTION",
        )


# ===========================================================================
# 17–18. Invalid input and stale snapshot
# ===========================================================================

class TestInputAndSnapshotFailures:

    def test_17_invalid_scenario_input_is_rejected_before_the_solver(self, orch, planner_actor):
        response = orch.run_sync(OrchestratorRequest(
            input="Close the Atlantis DC.",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY,
                facility_ids=["DC_ATLANTIS"],
            )],
            actor=planner_actor, disable_llm=True,
        ))

        assert response.status == ExecutionState.FAILED.value
        assert any("DC_ATLANTIS" in e["message"] for e in response.errors)
        # The scenario never reached the MILP.
        by_step = {s["step_id"]: s for s in response.steps}
        assert by_step["optimize_scenario"]["status"] == StepStatus.BLOCKED.value

    def test_18_stale_snapshot_stops_the_run(self, orch):
        stale_id = orch.snapshots.current_id

        # The observed world moves on.
        moved = build_delhi_network(delhi_capacity=4_000.0)
        orch.register_network(moved, label="updated")
        assert orch.snapshots.current_id != stale_id

        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi NCR.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(),
            network_snapshot_id=stale_id,
            disable_llm=True,
        ))

        assert response.status == ExecutionState.STALE.value
        assert any(e["code"] == "STALE_SNAPSHOT" for e in response.errors)
        assert response.risk is None, "no risk figure is produced from stale data"


# ===========================================================================
# §17 — cross-cutting invariants over the whole matrix
# ===========================================================================

class TestNoFabricationAcrossTheMatrix:

    @pytest.mark.parametrize("break_it,signal_p", [
        ("rei_timeout", 0.7),
        ("rei_exception", 0.7),
        ("rei_stale", 0.7),
        ("no_probability", None),
        ("reasoning", 0.7),
        (None, 0.7),
    ])
    def test_baseline_is_never_corrupted(self, break_it, signal_p, delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        before = orch.snapshots.current().network.model_dump_json()

        if break_it == "rei_timeout":
            orch.services["rei"] = TimingOutREIClient()
        elif break_it == "rei_exception":
            orch.services["rei"] = ExplodingREIClient()
        elif break_it == "rei_stale":
            orch.services["rei"] = StaleREIClient()
        elif break_it == "reasoning":
            orch.services["reasoning_agent"] = FailingReasoningAgent()

        _event_run(orch, flood_signal(probability=signal_p))

        assert orch.snapshots.current().network.model_dump_json() == before
        assert orch.scenarios.list_ids() == []

    @pytest.mark.parametrize("break_it", ["rei_timeout", "rei_stale", "no_probability"])
    def test_no_rf_is_ever_fabricated(self, break_it, delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        probability = 0.7

        if break_it == "rei_timeout":
            orch.services["rei"] = TimingOutREIClient()
        elif break_it == "rei_stale":
            orch.services["rei"] = StaleREIClient()
        else:
            probability = None

        response = _event_run(orch, flood_signal(probability=probability))

        assert response.risk["max_risk_factor"] is None
        assert response.risk["results"] == []
        assert response.risk["not_computable"], "the refusal is recorded, not silent"
        for row in response.risk["not_computable"]:
            assert row["risk_factor"] is None
            assert row["not_computable_reason"] is not None
            assert row["notes"], "every refusal explains itself"

    @pytest.mark.parametrize("break_it", ["rei_timeout", "reasoning"])
    def test_every_failure_is_visible_in_the_audit_trail(self, break_it, delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        if break_it == "rei_timeout":
            orch.services["rei"] = TimingOutREIClient()
        else:
            orch.services["reasoning_agent"] = FailingReasoningAgent()

        response = _event_run(orch)
        trace = orch.get_trace(response.execution_id)

        assert trace.has_event(events.STEP_FAILED), "no silent failure"
        assert trace.has_event(events.EVIDENCE_UNAVAILABLE)
        failed = [e.detail["capability"] for e in trace.events_of(events.STEP_FAILED)]
        expected = ("resilience.assess" if break_it == "rei_timeout"
                    else "reasoning.synthesise")
        assert expected in failed
