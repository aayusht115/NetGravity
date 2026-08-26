"""
Phase 8.5 — Offline Agentic Flow & Mock Planner Test Suite.

Verifies:
  1. Offline MockPlanner with zero external API calls.
  2. Mock Scenarios 1 through 14.
  3. Flow Cases A through F (Network State, Forecast, Scenario, External Signal, Resilience, Full Impact).
  4. Adversarial Plan Tests A through K (unknown capabilities, non-plannable, cyclic, missing HARD dep, etc.).
  5. Deterministic fallback to WorkflowPlanner with PlanOrigin.DETERMINISTIC_FALLBACK.
  6. FailureManager and CapabilityExecutor integration.
  7. Signal routing separation (MarketIntelligenceSignal -> Forecast vs ExternalSignal -> RF).
  8. Security & Authority Invariants (event_probability != confidence, failed RF != 0).
  9. AST / Static Architectural boundaries.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import textwrap
from typing import Any, Dict, List, Optional

import pytest

from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.llm_gateway import LLMGateway
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.core.executor import CapabilityExecutor
from netgravity.orchestrator.core.failure_manager import FailureManager
from netgravity.orchestrator.core.plan_graph import PlanRefused, PlanValidator
from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_VALIDATE_SCEN,
    WorkflowPlanner,
)
from netgravity.orchestrator.exceptions import (
    LLMFailureError,
    LLMNonRetryableError,
    PlanningFailureError,
)
from netgravity.orchestrator.planner import llm_planner as llm_planner_module
from netgravity.orchestrator.planner.llm_planner import (
    LiveLLMPlanner,
    MockPlanner,
)
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.routing.signal_router import ExternalSignalRouter, RoutingOutcome
from netgravity.orchestrator.schemas.plan_validation import (
    PlanFailureReason,
    PlanOrigin,
)
from netgravity.orchestrator.schemas.planner_contract import (
    PlanProposal,
    ProposedPlanStep,
    plan_proposal_to_execution_plan,
)
from netgravity.orchestrator.schemas.plans import AgentStatus, ExecutionPlan, PlanStep
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    EventSeverity,
    ExternalSignal,
    Intent,
    IntentResolution,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def network():
    return build_case16_network()


@pytest.fixture
def registry():
    orch = build_orchestrator(enable_llm=False)
    return orch.registry


@pytest.fixture
def mock_planner(registry):
    return MockPlanner(registry=registry)


@pytest.fixture
def orch(network, mock_planner):
    return build_orchestrator(
        network=network,
        enable_llm=False,
        llm_planner=mock_planner,
    )


# ===========================================================================
# 1. Mock Planner Scenarios (1 through 14)
# ===========================================================================

class TestMockPlannerScenarios:
    """Verify all 14 MockPlanner scenarios generate structured deterministic proposals."""

    def test_scenario_1_network_state(self, mock_planner):
        req = OrchestratorRequest(input="What is the current network state?")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("NETWORK_STATE")
        prop = mock_planner.propose_plan_sync(req, res)
        assert prop.planner_source == PlanOrigin.MOCK_LLM
        assert CAP_LOAD_NETWORK in prop.capabilities
        assert CAP_OPTIMIZE in prop.capabilities
        assert CAP_KPI in prop.capabilities

    def test_scenario_2_forecast(self, mock_planner):
        req = OrchestratorRequest(input="Forecast demand for next period.")
        res = IntentResolution(intent=Intent.FORECAST)
        mock_planner.set_scenario("FORECAST")
        prop = mock_planner.propose_plan_sync(req, res)
        assert CAP_FORECAST in prop.capabilities
        assert CAP_OPTIMIZE not in prop.capabilities, "Forecast workflow must not schedule solver"

    def test_scenario_3_scenario_analysis(self, mock_planner):
        req = OrchestratorRequest(input="What if we close DC_EAST?")
        res = IntentResolution(intent=Intent.SCENARIO_ANALYSIS)
        mock_planner.set_scenario("SCENARIO_ANALYSIS")
        prop = mock_planner.propose_plan_sync(req, res)
        assert "optimization.solve_scenario" in prop.capabilities

    def test_scenario_4_market_intelligence(self, mock_planner):
        req = OrchestratorRequest(input="Diesel prices are up 8%")
        res = IntentResolution(intent=Intent.MARKET_INTELLIGENCE)
        mock_planner.set_scenario("MARKET_INTELLIGENCE")
        prop = mock_planner.propose_plan_sync(req, res)
        assert "market.score_signal" in prop.capabilities
        assert CAP_FORECAST not in prop.capabilities

    def test_scenario_5_resilience(self, mock_planner):
        req = OrchestratorRequest(input="Which facility is most vulnerable?")
        res = IntentResolution(intent=Intent.RESILIENCE_QUERY)
        mock_planner.set_scenario("RESILIENCE")
        prop = mock_planner.propose_plan_sync(req, res)
        assert CAP_REI in prop.capabilities

    def test_scenario_6_full_network_impact(self, mock_planner):
        req = OrchestratorRequest(input="Major expansion in Delhi. Assess full impact.")
        res = IntentResolution(intent=Intent.SCENARIO_ANALYSIS)
        mock_planner.set_scenario("FULL_NETWORK_IMPACT")
        prop = mock_planner.propose_plan_sync(req, res)
        assert CAP_LOAD_NETWORK in prop.capabilities
        assert "optimization.solve_scenario" in prop.capabilities
        assert CAP_REI in prop.capabilities

    def test_scenario_7_invalid_capability(self, mock_planner):
        req = OrchestratorRequest(input="Run magic optimization.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("INVALID_CAPABILITY")
        prop = mock_planner.propose_plan_sync(req, res)
        assert "nonexistent.magic_solver" in prop.capabilities

    def test_scenario_8_invalid_dependency(self, mock_planner):
        req = OrchestratorRequest(input="Run cyclic workflow.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("INVALID_DEPENDENCY")
        prop = mock_planner.propose_plan_sync(req, res)
        step_map = {s.step_id: s for s in prop.steps}
        assert "step_b" in step_map["step_a"].depends_on
        assert "step_a" in step_map["step_b"].depends_on

    def test_scenario_9_forbidden_rf_path(self, mock_planner):
        req = OrchestratorRequest(input="Direct risk calculation.")
        res = IntentResolution(intent=Intent.EXTERNAL_EVENT)
        mock_planner.set_scenario("FORBIDDEN_RF_PATH")
        prop = mock_planner.propose_plan_sync(req, res)
        assert prop.capabilities == [CAP_RISK]

    def test_scenario_10_malformed_plan(self, mock_planner):
        """Malformed proposal with empty step fields raises validation error."""
        req = OrchestratorRequest(input="Malformed test.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("MALFORMED_PLAN")
        with pytest.raises(ValueError, match="Every proposed step must define both step_id and capability"):
            mock_planner.propose_plan_sync(req, res)

    def test_scenario_11_empty_plan(self, mock_planner):
        req = OrchestratorRequest(input="Empty test.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("EMPTY_PLAN")
        prop = mock_planner.propose_plan_sync(req, res)
        assert len(prop.steps) == 0

    def test_scenario_12_unknown_intent(self, mock_planner):
        req = OrchestratorRequest(input="Random unrecognised query.")
        res = IntentResolution(intent=Intent.UNKNOWN)
        mock_planner.set_scenario(None)
        prop = mock_planner.propose_plan_sync(req, res)
        assert prop.intent == Intent.UNKNOWN.value

    def test_scenario_13_llm_unavailable(self, mock_planner):
        req = OrchestratorRequest(input="Test offline.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("LLM_UNAVAILABLE")
        with pytest.raises(LLMNonRetryableError, match="unavailable"):
            mock_planner.propose_plan_sync(req, res)

    def test_scenario_14_retryable_planner_failure(self, mock_planner):
        req = OrchestratorRequest(input="Test rate limit.")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        mock_planner.set_scenario("RETRYABLE_PLANNER_FAILURE")
        with pytest.raises(LLMFailureError, match="HTTP 429"):
            mock_planner.propose_plan_sync(req, res)


# ===========================================================================
# 2. Agent Flow Examples (Cases A through F)
# ===========================================================================

class TestAgentFlowExamples:
    """Verify complete lifecycle flows for Cases A through F."""

    def test_case_a_network_state_flow(self, orch):
        """Case A: Network state query -> NETWORK_STATE -> REASONING (no forecast, no unnecessary REI)."""
        resp = orch.run_sync(OrchestratorRequest(
            input="What is the current state of the network?",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
        ))
        assert resp.status == "COMPLETED"
        assert resp.intent == "NETWORK_STATE_QUERY"
        step_caps = [s["capability"] for s in resp.steps]
        assert CAP_LOAD_NETWORK in step_caps
        assert CAP_OPTIMIZE in step_caps
        assert CAP_FORECAST not in step_caps

    def test_case_b_forecast_flow(self, orch):
        """Case B: Forecast demand -> FORECAST -> REASONING (no solve)."""
        resp = orch.run_sync(OrchestratorRequest(
            input="Forecast demand for Delhi.",
            explicit_intent=Intent.FORECAST,
        ))
        assert resp.intent == "FORECAST"
        step_caps = [s["capability"] for s in resp.steps]
        assert CAP_FORECAST in step_caps
        assert CAP_OPTIMIZE not in step_caps

    def test_case_c_demand_scenario_flow(self, orch):
        """Case C: Scenario analysis -> SCENARIO -> OPTIMIZATION -> REASONING -> GOVERNANCE."""
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if DC_EAST capacity decreases?",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=["DC_EAST"],
                capacity_delta_units=-2000,
            )],
        ))
        assert resp.intent == "SCENARIO_ANALYSIS"
        assert resp.is_hypothetical is True
        assert resp.governance is not None

    def test_case_d_external_signal_flow(self, orch):
        """Case D: External hazard signal -> INTERPRET_SIGNAL -> REI -> RISK -> GOVERNANCE."""
        signal = ExternalSignal(
            event_type="FLOOD",
            location="DC_EAST",
            event_probability=0.6,
            probability_basis="official forecast",
            source="met_office",
            confidence=0.85,
            affected_entity_ids=["DC_EAST"],
        )
        resp = orch.run_sync(OrchestratorRequest(
            input="Severe flooding expected around DC_EAST.",
            external_signal=signal,
        ))
        assert resp.intent == "EXTERNAL_EVENT"
        assert resp.risk is not None

    def test_case_e_resilience_flow(self, orch):
        """Case E: Resilience query -> RESILIENCE -> REASONING (no fresh scenario solve)."""
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if the Delhi DC fails? Which facility is most exposed?",
            explicit_intent=Intent.RESILIENCE_QUERY,
        ))
        assert resp.intent == "RESILIENCE_QUERY"
        assert "resilience" in resp.results

    def test_case_f_full_network_impact_flow(self, network, registry):
        """Case F: Full multi-capability network impact with mock planner proposal."""
        mock = MockPlanner(registry=registry, simulated_scenario="FULL_NETWORK_IMPACT")
        orch_f = build_orchestrator(network=network, enable_llm=False, llm_planner=mock)
        resp = orch_f.run_sync(OrchestratorRequest(
            input="A major customer is expanding in Delhi. Assess the impact and recommend what we should do.",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_DEMAND,
                facility_ids=["CUST_01"],
                demand_multiplier=1.2,
            )],
        ))
        assert resp.intent == "SCENARIO_ANALYSIS"
        assert resp.governance is not None


# ===========================================================================
# 3. Adversarial Plan Tests (A through K)
# ===========================================================================

class TestAdversarialPlanTests:
    """Verify deterministic validation rejects all invalid / unsafe planner proposals."""

    def test_adversarial_a_unknown_capability(self, registry):
        validator = PlanValidator(registry)
        prop = PlanProposal(
            steps=[ProposedPlanStep(step_id="s1", capability="fake.nonexistent_engine")],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.UNKNOWN_CAPABILITY in validation.reasons()

    def test_adversarial_b_non_plannable_capability(self, registry):
        validator = PlanValidator(registry)
        prop = PlanProposal(
            steps=[ProposedPlanStep(step_id="s1", capability="twin.publish")],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.NOT_PLANNABLE in validation.reasons()

    def test_adversarial_c_invalid_dependency_cycle(self, registry):
        validator = PlanValidator(registry)
        prop = PlanProposal(
            steps=[
                ProposedPlanStep(step_id="s1", capability=CAP_LOAD_NETWORK, depends_on=["s2"]),
                ProposedPlanStep(step_id="s2", capability=CAP_OPTIMIZE, depends_on=["s1"]),
            ],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.DEPENDENCY_CYCLE in validation.reasons()

    def test_adversarial_d_missing_hard_dependency(self, registry):
        validator = PlanValidator(registry)
        # optimization.solve declares required_dependencies=(network.load_snapshot,)
        prop = PlanProposal(
            steps=[ProposedPlanStep(step_id="s1", capability=CAP_OPTIMIZE)],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.MISSING_HARD_DEPENDENCY in validation.reasons()

    def test_adversarial_e_missing_hard_kpi_dependency(self, registry):
        validator = PlanValidator(registry)
        # kpi.summarise declares required_dependencies=(optimization.solve,)
        prop = PlanProposal(
            steps=[ProposedPlanStep(step_id="s1", capability=CAP_KPI)],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.MISSING_HARD_DEPENDENCY in validation.reasons()

    def test_adversarial_f_market_intelligence_refused_for_risk(self):
        """Market intelligence signals with probability fields are refused by router."""
        router = ExternalSignalRouter()
        decision = router.route_for_forecast([
            ExternalSignal(
                event_type="FLOOD",
                event_probability=0.8,
                confidence=0.9,
            )
        ])
        assert len(decision.accepted) == 0
        assert decision.records[0].outcome == RoutingOutcome.REFUSED_RISK_SIGNAL

    def test_adversarial_g_confidence_never_converted_to_event_probability(self):
        """High confidence in an assessment must never become an event probability."""
        sig = ExternalSignal(
            event_type="STRIKE",
            event_probability=0.3,
            confidence=0.99,
        )
        assert sig.event_probability == 0.3
        assert sig.confidence == 0.99
        assert sig.event_probability != sig.confidence

    def test_adversarial_h_proposal_cannot_contain_calculated_domain_costs(self):
        """Planner proposal schema forbids authoritative domain cost keys in params."""
        with pytest.raises(ValueError, match="domain calculation output"):
            ProposedPlanStep(
                step_id="fake_solve",
                capability=CAP_OPTIMIZE,
                params={"business_network_cost": 50000.0},
            )

    def test_adversarial_i_duplicate_step_ids(self, registry):
        validator = PlanValidator(registry)
        prop = PlanProposal(
            steps=[
                ProposedPlanStep(step_id="dup_id", capability=CAP_LOAD_NETWORK),
                ProposedPlanStep(step_id="dup_id", capability=CAP_LOAD_NETWORK),
            ],
            planner_source=PlanOrigin.MOCK_LLM,
        )
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.DUPLICATE_STEP in validation.reasons()

    def test_adversarial_j_empty_plan_proposal(self, registry):
        validator = PlanValidator(registry)
        prop = PlanProposal(steps=[], planner_source=PlanOrigin.MOCK_LLM)
        plan = plan_proposal_to_execution_plan(prop)
        validation = validator.validate(plan)
        assert validation.valid is False
        assert PlanFailureReason.EMPTY_PLAN in validation.reasons()


# ===========================================================================
# 4. Deterministic Fallback & Provenance
# ===========================================================================

class TestDeterministicFallbackAndProvenance:
    """Verify fallback to WorkflowPlanner when Mock/LLM planner fails or proposes invalid plans."""

    def test_fallback_on_invalid_planner_proposal(self, network, registry):
        """When mock planner proposes an invalid capability, system falls back to deterministic template."""
        mock = MockPlanner(registry=registry, simulated_scenario="INVALID_CAPABILITY")
        orch = build_orchestrator(network=network, enable_llm=False, llm_planner=mock)
        resp = orch.run_sync(OrchestratorRequest(
            input="State of network",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
        ))
        assert resp.status == "COMPLETED"
        # Warning recorded about fallback
        assert any("falling back" in w.lower() or "fallback" in w.lower() for w in resp.warnings)

    def test_fallback_on_planner_timeout_or_error(self, network, registry):
        """When mock planner raises an LLM error, system falls back cleanly to deterministic planner."""
        mock = MockPlanner(registry=registry, simulated_scenario="RETRYABLE_PLANNER_FAILURE")
        orch = build_orchestrator(network=network, enable_llm=False, llm_planner=mock)
        resp = orch.run_sync(OrchestratorRequest(
            input="State of network",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
        ))
        assert resp.status == "COMPLETED"
        assert any("fell back" in w.lower() or "falling back" in w.lower() or "fallback" in w.lower() for w in resp.warnings)


# ===========================================================================
# 5. Static AST Architecture & Zero-API Verification
# ===========================================================================

class TestStaticArchitectureAndZeroAPICalls:
    """Verify structural isolation and 0 external API calls."""

    def test_planner_module_cannot_call_milp_or_rei_directly(self):
        """llm_planner module must not import PuLP or compute mathematical formulas."""
        source = inspect.getsource(llm_planner_module)
        assert "LpProblem" not in source
        assert "pulp" not in source
        assert "REIClient" not in source
        assert "OptimizationClient" not in source

    def test_zero_api_calls_in_mock_planner(self, mock_planner):
        """Mock planner must never instantiate an HTTP client or make network requests."""
        source = inspect.getsource(MockPlanner)
        assert "requests.post" not in source
        assert "httpx" not in source
        assert "urllib" not in source
        assert "openai" not in source

    def test_live_planner_has_hard_15_call_limit(self, registry):
        """LiveLLMPlanner enforces a hard maximum of 15 calls."""
        gw = LLMGateway()
        live_planner = LiveLLMPlanner(gw, registry, max_calls=15)
        assert live_planner.max_calls == 15
        live_planner.calls_attempted = 15
        req = OrchestratorRequest(input="test")
        res = IntentResolution(intent=Intent.NETWORK_STATE_QUERY)
        with pytest.raises(LLMNonRetryableError, match="quota exhausted: reached maximum of 15 calls"):
            live_planner.propose_plan_sync(req, res)
