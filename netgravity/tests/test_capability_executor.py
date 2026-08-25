"""
Phase 8.2 — The capability execution seam.

Three things these tests are built to hold:

  1. A refusal is never a success. Whether the input was absent, the dependency
     unsatisfied, the capability unknown, or the output non-conformant, what
     comes back carries no value to misread.

  2. One execution, one record. The executor writes execution state, and
     `_execute_plan` no longer does. Tests count the writes rather than trusting
     the arrangement.

  3. The executor decides nothing. It runs what it is given. Several tests are
     static checks on the executor's own source, because "contains no planning"
     is a claim about code, not about behaviour on one input.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest

from netgravity.orchestrator.core import executor as executor_module
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.executor import CapabilityExecutor
from netgravity.orchestrator.core.planner import (
    CAP_EXTRACT,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_ROUTE_SIGNAL,
    CAP_TWIN_PUBLISH,
)
from netgravity.orchestrator.exceptions import (
    EngineFailureError,
    EngineTimeoutError,
    MissingDataError,
    SolverInfeasibleError,
)
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.agent_result import AgentResult
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
    InvocationMode,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionMode,
    ExecutionPlan,
    PlanStep,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    OrchestratorRequest,
)
from netgravity.orchestrator.tools.base import NO_RETRY, Capability
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _contract(capability_id: str, **kw) -> CapabilityContract:
    kw.setdefault("domain", CapabilityDomain.KPI)
    kw.setdefault("provider", "TestProvider")
    return CapabilityContract(capability_id=capability_id, **kw)


def _registry_with(*capabilities: Capability) -> CapabilityRegistry:
    registry = CapabilityRegistry()
    for capability in capabilities:
        registry.register(capability)
    return registry


def _capability(name: str, handler, *, contract=None, **kw) -> Capability:
    kw.setdefault("retry_policy", NO_RETRY)
    return Capability(name=name, handler=handler,
                      contract=contract or _contract(name), **kw)


def _run(coro):
    return asyncio.run(coro)


def _executor_code_references(*names: str) -> list:
    """
    Which of `names` appear in the executor's CODE, ignoring docstrings.

    The docstrings deliberately discuss retry, planning and escalation in order
    to say they are not implemented here, so a plain substring search over the
    source would flag the very comments that document the boundary.
    """
    source = pathlib.Path(inspect.getfile(executor_module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    code = ast.dump(tree)
    return [name for name in names if name in code]


# ===========================================================================
# A. Successful execution
# ===========================================================================

class TestSuccessfulExecution:

    def test_a_capability_runs_and_returns_success(self):
        async def handler(ctx, req):
            return {"answer": 42}

        registry = _registry_with(_capability("demo.ok", handler))
        result = _run(CapabilityExecutor(registry).execute("demo.ok", ExecutionContext()))

        assert result.status is AgentStatus.SUCCESS
        assert result.is_usable
        assert result.output == {"answer": 42}
        assert result.capability == "demo.ok"

    def test_the_typed_domain_result_is_preserved_not_flattened(self):
        """
        The envelope must carry the authoritative object itself. A flattened
        projection cannot express per-node calculation status, so rebuilding
        from it would default a FAILED node to OK.
        """
        from netgravity.schemas.results import (
            FacilityResilienceRegistry, SolverStatus,
        )

        registry_obj = FacilityResilienceRegistry(
            network_id="net-1", disruption_type="FULL_CLOSURE",
            disruption_period="P1", baseline_solver_status=SolverStatus.OPTIMAL,
        )

        async def handler(ctx, req):
            ctx.rei_registry = registry_obj
            return {"n_results": 0}

        registry = _registry_with(_capability(
            CAP_REI, handler,
            contract=_contract(CAP_REI, domain=CapabilityDomain.RESILIENCE,
                               output_type="FacilityResilienceRegistry",
                               authoritative_field="rei_registry"),
        ))
        result = _run(CapabilityExecutor(registry).execute(CAP_REI, ExecutionContext()))

        assert result.status is AgentStatus.SUCCESS
        assert result.output is registry_obj
        assert isinstance(result.output, FacilityResilienceRegistry)

    def test_the_provider_from_the_contract_is_attributed(self):
        async def handler(ctx, req):
            return {"ok": True}

        registry = _registry_with(_capability(
            "demo.ok", handler, contract=_contract("demo.ok", provider="MILP"),
        ))
        result = _run(CapabilityExecutor(registry).execute("demo.ok", ExecutionContext()))
        assert result.agent == "MILP"
        assert result.provenance.provider == "MILP"

    def test_params_reach_the_handler_unchanged(self):
        seen = {}

        async def handler(ctx, req):
            seen.update(req.params)
            return {"ok": True}

        registry = _registry_with(_capability("demo.ok", handler))
        _run(CapabilityExecutor(registry).execute(
            "demo.ok", ExecutionContext(), params={"horizon": 6},
        ))
        assert seen == {"horizon": 6}


# ===========================================================================
# B. Unknown capability
# ===========================================================================

class TestUnknownCapability:

    def test_an_unknown_capability_fails_explicitly(self):
        context = ExecutionContext()
        result = _run(CapabilityExecutor(CapabilityRegistry()).execute(
            "does.not.exist", context,
        ))
        assert result.status is AgentStatus.NON_RETRYABLE_FAILURE
        assert result.output is None
        assert result.errors[0].code == "CAPABILITY_NOT_FOUND"

    def test_an_unknown_capability_is_recorded_as_an_error(self):
        """
        Returned rather than raised, so a caller iterating capabilities is not
        derailed — but never silent.
        """
        context = ExecutionContext()
        _run(CapabilityExecutor(CapabilityRegistry()).execute("nope", context))
        assert any(e["code"] == "CAPABILITY_NOT_FOUND" for e in context.errors)
        assert "nope" in context.unavailable_evidence


# ===========================================================================
# C. Missing required input
# ===========================================================================

class TestInputValidation:

    def test_a_missing_declared_input_refuses_before_invoking(self):
        invoked = []

        async def handler(ctx, req):
            invoked.append(True)
            return {"ok": True}

        registry = _registry_with(_capability(
            "demo.needs", handler,
            contract=_contract("demo.needs", required_inputs=("source",)),
        ))
        result = _run(CapabilityExecutor(registry).execute("demo.needs", ExecutionContext()))

        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert result.output is None
        # The point of preflight: the handler never ran.
        assert invoked == []
        assert "source" in result.unavailable

    def test_a_none_valued_input_counts_as_absent(self):
        """
        Passing None through as though it were supplied is exactly how a missing
        input becomes a default deep inside a handler.
        """
        async def handler(ctx, req):  # pragma: no cover - must not run
            raise AssertionError("handler ran with a None required input")

        registry = _registry_with(_capability(
            "demo.needs", handler,
            contract=_contract("demo.needs", required_inputs=("source",)),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            "demo.needs", ExecutionContext(), params={"source": None},
        ))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE

    def test_a_supplied_input_permits_execution(self):
        async def handler(ctx, req):
            return {"read": req.params["source"]}

        registry = _registry_with(_capability(
            "demo.needs", handler,
            contract=_contract("demo.needs", required_inputs=("source",)),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            "demo.needs", ExecutionContext(), params={"source": "file.csv"},
        ))
        assert result.status is AgentStatus.SUCCESS
        assert result.output == {"read": "file.csv"}

    def test_a_refusal_is_recorded_as_missing_evidence(self):
        async def handler(ctx, req):  # pragma: no cover
            raise AssertionError

        registry = _registry_with(_capability(
            "demo.needs", handler,
            contract=_contract("demo.needs", required_inputs=("source",)),
        ))
        context = ExecutionContext()
        _run(CapabilityExecutor(registry).execute("demo.needs", context))
        assert "demo.needs" in context.unavailable_evidence
        assert context.capability_outcome("demo.needs") is AgentStatus.INSUFFICIENT_EVIDENCE


# ===========================================================================
# D & E. Dependency validation
# ===========================================================================

class TestDependencyValidation:

    @staticmethod
    def _dependent(handler, *, optional=()):
        return _registry_with(_capability(
            "demo.dependent", handler,
            contract=_contract("demo.dependent",
                               dependencies=("demo.upstream",),
                               optional_dependencies=optional),
        ))

    def test_a_missing_hard_dependency_refuses_execution(self):
        async def handler(ctx, req):  # pragma: no cover
            raise AssertionError("ran without its required dependency")

        result = _run(CapabilityExecutor(self._dependent(handler)).execute(
            "demo.dependent", ExecutionContext(),
        ))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert "demo.upstream" in result.unavailable
        assert "has not run" in result.unavailable["demo.upstream"].reason

    def test_a_satisfied_hard_dependency_permits_execution(self):
        from netgravity.orchestrator.schemas.plans import ToolResult

        async def handler(ctx, req):
            return {"ok": True}

        context = ExecutionContext()
        context.record_step("up", ToolResult(
            capability="demo.upstream", success=True, output={"v": 1}))

        result = _run(CapabilityExecutor(self._dependent(handler)).execute(
            "demo.dependent", context,
        ))
        assert result.status is AgentStatus.SUCCESS

    def test_a_failed_hard_dependency_refuses_execution(self):
        from netgravity.orchestrator.schemas.plans import ToolResult

        async def handler(ctx, req):  # pragma: no cover
            raise AssertionError("ran on a failed dependency")

        context = ExecutionContext()
        context.record_step("up", ToolResult(
            capability="demo.upstream", success=False, output={},
            error_code="ENGINE_FAILURE", failure_class="NON_RETRYABLE"))

        result = _run(CapabilityExecutor(self._dependent(handler)).execute(
            "demo.dependent", context,
        ))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE

    def test_a_declared_optional_dependency_does_not_block(self):
        """
        The provider handles the absence itself, and its own report — RF's
        NOT_COMPUTABLE, for instance — is more informative than a refusal.
        """
        ran = []

        async def handler(ctx, req):
            ran.append(True)
            return {"computed": False}

        result = _run(CapabilityExecutor(
            self._dependent(handler, optional=("demo.upstream",))
        ).execute("demo.dependent", ExecutionContext()))

        assert ran == [True]
        assert result.is_usable

    def test_a_plan_may_soften_an_edge_the_contract_calls_required(self):
        """
        The plan describes one concrete workflow and is the more specific
        statement. This check must not contradict a decision the plan made.
        """
        async def handler(ctx, req):
            return {"ok": True}

        context = ExecutionContext()
        context.plan = ExecutionPlan(
            workflow_id="wf", intent="X",
            steps=[
                PlanStep(step_id="up", capability="demo.upstream"),
                PlanStep(step_id="dep", capability="demo.dependent",
                         depends_on=["up"], soft_depends_on=["up"]),
            ],
        )
        result = _run(CapabilityExecutor(self._dependent(handler)).execute(
            "demo.dependent", context, step_id="dep",
        ))
        assert result.status is AgentStatus.SUCCESS

    def test_rf_is_not_refused_when_only_one_input_is_present(self):
        """
        The single most important criticality declaration in the catalogue.
        Refusing RF here would replace an explicit NOT_COMPUTABLE row — which
        names what was missing — with a capability that simply did not run.
        """
        orch = build_orchestrator(enable_llm=False)
        contract = orch.get_capability(CAP_RISK)
        assert contract.required_dependencies == ()
        assert set(contract.optional_dependencies) == set(contract.dependencies)


# ===========================================================================
# F. Invalid output
# ===========================================================================

class TestOutputValidation:

    def test_a_wrong_output_type_is_invalid_output(self):
        class NotWhatWasPromised:
            pass

        async def handler(ctx, req):
            ctx.rei_registry = NotWhatWasPromised()
            return {"n": 1}

        registry = _registry_with(_capability(
            CAP_REI, handler,
            contract=_contract(CAP_REI, output_type="FacilityResilienceRegistry",
                               authoritative_field="rei_registry"),
        ))
        context = ExecutionContext()
        result = _run(CapabilityExecutor(registry).execute(CAP_REI, context))

        assert result.status is AgentStatus.INVALID_OUTPUT
        # Rejected output must not be readable as a result.
        assert result.output is None
        assert "FacilityResilienceRegistry" in result.errors[0].message

    def test_invalid_output_is_recorded_as_unusable_evidence(self):
        """
        The handler thought it succeeded. A consumer must not still find its
        projection sitting in `engine_results`.
        """
        async def handler(ctx, req):
            ctx.rei_registry = object()
            return {"rei": 0.42}

        registry = _registry_with(_capability(
            CAP_REI, handler,
            contract=_contract(CAP_REI, output_type="FacilityResilienceRegistry",
                               authoritative_field="rei_registry"),
        ))
        context = ExecutionContext()
        _run(CapabilityExecutor(registry).execute(CAP_REI, context))

        assert context.output_of(CAP_REI) is None
        assert context.unavailable_evidence[CAP_REI].status is EvidenceStatus.INVALID
        assert context.capability_outcome(CAP_REI) is AgentStatus.INVALID_OUTPUT

    def test_success_with_no_output_at_all_is_invalid_output(self):
        async def handler(ctx, req):
            return {}

        registry = _registry_with(_capability(
            "demo.empty", handler,
            contract=_contract("demo.empty", output_type="NetworkKPIs",
                               authoritative_field="forecast_result"),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            "demo.empty", ExecutionContext()))
        assert result.status is AgentStatus.INVALID_OUTPUT

    def test_an_identifier_field_is_not_type_checked(self):
        """
        A pinned snapshot id is a `str` by design. Comparing it to
        "NetworkSnapshot" would reject a correct result, so the contract names
        the distinction rather than the validator guessing it.
        """
        async def handler(ctx, req):
            ctx.baseline_snapshot_id = "snap-1"
            return {"snapshot_id": "snap-1"}

        registry = _registry_with(_capability(
            CAP_LOAD_NETWORK, handler,
            contract=_contract(CAP_LOAD_NETWORK, output_type="NetworkSnapshot",
                               authoritative_field="baseline_snapshot_id",
                               authoritative_is_reference=True),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            CAP_LOAD_NETWORK, ExecutionContext()))
        assert result.status is AgentStatus.SUCCESS

    def test_a_list_of_the_declared_type_is_conformant(self):
        from netgravity.orchestrator.schemas.twin import (
            TwinCalculationStatus, TwinStateRef, TwinStateType,
        )

        ref = TwinStateRef(state_id="s1", snapshot_id="snap-1",
                           state_type=TwinStateType.OPTIMIZED,
                           calculation_status=TwinCalculationStatus.COMPLETE)

        async def handler(ctx, req):
            ctx.twin_refs = [ref]
            return {"n_states": 1}

        registry = _registry_with(_capability(
            CAP_TWIN_PUBLISH, handler,
            contract=_contract(CAP_TWIN_PUBLISH, output_type="TwinStateRef",
                               authoritative_field="twin_refs"),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            CAP_TWIN_PUBLISH, ExecutionContext()))
        assert result.status is AgentStatus.SUCCESS
        assert result.output == [ref]

    def test_advisory_validator_warnings_are_not_escalated(self):
        """
        `ResultValidator` produces WARNINGS by design — a KPI outside an
        expected band is worth flagging and is not grounds for discarding a
        solved network. Escalating them here would suppress correct results.
        """
        async def handler(ctx, req):
            ctx.add_warning("optimization: utilisation above expected band")
            return {"total_cost": 100.0}

        registry = _registry_with(_capability(CAP_OPTIMIZE, handler))
        context = ExecutionContext()
        result = _run(CapabilityExecutor(registry).execute(CAP_OPTIMIZE, context))
        assert result.status is AgentStatus.SUCCESS
        assert context.warnings


# ===========================================================================
# G. Domain failures
# ===========================================================================

class TestDomainFailureNormalisation:

    @staticmethod
    def _raising(exc):
        async def handler(ctx, req):
            raise exc
        return handler

    def test_infeasibility_is_non_retryable_and_never_a_solved_network(self):
        """
        The solver PROVED no solution exists. That is a finding, and it must not
        arrive looking like a network that solved.
        """
        registry = _registry_with(_capability(
            CAP_OPTIMIZE, self._raising(SolverInfeasibleError("no feasible plan")),
            contract=_contract(CAP_OPTIMIZE, domain=CapabilityDomain.OPTIMIZATION,
                               output_type="NetworkStateResult",
                               authoritative_field="network_states"),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            CAP_OPTIMIZE, ExecutionContext()))

        assert result.status is AgentStatus.NON_RETRYABLE_FAILURE
        assert not result.is_retryable
        assert result.output is None
        assert result.errors[0].code == "SOLVER_INFEASIBLE"

    def test_a_timeout_is_retryable_but_nothing_retries_it(self):
        registry = _registry_with(_capability(
            "demo.slow", self._raising(EngineTimeoutError("too slow")),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            "demo.slow", ExecutionContext()))
        assert result.status is AgentStatus.RETRYABLE_FAILURE
        assert result.is_retryable
        # Reported, and acted on nowhere: one attempt, no loop.
        assert result.provenance.attempts == 1

    def test_absent_data_is_insufficient_evidence_not_an_engine_fault(self):
        registry = _registry_with(_capability(
            CAP_FORECAST, self._raising(MissingDataError("no demand history")),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            CAP_FORECAST, ExecutionContext()))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE

    def test_an_unexpected_exception_is_classified_not_swallowed(self):
        """
        An unclassified exception becomes `EngineFailureError`, which the
        codebase classifies RETRYABLE — a pre-existing judgement, unchanged
        here: an unrecognised fault is more often transient than proven
        permanent, and only proven-permanent outcomes should be ruled out.

        What matters for this phase is that it is classified at all, carries the
        original message, and offers no value to read.
        """
        registry = _registry_with(_capability(
            "demo.boom", self._raising(RuntimeError("kaboom")),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            "demo.boom", ExecutionContext()))
        assert result.status is AgentStatus.RETRYABLE_FAILURE
        assert result.is_failure and result.output is None
        assert "kaboom" in result.errors[0].message
        assert result.errors[0].code == "ENGINE_FAILURE"

    def test_rei_is_insufficient_evidence_rather_than_zero(self):
        """
        REI unavailable because the network could not be solved must never
        arrive as an exposure of 0 — the most dangerous possible substitution,
        since zero exposure reads as a perfectly safe facility.
        """
        registry = _registry_with(_capability(
            CAP_REI, self._raising(MissingDataError("baseline solve unavailable")),
            contract=_contract(CAP_REI, domain=CapabilityDomain.RESILIENCE,
                               output_type="FacilityResilienceRegistry",
                               authoritative_field="rei_registry"),
        ))
        context = ExecutionContext()
        result = _run(CapabilityExecutor(registry).execute(CAP_REI, context))

        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert result.output is None
        with pytest.raises(ValueError, match="no usable output"):
            result.require()

    def test_a_failure_is_never_usable(self):
        for exc in (SolverInfeasibleError("x"), EngineTimeoutError("x"),
                    MissingDataError("x"), EngineFailureError("x")):
            registry = _registry_with(_capability("demo.f", self._raising(exc)))
            result = _run(CapabilityExecutor(registry).execute(
                "demo.f", ExecutionContext()))
            assert not result.is_usable
            assert result.output is None


# ===========================================================================
# H. State recording
# ===========================================================================

class TestStateRecording:

    def test_an_execution_is_recorded_exactly_once(self):
        async def handler(ctx, req):
            return {"ok": True}

        registry = _registry_with(_capability("demo.ok", handler))
        context = ExecutionContext()
        writes = []
        original = context.record_step

        def counting(step_id, result):
            writes.append(step_id)
            original(step_id, result)

        context.record_step = counting  # type: ignore[method-assign]
        _run(CapabilityExecutor(registry).execute("demo.ok", context, step_id="s1"))
        assert writes == ["s1"]

    def test_a_direct_invocation_is_recorded_under_the_capability_id(self):
        async def handler(ctx, req):
            return {"ok": True}

        registry = _registry_with(_capability("demo.ok", handler))
        context = ExecutionContext()
        _run(CapabilityExecutor(registry).execute("demo.ok", context))
        assert "demo.ok" in context.step_results
        assert context.capability_outcome("demo.ok") is AgentStatus.SUCCESS

    def test_the_recorded_status_matches_what_the_caller_received(self):
        """
        A result the executor rejected must be recorded as rejected, not as the
        success the handler believed it was.
        """
        async def handler(ctx, req):
            ctx.rei_registry = object()
            return {"rei": 1.0}

        registry = _registry_with(_capability(
            CAP_REI, handler,
            contract=_contract(CAP_REI, output_type="FacilityResilienceRegistry",
                               authoritative_field="rei_registry"),
        ))
        context = ExecutionContext()
        result = _run(CapabilityExecutor(registry).execute(CAP_REI, context))
        assert context.step_results[CAP_REI].status is result.status
        assert context.capability_outcome(CAP_REI) is result.status

    def test_record_false_writes_nothing(self):
        async def handler(ctx, req):
            return {"ok": True}

        registry = _registry_with(_capability("demo.ok", handler))
        context = ExecutionContext()
        result = _run(CapabilityExecutor(registry).execute(
            "demo.ok", context, record=False))
        assert result.status is AgentStatus.SUCCESS
        assert context.step_results == {}
        assert context.capability_status == {}

    def test_no_second_state_store_was_created(self):
        """
        §7: the executor holds no state. Everything it records goes into the
        `ExecutionContext` it was handed.
        """
        executor = CapabilityExecutor(CapabilityRegistry())
        assert set(vars(executor)) == {"registry"}


# ===========================================================================
# I. Provenance
# ===========================================================================

class TestProvenance:

    def test_provenance_survives_execution(self):
        async def handler(ctx, req):
            return {"ok": True}

        registry = _registry_with(_capability(
            "demo.ok", handler, contract=_contract("demo.ok", provider="KPIClient"),
        ))
        context = ExecutionContext(baseline_snapshot_id="snap-7", scenario_id="scn-3")
        result = _run(CapabilityExecutor(registry).execute("demo.ok", context))

        assert result.capability == "demo.ok"
        assert result.execution_id == context.execution_id
        assert result.provenance.execution_id == context.execution_id
        assert result.provenance.snapshot_id == "snap-7"
        assert result.provenance.scenario_id == "scn-3"
        assert result.provenance.provider == "KPIClient"
        assert result.provenance.duration_seconds >= 0.0

    def test_provenance_survives_a_failure_too(self):
        """A failed execution is exactly when provenance matters most."""
        async def handler(ctx, req):
            raise SolverInfeasibleError("infeasible")

        registry = _registry_with(_capability(CAP_OPTIMIZE, handler))
        context = ExecutionContext(baseline_snapshot_id="snap-7")
        result = _run(CapabilityExecutor(registry).execute(CAP_OPTIMIZE, context))
        assert result.provenance.snapshot_id == "snap-7"
        assert result.provenance.execution_id == context.execution_id

    def test_a_probabilistic_capability_is_not_marked_authoritative(self):
        async def handler(ctx, req):
            return {"narrative": "..."}

        registry = _registry_with(_capability(
            CAP_REASON, handler, execution_mode=ExecutionMode.PROBABILISTIC,
            contract=_contract(CAP_REASON, domain=CapabilityDomain.REASONING,
                               execution_mode=ExecutionMode.PROBABILISTIC,
                               llm_backed=True),
        ))
        result = _run(CapabilityExecutor(registry).execute(
            CAP_REASON, ExecutionContext()))
        assert result.provenance.is_authoritative is False


# ===========================================================================
# J, K, L. Boundaries
# ===========================================================================

class TestExecutorBoundaries:

    def test_the_executor_contains_no_planning_logic(self):
        """
        §10 K. A claim about the code, so checked against the code. The executor
        must not consult workflow templates, build plans, or choose a capability.
        """
        offenders = _executor_code_references(
            "WORKFLOW_TEMPLATES", "WorkflowPlanner", "available_workflows",
            "execution_layers", "resolve_capability",
        )
        assert not offenders, (
            f"the executor references {offenders}; planning belongs outside it"
        )

    def test_the_executor_implements_no_retry_reroute_or_escalation(self):
        source = pathlib.Path(inspect.getfile(executor_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # No loop anywhere in the executor may drive execution.
        for node in ast.walk(tree):
            if isinstance(node, (ast.While, ast.AsyncFor)):
                raise AssertionError(
                    f"the executor contains a {type(node).__name__}; this phase "
                    f"adds no retry or polling"
                )
        # Docstrings stripped: prose explaining that retry is NOT implemented
        # here names the very things being searched for.
        assert not _executor_code_references(
            "should_retry", "delay_for", "fallback", "escalate", "circuit",
        )

    def test_the_executor_imports_no_engine(self):
        """
        A seam that imports a solver is one refactor away from calling it
        directly instead of through the registered capability.
        """
        source = pathlib.Path(inspect.getfile(executor_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.forecasting", "netgravity.ingestion",
                     "netgravity.orchestrator.agents", "netgravity.orchestrator.twin",
                     "netgravity.orchestrator.risk", "pulp", "openai")
        assert not [m for m in imported if m.startswith(forbidden)]

    def test_the_executor_invokes_capabilities_only_through_the_registered_tool(self):
        """
        §9 in structural form: the executor may not reach a specialist except by
        the capability the registry holds. One call site, and it is the tool.
        """
        source = pathlib.Path(inspect.getfile(executor_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        awaited = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                func = node.value.func
                awaited.append(func.attr if isinstance(func, ast.Attribute) else "?")
        assert awaited == ["execute"], (
            f"expected exactly one awaited call (tool.execute); found {awaited}"
        )

    def test_the_executor_cannot_substitute_a_different_provider(self):
        """
        Given a capability id it runs THAT capability. No fallback selection.
        """
        async def handler(ctx, req):
            return {"who": req.capability}

        registry = _registry_with(
            _capability("demo.a", handler),
            _capability("demo.b", handler),
        )
        result = _run(CapabilityExecutor(registry).execute(
            "demo.a", ExecutionContext()))
        assert result.output == {"who": "demo.a"}

    def test_a_fabricated_agent_output_cannot_overwrite_an_authoritative_value(self):
        """
        §10 L. A PROBABILISTIC capability writing over a deterministic engine's
        recorded result must not be able to make its number authoritative. The
        deterministic record stands, and the advisory result is marked advisory.
        """
        from netgravity.orchestrator.schemas.plans import ToolResult

        context = ExecutionContext()
        context.record_step("opt", ToolResult(
            capability=CAP_OPTIMIZE, success=True,
            output={"total_cost": 1_000_000.0},
            execution_mode=ExecutionMode.DETERMINISTIC))

        async def lying_reasoner(ctx, req):
            return {"total_cost": 1.0, "narrative": "costs are trivial"}

        registry = _registry_with(_capability(
            CAP_REASON, lying_reasoner, execution_mode=ExecutionMode.PROBABILISTIC,
            contract=_contract(CAP_REASON, domain=CapabilityDomain.REASONING,
                               execution_mode=ExecutionMode.PROBABILISTIC,
                               llm_backed=True),
        ))
        result = _run(CapabilityExecutor(registry).execute(CAP_REASON, context))

        # The advisory result exists but is not authoritative...
        assert result.is_usable
        assert result.provenance.is_authoritative is False
        # ...and the deterministic engine's own figure is untouched.
        assert context.output_of(CAP_OPTIMIZE)["total_cost"] == 1_000_000.0
        assert context.capability_outcome(CAP_OPTIMIZE) is AgentStatus.SUCCESS

    def test_no_specialist_agent_gained_a_path_to_the_executor(self):
        """
        Adapters were added around agents, not inside them. An agent importing
        the executor could start executing other capabilities.
        """
        for path in sorted((ROOT / "orchestrator" / "agents").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            assert not [m for m in modules if "core.executor" in m or "core.orchestrator" in m], (
                f"{path.name} reaches the control plane"
            )


# ===========================================================================
# §11. Real capability integration — explicit order, no planning
# ===========================================================================

class TestRealCapabilityChain:
    """
    A deterministic chain driven BY HAND through the executor.

    This is not the agentic workflow and contains no planning: each capability
    is named explicitly, in dependency order, by the test. What it proves is
    that the seam carries real results between real specialists — every
    capability below is the production one, on the Phase 8.0 synthetic network.
    """

    @pytest.fixture(scope="class")
    def world(self):
        network = build_case16_network()
        orch = build_orchestrator(network=network, enable_llm=False)
        return orch

    @pytest.fixture()
    def context(self, world):
        return ExecutionContext(
            request_id="phase-8-2-chain",
            actor=Actor(actor_id="tester", role=ActorRole.PLANNER),
            baseline_snapshot_id=world.snapshots.current_id,
        )

    def test_the_full_deterministic_chain_carries_results_through_the_seam(
        self, world, context,
    ):
        executor = world.executor
        statuses = {}

        async def chain():
            for capability, params in (
                (CAP_LOAD_NETWORK, {}),
                (CAP_ROUTE_SIGNAL, {}),
                (CAP_FORECAST, {"horizon": 3, "run_backtest": False}),
                (CAP_REI, {}),
                (CAP_OPTIMIZE, {}),
                (CAP_KPI, {}),
                (CAP_RISK, {}),
                (CAP_REASON, {}),
                (CAP_GOVERN, {}),
                (CAP_TWIN_PUBLISH, {}),
            ):
                result = await executor.execute(capability, context, params=params)
                statuses[capability] = result.status
            return statuses

        _run(chain())

        # Every capability produced a definite, explainable outcome — none
        # silently absent, none an empty success.
        assert set(statuses) == {
            CAP_LOAD_NETWORK, CAP_ROUTE_SIGNAL, CAP_FORECAST, CAP_REI,
            CAP_OPTIMIZE, CAP_KPI, CAP_RISK, CAP_REASON, CAP_GOVERN,
            CAP_TWIN_PUBLISH,
        }
        for capability, status in statuses.items():
            assert isinstance(status, AgentStatus), capability

        # The deterministic backbone must actually have run.
        for capability in (CAP_LOAD_NETWORK, CAP_OPTIMIZE, CAP_KPI, CAP_REI):
            assert statuses[capability] in (AgentStatus.SUCCESS, AgentStatus.PARTIAL), (
                f"{capability} -> {statuses[capability].value}"
            )

    def test_the_chain_preserves_the_typed_domain_results(self, world, context):
        from netgravity.forecasting.schemas import ForecastResult
        from netgravity.schemas.contracts import NetworkStateResult
        from netgravity.schemas.results import FacilityResilienceRegistry

        executor = world.executor

        async def chain():
            out = {}
            for capability, params in (
                (CAP_LOAD_NETWORK, {}),
                (CAP_FORECAST, {"horizon": 2, "run_backtest": False}),
                (CAP_REI, {}),
                (CAP_OPTIMIZE, {}),
            ):
                out[capability] = await executor.execute(
                    capability, context, params=params)
            return out

        results = _run(chain())

        expected = {
            CAP_FORECAST: ForecastResult,
            CAP_REI: FacilityResilienceRegistry,
            CAP_OPTIMIZE: NetworkStateResult,
        }
        for capability, kind in expected.items():
            result = results[capability]
            if not result.is_usable:
                continue  # reported honestly elsewhere; nothing to type-check
            assert isinstance(result.output, kind), (
                f"{capability} produced {type(result.output).__name__}, "
                f"expected {kind.__name__}"
            )
            # The declared type and the delivered type agree.
            assert type(result.output).__name__ == \
                world.get_capability(capability).output_type

    def test_a_capability_run_out_of_order_is_refused_not_guessed(self, world):
        """
        The seam's whole point under misuse: asking for KPIs before anything has
        solved yields an explicit refusal, not a zero-cost network.
        """
        context = ExecutionContext(baseline_snapshot_id=world.snapshots.current_id)
        result = _run(world.executor.execute(CAP_KPI, context))

        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert result.output is None
        assert CAP_OPTIMIZE in result.unavailable

    def test_extraction_executes_through_the_same_seam(self, world):
        from netgravity.orchestrator.schemas.extraction import ExtractionResult

        context = ExecutionContext()
        result = _run(world.executor.execute(
            CAP_EXTRACT, context,
            params={"source": "Flooding expected near DC_DELHI with 30% probability.",
                    "source_type": "EXTERNAL_SIGNAL_TEXT"},
        ))
        assert result.is_usable
        assert isinstance(result.output, ExtractionResult)
        assert result.output is context.extraction_result

    def test_extraction_without_a_source_is_refused(self, world):
        result = _run(world.executor.execute(CAP_EXTRACT, ExecutionContext()))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert "source" in result.unavailable

    def test_governance_runs_outside_a_plan_and_stays_authoritative(self, world):
        """
        Regression, Phase 8.2. `governance.classify` passed
        `audit.get(execution_id)` straight into `_govern`, which records
        unconditionally. A context built directly has no registered trace, so
        governance crashed with an ENGINE_FAILURE — a governed decision failing
        for want of a log entry.

        Governance must ALWAYS produce a verdict; missing evidence makes it more
        conservative, never absent. A missing audit trace least of all.
        """
        from netgravity.orchestrator.schemas.actions import GovernanceDecision

        context = ExecutionContext(
            actor=Actor(actor_id="t", role=ActorRole.PLANNER),
            baseline_snapshot_id=world.snapshots.current_id,
        )
        result = _run(world.executor.execute(CAP_GOVERN, context))

        assert result.status is AgentStatus.SUCCESS
        assert isinstance(result.output, GovernanceDecision)
        assert result.provenance.is_authoritative
        # The execution was audited rather than skipped.
        assert world.audit.get(context.execution_id) is not None

    def test_the_twin_reports_failure_rather_than_publishing_nothing(self, world):
        """
        Regression, Phase 8.2. `_project_twin` never raises — inside a workflow
        a failure to draw the picture must not fail the analysis. But when
        `twin.publish` IS the request, an empty `twin_refs` list satisfied the
        envelope's "output is not None" invariant and came back SUCCESS.

        That is a failure masquerading as a successful output, which is the one
        thing this seam exists to prevent.
        """
        # No snapshot pinned, so there is no network for a state to describe.
        context = ExecutionContext()
        result = _run(world.executor.execute(CAP_TWIN_PUBLISH, context))

        assert result.is_failure
        assert result.status is not AgentStatus.SUCCESS
        assert result.output is None
        assert context.twin_refs == []

    def test_the_in_run_twin_projection_still_degrades_quietly(self):
        """
        The other half of the fix. A workflow whose twin projection fails must
        still complete — the analysis is not invalidated by a missing picture.
        Only the DIRECT invocation reports it as a failure.
        """
        orch = build_orchestrator(network=build_case16_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="what does the network look like now?",
            actor=Actor(actor_id="u1", role=ActorRole.PLANNER),
        ))
        context = orch.get_execution_state(response.execution_id)
        assert context.current_state.value == "COMPLETED"
        # The in-run projection is called directly, not through the seam, so it
        # is not among the recorded capability executions.
        assert CAP_TWIN_PUBLISH not in context.capability_status

    def test_the_chain_records_provenance_for_every_capability(self, world, context):
        executor = world.executor

        async def chain():
            for capability in (CAP_LOAD_NETWORK, CAP_OPTIMIZE, CAP_KPI):
                await executor.execute(capability, context)

        _run(chain())
        provenance = context.capability_provenance()
        for capability in (CAP_LOAD_NETWORK, CAP_OPTIMIZE, CAP_KPI):
            assert capability in provenance
            assert provenance[capability]["execution_id"] == context.execution_id
            assert provenance[capability]["snapshot_id"] == context.baseline_snapshot_id


# ===========================================================================
# The plan path now runs through the seam
# ===========================================================================

class TestPlanPathUsesTheExecutor:

    @pytest.fixture(scope="class")
    def run(self):
        network = build_case16_network()
        orch = build_orchestrator(network=network, enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="what does the network look like now?",
            actor=Actor(actor_id="u1", role=ActorRole.PLANNER),
        ))
        return orch, orch.get_execution_state(response.execution_id), response

    def test_the_workflow_still_completes(self, run):
        _, context, response = run
        assert context.current_state.value == "COMPLETED"
        assert context.failed_steps == []
        assert response.execution_id

    def test_every_step_was_recorded_once(self, run):
        _, context, _ = run
        assert len(context.step_results) == len(context.completed_steps)
        assert sorted(context.step_results) == sorted(context.completed_steps)

    def test_the_orchestrator_holds_exactly_one_executor(self, run):
        orch, _, _ = run
        assert isinstance(orch.executor, CapabilityExecutor)
        assert orch.executor.registry is orch.registry

    def test_run_step_returns_the_standard_envelope(self, run):
        orch, context, _ = run

        async def rerun():
            return await orch._run_step(context, "kpi")  # noqa: SLF001

        # The plan is still on the context, so the step can be re-executed.
        result = _run(rerun())
        assert isinstance(result, AgentResult)
        assert result.capability == CAP_KPI
