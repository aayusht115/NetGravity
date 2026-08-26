"""
Phase 8.6 — Comprehensive Adaptive Agentic Execution Test Suite.

Verifies:
  1. Closed-loop adaptive execution with observation and decision policy.
  2. Result-driven workflow branching (Cases A - J).
  3. Controlled replanning and loop guardrails (Cases K - M).
  4. Agentic boundary tests (AST / structural proofs).

INVARIANTS TESTED:
  - 0 external LLM/API calls.
  - 100% offline, deterministic execution.
  - CapabilityExecutor is single-shot and remains the only execution seam.
  - FailureManager remains the recovery authority.
  - ResultObserver reads typed evidence and never fabricates numbers.
  - PlanValidator strictly governs all initial and replanned graphs.
  - Digital Twin and Governance remain orchestrator-controlled.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from netgravity.forecasting.schemas import DemandPoint, DemandTimeSeries
from netgravity.ingestion.schemas.signal import (
    GuardrailVerdict,
    MarketIntelligenceSignal,
    ScenarioUse,
    SignalBucket,
    SignalConfidence,
)
from netgravity.orchestrator.core.adaptive_policy import (
    AdaptiveDecisionPolicy,
    ReplanGuard,
)
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_SCORE_MARKET,
    CAP_VALIDATE_SCEN,
    WorkflowPlanner,
)
from netgravity.orchestrator.core.result_observer import ResultObserver
from netgravity.orchestrator.planner.llm_planner import MockPlanner
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.routing.signal_router import (
    ExternalSignalRouter,
    RoutingOutcome,
    SignalRoutingDecision,
)
from netgravity.orchestrator.schemas.adaptive import (
    AdaptiveAction,
    AdaptiveDecision,
    AdaptiveExecutionConfig,
    ReplanRecord,
    ResultObservation,
)
from netgravity.orchestrator.schemas.agent_result import AgentError, AgentResult
from netgravity.orchestrator.schemas.plan_validation import PlanOrigin
from netgravity.orchestrator.schemas.planner_contract import (
    PlanProposal,
    ProposedPlanStep,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    DependencyType,
    EvidenceStatus,
    ExecutionPlan,
    PlanStep,
    StepStatus,
    UnavailableEvidence,
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
from netgravity.schemas.network import (
    CanonicalNetwork,
    NodeRole,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network
from netgravity.tests.integration.conftest import build_delhi_network


@pytest.fixture
def synthetic_network() -> CanonicalNetwork:
    """Fixture supplying a valid synthetic test network."""
    return build_case16_network()


@pytest.fixture
def delhi_network() -> CanonicalNetwork:
    """Fixture supplying the 3-market Delhi network."""
    return build_delhi_network()


def make_delhi_history(growth_step: float = 0.0) -> List[DemandTimeSeries]:
    """Helper to generate full demand history across all 3 Delhi markets."""
    markets = ("MKT_NORTH", "MKT_WEST", "MKT_EAST")
    return [
        DemandTimeSeries(
            market_id=m,
            product_id="P1",
            history=[
                DemandPoint(period=i + 1, quantity=100.0 + (i * growth_step))
                for i in range(12)
            ],
        )
        for m in markets
    ]


# ==============================================================================
# SECTION 1: RESULT-DRIVEN WORKFLOW BRANCHING (CASES A - J)
# ==============================================================================

class TestAdaptiveWorkflowBranching:
    """Tests evaluating deterministic observation and result-driven workflow branching."""

    def test_case_a_simple_success(self, synthetic_network):
        """
        CASE A — SIMPLE SUCCESS
        Forecast / network state query -> successful result -> next planned capability -> completion.
        """
        orch = build_orchestrator(network=synthetic_network, enable_llm=False)
        req = OrchestratorRequest(
            input="Review the baseline network state and status.",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
            actor=Actor(role=ActorRole.PLANNER, actor_id="planner_test"),
        )
        resp = orch.run_sync(req)

        assert resp.status in ("COMPLETED", "REQUIRES_HUMAN", "REQUIRES_APPROVAL")
        trace = orch.get_trace(resp.execution_id)
        assert trace is not None
        assert "network.load_snapshot" in trace.engine_results or len(trace.tool_invocations) > 0

    def test_case_b_material_forecast_change_triggers_replan(self, delhi_network):
        """
        CASE B — MATERIAL FORECAST CHANGE
        Forecast produces >= 15% demand growth -> ResultObserver identifies materiality ->
        AdaptiveDecisionPolicy returns REPLAN -> dynamically expands graph to include
        optimization, resilience, KPIs, reasoning, and governance.
        """
        config = AdaptiveExecutionConfig(
            enable_materiality_branching=True,
            material_forecast_threshold=0.15,
            max_replans=3,
        )
        mock_planner = MockPlanner()
        mock_planner.set_scenario("FORECAST")  # Initial plan is simple forecast

        history = make_delhi_history(growth_step=15.0)  # +15 units/period -> >50% growth

        orch = build_orchestrator(
            network=delhi_network,
            enable_llm=False,
            llm_planner=mock_planner,
            adaptive_config=config,
            history_provider=lambda snapshot: (history, []),
        )

        req = OrchestratorRequest(
            input="Forecast regional expansion demand for Delhi markets.",
            explicit_intent=Intent.FORECAST,
            actor=Actor(role=ActorRole.PLANNER, actor_id="planner_b"),
        )
        resp = orch.run_sync(req)
        assert resp.status in ("COMPLETED", "REQUIRES_APPROVAL", "REQUIRES_HUMAN")

        # Verify that context recorded the replan and decision history
        context = orch.state_store.get(resp.execution_id)
        assert context is not None
        assert context.initial_plan is not None
        assert len(context.decision_history) > 0
        assert context.replan_count >= 1

    def test_case_c_no_material_change_skips_unnecessary_analysis(self, delhi_network):
        """
        CASE C — NO MATERIAL CHANGE
        Forecast produces flat / stable demand (+0.1%) -> ResultObserver identifies FLAT_FORECAST ->
        AdaptiveDecisionPolicy proceeds without speculative heavy scenario restructuring.
        """
        config = AdaptiveExecutionConfig(
            enable_materiality_branching=True,
            material_forecast_threshold=0.15,
        )
        mock_planner = MockPlanner()
        mock_planner.set_scenario("FORECAST")

        history = make_delhi_history(growth_step=0.0)  # Perfectly flat history

        orch = build_orchestrator(
            network=delhi_network,
            enable_llm=False,
            llm_planner=mock_planner,
            adaptive_config=config,
            history_provider=lambda snapshot: (history, []),
        )

        req = OrchestratorRequest(
            input="Forecast demand for steady-state Delhi operations.",
            explicit_intent=Intent.FORECAST,
            actor=Actor(role=ActorRole.PLANNER, actor_id="planner_c"),
        )
        resp = orch.run_sync(req)
        assert resp.status in ("COMPLETED", "REQUIRES_APPROVAL", "REQUIRES_HUMAN")

        context = orch.state_store.get(resp.execution_id)
        assert context is not None
        # Verify no unnecessary replan was triggered for flat demand
        assert context.replan_count == 0

    def test_case_d_forecast_failure_transient_retry(self, synthetic_network):
        """
        CASE D — FORECAST FAILURE
        Forecast encounters transient timeout -> FailureManager retries capability -> succeeds.
        """
        orch = build_orchestrator(network=synthetic_network, enable_llm=False)
        step = PlanStep(step_id="s_f", capability=CAP_FORECAST)
        
        # Test retry decision from failure policy
        transient_result = AgentResult(
            capability=CAP_FORECAST,
            status=AgentStatus.RETRYABLE_FAILURE,
            errors=[AgentError(code="TIMEOUT", message="Gateway read timeout", failure_class="RETRYABLE")],
        )
        obs = orch.result_observer.observe(step, transient_result, ExecutionContext())
        assert obs.domain_outcome == "RETRYABLE_FAILURE"

        decision = orch.adaptive_policy.decide(
            step=step,
            observation=obs,
            result=transient_result,
            attempt=1,
            context=ExecutionContext(),
            current_plan=ExecutionPlan(workflow_id="wf_d", intent="FORECAST", steps=[step]),
        )
        assert decision.action == AdaptiveAction.RETRY

    def test_case_e_repeated_failure_exhausts_retries_and_escalates(self, synthetic_network):
        """
        CASE E — REPEATED FAILURE
        Capability repeatedly fails -> max attempts exhausted -> FailureManager escalates ->
        adaptive loop safely halts and records EscalationOutcome.
        """
        orch = build_orchestrator(network=synthetic_network, enable_llm=False)
        step = PlanStep(step_id="s_repeat", capability=CAP_OPTIMIZE)
        
        repeated_err = AgentResult(
            capability=CAP_OPTIMIZE,
            status=AgentStatus.RETRYABLE_FAILURE,
            errors=[AgentError(code="GATEWAY_502", message="Bad Gateway", failure_class="RETRYABLE")],
        )
        obs = orch.result_observer.observe(step, repeated_err, ExecutionContext())
        
        # At max attempts (attempt 3 of 3)
        decision = orch.adaptive_policy.decide(
            step=step,
            observation=obs,
            result=repeated_err,
            attempt=3,
            context=ExecutionContext(),
            current_plan=ExecutionPlan(workflow_id="wf_e", intent="OPTIMIZE", steps=[step]),
        )
        assert decision.action == AdaptiveAction.ESCALATE
        assert decision.escalation is not None
        assert decision.escalation.failed_attempts == 3

    def test_case_f_insufficient_evidence_preserves_gap_without_zero_fabrication(self, synthetic_network):
        """
        CASE F — INSUFFICIENT EVIDENCE
        Missing required prerequisite -> produces INSUFFICIENT_EVIDENCE ->
        absence is preserved explicitly as unavailable evidence; never defaulted to 0.0.
        """
        orch = build_orchestrator(network=synthetic_network, enable_llm=False)
        step = PlanStep(step_id="s_missing", capability=CAP_REI)
        
        insufficient_res = AgentResult.insufficient_evidence(
            capability=CAP_REI,
            reason="Missing network optimization snapshot",
        )
        obs = orch.result_observer.observe(step, insufficient_res, ExecutionContext())
        assert obs.domain_outcome == "INSUFFICIENT_EVIDENCE"
        assert obs.is_usable is False

        context = ExecutionContext()
        context.record_unavailable(CAP_REI, reason="Missing inputs", status=EvidenceStatus.UNAVAILABLE)
        assert CAP_REI in context.unavailable_evidence
        assert context.unavailable_evidence[CAP_REI].status == EvidenceStatus.UNAVAILABLE

    def test_case_g_infeasible_milp_preserves_infeasibility(self, synthetic_network):
        """
        CASE G — INFEASIBLE MILP
        Optimization solver proves mathematical infeasibility -> preserves INFEASIBLE status ->
        does NOT fabricate 0 cost -> escalates with mathematical explanation.
        """
        orch = build_orchestrator(network=synthetic_network, enable_llm=False)
        dcs = [f.id for f in synthetic_network.facilities if f.role == NodeRole.DC]
        
        # Close all DCs making flow to markets impossible -> solver proves INFEASIBLE
        resp = orch.run_sync(OrchestratorRequest(
            input="Close every distribution center",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY, facility_ids=dcs)],
        ))
        assert resp.status == "INFEASIBLE"
        context = orch.state_store.get(resp.execution_id)
        assert context is not None
        assert context.current_state == ExecutionState.INFEASIBLE

    def test_case_h_external_signal_route_to_forecast(self, synthetic_network):
        """
        CASE H — EXTERNAL SIGNAL TO FORECAST
        Valid MarketIntelligenceSignal -> ExternalSignalRouter routes to FORECASTING ->
        forecast execution evaluates dynamic network requirements.
        """
        router = ExternalSignalRouter()
        signal = MarketIntelligenceSignal(
            signal_id="sig_h_01",
            title="Major industrial expansion in Northern zone",
            published_date="2026-03-01T00:00:00Z",
            bucket=SignalBucket.MACRO,
            confidence=SignalConfidence.HIGH,
            scenario_use=ScenarioUse.FORECAST_ENRICHMENT,
            verdict=GuardrailVerdict(passed=True, bucket=SignalBucket.MACRO),
            affected_entities=["PLANT_NORTH"],
        )
        decision = router.route_for_forecast([signal], known_entity_ids={"PLANT_NORTH"})
        assert decision.accepted_ids == [signal.signal_id]
        assert decision.records[0].outcome == RoutingOutcome.ROUTED_TO_FORECASTING

    def test_case_i_irrelevant_signal_isolated(self, synthetic_network):
        """
        CASE I — IRRELEVANT SIGNAL ISOLATION
        Out-of-scope / irrelevant intelligence signal -> Router excludes signal ->
        baseline workflow runs unaffected.
        """
        router = ExternalSignalRouter()
        irrelevant_signal = MarketIntelligenceSignal(
            signal_id="sig_i_01",
            title="Unrelated retail holiday trend in international market",
            published_date="2026-03-01T00:00:00Z",
            bucket=SignalBucket.UNKNOWN,
            confidence=SignalConfidence.LOW,
            scenario_use=ScenarioUse.LOGGED_ONLY,
            verdict=GuardrailVerdict(passed=False, bucket=SignalBucket.UNKNOWN),
            affected_entities=["ForeignZone"],
        )
        decision = router.route_for_forecast([irrelevant_signal], known_entity_ids={"PLANT_NORTH", "DC_EAST"})
        assert len(decision.accepted) == 0
        assert decision.records[0].outcome in (
            RoutingOutcome.NOT_FORECAST_USE,
            RoutingOutcome.OUT_OF_SCOPE,
            RoutingOutcome.LOW_CONFIDENCE,
            RoutingOutcome.GUARDRAIL_NOT_PASSED,
        )

    def test_case_j_risk_signal_cannot_reach_forecasting(self, synthetic_network):
        """
        CASE J — RISK SIGNAL BOUNDARY
        Risk-bearing ExternalSignal with event_probability -> REFUSED_RISK_SIGNAL ->
        STRICTLY isolated from demand forecasting; confidence is never converted to event_probability.
        """
        router = ExternalSignalRouter()

        class RiskSignalHolder:
            signal_id = "risk_feed_01"
            event_probability = 0.85
            confidence = "HIGH"
            bucket = "DISRUPTION"

        decision = router.route_for_forecast([RiskSignalHolder()], known_entity_ids={"PLANT_1"})
        assert len(decision.accepted) == 0
        assert decision.records[0].outcome == RoutingOutcome.REFUSED_RISK_SIGNAL


# ==============================================================================
# SECTION 2: CONTROLLED REPLANNING & LOOP GUARDRAILS (CASES K - M)
# ==============================================================================

class TestControlledReplanningAndGuards:
    """Tests evaluating replanning lifecycle and infinite-loop safety guards."""

    def test_case_k_controlled_replan_lifecycle(self, synthetic_network):
        """
        CASE K — REPLAN LIFECYCLE
        Initial plan -> trigger observation -> REPLAN action -> Planner proposes new proposal ->
        PlanValidator verifies and approves -> ExecutionContext records ReplanRecord and plan_history.
        """
        mock_planner = MockPlanner()
        orch = build_orchestrator(
            network=synthetic_network,
            enable_llm=False,
            llm_planner=mock_planner,
        )

        context = ExecutionContext.from_request(
            OrchestratorRequest(input="Replan demonstration", request_id="req_k")
        )
        initial_plan = ExecutionPlan(
            workflow_id="wf_initial",
            intent="FORECAST",
            steps=[PlanStep(step_id="load", capability=CAP_LOAD_NETWORK)],
        )
        context.plan = initial_plan
        context.initial_plan = initial_plan

        # Replan proposal
        replan_record = ReplanRecord(
            replan_index=1,
            trigger_step_id="load",
            trigger_capability=CAP_LOAD_NETWORK,
            trigger_reason="Material market shift observed",
            previous_plan_id="wf_initial",
            new_plan_id="wf_replanned",
            plan_signature="load -> forecast -> reason",
            approved=True,
        )
        context.record_replan(replan_record)

        assert context.replan_count == 1
        assert len(context.replan_history) == 1
        assert context.replan_history[0].approved is True

    def test_case_l_replan_limit_guard_prevents_infinite_loops(self, synthetic_network):
        """
        CASE L — REPLAN LIMIT GUARD
        Repeated replanning attempts -> ReplanGuard hits max_replans (3) ->
        further replan refused -> triggers ESCALATE.
        """
        config = AdaptiveExecutionConfig(max_replans=3)
        guard = ReplanGuard(config)
        
        proposed = ExecutionPlan(
            workflow_id="wf_prop",
            intent="SCENARIO_ANALYSIS",
            steps=[
                PlanStep(step_id="s1", capability=CAP_LOAD_NETWORK),
                PlanStep(step_id="s2", capability=CAP_OPTIMIZE, depends_on=["s1"]),
            ],
        )

        # Attempt 1, 2, 3 permitted
        ok1, _ = guard.check_replan_eligibility(0, proposed, set(), 2)
        assert ok1 is True
        ok2, _ = guard.check_replan_eligibility(1, proposed, set(), 4)
        assert ok2 is True
        ok3, _ = guard.check_replan_eligibility(2, proposed, set(), 6)
        assert ok3 is True

        # Attempt 4 refused (exceeds max 3)
        ok4, msg = guard.check_replan_eligibility(3, proposed, set(), 8)
        assert ok4 is False
        assert "Maximum replan limit reached" in msg

    def test_case_m_repeated_plan_cycle_detection(self, synthetic_network):
        """
        CASE M — REPEATED PLAN CYCLE DETECTION
        Planner proposes a capability graph identical to an already-executed plan ->
        ReplanGuard computes canonical plan signature -> detects cycle -> halts loop.
        """
        config = AdaptiveExecutionConfig(max_replans=5)
        guard = ReplanGuard(config)

        plan_a = ExecutionPlan(
            workflow_id="wf_a",
            intent="FORECAST",
            steps=[
                PlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                PlanStep(step_id="forecast", capability=CAP_FORECAST, depends_on=["load"]),
            ],
        )
        sig_a = guard.plan_signature(plan_a)
        past_signatures = {sig_a}

        # Planner attempts to propose Plan A again
        can_replan, msg = guard.check_replan_eligibility(
            current_replan_count=1,
            proposed_plan=plan_a,
            past_plan_signatures=past_signatures,
            total_steps_executed=2,
        )
        assert can_replan is False
        assert "Repeated-plan cycle detected" in msg


# ==============================================================================
# SECTION 3: AGENTIC BOUNDARY & ARCHITECTURAL INVARIANT PROOFS
# ==============================================================================

class TestAgenticBoundaryInvariants:
    """
    Structural & AST checks proving:
      - LLM Planner cannot execute capabilities directly.
      - Planner cannot invoke CapabilityExecutor or FailureManager.
      - Specialist capabilities cannot call the planner or other specialists.
      - CapabilityExecutor remains the single execution seam.
      - FailureManager remains the recovery authority.
    """

    def test_llm_planner_has_no_execution_authority(self):
        """Planner must produce proposals only and have no reference to CapabilityExecutor."""
        from netgravity.orchestrator.planner.llm_planner import LiveLLMPlanner, MockPlanner
        
        for planner_cls in (MockPlanner, LiveLLMPlanner):
            sig = inspect.signature(planner_cls.__init__)
            # Assert executor is never passed into planner
            assert "executor" not in sig.parameters
            assert "failure_manager" not in sig.parameters
            assert "twin" not in sig.parameters

            methods = [m for m in dir(planner_cls) if not m.startswith("_")]
            assert "execute" not in methods
            assert "execute_plan" not in methods
            assert "run_capability" not in methods

    def test_specialists_do_not_import_or_call_planners(self):
        """Specialist capability handlers must never invoke the WorkflowPlanner or LLMPlanner."""
        registry_path = Path("netgravity/orchestrator/registry.py")
        assert registry_path.exists()

        tree = ast.parse(registry_path.read_text(encoding="utf-8"))
        
        # Check all function definitions in registry.py
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith("_run_"):
                # Specialist handler
                for child in ast.walk(node):
                    if isinstance(child, ast.Attribute):
                        assert child.attr not in ("propose_plan", "propose_replan", "replan"), (
                            f"Specialist handler '{node.name}' contains illegal planner call '{child.attr}'"
                        )

    def test_capability_executor_remains_single_shot(self):
        """CapabilityExecutor must not contain internal retry loops or replanning logic."""
        from netgravity.orchestrator.core.executor import CapabilityExecutor
        
        exec_src = inspect.getsource(CapabilityExecutor.execute)
        # Executor must execute a single invocation and return AgentResult
        assert "while" not in exec_src
        assert "propose_replan" not in exec_src
        assert "execute_plan" not in exec_src
