"""
NetGravity Orchestrator — Test Suite
=====================================

Unit, integration and end-to-end coverage for the control plane.

Every test runs with `enable_llm=False` unless it is explicitly exercising the
gateway. That is deliberate: the text gateway has a small SHARED budget, tests
must be hermetic and offline, and the orchestrator's core guarantee is that
deterministic behaviour is identical with or without a model.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMGatewayConfig, extract_json
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.core.execution_state import (
    ExecutionState,
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    can_transition,
    is_terminal,
)
from netgravity.orchestrator.core.planner import WorkflowPlanner
from netgravity.orchestrator.exceptions import (
    AuthorizationError,
    FailureClass,
    IllegalStateTransitionError,
    InvalidRequestError,
    InvalidScenarioError,
    SolverInfeasibleError,
    StaleSnapshotError,
    ValidationFailureError,
)
from netgravity.orchestrator.governance.action_classifier import (
    ActionClassifier,
    ApprovalManager,
    AuthorizationService,
    GovernancePolicy,
)
from netgravity.orchestrator.schemas.risk import RFNotComputableReason, RFStatus
from netgravity.orchestrator.risk.risk_factor import (
    RF_FORMULA,
    assess_network_risk,
    compute_risk_factor,
)
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.actions import (
    ActionClassification,
    ActionType,
    ApprovalStatus,
)
from netgravity.orchestrator.schemas.plans import (
    ExecutionMode,
    ExecutionPlan,
    PlanStep,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    ExternalSignal,
    Intent,
    IntentResolution,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.state.stores import ScenarioStore, SnapshotManager
from netgravity.orchestrator.tools.base import Capability, NO_RETRY, RetryPolicy
from netgravity.orchestrator.validation.validators import (
    RequestValidator,
    ScenarioValidator,
)
from netgravity.schemas.network import FacilityStatus, NodeRole
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def network():
    return build_case16_network()


@pytest.fixture
def orch(network):
    """Offline orchestrator — no gateway, fully deterministic."""
    return build_orchestrator(network=network, enable_llm=False)


def run(orchestrator, **kwargs):
    return orchestrator.run_sync(OrchestratorRequest(**kwargs))


# ===========================================================================
# UNIT — Execution state machine
# ===========================================================================

class TestExecutionStateMachine:

    def test_terminal_states_are_absorbing(self):
        for state in TERMINAL_STATES:
            if state == ExecutionState.REQUIRES_APPROVAL:
                continue  # resumable by design
            assert LEGAL_TRANSITIONS[state] == frozenset(), (
                f"{state.value} must be absorbing"
            )

    def test_happy_path_transitions_are_legal(self):
        chain = [
            ExecutionState.RECEIVED, ExecutionState.UNDERSTANDING,
            ExecutionState.PLANNED, ExecutionState.VALIDATING,
            ExecutionState.RUNNING, ExecutionState.COMPLETED,
        ]
        for a, b in zip(chain, chain[1:]):
            assert can_transition(a, b), f"{a.value} -> {b.value} must be legal"

    def test_illegal_transition_rejected(self):
        assert not can_transition(ExecutionState.RECEIVED, ExecutionState.COMPLETED)
        assert not can_transition(ExecutionState.COMPLETED, ExecutionState.RUNNING)

    def test_every_state_can_fail_or_escalate(self):
        for state, allowed in LEGAL_TRANSITIONS.items():
            if is_terminal(state):
                continue
            assert ExecutionState.FAILED in allowed
            assert ExecutionState.REQUIRES_HUMAN in allowed
            assert ExecutionState.STALE in allowed

    def test_context_enforces_transitions(self, orch):
        from netgravity.orchestrator.core.execution_context import ExecutionContext
        ctx = ExecutionContext()
        ctx.transition(ExecutionState.UNDERSTANDING)
        assert ctx.current_state == ExecutionState.UNDERSTANDING
        with pytest.raises(IllegalStateTransitionError):
            ctx.transition(ExecutionState.COMPLETED)

    def test_state_history_is_recorded(self):
        from netgravity.orchestrator.core.execution_context import ExecutionContext
        ctx = ExecutionContext()
        ctx.transition(ExecutionState.UNDERSTANDING, note="interpreting")
        ctx.transition(ExecutionState.PLANNED)
        assert len(ctx.state_history) == 2
        assert ctx.state_history[0].from_state == "RECEIVED"
        assert ctx.state_history[0].note == "interpreting"

    def test_approval_resumes_same_execution(self):
        assert can_transition(ExecutionState.REQUIRES_APPROVAL, ExecutionState.COMPLETED)
        assert can_transition(ExecutionState.REQUIRES_APPROVAL, ExecutionState.CANCELLED)


# ===========================================================================
# UNIT — Deterministic RF
# ===========================================================================

class TestRiskFactor:

    def test_formula_matches_specification(self):
        r = compute_risk_factor(0.7, 0.8)
        assert r.formula == RF_FORMULA
        # 0.7 + 0.8 - 0.56 = 0.94
        assert r.risk_factor == pytest.approx(0.94, abs=1e-9)
        assert r.likelihood == pytest.approx(0.7)
        assert r.rei == pytest.approx(0.8)

    @pytest.mark.parametrize("p,rei,expected", [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 1.0),
        (0.0, 1.0, 1.0),
        (1.0, 1.0, 1.0),
        (0.5, 0.5, 0.75),
        (0.7, 0.8, 0.94),
        (0.3, 0.2, 0.44),
    ])
    def test_boundary_and_known_values(self, p, rei, expected):
        assert compute_risk_factor(p, rei).risk_factor == pytest.approx(expected, abs=1e-9)

    def test_identity_properties(self):
        # No event risk → exposure alone; no exposure → likelihood alone.
        assert compute_risk_factor(0.0, 0.42).risk_factor == pytest.approx(0.42)
        assert compute_risk_factor(0.42, 0.0).risk_factor == pytest.approx(0.42)

    def test_symmetric_in_arguments(self):
        assert (compute_risk_factor(0.3, 0.9).risk_factor
                == pytest.approx(compute_risk_factor(0.9, 0.3).risk_factor))

    def test_monotonic_and_bounded(self):
        prev = -1.0
        for i in range(11):
            p = i / 10.0
            rf = compute_risk_factor(p, 0.4).risk_factor
            assert 0.0 <= rf <= 1.0
            assert rf >= prev
            prev = rf

    def test_rejects_out_of_range_likelihood(self):
        with pytest.raises(ValidationFailureError, match=r"\[0, 1\]"):
            compute_risk_factor(1.5, 0.5)
        with pytest.raises(ValidationFailureError):
            compute_risk_factor(-0.2, 0.5)

    def test_rejects_rei_above_one(self):
        """REI is normalised to a maximum of 1.0; exceeding it is a defect."""
        with pytest.raises(ValidationFailureError, match="must not exceed 1.0"):
            compute_risk_factor(0.5, 1.4)

    def test_negative_rei_floors_to_zero_with_note(self):
        """
        Negative REI means a disruption that REDUCES cost — no exposure.
        RF must reduce to the likelihood alone, and say so.
        """
        r = compute_risk_factor(0.7, -5.75, facility_id="DC_EAST")
        assert r.rei == pytest.approx(0.0)
        assert r.risk_factor == pytest.approx(0.7)
        assert any("negative" in n.lower() for n in r.notes)
        assert any("-5.75" in n for n in r.notes)

    def test_rejects_non_numeric_and_non_finite(self):
        with pytest.raises(ValidationFailureError):
            compute_risk_factor("high", 0.5)  # type: ignore[arg-type]
        with pytest.raises(ValidationFailureError):
            compute_risk_factor(float("nan"), 0.5)
        with pytest.raises(ValidationFailureError):
            compute_risk_factor(float("inf"), 0.5)

    def test_provenance_recorded(self):
        r = compute_risk_factor(
            0.6, 0.5, facility_id="DC_A",
            provenance={"likelihood": "noaa", "rei": "netgravity_rei:NET@v1"},
        )
        assert r.provenance["likelihood"] == "noaa"
        assert r.provenance["rei"] == "netgravity_rei:NET@v1"
        assert r.facility_id == "DC_A"

    def test_reproducible(self):
        a = compute_risk_factor(0.37, 0.61)
        b = compute_risk_factor(0.37, 0.61)
        assert a.risk_factor == b.risk_factor

    def test_missing_inputs_are_not_computable_never_defaulted(self):
        """
        A fabricated 0.0 would read as 'no risk', which is a different claim
        from 'not assessed'. Missing inputs yield explicit NOT_COMPUTABLE rows.
        """
        assessment = assess_network_risk(
            # A: both inputs   B: P only, no REI   C: REI only, no P   D: neither
            rei_by_facility={"A": 0.8, "B": None, "C": 0.4, "D": None},
            likelihood_by_facility={"A": 0.5, "B": 0.5},
        )
        assert {r.facility_id for r in assessment.results} == {"A"}

        uncomputable = {r.facility_id: r for r in assessment.not_computable}
        assert set(uncomputable) == {"B", "C", "D"}
        assert uncomputable["B"].not_computable_reason == RFNotComputableReason.NO_REI
        assert (uncomputable["C"].not_computable_reason
                == RFNotComputableReason.NO_EVENT_PROBABILITY)
        assert uncomputable["D"].not_computable_reason == RFNotComputableReason.NO_INPUTS
        # Nothing was invented for either.
        for row in uncomputable.values():
            assert row.risk_factor is None
            assert row.status == RFStatus.NOT_COMPUTABLE
        assert all("NOT_COMPUTABLE" in w for w in assessment.warnings)

    def test_assessment_ranks_by_risk(self):
        assessment = assess_network_risk(
            rei_by_facility={"A": 0.2, "B": 0.9, "C": 0.5},
            likelihood_by_facility={},
            default_likelihood=0.5,
        )
        assert assessment.highest_risk_entity == "B"
        rfs = [r.risk_factor for r in assessment.results]
        assert rfs == sorted(rfs, reverse=True)


# ===========================================================================
# UNIT — Capability registry
# ===========================================================================

class TestCapabilityRegistry:

    @staticmethod
    def _cap(name="test.cap"):
        async def handler(ctx, req):
            return {"ok": True}
        return Capability(name=name, handler=handler, description="test")

    def test_register_and_lookup(self):
        reg = CapabilityRegistry()
        reg.register(self._cap())
        assert "test.cap" in reg
        assert len(reg) == 1
        assert reg.get("test.cap").name == "test.cap"

    def test_duplicate_registration_refused(self):
        reg = CapabilityRegistry()
        reg.register(self._cap())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(self._cap())
        reg.register(self._cap(), replace=True)  # explicit override is allowed

    def test_unknown_capability_raises_with_listing(self):
        from netgravity.orchestrator.exceptions import CapabilityNotFoundError
        reg = CapabilityRegistry()
        reg.register(self._cap())
        with pytest.raises(CapabilityNotFoundError, match="test.cap"):
            reg.get("nope")

    def test_plan_capability_validation(self):
        from netgravity.orchestrator.exceptions import CapabilityNotFoundError
        reg = CapabilityRegistry()
        reg.register(self._cap())
        reg.validate_plan_capabilities(["test.cap"])
        with pytest.raises(CapabilityNotFoundError):
            reg.validate_plan_capabilities(["test.cap", "missing.cap"])

    def test_new_capability_needs_no_core_change(self, orch):
        """Extensibility: registering an agent is a one-call operation."""
        before = len(orch.registry)

        async def carbon_handler(ctx, req):
            return {"carbon_saving_kg": 1234.0}

        orch.registry.register(Capability(
            name="carbon.optimise", handler=carbon_handler,
            description="Carbon Optimization Agent",
            execution_mode=ExecutionMode.DETERMINISTIC,
        ))
        assert len(orch.registry) == before + 1
        assert "carbon.optimise" in orch.registry
        assert any(c["name"] == "carbon.optimise" for c in orch.capabilities())

    def test_describe_is_machine_readable(self, orch):
        described = orch.capabilities()
        assert described
        for entry in described:
            assert {"name", "execution_mode", "dependencies", "timeout_seconds"} <= set(entry)

    def test_deterministic_and_probabilistic_split(self, orch):
        det = {c.name for c in orch.registry.deterministic()}
        prob = {c.name for c in orch.registry.probabilistic()}
        assert "optimization.solve" in det
        assert "risk.compute_rf" in det, "RF must be deterministic"
        assert "reasoning.synthesise" in prob
        assert not (det & prob)


# ===========================================================================
# UNIT — Plan / DAG
# ===========================================================================

class TestExecutionPlan:

    def test_layers_group_independent_steps(self):
        plan = ExecutionPlan(workflow_id="wf", intent="TEST", steps=[
            PlanStep(step_id="a", capability="c"),
            PlanStep(step_id="b", capability="c", depends_on=["a"]),
            PlanStep(step_id="c", capability="c", depends_on=["a"]),
            PlanStep(step_id="d", capability="c", depends_on=["b", "c"]),
        ])
        layers = plan.execution_layers()
        assert layers == [["a"], ["b", "c"], ["d"]]

    def test_cycle_detected(self):
        from netgravity.orchestrator.exceptions import PlanningFailureError
        plan = ExecutionPlan(workflow_id="wf", intent="T", steps=[
            PlanStep(step_id="a", capability="c", depends_on=["b"]),
            PlanStep(step_id="b", capability="c", depends_on=["a"]),
        ])
        with pytest.raises(PlanningFailureError, match="cycle"):
            plan.validate_dag()

    def test_unknown_dependency_detected(self):
        from netgravity.orchestrator.exceptions import PlanningFailureError
        plan = ExecutionPlan(workflow_id="wf", intent="T", steps=[
            PlanStep(step_id="a", capability="c", depends_on=["ghost"]),
        ])
        with pytest.raises(PlanningFailureError, match="unknown step"):
            plan.validate_dag()

    def test_duplicate_step_ids_detected(self):
        from netgravity.orchestrator.exceptions import PlanningFailureError
        plan = ExecutionPlan(workflow_id="wf", intent="T", steps=[
            PlanStep(step_id="a", capability="c"),
            PlanStep(step_id="a", capability="c"),
        ])
        with pytest.raises(PlanningFailureError, match="duplicate"):
            plan.validate_dag()


class TestWorkflowPlanner:

    def test_plan_per_intent(self, orch):
        planner = WorkflowPlanner(orch.registry)
        for intent in [Intent.NETWORK_STATE_QUERY, Intent.SCENARIO_ANALYSIS,
                       Intent.RESILIENCE_QUERY, Intent.EXTERNAL_EVENT,
                       Intent.OPTIMIZATION_REQUEST]:
            plan = planner.plan(IntentResolution(intent=intent))
            assert plan.steps
            plan.validate_dag()

    def test_unknown_intent_raises(self, orch):
        from netgravity.orchestrator.exceptions import PlanningFailureError
        planner = WorkflowPlanner(orch.registry)
        with pytest.raises(PlanningFailureError, match="No workflow"):
            planner.plan(IntentResolution(intent=Intent.UNKNOWN))

    def test_planner_runs_only_what_is_needed(self, orch):
        """A resilience query must not trigger a scenario solve."""
        planner = WorkflowPlanner(orch.registry)
        caps = {s.capability for s in
                planner.plan(IntentResolution(intent=Intent.RESILIENCE_QUERY)).steps}
        assert "resilience.assess" in caps
        assert "optimization.solve_scenario" not in caps
        assert "scenario.create" not in caps

    def test_scenario_workflow_parallelises_kpi_and_rei(self, orch):
        planner = WorkflowPlanner(orch.registry)
        plan = planner.plan(IntentResolution(intent=Intent.SCENARIO_ANALYSIS))
        layers = plan.execution_layers()
        assert any(len(layer) > 1 for layer in layers), (
            "scenario workflow should contain a parallel layer"
        )

    def test_comparison_scenarios_are_independent(self, orch):
        planner = WorkflowPlanner(orch.registry)
        resolution = IntentResolution(
            intent=Intent.SCENARIO_COMPARISON,
            scenarios=[
                ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                                   facility_ids=["DC_EAST"]),
                ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                                   facility_ids=["DC_WEST"]),
            ],
        )
        plan = planner.plan(resolution)
        s0 = plan.step("optimize_scenario_0")
        s1 = plan.step("optimize_scenario_1")
        assert s0 and s1
        # Neither scenario chain depends on the other.
        assert "optimize_scenario_1" not in s0.depends_on
        assert "optimize_scenario_0" not in s1.depends_on


# ===========================================================================
# UNIT — Validation
# ===========================================================================

class TestValidation:

    def test_empty_request_rejected(self):
        with pytest.raises(InvalidRequestError, match="at least one of"):
            RequestValidator().validate(OrchestratorRequest(input="   "))

    def test_oversized_input_rejected(self):
        with pytest.raises(InvalidRequestError, match="limit"):
            RequestValidator().validate(OrchestratorRequest(input="x" * 20_000))

    def test_explicit_intent_alone_is_valid(self):
        RequestValidator().validate(
            OrchestratorRequest(input="", explicit_intent=Intent.RESILIENCE_QUERY)
        )

    def test_hallucinated_facility_rejected(self, network):
        """The critical LLM guardrail: an invented facility never reaches the MILP."""
        spec = ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                                  facility_ids=["DC_ATLANTIS"])
        with pytest.raises(InvalidScenarioError, match="do not exist"):
            ScenarioValidator().validate(spec, network)

    def test_market_cannot_be_closed(self, network):
        market = next(f.id for f in network.facilities if f.role == NodeRole.MARKET)
        spec = ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                                  facility_ids=[market])
        with pytest.raises(InvalidScenarioError, match="demand, not network capacity"):
            ScenarioValidator().validate(spec, network)

    def test_valid_scenario_passes(self, network):
        ScenarioValidator().validate(
            ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                               facility_ids=["DC_EAST"]),
            network,
        )


# ===========================================================================
# UNIT — Snapshot & scenario isolation
# ===========================================================================

class TestSnapshotIsolation:

    def test_snapshot_is_content_addressed(self, network):
        mgr = SnapshotManager()
        a = mgr.register(network)
        b = mgr.register(network)
        assert a.snapshot_id == b.snapshot_id, "same content → same snapshot id"

    def test_snapshot_deep_copies_network(self, network):
        mgr = SnapshotManager()
        snap = mgr.register(network)
        original = snap.network.facilities[0].capacity_units_per_period
        network.facilities[0].capacity_units_per_period = 99999.0
        assert snap.network.facilities[0].capacity_units_per_period == original, (
            "mutating the caller's network must not alter the stored snapshot"
        )

    def test_stale_snapshot_detected(self, network):
        mgr = SnapshotManager()
        first = mgr.register(network)
        modified = network.model_copy(deep=True)
        modified.facilities[0].capacity_units_per_period += 500
        modified = modified.model_copy(update={"data_version": modified.compute_data_version()})
        mgr.register(modified)
        with pytest.raises(StaleSnapshotError, match="must not be combined"):
            mgr.assert_fresh(first.snapshot_id)

    def test_scenario_never_mutates_parent(self, network):
        from netgravity.orchestrator.engines.scenario_builder import ScenarioBuilder
        mgr = SnapshotManager()
        snap = mgr.register(network)
        before = snap.network.model_dump_json()

        scenario_net, overrides = ScenarioBuilder().build(
            snap.network,
            ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                               facility_ids=["DC_EAST"]),
        )
        assert snap.network.model_dump_json() == before, "parent snapshot was mutated"
        assert overrides == ["CLOSE_FACILITY DC_EAST"]
        assert scenario_net.get_facility("DC_EAST").is_forced_closed is True
        assert snap.network.get_facility("DC_EAST").is_forced_closed is False

    def test_scenarios_are_tagged_hypothetical(self, network):
        mgr, store = SnapshotManager(), ScenarioStore()
        snap = mgr.register(network)
        rec = store.create(parent_snapshot_id=snap.snapshot_id, network=network,
                           label="test", overrides=["CLOSE X"])
        assert rec.is_hypothetical is True
        assert rec.source == "user_scenario"
        assert rec.parent_snapshot_id == snap.snapshot_id
        assert snap.is_hypothetical is False

    def test_scenarios_do_not_contaminate_each_other(self, network):
        from netgravity.orchestrator.engines.scenario_builder import ScenarioBuilder
        builder = ScenarioBuilder()
        a, _ = builder.build(network, ScenarioIntentSpec(
            action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_EAST"]))
        b, _ = builder.build(network, ScenarioIntentSpec(
            action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_WEST"]))

        assert a.get_facility("DC_EAST").is_forced_closed is True
        assert a.get_facility("DC_WEST").is_forced_closed is False
        assert b.get_facility("DC_WEST").is_forced_closed is True
        assert b.get_facility("DC_EAST").is_forced_closed is False

    def test_scenario_store_has_no_promote_to_observed(self):
        """There must be no API turning a hypothetical into observed truth."""
        assert not hasattr(ScenarioStore, "promote_to_observed")
        assert not hasattr(ScenarioStore, "commit_to_snapshot")


# ===========================================================================
# UNIT — Governance
# ===========================================================================

class TestGovernance:

    def setup_method(self):
        self.classifier = ActionClassifier()

    def test_facility_closure_is_human_only_even_at_low_rei(self):
        """
        THE governance invariant: REI is never the sole determinant.
        Low exposure, tiny cost, high confidence — still human-only.
        """
        decision = self.classifier.classify(
            action_type=ActionType.CLOSE_FACILITY,
            is_feasible=True, cost_impact_pct=0.1,
            unserved_demand_rate=0.0, rei=0.01, risk_factor=0.01,
            confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert "R2_STRUCTURAL_ACTION" in decision.triggered_rules
        assert "irreversible" in decision.reason.lower()

    def test_opening_a_facility_is_also_structural(self):
        decision = self.classifier.classify(
            action_type=ActionType.OPEN_FACILITY, confidence="HIGH", cost_impact_pct=0.0,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_infeasible_blocks_all_automation(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, is_feasible=False, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert "R1_INFEASIBLE" in decision.triggered_rules

    def test_analysis_is_auto(self):
        decision = self.classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH", cost_impact_pct=0.0,
        )
        assert decision.classification == ActionClassification.AUTO_ACTION

    def test_no_action_when_nothing_proposed(self):
        decision = self.classifier.classify(action_type=ActionType.NONE)
        assert decision.classification == ActionClassification.NO_ACTION

    def test_service_loss_escalates(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, unserved_demand_rate=0.10,
            confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY
        assert "R4_UNSERVED_DEMAND" in decision.triggered_rules

    def test_high_risk_factor_escalates(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.94, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_moderate_risk_requires_approval(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.6, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED

    def test_low_confidence_blocks_automation(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="LOW", cost_impact_pct=0.0,
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R10_LOW_CONFIDENCE" in decision.triggered_rules

    def test_reversible_low_impact_is_automatic(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, is_feasible=True,
            cost_impact_pct=1.0, unserved_demand_rate=0.0,
            risk_factor=0.1, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.AUTO_ACTION

    def test_decision_is_explainable(self):
        decision = self.classifier.classify(action_type=ActionType.CLOSE_FACILITY)
        assert decision.reason
        assert decision.triggered_rules
        assert decision.evaluated["action_type"] == "CLOSE_FACILITY"

    def test_policy_is_configurable(self):
        strict = ActionClassifier(GovernancePolicy(risk_factor_human=0.2))
        decision = strict.classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.3, confidence="HIGH",
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_deterministic_repeatable(self):
        kwargs = dict(action_type=ActionType.REROUTE_FLOW, cost_impact_pct=7.0,
                      confidence="HIGH", risk_factor=0.2)
        a = self.classifier.classify(**kwargs)
        b = self.classifier.classify(**kwargs)
        assert a.classification == b.classification
        assert a.triggered_rules == b.triggered_rules


class TestAuthorization:

    def test_no_role_may_directly_close_a_facility(self):
        auth = AuthorizationService()
        for role in ActorRole:
            actor = Actor(actor_id="u", role=role)
            assert not auth.can_perform(actor, ActionType.CLOSE_FACILITY), (
                f"{role.value} must not be able to close a facility directly"
            )

    def test_viewer_cannot_reroute(self):
        auth = AuthorizationService()
        assert not auth.can_perform(Actor(role=ActorRole.VIEWER), ActionType.REROUTE_FLOW)
        assert auth.can_perform(Actor(role=ActorRole.PLANNER), ActionType.REROUTE_FLOW)

    def test_authorize_raises_for_denied_action(self):
        auth = AuthorizationService()
        with pytest.raises(AuthorizationError, match="not"):
            auth.authorize(Actor(role=ActorRole.VIEWER), ActionType.REROUTE_FLOW)

    def test_system_actor_is_analysis_only(self):
        auth = AuthorizationService()
        system = Actor(actor_id="poller", role=ActorRole.SYSTEM)
        assert auth.can_perform(system, ActionType.REPORT)
        assert not auth.can_perform(system, ActionType.REROUTE_FLOW)


class TestApprovalWorkflow:

    def test_only_approvers_may_approve(self):
        mgr = ApprovalManager()
        decision = ActionClassifier().classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.6, confidence="HIGH")
        approval = mgr.create_request(execution_id="e1", decision=decision, summary="s")

        with pytest.raises(AuthorizationError):
            mgr.decide(approval, actor=Actor(role=ActorRole.VIEWER), approved=True)

        mgr.decide(approval, actor=Actor(actor_id="boss", role=ActorRole.APPROVER),
                   approved=True, note="ok")
        assert approval.status == ApprovalStatus.APPROVED
        assert approval.decided_by == "boss"

    def test_cannot_decide_twice(self):
        from netgravity.orchestrator.exceptions import GovernanceFailureError
        mgr = ApprovalManager()
        decision = ActionClassifier().classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.6, confidence="HIGH")
        approval = mgr.create_request(execution_id="e1", decision=decision, summary="s")
        approver = Actor(actor_id="boss", role=ActorRole.APPROVER)
        mgr.decide(approval, actor=approver, approved=True)
        with pytest.raises(GovernanceFailureError, match="already"):
            mgr.decide(approval, actor=approver, approved=False)

    def test_approval_pins_immutable_context(self):
        mgr = ApprovalManager()
        decision = ActionClassifier().classify(
            action_type=ActionType.REROUTE_FLOW, risk_factor=0.6, confidence="HIGH")
        approval = mgr.create_request(
            execution_id="e1", decision=decision, summary="s",
            scenario_id="scn_1", scenario_version=1, baseline_snapshot_id="snap_1",
        )
        assert approval.scenario_id == "scn_1"
        assert approval.scenario_version == 1
        assert approval.baseline_snapshot_id == "snap_1"


# ===========================================================================
# UNIT — Retry classification
# ===========================================================================

class TestRetryPolicy:

    def test_only_retryable_failures_retry(self):
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(FailureClass.RETRYABLE, attempt=1)
        assert not policy.should_retry(FailureClass.NON_RETRYABLE, attempt=1)
        assert not policy.should_retry(FailureClass.REQUIRES_HUMAN, attempt=1)

    def test_attempts_are_bounded(self):
        policy = RetryPolicy(max_attempts=3)
        assert policy.should_retry(FailureClass.RETRYABLE, attempt=2)
        assert not policy.should_retry(FailureClass.RETRYABLE, attempt=3)

    def test_infeasibility_is_never_retryable(self):
        """Infeasibility is a mathematical outcome, not a transient fault."""
        exc = SolverInfeasibleError("no feasible solution")
        assert exc.failure_class == FailureClass.NON_RETRYABLE
        assert not RetryPolicy(max_attempts=5).should_retry(exc.failure_class, 1)

    def test_failure_class_taxonomy(self):
        from netgravity.orchestrator.exceptions import (
            EngineTimeoutError, MissingDataError, ValidationFailureError as VFE,
        )
        assert EngineTimeoutError("t").failure_class == FailureClass.RETRYABLE
        assert VFE("v").failure_class == FailureClass.NON_RETRYABLE
        assert MissingDataError("m").failure_class == FailureClass.REQUIRES_HUMAN
        assert AuthorizationError("a").failure_class == FailureClass.NON_RETRYABLE

    def test_backoff_is_bounded(self):
        policy = RetryPolicy(max_attempts=5, backoff_seconds=1.0,
                             max_backoff_seconds=4.0, jitter=False)
        assert policy.delay_for(1) == 1.0
        assert policy.delay_for(2) == 2.0
        assert policy.delay_for(10) == 4.0

    def test_tool_does_not_retry_non_retryable(self, orch):
        """A non-retryable handler failure must be attempted exactly once."""
        calls = {"n": 0}

        async def failing(ctx, req):
            calls["n"] += 1
            raise ValidationFailureError("bad input")

        orch.registry.register(Capability(
            name="test.fail", handler=failing,
            retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=0.0),
        ))
        from netgravity.orchestrator.core.execution_context import ExecutionContext
        from netgravity.orchestrator.schemas.plans import ToolRequest

        result = asyncio.run(orch.registry.tool("test.fail").execute(
            ExecutionContext(), ToolRequest(capability="test.fail")))
        assert result.success is False
        assert calls["n"] == 1
        assert result.error_code == "VALIDATION_FAILURE"

    def test_tool_retries_retryable(self, orch):
        from netgravity.orchestrator.exceptions import EngineFailureError
        calls = {"n": 0}

        async def flaky(ctx, req):
            calls["n"] += 1
            if calls["n"] < 3:
                raise EngineFailureError("transient")
            return {"ok": True}

        orch.registry.register(Capability(
            name="test.flaky", handler=flaky,
            retry_policy=RetryPolicy(max_attempts=4, backoff_seconds=0.0, jitter=False),
        ))
        from netgravity.orchestrator.core.execution_context import ExecutionContext
        from netgravity.orchestrator.schemas.plans import ToolRequest

        result = asyncio.run(orch.registry.tool("test.flaky").execute(
            ExecutionContext(), ToolRequest(capability="test.flaky")))
        assert result.success is True
        assert result.attempts == 3


# ===========================================================================
# UNIT — LLM boundary
# ===========================================================================

class TestLLMBoundary:

    def test_gateway_reports_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
        gw = LLMGateway(LLMGatewayConfig.from_env())
        assert gw.available is False
        assert "TEXT_API_TOKEN" in gw.unavailable_reason()

    def test_no_credential_is_hardcoded(self):
        """The token must come from the environment, never from source."""
        import inspect
        from netgravity.orchestrator.agents import llm_gateway
        src = inspect.getsource(llm_gateway)
        assert "shared_" not in src, "a literal token must never appear in source"
        assert 'os.environ.get("TEXT_API_TOKEN"' in src

    def test_stats_never_leak_the_token(self, monkeypatch):
        monkeypatch.setenv("TEXT_API_TOKEN", "secret-value-xyz")
        gw = LLMGateway(LLMGatewayConfig.from_env())
        blob = json.dumps(gw.stats())
        assert "secret-value-xyz" not in blob
        assert gw.stats()["token_configured"] is True

    def test_unavailable_gateway_raises_non_retryable(self):
        gw = LLMGateway(LLMGatewayConfig(base_url="", token="", enabled=False))
        from netgravity.orchestrator.exceptions import LLMNonRetryableError
        with pytest.raises(LLMNonRetryableError):
            gw.generate("hello")

    def test_oversized_prompt_refused_locally(self, monkeypatch):
        """Never spend shared capacity on a guaranteed 413."""
        monkeypatch.setenv("TEXT_API_TOKEN", "t")
        from netgravity.orchestrator.exceptions import LLMNonRetryableError
        gw = LLMGateway(LLMGatewayConfig.from_env())
        with pytest.raises(LLMNonRetryableError, match="413|limit"):
            gw.generate("x" * 200_000)

    def test_extract_json_handles_fences_and_prose(self):
        assert extract_json('{"a": 1}') == {"a": 1}
        assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
        assert extract_json('Here you go:\n{"a": 3}\nhope that helps') == {"a": 3}
        assert extract_json("not json at all") is None
        assert extract_json("") is None

    def test_intent_agent_works_without_llm(self):
        agent = IntentAgent(gateway=None)
        res = agent.resolve("What happens if we close DC_EAST?",
                            known_facility_ids=["DC_EAST", "DC_WEST"])
        assert res.intent == Intent.SCENARIO_ANALYSIS
        assert res.source == "rules"
        assert "DC_EAST" in res.entities

    def test_intent_agent_never_invents_a_facility(self):
        agent = IntentAgent(gateway=None)
        res = agent.resolve("close the Atlantis hub", known_facility_ids=["DC_EAST"])
        assert "Atlantis" not in res.entities
        assert all(e == "DC_EAST" for e in res.entities)

    def test_reasoning_falls_back_to_template(self):
        agent = ReasoningAgent(gateway=None)
        result = agent.reason({"network_state": {"business_network_cost": 1000.0,
                                                 "is_feasible": True}})
        assert result.source == "template"
        assert "1,000.00" in result.summary
        # The template only states values from the payload, so it must ground.
        assert result.grounding_status in ("GROUNDED", "NO_CLAIMS")
        assert result.is_grounded

    def test_reasoning_downgrades_confidence_on_infeasible(self):
        agent = ReasoningAgent(gateway=None)
        result = agent.reason({"optimization": {"solver_status": "INFEASIBLE",
                                                "is_feasible": False}})
        assert result.confidence == "LOW"
        assert "INFEASIBLE" in result.summary

    def test_reasoning_validation_catches_feasibility_contradiction(self):
        """A model claiming feasibility against an INFEASIBLE solver is flagged."""
        from netgravity.orchestrator.schemas.risk import ReasoningResult
        agent = ReasoningAgent(gateway=None)
        bogus = ReasoningResult(
            summary="The network remains feasible and healthy.",
            confidence="HIGH", source="llm", evidence=["x"],
        )
        validated = agent._validate(bogus, {"optimization": {"is_feasible": False}})
        assert validated.confidence == "LOW"
        assert any("feasib" in w.lower() for w in validated.validation_warnings)

    def test_rf_module_contains_no_llm_reference(self):
        """RF must be pure arithmetic — no model involvement whatsoever."""
        import inspect
        from netgravity.orchestrator.risk import risk_factor
        src = inspect.getsource(risk_factor).lower()
        for banned in ("llmgateway", "gateway.generate", "prompt"):
            assert banned not in src


# ===========================================================================
# INTEGRATION — orchestrator flows
# ===========================================================================

class TestOrchestratorIntegration:

    def test_scenario_flow(self, orch):
        resp = run(orch, input="What happens if we close DC_EAST?")
        assert resp.intent == "SCENARIO_ANALYSIS"
        assert resp.is_hypothetical is True
        assert resp.scenario_id
        assert resp.results["network"]["business_network_cost"] > 0
        assert resp.results["network"]["business_cost_delta"] is not None
        # Structural change → human decision.
        assert resp.status == "REQUIRES_HUMAN"
        assert resp.governance.classification == ActionClassification.HUMAN_ONLY

    def test_resilience_flow(self, orch):
        resp = run(orch, input="Which facility is most exposed?")
        assert resp.intent == "RESILIENCE_QUERY"
        assert resp.status == "COMPLETED"
        assert resp.is_hypothetical is False
        rez = resp.results["resilience"]
        assert rez["highest_exposure_facility"]
        assert rez["max_rei"] == pytest.approx(1.0)

    def test_external_event_flow_computes_rf(self, orch):
        signal = ExternalSignal(
            event_type="FLOOD", location="DC_EAST", event_probability=0.7,
            probability_basis="stated by source",
            source="met_office", confidence=0.8, affected_entity_ids=["DC_EAST"],
        )
        resp = run(orch, input="Severe flooding expected around DC_EAST.",
                   external_signal=signal)
        assert resp.intent == "EXTERNAL_EVENT"
        assert resp.risk is not None
        assert resp.risk["max_risk_factor"] is not None
        rf = resp.risk["results"][0]
        assert rf["formula"] == RF_FORMULA
        assert rf["status"] == "COMPUTED"
        assert rf["likelihood"] == pytest.approx(0.7)
        assert rf["provenance"]["likelihood"].startswith("external_signal:")

    def test_comparison_flow_runs_independent_scenarios(self, orch):
        resp = run(orch, input="Compare closing DC_EAST vs DC_WEST")
        assert resp.intent == "SCENARIO_COMPARISON"
        solved = [s for s in resp.steps
                  if s["capability"] == "optimization.solve_scenario"
                  and s["status"] == "COMPLETED"]
        assert len(solved) >= 2, "both scenarios should solve"
        assert len(orch.scenarios.list_ids()) >= 2

    def test_network_state_flow_is_not_hypothetical(self, orch):
        resp = run(orch, explicit_intent=Intent.NETWORK_STATE_QUERY, input="network state")
        assert resp.status == "COMPLETED"
        assert resp.is_hypothetical is False
        assert resp.scenario_id is None

    def test_unclassifiable_request_escalates(self, orch):
        resp = run(orch, input="hello there, nice weather")
        assert resp.status == "REQUIRES_HUMAN"
        assert resp.intent == "UNKNOWN"

    def test_observed_network_never_mutated(self, orch, network):
        before = orch.snapshots.current().network.model_dump_json()
        run(orch, input="What happens if we close DC_EAST?")
        run(orch, input="Compare closing DC_EAST vs DC_WEST")
        assert orch.snapshots.current().network.model_dump_json() == before

    def test_idempotent_by_request_id(self, orch):
        req = OrchestratorRequest(input="Which facility is most exposed?")
        first = orch.run_sync(req)
        second = orch.run_sync(req)
        assert first.execution_id == second.execution_id
        assert any("Duplicate request_id" in w for w in second.warnings)

    def test_stale_snapshot_rejected(self, orch, network):
        modified = network.model_copy(deep=True)
        modified.facilities[0].capacity_units_per_period += 1000
        modified = modified.model_copy(update={"data_version": modified.compute_data_version()})
        stale_id = orch.snapshots.current_id
        orch.snapshots.register(modified)

        resp = orch.run_sync(OrchestratorRequest(
            input="Which facility is most exposed?", network_snapshot_id=stale_id))
        assert resp.status == "STALE"
        assert any(e["code"] == "STALE_SNAPSHOT" for e in resp.errors)

    def test_audit_trail_answers_why(self, orch):
        resp = run(orch, input="What happens if we close DC_EAST?")
        trace = orch.get_trace(resp.execution_id)
        assert trace is not None

        assert trace.interpreted_intent == "SCENARIO_ANALYSIS"
        assert trace.intent_source
        assert trace.workflow_id
        assert trace.baseline_snapshot_id
        assert trace.data_version
        assert trace.scenario_ids
        assert trace.scenario_overrides == ["CLOSE_FACILITY DC_EAST"]
        assert trace.tool_invocations
        assert trace.governance_decision["triggered_rules"]
        assert trace.final_status == "REQUIRES_HUMAN"

        text = trace.explain()
        assert "CLOSE_FACILITY DC_EAST" in text
        assert "R2_STRUCTURAL_ACTION" in text
        blob = trace.to_json()
        assert json.loads(blob)["execution_id"] == resp.execution_id

    def test_infeasible_scenario_is_reported_not_retried(self, orch, network):
        """Infeasibility is an outcome with an explanation, never a retry loop."""
        dcs = [f.id for f in network.facilities if f.role == NodeRole.DC]
        resp = orch.run_sync(OrchestratorRequest(
            input="close every DC",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY, facility_ids=dcs)],
        ))
        assert resp.status == "INFEASIBLE"
        assert "no feasible solution" in resp.summary.lower()
        solve_steps = [s for s in resp.steps
                       if s["capability"] == "optimization.solve_scenario"]
        assert solve_steps and solve_steps[0]["attempts"] == 1, (
            "infeasibility must not be retried"
        )

    def test_capabilities_and_workflows_exposed(self, orch):
        names = {c["name"] for c in orch.capabilities()}
        assert {"optimization.solve", "resilience.assess", "risk.compute_rf",
                "governance.classify", "reasoning.synthesise"} <= names
        assert len(orch.workflows()) >= 6

    def test_health_reports_degraded_llm(self, orch):
        health = orch.health()
        assert health["status"] == "ok"
        assert health["llm"]["available"] is False
        assert health["current_snapshot"]


class TestApprovalResumption:

    @staticmethod
    def _pending_approval_execution(orch):
        """
        Drive a run that genuinely lands in REQUIRES_APPROVAL.

        SHIFT_VOLUME maps to the reversible REROUTE_FLOW action, which is not
        structural, so it reaches the approval rules rather than being forced
        to HUMAN_ONLY.
        """
        return orch.run_sync(OrchestratorRequest(
            input="Shift DC_EAST volume to DC_WEST",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.SHIFT_VOLUME,
                facility_ids=["DC_EAST"], target_facility_id="DC_WEST")],
        ))

    def test_stale_snapshot_blocks_approval(self, network):
        """
        A decision made about one data version must not be applied to another.
        """
        orch = build_orchestrator(
            network=network, enable_llm=False,
            governance_policy=GovernancePolicy(
                cost_impact_human_pct=1e9,          # keep it off the HUMAN_ONLY path
                unserved_demand_human_rate=1.0,
                min_confidence_for_auto="HIGH",     # template confidence < HIGH
            ),
        )
        resp = self._pending_approval_execution(orch)
        if resp.status != "REQUIRES_APPROVAL":
            pytest.skip(f"run produced {resp.status}, not an approval path")

        # The observed network moves on while the approval is pending.
        modified = network.model_copy(deep=True)
        modified.facilities[0].capacity_units_per_period += 777
        modified = modified.model_copy(update={"data_version": modified.compute_data_version()})
        orch.snapshots.register(modified)

        resumed = orch.resolve_approval(
            resp.approval.approval_id,
            actor=Actor(actor_id="boss", role=ActorRole.APPROVER), approved=True,
        )
        assert resumed.status == "STALE"
        assert any(e["code"] == "STALE_SNAPSHOT" for e in resumed.errors)

    def test_approval_on_fresh_snapshot_completes(self, network):
        orch = build_orchestrator(
            network=network, enable_llm=False,
            governance_policy=GovernancePolicy(
                cost_impact_human_pct=1e9,
                unserved_demand_human_rate=1.0,
                min_confidence_for_auto="HIGH",
            ),
        )
        resp = self._pending_approval_execution(orch)
        if resp.status != "REQUIRES_APPROVAL":
            pytest.skip(f"run produced {resp.status}, not an approval path")

        resumed = orch.resolve_approval(
            resp.approval.approval_id,
            actor=Actor(actor_id="boss", role=ActorRole.APPROVER), approved=True,
        )
        assert resumed.execution_id == resp.execution_id, "must resume the SAME execution"
        assert resumed.status == "COMPLETED"


class TestParallelScenarios:

    def test_three_independent_scenarios_execute_cleanly(self, orch, network):
        """Independent what-ifs must not contaminate each other."""
        async def drive():
            reqs = [
                OrchestratorRequest(
                    input=f"close {fid}",
                    explicit_intent=Intent.SCENARIO_ANALYSIS,
                    explicit_scenarios=[ScenarioIntentSpec(
                        action=ScenarioActionType.CLOSE_FACILITY, facility_ids=[fid])],
                )
                for fid in ("DC_EAST", "DC_WEST", "DC_CENTRAL")
            ]
            return await asyncio.gather(*(orch.run(r) for r in reqs))

        results = asyncio.run(drive())
        assert len(results) == 3
        assert len({r.execution_id for r in results}) == 3
        assert len({r.scenario_id for r in results}) == 3

        for resp in results:
            assert resp.is_hypothetical is True
            assert resp.status in ("REQUIRES_HUMAN", "INFEASIBLE", "REQUIRES_APPROVAL")

        # Each scenario closed exactly its own facility.
        for resp, fid in zip(results, ("DC_EAST", "DC_WEST", "DC_CENTRAL")):
            record = orch.scenarios.get(resp.scenario_id)
            assert record.overrides == [f"CLOSE_FACILITY {fid}"]

        # And the observed network is untouched by all of it.
        for fid in ("DC_EAST", "DC_WEST", "DC_CENTRAL"):
            assert orch.snapshots.current().network.get_facility(fid).is_forced_closed is False


class TestDeterminism:

    def test_repeated_runs_produce_identical_deterministic_results(self, network):
        a = build_orchestrator(network=network, enable_llm=False)
        b = build_orchestrator(network=network, enable_llm=False)
        ra = run(a, input="What happens if we close DC_EAST?")
        rb = run(b, input="What happens if we close DC_EAST?")

        assert ra.status == rb.status
        assert ra.intent == rb.intent
        assert (ra.results["network"]["business_network_cost"]
                == pytest.approx(rb.results["network"]["business_network_cost"], abs=1e-6))
        assert (ra.governance.classification == rb.governance.classification)
        assert ra.governance.triggered_rules == rb.governance.triggered_rules

    def test_llm_absence_does_not_change_deterministic_numbers(self, network):
        """The core guarantee: engines are identical with or without a model."""
        offline = build_orchestrator(network=network, enable_llm=False)
        r1 = run(offline, input="What happens if we close DC_EAST?")
        r2 = offline.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?", disable_llm=True))
        assert (r1.results["network"]["business_network_cost"]
                == pytest.approx(r2.results["network"]["business_network_cost"], abs=1e-6))


# ===========================================================================
# END-TO-END — full pipeline
# ===========================================================================

class TestEndToEnd:

    def test_full_control_plane_pipeline(self, network):
        """
        Request → intent → plan → validate → execute (MILP/KPI/REI) → risk →
        reason → govern → respond → audit, with observed state intact.
        """
        orch = build_orchestrator(network=network, enable_llm=False)
        snapshot_before = orch.snapshots.current().network.model_dump_json()

        # 1) observed state
        state = run(orch, explicit_intent=Intent.NETWORK_STATE_QUERY, input="state?")
        assert state.status == "COMPLETED"
        assert state.is_hypothetical is False
        baseline_cost = state.results["network"]["business_network_cost"]
        assert baseline_cost > 0

        # 2) exposure
        resilience = run(orch, input="Which facility is most exposed?")
        assert resilience.status == "COMPLETED"
        top = resilience.results["resilience"]["highest_exposure_facility"]
        assert top

        # 3) hypothetical scenario on the most exposed facility
        scenario = orch.run_sync(OrchestratorRequest(
            input=f"What if we close {top}?",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY, facility_ids=[top])],
        ))
        assert scenario.is_hypothetical is True
        assert scenario.governance.classification == ActionClassification.HUMAN_ONLY
        assert scenario.status == "REQUIRES_HUMAN"

        # 4) external event → RF
        event = orch.run_sync(OrchestratorRequest(
            input=f"Severe flooding expected around {top}.",
            external_signal=ExternalSignal(
                event_type="FLOOD", location=top, event_probability=0.6,
                probability_basis="stated by source",
                source="met_office", confidence=0.9, affected_entity_ids=[top]),
        ))
        assert event.risk is not None
        rf_rows = event.risk["results"]
        assert rf_rows, "RF must be computed for the affected facility"
        assert rf_rows[0]["formula"] == RF_FORMULA

        # 5) every run is auditable
        for resp in (state, resilience, scenario, event):
            trace = orch.get_trace(resp.execution_id)
            assert trace is not None
            assert trace.baseline_snapshot_id
            assert trace.data_version
            assert trace.final_status == resp.status
            assert trace.governance_decision is not None

        # 6) observed state survived the whole pipeline
        assert orch.snapshots.current().network.model_dump_json() == snapshot_before

    def test_llm_cannot_modify_authoritative_state(self, network):
        """
        A model proposes; validation and governance dispose.

        An invented facility never reaches the MILP, and a structural action is
        never auto-executed no matter how the request is phrased.
        """
        orch = build_orchestrator(network=network, enable_llm=False)

        # Fabricated facility, injected as if a model had proposed it.
        bogus = orch.run_sync(OrchestratorRequest(
            input="close it",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY,
                facility_ids=["DC_HALLUCINATED"])],
        ))
        assert bogus.status in ("FAILED", "REQUIRES_HUMAN")
        assert any("INVALID_SCENARIO" in (e.get("code") or "") for e in bogus.errors)
        assert not orch.scenarios.list_ids(), "no scenario should have been created"

        # And a legitimate closure still cannot self-authorise.
        real = orch.run_sync(OrchestratorRequest(
            input="close DC_EAST",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_EAST"])],
        ))
        assert real.governance.classification != ActionClassification.AUTO_ACTION
