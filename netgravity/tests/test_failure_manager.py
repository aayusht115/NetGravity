"""
Phase 8.4 — Failure Management, Recovery Policy, and Circuit Breaker Suite.

Tests verify:
  1. Static AST Invariants:
     - CapabilityExecutor remains strictly single-shot with no retry/recovery loops.
     - FailureManager sits above CapabilityExecutor and coordinates execution via the seam.
     - FailureManager does not invent arbitrary workflows or perform domain calculations.

  2. Scenarios A through Q (17 scenarios):
     - Scenario A: Normal success
     - Scenario B: Retryable failure then success (attempt 1 fails, attempt 2 succeeds)
     - Scenario C: Retryable failure exhausting attempts -> Escalation
     - Scenario D: Non-retryable failure -> no retry (attempts=1)
     - Scenario E: Insufficient evidence -> no retry, hard dependents blocked
     - Scenario F: Invalid output -> INVALID_OUTPUT, no fake output, no retry
     - Scenario G: MILP solver infeasible -> mathematical outcome, never retried
     - Scenario H: REI unavailable when prerequisite fails -> NEVER zero
     - Scenario I: Missing HARD dependency -> BLOCKED, NOT_RUN
     - Scenario J: Missing SOFT dependency -> runs degraded with UnavailableEvidence
     - Scenario K: Valid reroute to registered alternative capability
     - Scenario L: No valid reroute -> ESCALATE
     - Scenario M: Repeated LLM gateway failure -> Circuit breaker trips to OPEN
     - Scenario N: Circuit OPEN -> fast-fails without external calls
     - Scenario O: Circuit recovery via HALF_OPEN probe -> reset to CLOSED
     - Scenario P: Provenance integrity across retries/reroutes
     - Scenario Q: Execution state history preservation (attempt 1 never overwritten)

  3. Realistic Integration Cases (Case 16 network / synthetic data):
     - Case 1: Forecast succeeds end-to-end
     - Case 2: Forecast insufficient data -> Optimization blocked -> Reasoning degrades honestly
     - Case 3: Transient reasoning failure -> retry succeeds
     - Case 4: Persistent external failure -> retries exhausted -> escalation & circuit breaker

  4. Security and Authority Invariants:
     - Output is never fabricated or defaulted to 0
     - event_probability != signal_confidence
     - failed RF != (RF = 0)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import textwrap
import time
from typing import Any, Dict, List, Optional

import pytest

from netgravity.orchestrator.core import executor as executor_module
from netgravity.orchestrator.core import failure_manager as failure_manager_module
from netgravity.orchestrator.core.circuit_breaker import CircuitBreaker
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.executor import CapabilityExecutor
from netgravity.orchestrator.core.failure_manager import FailureManager
from netgravity.orchestrator.core.planner import (
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
)
from netgravity.orchestrator.exceptions import (
    EngineFailureError,
    EngineTimeoutError,
    FailureClass,
    MissingDataError,
    SolverInfeasibleError,
    ValidationFailureError,
)
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.agent_result import AgentError, AgentResult
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
    InvocationMode,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    DependencyType,
    EvidenceStatus,
    ExecutionMode,
    ExecutionPlan,
    PlanStep,
    StepStatus,
    ToolRequest,
    ToolResult,
    UnavailableEvidence,
)
from netgravity.orchestrator.schemas.recovery import (
    CircuitState,
    EscalationOutcome,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    ExternalSignal,
    Intent,
    OrchestratorRequest,
)
from netgravity.orchestrator.tools.base import NO_RETRY, Capability
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _contract(capability_id: str, **kw) -> CapabilityContract:
    kw.setdefault("domain", CapabilityDomain.KPI)
    kw.setdefault("provider", "TestProvider")
    if kw.get("llm_backed"):
        kw.setdefault("execution_mode", ExecutionMode.PROBABILISTIC)
    return CapabilityContract(capability_id=capability_id, **kw)


def _capability(name: str, handler, *, contract=None, **kw) -> Capability:
    kw.setdefault("retry_policy", NO_RETRY)
    c = contract or _contract(name)
    if c.llm_backed:
        kw.setdefault("execution_mode", ExecutionMode.PROBABILISTIC)
    return Capability(name=name, handler=handler, contract=c, **kw)


def _registry_with(*capabilities: Capability) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for capability in capabilities:
        registry.register(capability)
    return registry


# ===========================================================================
# 1. Static AST Invariant Tests
# ===========================================================================

class TestStaticArchitectureInvariants:
    """Verify architectural boundaries via static AST inspection."""

    def test_capability_executor_remains_single_shot(self):
        """CapabilityExecutor must NOT contain while loops or retry logic."""
        source = textwrap.dedent(inspect.getsource(CapabilityExecutor.execute))
        tree = ast.parse(source)
        while_loops = [node for node in ast.walk(tree) if isinstance(node, ast.While)]
        assert len(while_loops) == 0, "CapabilityExecutor.execute must remain strictly single-shot without while loops"

    def test_failure_manager_calls_executor_seam(self):
        """FailureManager must coordinate step execution via CapabilityExecutor.execute."""
        source = inspect.getsource(FailureManager.execute_step_with_recovery)
        assert "self.executor.execute" in source, "FailureManager must delegate execution to CapabilityExecutor.execute"

    def test_failure_manager_does_not_modify_algorithms(self):
        """FailureManager must NOT contain domain math calculations (MILP, REI, RF formulas)."""
        source = inspect.getsource(failure_manager_module)
        assert "LpProblem" not in source
        assert "pulp" not in source
        assert "REI =" not in source
        assert "RF =" not in source


# ===========================================================================
# 2. Scenarios A through Q (17 Scenarios)
# ===========================================================================

class TestFailureScenariosAThroughQ:
    """
    Exhaustive verification of failure management Scenarios A through Q.
    """

    def test_scenario_a_normal_success(self):
        """Scenario A: Successful execution -> SUCCESS, 1 attempt, no recovery needed."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            return {"kpi": "value"}

        cap = _capability("kpi.calc", handler)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="kpi.calc")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.SUCCESS
        assert calls == 1
        assert len(ctx.step_attempts["s1"]) == 1
        assert ctx.step_attempts["s1"][0].status == AgentStatus.SUCCESS
        assert len(ctx.escalations) == 0

    def test_scenario_b_retryable_failure_then_success(self):
        """Scenario B: Attempt 1 fails (transient), attempt 2 succeeds. Both attempts preserved in history."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EngineTimeoutError("Transient timeout on attempt 1")
            return {"status": "recovered"}

        cap = _capability("transient.calc", handler)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=3, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="transient.calc")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.SUCCESS
        assert calls == 2
        # History must contain BOTH attempt 1 (failed) and attempt 2 (success)
        attempts = ctx.step_attempts["s1"]
        assert len(attempts) == 2
        assert attempts[0].attempt == 1
        assert attempts[0].status == AgentStatus.RETRYABLE_FAILURE
        assert attempts[0].error_code == "ENGINE_TIMEOUT"
        assert attempts[1].attempt == 2
        assert attempts[1].status == AgentStatus.SUCCESS

    def test_scenario_c_retryable_failure_exhausting_attempts(self):
        """Scenario C: Transient failure fails all 3 attempts -> ESCALATE with EscalationOutcome."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            raise EngineTimeoutError(f"Persistent timeout on attempt {calls}")

        cap = _capability("timeout.calc", handler)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=3, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="timeout.calc")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.RETRYABLE_FAILURE
        assert calls == 3
        assert len(ctx.step_attempts["s1"]) == 3
        assert len(ctx.escalations) == 1
        esc = ctx.escalations[0]
        assert esc.capability == "timeout.calc"
        assert esc.failed_attempts == 3
        assert "timeout.calc" in esc.reason

    def test_scenario_d_non_retryable_failure_no_retry(self):
        """Scenario D: Non-retryable error (e.g. MissingDataError) -> exactly 1 attempt made, never retried."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            raise MissingDataError("Demand baseline is absent")

        cap = _capability("missing.data", handler)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=3, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="missing.data")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status in (AgentStatus.NON_RETRYABLE_FAILURE, AgentStatus.INSUFFICIENT_EVIDENCE)
        assert calls == 1, "Non-retryable failure must not be retried"
        assert len(ctx.step_attempts["s1"]) == 1

    def test_scenario_e_insufficient_evidence_no_retry(self):
        """Scenario E: Missing declared required input refused at preflight -> INSUFFICIENT_EVIDENCE, no retry."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            return {"ok": True}

        contract = _contract("req.input", required_inputs=("facility_id",))
        cap = _capability("req.input", handler, contract=contract)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="req.input", params={})  # missing facility_id

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.INSUFFICIENT_EVIDENCE
        assert calls == 0, "Preflight refusal must not execute handler"
        assert len(ctx.step_attempts["s1"]) == 1

    def test_scenario_f_invalid_output_no_fake_output_no_retry(self):
        """Scenario F: Output failing validation schema -> INVALID_OUTPUT, output is None, no retry."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            # Put invalid non-registry object on typed field
            ctx.rei_registry = "not_a_registry_object"
            return {"raw": 123}

        contract = _contract("val.out", authoritative_field="rei_registry", output_type="FacilityResilienceRegistry")
        cap = _capability("val.out", handler, contract=contract)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="val.out")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.INVALID_OUTPUT
        assert result.output is None
        assert calls == 1

    def test_scenario_g_milp_solver_infeasible_halts_plan(self):
        """Scenario G: Solver infeasibility is a mathematical finding, never retried, halts plan execution."""
        calls = 0

        async def handler(ctx, req):
            nonlocal calls
            calls += 1
            raise SolverInfeasibleError("Network constraints cannot be satisfied", context={"unmet_demand": 5000})

        cap = _capability("network.solve", handler)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()
        plan = ExecutionPlan(
            workflow_id="wf1", intent="optimize",
            steps=[PlanStep(step_id="s1", capability="network.solve")]
        )
        ctx.plan = plan

        _run(fm.execute_plan(plan, ctx))
        assert calls == 1, "Infeasible solve must NEVER be retried"
        assert ctx.audit_metadata.get("infeasible_step") == "s1"
        assert ctx.audit_metadata.get("infeasible_detail", {}).get("unmet_demand") == 5000

    def test_scenario_h_rei_unavailable_is_never_zero(self):
        """Scenario H: When prerequisite fails, REI output is absent / UNAVAILABLE, NEVER defaulting to 0."""
        reg = CapabilityRegistry()
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()

        # Step 1 fails
        ctx.record_unavailable("resilience.calc", reason="Prerequisite optimization failed", status=EvidenceStatus.UNAVAILABLE)

        res = ctx.agent_result("resilience.calc")
        assert res.status == AgentStatus.INSUFFICIENT_EVIDENCE
        assert res.output is None
        assert "resilience.calc" in ctx.unavailable_evidence
        # Assert neither output_of nor typed_output fabricates 0
        assert ctx.output_of("resilience.calc") is None
        assert ctx.typed_output("resilience.calc", "rei_registry") is None

    def test_scenario_i_missing_hard_dependency_blocks_downstream(self):
        """Scenario I: Upstream HARD dependency fails -> dependent step marked BLOCKED, NOT_RUN, never invoked."""
        calls_step2 = 0

        async def h1(ctx, req):
            raise EngineFailureError("Step 1 failed")

        async def h2(ctx, req):
            nonlocal calls_step2
            calls_step2 += 1
            return {"done": True}

        cap1 = _capability("step1", h1)
        cap2 = _capability("step2", h2)
        reg = _registry_with(cap1, cap2)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg, policy=RecoveryPolicy(max_attempts=1))
        ctx = ExecutionContext()

        plan = ExecutionPlan(
            workflow_id="wf", intent="test",
            steps=[
                PlanStep(step_id="s1", capability="step1"),
                PlanStep(step_id="s2", capability="step2", depends_on=["s1"]),  # HARD dependency
            ]
        )
        ctx.plan = plan

        _run(fm.execute_plan(plan, ctx))
        assert calls_step2 == 0, "Dependent on hard dependency must NOT execute"
        assert "s2" in ctx.blocked_steps
        assert plan.step("s2").status == StepStatus.BLOCKED
        assert ctx.unavailable_evidence["step2"].status == EvidenceStatus.NOT_RUN

    def test_scenario_j_missing_soft_dependency_runs_degraded(self):
        """Scenario J: Upstream SOFT dependency fails -> dependent step still RUNS and receives UnavailableEvidence."""
        received_unavailable = {}

        async def h1(ctx, req):
            raise EngineFailureError("Soft step 1 failed")

        async def h2(ctx, req):
            nonlocal received_unavailable
            received_unavailable = dict(req.unavailable)
            return {"narrative": "Computed with degraded input"}

        cap1 = _capability("step1", h1)
        cap2 = _capability("step2", h2)
        reg = _registry_with(cap1, cap2)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg, policy=RecoveryPolicy(max_attempts=1))
        ctx = ExecutionContext()

        plan = ExecutionPlan(
            workflow_id="wf", intent="test",
            steps=[
                PlanStep(step_id="s1", capability="step1"),
                PlanStep(step_id="s2", capability="step2", depends_on=["s1"], soft_depends_on=["s1"]),
            ]
        )
        ctx.plan = plan

        _run(fm.execute_plan(plan, ctx))
        assert "s2" in ctx.completed_steps
        assert plan.step("s2").status == StepStatus.COMPLETED
        assert "step1" in received_unavailable
        assert received_unavailable["step1"].status == EvidenceStatus.UNAVAILABLE

    def test_scenario_k_valid_reroute_to_alternative(self):
        """Scenario K: Primary fails, registered alternative executes and succeeds. Both recorded in history."""
        calls_primary = 0
        calls_alt = 0

        async def h_primary(ctx, req):
            nonlocal calls_primary
            calls_primary += 1
            raise EngineFailureError("Primary service down")

        async def h_alt(ctx, req):
            nonlocal calls_alt
            calls_alt += 1
            return {"source": "alternative_provider"}

        cap_prim = _capability("solver.cloud", h_primary)
        cap_alt = _capability("solver.local", h_alt)
        reg = _registry_with(cap_prim, cap_alt)
        reg.register_alternative("solver.cloud", "solver.local")

        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=1, enable_rerouting=True)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="solver.cloud")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.SUCCESS
        assert calls_primary == 1
        assert calls_alt == 1
        # Check attempt history
        attempts = ctx.step_attempts["s1"]
        assert len(attempts) == 2
        assert attempts[0].capability == "solver.cloud"
        assert attempts[1].capability == "solver.local"
        assert attempts[1].is_reroute is True
        assert attempts[1].rerouted_from == "solver.cloud"
        assert attempts[1].status == AgentStatus.SUCCESS

    def test_scenario_l_no_valid_reroute_escalates(self):
        """Scenario L: Primary fails, no valid alternative registered -> ESCALATE."""
        async def h(ctx, req):
            raise ValidationFailureError("Primary validation failed with no alternative")

        cap = _capability("unique.solver", h)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=1, enable_rerouting=True)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="unique.solver")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status in (AgentStatus.NON_RETRYABLE_FAILURE, AgentStatus.INVALID_OUTPUT)
        assert len(ctx.escalations) == 1
        assert ctx.escalations[0].capability == "unique.solver"

    def test_scenario_m_repeated_llm_failure_trips_circuit_breaker(self):
        """Scenario M: Repeated LLM gateway failures trip circuit breaker from CLOSED to OPEN."""
        cb = CircuitBreaker(name="test_llm", failure_threshold=3, recovery_timeout_seconds=10.0)
        assert cb.state == CircuitState.CLOSED

        cb.record_failure(failure_class="RETRYABLE")
        assert cb.state == CircuitState.CLOSED
        cb.record_failure(failure_class="RETRYABLE")
        assert cb.state == CircuitState.CLOSED
        cb.record_failure(failure_class="RETRYABLE")
        # 3 consecutive failures -> OPEN
        assert cb.state == CircuitState.OPEN
        assert cb.can_execute() is False

    def test_scenario_n_circuit_open_fast_fails_without_calls(self):
        """Scenario N: When circuit is OPEN, requests fast-fail immediately without hitting the handler."""
        calls = 0

        async def h_llm(ctx, req):
            nonlocal calls
            calls += 1
            return {"text": "should not be reached"}

        contract = _contract("reasoning.llm", llm_backed=True)
        cap = _capability("reasoning.llm", h_llm, contract=contract)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        cb = CircuitBreaker(name="test_llm", failure_threshold=2)
        cb.trip_open("Manual trip for test")

        fm = FailureManager(executor, reg, circuit_breaker=cb)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="reasoning.llm")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert calls == 0, "Circuit OPEN must fast-fail without calling handler"
        assert result.errors[0].code == "CIRCUIT_BREAKER_OPEN"

    def test_scenario_o_circuit_recovery_via_half_open_probe(self):
        """Scenario O: Cooldown elapsed -> HALF_OPEN probe call succeeds -> reset to CLOSED."""
        cb = CircuitBreaker(name="test_llm", failure_threshold=2, recovery_timeout_seconds=0.1)
        cb.trip_open("Tripped for test")
        assert cb.state == CircuitState.OPEN

        time.sleep(0.15)
        # Cooldown elapsed -> state becomes HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN
        assert cb.can_execute() is True  # probe permitted

        # Successful probe call
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.stats().failure_count == 0

    def test_scenario_p_provenance_integrity(self):
        """Scenario P: Provenance captures execution attempts and authoritative status."""
        async def h(ctx, req):
            return {"kpi": 100}

        cap = _capability("kpi.calc", h)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        fm = FailureManager(executor, reg)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="kpi.calc")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.provenance.capability == "kpi.calc"
        assert result.provenance.is_authoritative is True
        assert result.provenance.attempts == 1

    def test_scenario_q_execution_state_history_preservation(self):
        """Scenario Q: Attempt 1 is NEVER erased when attempt 2 runs; history holds all attempts."""
        calls = 0

        async def h(ctx, req):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise EngineTimeoutError(f"Fail {calls}")
            return {"ok": True}

        cap = _capability("multi.retry", h)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=3, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="multi.retry")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.SUCCESS
        history = ctx.step_attempts["s1"]
        assert len(history) == 3
        assert [h.attempt for h in history] == [1, 2, 3]
        assert history[0].status == AgentStatus.RETRYABLE_FAILURE
        assert history[1].status == AgentStatus.RETRYABLE_FAILURE
        assert history[2].status == AgentStatus.SUCCESS


# ===========================================================================
# 3. Realistic Synthetic Integration Tests
# ===========================================================================

class TestRealisticSyntheticIntegrationCases:
    """Integration cases using realistic workflows."""

    @pytest.fixture(scope="class")
    def orchestrator_instance(self):
        network = build_case16_network()
        return build_orchestrator(network=network, enable_llm=False)

    def test_case_1_baseline_workflow_succeeds_end_to_end(self, orchestrator_instance):
        """Case 1: Standard baseline network query workflow succeeds end-to-end."""
        req = OrchestratorRequest(
            input="What does the current baseline network look like?",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
            actor=Actor(role=ActorRole.PLANNER, actor_id="planner_1"),
        )
        resp = orchestrator_instance.run_sync(req)
        assert resp.status == "COMPLETED"
        assert resp.intent == "NETWORK_STATE_QUERY"
        assert resp.execution_id is not None

    def test_case_2_forecast_insufficient_evidence_blocks_optimization(self, orchestrator_instance):
        """Case 2: When forecast fails / has missing data, optimization is blocked, reasoning degrades honestly."""
        ctx = ExecutionContext(baseline_snapshot_id=orchestrator_instance.snapshots.current_id)
        ctx.record_unavailable("demand.forecast", reason="Missing customer demand historical series", status=EvidenceStatus.UNAVAILABLE)

        plan = ExecutionPlan(
            workflow_id="wf_case2",
            intent="optimize_network",
            steps=[
                PlanStep(step_id="s_fc", capability=CAP_FORECAST),
                PlanStep(step_id="s_opt", capability=CAP_OPTIMIZE, depends_on=["s_fc"]),
                PlanStep(step_id="s_rsn", capability=CAP_REASON, depends_on=["s_opt"], soft_depends_on=["s_opt"]),
            ]
        )
        ctx.plan = plan

        fm = FailureManager(orchestrator_instance.executor, orchestrator_instance.registry, policy=RecoveryPolicy(max_attempts=1))
        _run(fm.execute_plan(plan, ctx))

        assert "s_opt" in ctx.blocked_steps
        assert plan.step("s_opt").status == StepStatus.BLOCKED
        assert ctx.unavailable_evidence["demand.forecast"].reason == "Missing customer demand historical series"
        assert ctx.output_of(CAP_OPTIMIZE) is None

    def test_case_3_transient_reasoning_failure_recovers_on_retry(self):
        """Case 3: Transient reasoning failure recovers on retry attempt 2."""
        calls = 0

        async def h_reason(ctx, req):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EngineTimeoutError("LLM Gateway gateway_500 transient")
            return {"summary": "Demand surge observed in Central region", "explanation": "Capacity is constrained"}

        contract = _contract(CAP_REASON, domain=CapabilityDomain.REASONING, llm_backed=True)
        cap = _capability(CAP_REASON, h_reason, contract=contract)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        policy = RecoveryPolicy(max_attempts=3, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s_rsn", capability=CAP_REASON)

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.SUCCESS
        assert calls == 2
        assert len(ctx.step_attempts["s_rsn"]) == 2

    def test_case_4_repeated_external_failure_exhausts_and_escalates(self):
        """Case 4: Repeated external service failure trips circuit breaker and creates EscalationOutcome."""
        calls = 0

        async def h_ext(ctx, req):
            nonlocal calls
            calls += 1
            raise EngineTimeoutError("Gateway 502 Bad Gateway")

        contract = _contract("external.service", llm_backed=True)
        cap = _capability("external.service", h_ext, contract=contract)
        reg = _registry_with(cap)
        executor = CapabilityExecutor(reg)
        cb = CircuitBreaker(name="external_gw", failure_threshold=2)
        policy = RecoveryPolicy(max_attempts=2, backoff_seconds=0.0)
        fm = FailureManager(executor, reg, policy=policy, circuit_breaker=cb)
        ctx = ExecutionContext()
        step = PlanStep(step_id="s1", capability="external.service")

        result = _run(fm.execute_step_with_recovery(step, ctx))
        assert result.status == AgentStatus.RETRYABLE_FAILURE
        assert len(ctx.escalations) == 1
        assert cb.state == CircuitState.OPEN


# ===========================================================================
# 4. Security & Authority Invariant Verification
# ===========================================================================

class TestSecurityAndAuthorityInvariants:
    """Verify core NetGravity invariant rules under failure conditions."""

    def test_event_probability_never_conflated_with_signal_confidence(self):
        """event_probability feeds RF calculation; confidence reflects assessment confidence. They are distinct."""
        sig = ExternalSignal(
            event_type="PORT_STRIKE",
            event_probability=0.75,
            confidence=0.95,
        )
        assert sig.event_probability == 0.75
        assert sig.confidence == 0.95
        assert sig.event_probability != sig.confidence

    def test_failed_rf_never_equals_zero(self):
        """A failed or uncomputed RF calculation must NEVER default to 0.0."""
        ctx = ExecutionContext()
        ctx.record_unavailable(CAP_RISK, reason="Missing event probability", status=EvidenceStatus.UNAVAILABLE)

        res = ctx.agent_result(CAP_RISK)
        assert res.output is None
        assert ctx.output_of(CAP_RISK) is None
        # Verify that require() raises rather than yielding 0
        with pytest.raises(ValueError, match="produced no usable output"):
            res.require()

    def test_non_plannable_capability_cannot_be_executed_as_plan_step(self):
        """Capabilities marked planner_selectable=False cannot be executed as scheduled plan goals."""
        contract = _contract("twin.publish", invocation=InvocationMode.SERVICE, planner_selectable=False)
        assert contract.is_plan_schedulable is False
        assert contract.is_plannable is False
