"""
Phase 8.1 — Agent contract, capability registry and execution state.

These tests exist to hold three things still:

  1. A result that cannot be trusted never presents a value. Most of the
     `AgentResult` tests are really one test written six ways: no path through
     the contract turns a missing measurement into a number.

  2. The catalogue describes the code, not an intention. Every declared provider,
     output type and authoritative field is checked against the live registry and
     the real `ExecutionContext`, so a rename breaks a test rather than leaving
     the metadata quietly lying to a future planner.

  3. Nothing here executes anything. The registry is metadata; the tests assert
     that structurally, not by convention.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from dataclasses import fields as dataclass_fields

import pytest

from netgravity.orchestrator.core import planner as planner_module
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.planner import (
    CAP_EXTRACT,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_INTERPRET_SIG,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_ROUTE_SIGNAL,
    CAP_TWIN_PUBLISH,
    WORKFLOW_TEMPLATES,
)
from netgravity.orchestrator.exceptions import CapabilityNotFoundError
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_contracts import CAPABILITY_CONTRACTS
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.agent_result import (
    AgentError,
    AgentResult,
    NO_OUTPUT_STATUSES,
    USABLE_STATUSES,
)
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
    InvocationMode,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionMode,
    ToolResult,
    UnavailableEvidence,
)
from netgravity.orchestrator.schemas.requests import Actor, ActorRole, OrchestratorRequest
from netgravity.orchestrator.tools.base import Capability, NO_RETRY
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(capability: str = "test.cap", **kw) -> ToolResult:
    return ToolResult(capability=capability, success=True,
                      output={"value": 1}, **kw)


def _bad(capability: str = "test.cap", **kw) -> ToolResult:
    kw.setdefault("error_code", "ENGINE_FAILURE")
    kw.setdefault("error_message", "boom")
    kw.setdefault("failure_class", "NON_RETRYABLE")
    return ToolResult(capability=capability, success=False, output={}, **kw)


async def _noop_handler(context, request):  # pragma: no cover - never invoked
    raise AssertionError(
        "a registry lookup executed a handler; the registry must be metadata only"
    )


def _module_imports(path: pathlib.Path) -> set:
    """Every module name imported by one file, by AST rather than by executing it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# ===========================================================================
# A. AgentResult
# ===========================================================================

class TestAgentResultStatuses:
    """One construction per status, plus what each one promises."""

    def test_success_carries_the_authoritative_output(self):
        domain_object = {"cost": 12345.6}
        result = AgentResult.from_tool_result(
            _ok("optimization.solve"), output=domain_object, agent="MILP",
        )
        assert result.status is AgentStatus.SUCCESS
        assert result.is_success and result.is_usable and not result.is_failure
        # The object handed in, not a copy of the flattened transport dict.
        assert result.output is domain_object
        assert result.require() is domain_object
        assert result.errors == []

    def test_partial_is_usable_but_says_what_is_missing(self):
        result = AgentResult.from_tool_result(
            _ok("reasoning.synthesise"),
            output={"narrative": "..."},
            degraded=["resilience.assess"],
        )
        assert result.status is AgentStatus.PARTIAL
        # The whole point of PARTIAL: usable, and honest about being incomplete.
        assert result.is_usable
        assert not result.is_success
        assert any("resilience.assess" in w for w in result.warnings)

    def test_retryable_failure_is_distinguished_from_permanent_one(self):
        retryable = AgentResult.from_tool_result(
            _bad(error_code="ENGINE_TIMEOUT", failure_class="RETRYABLE"),
        )
        permanent = AgentResult.from_tool_result(
            _bad(error_code="SOLVER_INFEASIBLE", failure_class="NON_RETRYABLE"),
        )
        assert retryable.status is AgentStatus.RETRYABLE_FAILURE
        assert retryable.is_retryable
        assert permanent.status is AgentStatus.NON_RETRYABLE_FAILURE
        assert not permanent.is_retryable
        # Both are failures, and neither offers a value to read.
        assert retryable.is_failure and permanent.is_failure
        assert retryable.output is None and permanent.output is None

    def test_infeasibility_is_non_retryable_rather_than_an_error_to_repeat(self):
        """
        The solver PROVED there is no solution. Re-running spends solver time to
        obtain the same answer, so it must not be classified retryable.
        """
        result = AgentResult.from_tool_result(
            _bad("optimization.solve", error_code="SOLVER_INFEASIBLE",
                 failure_class="NON_RETRYABLE",
                 error_message="no feasible assignment exists"),
        )
        assert result.status is AgentStatus.NON_RETRYABLE_FAILURE
        assert not result.is_retryable

    def test_invalid_output_keeps_the_rejected_payload_out_of_output(self):
        """
        Validation refused it, so it must not be readable as a result — but it is
        still needed for diagnosis, which is what `metadata` is for.
        """
        rejected = ToolResult(
            capability="optimization.solve", success=False,
            output={"total_cost": -5.0},
            error_code="VALIDATION_FAILURE",
            error_message="total_cost is negative",
            failure_class="NON_RETRYABLE",
        )
        result = AgentResult.from_tool_result(rejected, output={"total_cost": -5.0})
        assert result.status is AgentStatus.INVALID_OUTPUT
        assert result.output is None
        assert result.metadata["rejected_output"] == {"total_cost": -5.0}
        assert result.errors[0].code == "VALIDATION_FAILURE"

    def test_a_successful_run_whose_output_fails_validation_is_invalid(self):
        """A rejected result is worse than a missing one: it looks usable."""
        result = AgentResult.from_tool_result(
            _ok("resilience.assess"),
            output={"rei": 2.5},
            validation_errors=["REI outside [0, 1]"],
        )
        assert result.status is AgentStatus.INVALID_OUTPUT
        assert result.output is None
        assert "REI outside [0, 1]" in result.errors[-1].message

    def test_insufficient_evidence_is_not_a_failure_and_not_a_zero(self):
        result = AgentResult.insufficient_evidence(
            "resilience.assess",
            reason="no REI sweep was requested for this workflow",
        )
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        # It IS unusable...
        assert result.is_failure and not result.is_usable
        # ...but it is not a malfunction, and it names what is absent.
        assert result.errors == []
        assert result.unavailable["resilience.assess"].status is EvidenceStatus.NOT_RUN
        assert result.output is None

    def test_missing_data_reads_as_insufficient_evidence_not_engine_failure(self):
        result = AgentResult.from_tool_result(
            _bad("forecast.demand", error_code="MISSING_DATA",
                 error_message="no demand history available"),
        )
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        # Pointing an operator at the forecaster would be the wrong diagnosis.
        assert not result.is_retryable


class TestAgentResultInvariants:
    """The contract refuses states that would let a caller be misled."""

    @pytest.mark.parametrize("status", sorted(NO_OUTPUT_STATUSES, key=lambda s: s.value))
    def test_a_failing_status_may_not_carry_output(self, status):
        with pytest.raises(ValueError, match="carries output"):
            AgentResult(
                capability="x", status=status, output={"value": 0},
                errors=[AgentError(code="E", message="m")],
            )

    @pytest.mark.parametrize("status", sorted(USABLE_STATUSES, key=lambda s: s.value))
    def test_a_usable_status_may_not_be_empty(self, status):
        with pytest.raises(ValueError, match="no output"):
            AgentResult(capability="x", status=status, output=None)

    def test_an_unexplained_failure_is_refused(self):
        """A failure nobody can act on is not a report, it is a shrug."""
        with pytest.raises(ValueError, match="neither an error nor missing"):
            AgentResult(capability="x", status=AgentStatus.NON_RETRYABLE_FAILURE)

    def test_require_raises_rather_than_returning_a_default(self):
        result = AgentResult.insufficient_evidence("x", reason="never ran")
        with pytest.raises(ValueError, match="no usable output"):
            result.require()

    def test_reasoning_output_is_never_marked_authoritative(self):
        """
        A narrative may not be cited as a figure. The provenance says so on the
        result itself, so a consumer does not have to know which capability
        produced it.
        """
        advisory = AgentResult.from_tool_result(
            _ok("reasoning.synthesise", execution_mode=ExecutionMode.PROBABILISTIC),
            output={"narrative": "..."},
        )
        deterministic = AgentResult.from_tool_result(
            _ok("optimization.solve"), output={"cost": 1.0},
        )
        assert advisory.provenance.is_authoritative is False
        assert deterministic.provenance.is_authoritative is True

    def test_provenance_records_the_data_version_the_result_describes(self):
        result = AgentResult.from_tool_result(
            _ok("optimization.solve_scenario"), output={"cost": 1.0},
            execution_id="exec-9", snapshot_id="snap-1", scenario_id="scn-2",
            agent="OptimizationClient",
        )
        assert result.provenance.snapshot_id == "snap-1"
        assert result.provenance.scenario_id == "scn-2"
        assert result.provenance.execution_id == "exec-9"
        assert result.provenance.provider == "OptimizationClient"

    def test_an_explicit_handler_status_wins_over_the_derived_one(self):
        """
        A handler that knows its result is incomplete can say so; nothing else
        can infer it from a boolean.
        """
        declared = ToolResult(
            capability="resilience.assess", success=True,
            output={"n_ok": 3, "n_failed": 2}, status=AgentStatus.PARTIAL,
        )
        result = AgentResult.from_tool_result(declared, output={"n_ok": 3})
        assert result.status is AgentStatus.PARTIAL

    def test_classify_is_a_pure_function_of_existing_evidence(self):
        C = AgentResult.classify
        assert C(success=True) is AgentStatus.SUCCESS
        assert C(success=True, degraded=["a"]) is AgentStatus.PARTIAL
        assert C(success=True, complete=False) is AgentStatus.PARTIAL
        assert C(success=True, validation_errors=["v"]) is AgentStatus.INVALID_OUTPUT
        assert C(success=False, failure_class="RETRYABLE") is AgentStatus.RETRYABLE_FAILURE
        assert C(success=False, failure_class="NON_RETRYABLE") is AgentStatus.NON_RETRYABLE_FAILURE
        assert C(success=False, error_code="VALIDATION_FAILURE") is AgentStatus.INVALID_OUTPUT
        assert C(success=False, error_code="MISSING_DATA") is AgentStatus.INSUFFICIENT_EVIDENCE
        assert C(success=False, error_code="DEPENDENCY_FAILURE") is AgentStatus.INSUFFICIENT_EVIDENCE

    def test_the_generic_envelope_preserves_the_domain_type(self):
        """
        `AgentResult[ForecastResult]`, not `AgentResult` around a dict. The
        typed result stays the authoritative one.
        """
        from netgravity.forecasting.schemas import (
            ForecastProvenance, ForecastResult, ForecastStatus,
        )

        empty = ForecastResult(
            status=ForecastStatus.INSUFFICIENT_HISTORY,
            provenance=ForecastProvenance(snapshot_id="snap-1"),
        )
        typed: AgentResult[ForecastResult] = AgentResult.from_tool_result(
            _ok(CAP_FORECAST), output=empty,
        )
        assert isinstance(typed.output, ForecastResult)
        assert typed.output is empty


# ===========================================================================
# B. Capability registry
# ===========================================================================

class TestCapabilityRegistry:

    @pytest.fixture()
    def registry(self) -> CapabilityRegistry:
        return CapabilityRegistry()

    @pytest.fixture()
    def contract(self) -> CapabilityContract:
        return CapabilityContract(
            capability_id="demo.thing", domain=CapabilityDomain.KPI,
            provider="DemoService", output_type="NetworkKPIs",
            required_inputs=("horizon",),
        )

    def test_registration_and_lookup(self, registry, contract):
        registry.register_contract(contract)
        assert registry.has_contract("demo.thing")
        assert registry.contract("demo.thing").provider == "DemoService"
        assert [c.capability_id for c in registry.contracts()] == ["demo.thing"]

    def test_duplicate_declaration_is_refused(self, registry, contract):
        registry.register_contract(contract)
        conflicting = contract.model_copy(update={"provider": "SomethingElse"})
        with pytest.raises(ValueError, match="already declared"):
            registry.register_contract(conflicting)
        # Overriding must be deliberate.
        registry.register_contract(conflicting, replace=True)
        assert registry.contract("demo.thing").provider == "SomethingElse"

    def test_re_declaring_the_identical_contract_is_harmless(self, registry, contract):
        """Idempotent wiring must not need `replace=True` to run twice."""
        registry.register_contract(contract)
        registry.register_contract(contract)
        assert len(registry.contracts()) == 1

    def test_unknown_capability_raises_with_the_alternatives(self, registry, contract):
        registry.register_contract(contract)
        with pytest.raises(CapabilityNotFoundError) as exc:
            registry.contract("does.not.exist")
        assert "demo.thing" in str(exc.value)

    def test_duplicate_capability_registration_is_refused(self, registry):
        cap = Capability(name="demo.thing", handler=_noop_handler, retry_policy=NO_RETRY)
        registry.register(cap)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(cap)

    def test_resolution_is_by_domain_so_a_provider_can_be_swapped(self, registry):
        registry.register_contract(CapabilityContract(
            capability_id="a.solve", domain=CapabilityDomain.OPTIMIZATION,
            provider="MILP", output_type="NetworkStateResult"))
        registry.register_contract(CapabilityContract(
            capability_id="b.solve", domain=CapabilityDomain.OPTIMIZATION,
            provider="MILP", output_type="NetworkStateResult"))
        assert registry.providers_of(CapabilityDomain.OPTIMIZATION) == ["a.solve", "b.solve"]
        assert registry.resolve_capability(CapabilityDomain.OPTIMIZATION).capability_id == "a.solve"

    def test_an_unserved_domain_answers_none_rather_than_raising(self, registry):
        """A planner asking what a question needs is not making a mistake."""
        assert registry.resolve_capability(CapabilityDomain.FORECAST) is None

    def test_a_service_capability_is_never_offered_as_schedulable(self, registry):
        # Phase 8.3 made the distinction explicit: a SERVICE capability must
        # also declare planner_selectable=False, and the contract now refuses
        # the inconsistent combination this test used to construct.
        registry.register_contract(CapabilityContract(
            capability_id="extraction.parse", domain=CapabilityDomain.EXTRACTION,
            provider="ExtractionParsingAgent", invocation=InvocationMode.SERVICE,
            planner_selectable=False))
        assert registry.resolve(CapabilityDomain.EXTRACTION)          # declared
        assert registry.resolve_capability(CapabilityDomain.EXTRACTION) is None
        assert registry.resolve_capability(
            CapabilityDomain.EXTRACTION, schedulable_only=False) is not None

    def test_validate_inputs_reports_what_is_missing_and_runs_nothing(self, registry, contract):
        registry.register_contract(contract)
        assert registry.validate_inputs("demo.thing", {}) == ["horizon"]
        assert registry.validate_inputs("demo.thing", {"horizon": 3}) == []

    def test_validate_inputs_raises_for_a_capability_that_does_not_exist(self, registry):
        with pytest.raises(CapabilityNotFoundError):
            registry.validate_inputs("nope", {})

    def test_an_embedded_capability_must_name_its_host(self):
        with pytest.raises(ValueError, match="names no host_capability"):
            CapabilityContract(
                capability_id="x", domain=CapabilityDomain.SIGNAL_ROUTING,
                provider="p", invocation=InvocationMode.EMBEDDED)

    def test_a_model_backed_capability_may_not_claim_determinism(self):
        """
        Marking a model call reproducible is how a narrative becomes a figure.
        """
        with pytest.raises(ValueError, match="not reproducible"):
            CapabilityContract(
                capability_id="x", domain=CapabilityDomain.REASONING,
                provider="p", llm_backed=True,
                execution_mode=ExecutionMode.DETERMINISTIC)

    def test_a_capability_may_not_depend_on_itself(self):
        with pytest.raises(ValueError, match="itself as a dependency"):
            CapabilityContract(
                capability_id="x", domain=CapabilityDomain.KPI,
                provider="p", dependencies=("x",))

    def test_a_registered_capability_publishes_its_own_contract(self, registry, contract):
        """Handler and metadata are registered together or they drift apart."""
        registry.register(Capability(
            name="demo.thing", handler=_noop_handler,
            retry_policy=NO_RETRY, contract=contract))
        assert registry.has_contract("demo.thing")


class TestLiveRegistry:
    """The wired system, not a fixture."""

    @pytest.fixture(scope="class")
    def orch(self):
        return build_orchestrator(enable_llm=False)

    def test_every_registered_capability_is_declared(self, orch):
        assert orch.registry.undeclared() == []

    def test_every_declared_capability_is_executable(self, orch):
        """
        Strengthened in Phase 8.2. This previously asserted that extraction, the
        twin projection and signal routing had NO handler — a gap, recorded
        honestly at the time. Phase 8.2 closed it: all three now execute through
        the capability seam.

        What replaces it is a stronger claim, not a weaker one. Every declared
        capability is executable, AND being executable is kept separate from
        being plannable — which is the invariant the old assertion was really
        protecting. The next test pins that second half.
        """
        assert orch.registry.unimplemented() == []
        assert len(orch.registry.contracts()) == len(orch.registry.all())

    def test_being_executable_does_not_make_a_capability_plannable(self, orch):
        """
        The three service/embedded capabilities now have handlers. `invocation`,
        not handler presence, is what keeps them out of a plan — so a planner
        still cannot be handed one.
        """
        for name in (CAP_EXTRACT, CAP_ROUTE_SIGNAL, CAP_TWIN_PUBLISH):
            assert orch.registry.has(name), f"{name} should be executable"
            assert not orch.registry.contract(name).is_plan_schedulable

        schedulable = {c.capability_id for c in orch.registry.schedulable()}
        assert not schedulable & {CAP_EXTRACT, CAP_ROUTE_SIGNAL, CAP_TWIN_PUBLISH}
        assert len(schedulable) == 13

    def test_the_eight_capabilities_the_phase_requires_are_all_present(self, orch):
        required = {
            CapabilityDomain.EXTRACTION, CapabilityDomain.SIGNAL_ROUTING,
            CapabilityDomain.FORECAST, CapabilityDomain.RESILIENCE,
            CapabilityDomain.OPTIMIZATION, CapabilityDomain.REASONING,
            CapabilityDomain.GOVERNANCE, CapabilityDomain.DIGITAL_TWIN,
        }
        for domain in required:
            assert orch.registry.resolve(domain), f"no capability serves {domain.value}"

    def test_conversation_is_not_a_capability(self, orch):
        """
        NLU decides WHICH capabilities a turn needs, before any execution exists.
        Registering it would make understanding a request a step inside executing
        one. Asserted so the omission stays a decision rather than an oversight.
        """
        declared = " ".join(
            f"{c.capability_id} {c.provider}" for c in orch.registry.contracts()
        ).lower()
        assert "nlu" not in declared
        assert "conversation" not in declared

    def test_declared_output_types_name_real_classes(self, orch):
        """
        The catalogue is checked against the code, so a rename cannot leave it
        describing something that no longer exists.
        """
        import importlib

        search = [
            "netgravity.schemas.results", "netgravity.schemas.contracts",
            "netgravity.forecasting.schemas", "netgravity.orchestrator.schemas.risk",
            "netgravity.orchestrator.schemas.actions", "netgravity.orchestrator.schemas.twin",
            "netgravity.orchestrator.schemas.extraction", "netgravity.orchestrator.schemas.requests",
            "netgravity.orchestrator.state.stores", "netgravity.orchestrator.routing.signal_router",
            "netgravity.ingestion.schemas.signal", "netgravity.validation.checks",
        ]
        modules = [importlib.import_module(m) for m in search]
        for contract in orch.registry.contracts():
            for type_name in (contract.input_type, contract.output_type):
                if not type_name:
                    continue
                assert any(hasattr(m, type_name) for m in modules), (
                    f"{contract.capability_id} declares type '{type_name}', "
                    f"which no schema module defines"
                )

    def test_declared_authoritative_fields_exist_on_the_execution_context(self, orch):
        known = {f.name for f in dataclass_fields(ExecutionContext)}
        for contract in orch.registry.contracts():
            if contract.authoritative_field:
                assert contract.authoritative_field in known, (
                    f"{contract.capability_id} points at "
                    f"'{contract.authoritative_field}', not a context field"
                )

    def test_declared_dependencies_match_the_registered_ones(self, orch):
        """
        Two places state a capability's dependencies — the executable
        `Capability` and its contract. Where both speak, they must agree.
        """
        for capability in orch.registry.all():
            contract = capability.contract
            if contract is None or not capability.dependencies:
                continue
            if not contract.dependencies:
                continue  # deliberately empty; see the reasoning/governance notes
            assert set(contract.dependencies) == set(capability.dependencies), (
                f"{capability.name}: contract says {contract.dependencies}, "
                f"registration says {capability.dependencies}"
            )

    def test_declared_execution_modes_match_the_registered_ones(self, orch):
        for capability in orch.registry.all():
            if capability.contract is not None:
                assert capability.contract.execution_mode == capability.execution_mode


# ===========================================================================
# C. ExecutionContext
# ===========================================================================

class TestExecutionContextCapabilityState:

    def test_a_successful_step_marks_its_capability_complete(self):
        ctx = ExecutionContext()
        ctx.record_step("s1", _ok(CAP_OPTIMIZE))
        assert ctx.capability_outcome(CAP_OPTIMIZE) is AgentStatus.SUCCESS
        assert ctx.completed_capabilities() == [CAP_OPTIMIZE]
        assert ctx.failed_capabilities() == []

    def test_a_failed_step_records_the_failure_class_not_just_a_flag(self):
        ctx = ExecutionContext()
        ctx.record_step("s1", _bad(CAP_OPTIMIZE, error_code="ENGINE_TIMEOUT",
                                   failure_class="RETRYABLE"))
        assert ctx.capability_outcome(CAP_OPTIMIZE) is AgentStatus.RETRYABLE_FAILURE
        assert ctx.failed_capabilities() == [CAP_OPTIMIZE]
        assert CAP_OPTIMIZE in ctx.unavailable_evidence

    def test_recording_missing_evidence_does_not_overwrite_a_real_failure(self):
        """
        `record_step` classifies, then records the absence. If the second write
        won, every engine fault would read as "the inputs were missing" and the
        retryable distinction would be gone.
        """
        ctx = ExecutionContext()
        ctx.record_step("s1", _bad(CAP_REI, error_code="ENGINE_TIMEOUT",
                                   failure_class="RETRYABLE"))
        assert ctx.capability_outcome(CAP_REI) is AgentStatus.RETRYABLE_FAILURE

    def test_a_blocked_step_reads_as_insufficient_evidence(self):
        ctx = ExecutionContext()
        ctx.record_blocked("s2", CAP_RISK, ["resilience.assess"])
        assert ctx.capability_outcome(CAP_RISK) is AgentStatus.INSUFFICIENT_EVIDENCE
        assert ctx.unavailable_evidence[CAP_RISK].status is EvidenceStatus.NOT_RUN

    def test_two_steps_of_one_capability_disagreeing_is_partial(self):
        """
        A comparison run solves two scenarios through one capability. SUCCESS
        would hide the failed solve; failure would discard the good one.
        """
        ctx = ExecutionContext()
        ctx.record_step("scn_a", _ok(CAP_OPTIMIZE))
        ctx.record_step("scn_b", _bad(CAP_OPTIMIZE))
        assert ctx.capability_outcome(CAP_OPTIMIZE) is AgentStatus.PARTIAL
        assert ctx.completed_capabilities() == [CAP_OPTIMIZE]

    def test_pending_capabilities_come_from_the_plan_not_a_stored_list(self):
        from netgravity.orchestrator.schemas.plans import ExecutionPlan, PlanStep

        ctx = ExecutionContext()
        ctx.plan = ExecutionPlan(
            workflow_id="wf", intent="X",
            steps=[
                PlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                PlanStep(step_id="opt", capability=CAP_OPTIMIZE, depends_on=["load"]),
                PlanStep(step_id="rei", capability=CAP_REI, depends_on=["load"]),
            ],
        )
        assert ctx.pending_capabilities() == sorted([CAP_LOAD_NETWORK, CAP_OPTIMIZE, CAP_REI])
        ctx.record_step("load", _ok(CAP_LOAD_NETWORK))
        ctx.record_step("opt", _ok(CAP_OPTIMIZE))
        assert ctx.pending_capabilities() == [CAP_REI]
        ctx.record_step("rei", _bad(CAP_REI))
        # Settled is settled, whether it succeeded or not.
        assert ctx.pending_capabilities() == []

    def test_a_capability_that_never_ran_yields_insufficient_evidence(self):
        """Not an empty success, and above all not a zero."""
        ctx = ExecutionContext()
        result = ctx.agent_result(CAP_REI)
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        assert result.output is None
        assert not result.is_usable

    def test_agent_result_reads_the_typed_field_not_the_flattened_projection(self):
        """
        `engine_results` holds a projection for transport. Rebuilding a registry
        from it would default a FAILED node's status to OK, so the envelope must
        take the typed object.
        """
        from netgravity.schemas.results import (
            FacilityResilienceRegistry, SolverStatus,
        )

        ctx = ExecutionContext()
        registry_obj = FacilityResilienceRegistry(
            network_id="net-1", disruption_type="FULL_CLOSURE",
            disruption_period="P1",
            baseline_solver_status=SolverStatus.OPTIMAL,
        )
        ctx.rei_registry = registry_obj
        ctx.record_step("rei", _ok(CAP_REI))

        result = ctx.agent_result(CAP_REI, authoritative_field="rei_registry")
        assert result.output is registry_obj
        assert isinstance(result.output, FacilityResilienceRegistry)

    def test_agent_result_falls_back_to_the_projection_when_there_is_no_typed_field(self):
        ctx = ExecutionContext()
        ctx.record_step("kpi", _ok("kpi.summarise"))
        result = ctx.agent_result("kpi.summarise")
        assert result.is_usable
        assert result.output == {"value": 1}

    def test_provenance_is_preserved_per_capability(self):
        ctx = ExecutionContext(baseline_snapshot_id="snap-7", scenario_id="scn-3")
        ctx.record_step("s1", _ok(CAP_OPTIMIZE))
        ctx.record_step("s2", _ok(CAP_REASON,
                                  execution_mode=ExecutionMode.PROBABILISTIC))

        provenance = ctx.capability_provenance()
        assert provenance[CAP_OPTIMIZE]["snapshot_id"] == "snap-7"
        assert provenance[CAP_OPTIMIZE]["authoritative"] is True
        # The narrative is recorded, and recorded as not authoritative.
        assert provenance[CAP_REASON]["authoritative"] is False

        result = ctx.agent_result(CAP_OPTIMIZE)
        assert result.provenance.snapshot_id == "snap-7"
        assert result.provenance.scenario_id == "scn-3"
        assert result.execution_id == ctx.execution_id

    def test_capability_state_cannot_disagree_with_the_step_lists(self):
        """
        The capability view is derived. Nothing writes it independently, so a
        step recorded as complete cannot show up as a failed capability.
        """
        ctx = ExecutionContext()
        ctx.record_step("a", _ok(CAP_LOAD_NETWORK))
        ctx.record_step("b", _bad(CAP_REI))
        completed_caps = {ctx.step_results[s].capability for s in ctx.completed_steps}
        failed_caps = {ctx.step_results[s].capability for s in ctx.failed_steps}
        assert completed_caps == set(ctx.completed_capabilities())
        assert failed_caps <= set(ctx.failed_capabilities())


# ===========================================================================
# D. Integration — every required specialist is representable
# ===========================================================================

REQUIRED_FOR_PHASE = [
    CAP_EXTRACT, CAP_FORECAST, CAP_REI, CAP_OPTIMIZE,
    CAP_REASON, CAP_GOVERN, CAP_TWIN_PUBLISH,
]


class TestSpecialistsAreRepresentable:
    """
    §10 D: each specialist can be expressed through the capability model without
    its behaviour changing. Representation is a VIEW — these tests check the view
    exists and is faithful, and that the underlying results are untouched.
    """

    @pytest.fixture(scope="class")
    def run(self):
        network = build_case16_network()
        orch = build_orchestrator(network=network, enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="what does the network look like now?",
            actor=Actor(actor_id="u1", role=ActorRole.PLANNER),
        ))
        return orch, orch.get_execution_state(response.execution_id), response

    @pytest.mark.parametrize("capability", REQUIRED_FOR_PHASE)
    def test_the_capability_is_declared_with_a_provider_and_an_output(self, run, capability):
        orch, _, _ = run
        contract = orch.get_capability(capability)
        assert contract.provider, f"{capability} names no provider"
        assert contract.output_type, f"{capability} declares no output type"
        assert contract.domain

    def test_a_state_query_does_not_require_forecast_rei_and_risk(self, run):
        """
        §6: the dependency model must not force one universal chain. "What does
        the network look like now?" is answered without a forecast, an REI sweep
        or an RF calculation — and each of those reports INSUFFICIENT_EVIDENCE
        rather than a zero.
        """
        _, ctx, _ = run
        assert ctx.current_state.value == "COMPLETED"
        for capability in (CAP_FORECAST, CAP_REI, CAP_RISK):
            assert ctx.capability_outcome(capability) is None
            result = ctx.agent_result(capability)
            assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE
            assert result.output is None

    def test_the_optimization_result_arrives_typed_and_authoritative(self, run):
        from netgravity.schemas.contracts import NetworkStateResult

        orch, ctx, _ = run
        contract = orch.get_capability(CAP_OPTIMIZE)
        result = ctx.agent_result(
            CAP_OPTIMIZE,
            authoritative_field=contract.authoritative_field,
            agent=contract.provider,
        )
        assert result.status is AgentStatus.SUCCESS
        assert isinstance(result.output, NetworkStateResult)
        assert result.provenance.is_authoritative
        assert type(result.output).__name__ == contract.output_type

    def test_reasoning_is_representable_and_stays_advisory(self, run):
        orch, ctx, _ = run
        contract = orch.get_capability(CAP_REASON)
        result = ctx.agent_result(
            CAP_REASON, authoritative_field=contract.authoritative_field,
        )
        assert result.is_usable
        assert result.provenance.is_authoritative is False
        assert contract.is_authoritative is False

    def test_extraction_is_representable_through_its_adapter(self):
        from netgravity.orchestrator.agents.extraction_agent import ExtractionParsingAgent
        from netgravity.orchestrator.schemas.extraction import (
            ExtractionRequest, ExtractionResult, SourceType,
        )
        from netgravity.orchestrator.tools.adapters import extraction_to_agent_result

        native = ExtractionParsingAgent().extract(ExtractionRequest(
            source="Flooding expected near DC_DELHI with 30% probability next month.",
            source_type=SourceType.EXTERNAL_SIGNAL_TEXT,
        ))
        wrapped = extraction_to_agent_result(native, execution_id="exec-1")

        assert wrapped.capability == CAP_EXTRACT
        assert wrapped.is_usable
        # The domain result is carried through UNCHANGED — the adapter translates
        # the status vocabulary and nothing else.
        assert wrapped.output is native
        assert isinstance(wrapped.output, ExtractionResult)
        assert wrapped.metadata["extraction_status"] == native.status.value

    def test_a_rejected_extraction_is_invalid_output_not_a_failure(self):
        """Nothing broke: the pipeline ran and refused the data."""
        from netgravity.orchestrator.schemas.extraction import (
            ExtractionResult, ExtractionStatus, ValidationFinding, ValidationSeverity,
        )
        from netgravity.orchestrator.tools.adapters import extraction_to_agent_result

        rejected = ExtractionResult(
            status=ExtractionStatus.REJECTED,
            validation_results=[ValidationFinding(
                severity=ValidationSeverity.ERROR, code="R-001",
                message="required field missing")],
        )
        wrapped = extraction_to_agent_result(rejected)
        assert wrapped.status is AgentStatus.INVALID_OUTPUT
        assert wrapped.output is None
        assert wrapped.errors[0].code == "R-001"

    def test_review_required_is_not_collapsed_into_rejection(self):
        from netgravity.orchestrator.schemas.extraction import (
            ExtractionResult, ExtractionStatus,
        )
        from netgravity.orchestrator.tools.adapters import extraction_to_agent_result

        # The schema refuses a review requirement that names nothing to review,
        # so a realistic instance carries the item a person must look at.
        wrapped = extraction_to_agent_result(ExtractionResult(
            status=ExtractionStatus.HUMAN_REVIEW_REQUIRED,
            review_items=[{"field": "capacity_units_per_period",
                           "reason": "ambiguous column header"}],
        ))
        assert wrapped.status is AgentStatus.PARTIAL
        assert wrapped.is_usable
        assert any("human review" in w for w in wrapped.warnings)

    def test_the_digital_twin_state_is_representable(self, run):
        from netgravity.orchestrator.tools.adapters import twin_state_to_agent_result

        orch, ctx, _ = run
        refs = orch.twin.list_states()
        assert refs, "the run published no twin state"
        state = orch.twin.materialize(refs[0].state_id)
        wrapped = twin_state_to_agent_result(state, execution_id=ctx.execution_id)
        assert wrapped.capability == CAP_TWIN_PUBLISH
        assert wrapped.is_usable
        assert wrapped.output is state

    def test_a_failed_twin_state_is_published_but_offers_no_numbers(self):
        """
        The twin represents failed runs on purpose — a stale picture with no
        warning is worse. But nothing may be read off it as a measurement.
        """
        from netgravity.orchestrator.schemas.twin import (
            DigitalTwinState, TwinCalculationStatus, TwinProvenance, TwinStateType,
        )
        from netgravity.orchestrator.tools.adapters import twin_state_to_agent_result

        state = DigitalTwinState(
            state_id="tws_x", snapshot_id="snap-1",
            state_type=TwinStateType.OPTIMIZED,
            provenance=TwinProvenance(execution_id="e1", snapshot_id="snap-1"),
            calculation_status=TwinCalculationStatus.INFEASIBLE,
        )
        wrapped = twin_state_to_agent_result(state)
        assert wrapped.status is AgentStatus.NON_RETRYABLE_FAILURE
        assert wrapped.output is None
        assert wrapped.metadata["state_id"] == "tws_x"

    def test_signal_routing_is_representable_and_never_yields_a_probability(self):
        from netgravity.orchestrator.routing.signal_router import (
            RoutingOutcome, SignalRoutingDecision, SignalRoutingRecord,
        )
        from netgravity.orchestrator.tools.adapters import routing_decision_to_agent_result

        decision = SignalRoutingDecision(records=[SignalRoutingRecord(
            signal_id="sig-1", outcome=RoutingOutcome.LOW_CONFIDENCE,
            reason="below the routing threshold")])
        wrapped = routing_decision_to_agent_result(decision)

        # Refusing to route is a correct answer, not a failure.
        assert wrapped.status is AgentStatus.PARTIAL
        assert wrapped.is_usable
        assert any("sig-1" in w for w in wrapped.warnings)
        # Nothing resembling an event probability appears on the result.
        assert "event_probability" not in wrapped.metadata
        assert "probability" not in str(wrapped.metadata)

    def test_representation_changed_none_of_the_recorded_results(self, run):
        """
        The whole phase is a view over existing state. If building the envelopes
        mutated anything, this would catch it.
        """
        _, ctx, _ = run
        before = {k: v.model_dump() for k, v in ctx.step_results.items()}
        for capability in ctx.capability_status:
            ctx.agent_result(capability)
        after = {k: v.model_dump() for k, v in ctx.step_results.items()}
        assert before == after


# ===========================================================================
# E. Architectural boundaries (§9)
# ===========================================================================

class TestArchitecturalBoundaries:

    def test_the_registry_cannot_execute_a_capability(self):
        """
        Structural, not conventional: every public method of the registry that
        takes a capability name returns metadata. The handler in this test raises
        if it is ever called.
        """
        registry = CapabilityRegistry()
        contract = CapabilityContract(
            capability_id="demo.thing", domain=CapabilityDomain.KPI, provider="p")
        registry.register(Capability(
            name="demo.thing", handler=_noop_handler,
            retry_policy=NO_RETRY, contract=contract))

        # None of these may invoke the handler.
        registry.contract("demo.thing")
        registry.resolve(CapabilityDomain.KPI)
        registry.resolve_capability(CapabilityDomain.KPI)
        registry.validate_inputs("demo.thing", {})
        registry.dependency_map()
        registry.describe_contracts()
        registry.providers_of(CapabilityDomain.KPI)
        registry.authoritative()
        registry.schedulable()

    def test_the_registry_modules_import_no_engine(self):
        """
        A metadata layer that imports a solver is one refactor away from calling
        it. Checked by AST so the guarantee does not depend on running anything.
        """
        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.forecasting", "pulp")
        for name in ("capability_registry.py", "capability_contracts.py"):
            path = ROOT / "orchestrator" / "routing" / name
            imported = _module_imports(path)
            assert not [m for m in imported if m.startswith(forbidden)], (
                f"{name} imports an engine"
            )
        # The declaration types themselves must stay just as light.
        schema_imports = _module_imports(ROOT / "orchestrator" / "schemas" / "capability.py")
        assert not [m for m in schema_imports if m.startswith(forbidden)]

    def test_the_planner_never_schedules_a_service_or_embedded_capability(self):
        """
        Extraction, the twin projection and signal routing are declared so a
        planner can see they exist — not so it can put them in a plan.
        """
        orch = build_orchestrator(enable_llm=False)
        unschedulable = {
            c.capability_id for c in orch.registry.contracts()
            if not c.is_plan_schedulable
        }
        assert unschedulable == {CAP_EXTRACT, CAP_ROUTE_SIGNAL, CAP_TWIN_PUBLISH}

        source = pathlib.Path(inspect.getfile(planner_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Find the assignment to WORKFLOW_TEMPLATES and every builder it names,
        # then confirm no PlanStep anywhere in the module uses a forbidden name.
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "PlanStep"):
                continue
            for keyword in node.keywords:
                if keyword.arg == "capability" and isinstance(keyword.value, ast.Name):
                    assert keyword.value.id not in {
                        "CAP_EXTRACT", "CAP_ROUTE_SIGNAL", "CAP_TWIN_PUBLISH",
                    }, "the planner scheduled a capability that is not a plan step"

    def test_every_workflow_template_references_only_registered_capabilities(self):
        """Nothing plannable may be missing a handler."""
        from netgravity.orchestrator.schemas.requests import IntentResolution

        orch = build_orchestrator(enable_llm=False)
        for intent, template in WORKFLOW_TEMPLATES.items():
            steps = template.build(IntentResolution(intent=intent))
            for step in steps:
                assert orch.registry.has(step.capability), (
                    f"workflow {template.workflow_id} names unregistered "
                    f"'{step.capability}'"
                )

    def test_no_specialist_agent_reaches_an_engine_or_the_control_plane(self):
        """
        The rule that keeps the graph one-directional: a specialist answers, the
        orchestrator composes. An agent importing the MILP or the orchestrator
        core could start its own workflow.
        """
        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.orchestrator.core", "netgravity.orchestrator.risk",
                     "netgravity.orchestrator.twin", "netgravity.orchestrator.governance")
        for path in sorted((ROOT / "orchestrator" / "agents").glob("*.py")):
            offenders = [m for m in _module_imports(path) if m.startswith(forbidden)]
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_only_agent_to_agent_dependency_is_the_documented_one(self):
        """
        `ExtractionParsingAgent` calls `ExternalSignalAgent` to PARSE text, with
        the model tier disabled, and stops at the signal — it never reaches REI
        or RF. That is a real exception to "agents do not invoke other agents",
        so it is pinned here rather than hidden: this test fails if a second such
        edge appears.
        """
        edges = {}
        for path in sorted((ROOT / "orchestrator" / "agents").glob("*.py")):
            peers = [
                m for m in _module_imports(path)
                if m.startswith("netgravity.orchestrator.agents")
                and "llm_gateway" not in m
            ]
            if peers:
                edges[path.name] = sorted(peers)

        assert edges == {
            "extraction_agent.py": [
                "netgravity.orchestrator.agents.external_signal_agent"
            ]
        }, f"an undocumented agent-to-agent dependency appeared: {edges}"

        # And that edge must not be a route to the risk chain.
        source = (ROOT / "orchestrator" / "agents" / "extraction_agent.py").read_text(
            encoding="utf-8")
        assert "assess_network_risk" not in source
        assert "risk_factor" not in source

    def test_forecasting_cannot_reach_the_solver_rei_or_rf(self):
        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.orchestrator")
        for path in (ROOT.parent / "netgravity" / "forecasting").rglob("*.py"):
            offenders = [m for m in _module_imports(path) if m.startswith(forbidden)]
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_digital_twin_invokes_no_engine_of_its_own(self):
        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.forecasting", "netgravity.orchestrator.risk")
        for path in (ROOT / "orchestrator" / "twin").rglob("*.py"):
            offenders = [m for m in _module_imports(path) if m.startswith(forbidden)]
            assert not offenders, f"{path.name} imports {offenders}"

    def test_reasoning_is_declared_advisory_and_governance_authoritative(self):
        orch = build_orchestrator(enable_llm=False)
        assert orch.get_capability(CAP_REASON).is_authoritative is False
        assert orch.get_capability(CAP_REASON).llm_backed is True
        assert orch.get_capability(CAP_GOVERN).is_authoritative is True
        assert orch.get_capability(CAP_GOVERN).llm_backed is False

    def test_rf_and_signal_routing_remain_separate_domains(self):
        """
        RF's probability and a signal's relevance confidence are different
        numbers. Keeping them in one domain would invite a planner to substitute
        one for the other.
        """
        orch = build_orchestrator(enable_llm=False)
        interpretation = orch.registry.providers_of(CapabilityDomain.SIGNAL_INTERPRETATION)
        routing = orch.registry.providers_of(CapabilityDomain.SIGNAL_ROUTING)

        assert CAP_INTERPRET_SIG in interpretation
        assert CAP_INTERPRET_SIG not in routing
        assert not set(interpretation) & set(routing)
        # RF depends on the interpretation, never on the routing decision.
        rf_dependencies = orch.get_capability(CAP_RISK).dependencies
        assert CAP_INTERPRET_SIG in rf_dependencies
        assert not set(rf_dependencies) & set(routing)

    def test_the_control_plane_primitives_neither_plan_nor_retry(self):
        """
        §11: this phase establishes primitives only. The methods added for the
        future planner must not have quietly become the planner.
        """
        source = pathlib.Path(
            inspect.getfile(build_orchestrator.__globals__["Orchestrator"])
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        added = {
            "resolve_capability", "get_capability", "validate_inputs",
            "record_result", "get_execution_state", "capability_contracts",
            "capability_dependencies",
        }
        checked = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name in added):
                continue
            checked += 1
            body = ast.dump(node)
            for forbidden in ("Await", "asyncio", "sleep", "retry", "reroute",
                              "escalate", "generate"):
                assert forbidden not in body, (
                    f"Orchestrator.{node.name} contains '{forbidden}'; the "
                    f"control-plane primitives must only read"
                )
        assert checked == len(added), f"expected {len(added)} primitives, found {checked}"

    def test_no_llm_planner_or_agent_sdk_was_introduced(self):
        """
        §11 in the negative. The catalogue and the contract are deterministic
        metadata; nothing here reaches for a model or an agent framework.
        """
        new_modules = [
            ROOT / "orchestrator" / "schemas" / "agent_result.py",
            ROOT / "orchestrator" / "schemas" / "capability.py",
            ROOT / "orchestrator" / "routing" / "capability_contracts.py",
            ROOT / "orchestrator" / "tools" / "adapters.py",
        ]
        for path in new_modules:
            imported = _module_imports(path)
            for banned in ("openai", "agents", "agno", "langchain", "litellm"):
                assert not any(m == banned or m.startswith(banned + ".")
                               for m in imported), f"{path.name} imports {banned}"
            source = path.read_text(encoding="utf-8")
            assert "LLMGateway" not in source
            assert "gateway.generate" not in source


class TestCatalogueIntegrity:

    def test_the_catalogue_declares_every_capability_exactly_once(self):
        ids = [c.capability_id for c in CAPABILITY_CONTRACTS]
        assert len(ids) == len(set(ids))

    def test_every_declared_dependency_is_itself_declared(self):
        known = {c.capability_id for c in CAPABILITY_CONTRACTS}
        for contract in CAPABILITY_CONTRACTS:
            assert set(contract.dependencies) <= known
            if contract.host_capability:
                assert contract.host_capability in known

    def test_no_dependency_cycle_exists_among_the_declarations(self):
        graph = {c.capability_id: set(c.dependencies) for c in CAPABILITY_CONTRACTS}
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {k: WHITE for k in graph}

        def visit(node, trail):
            colour[node] = GREY
            for dep in graph.get(node, ()):
                if colour.get(dep) == GREY:
                    raise AssertionError(
                        f"dependency cycle: {' -> '.join(trail + [node, dep])}")
                if colour.get(dep) == WHITE:
                    visit(dep, trail + [node])
            colour[node] = BLACK

        for node in graph:
            if colour[node] == WHITE:
                visit(node, [])

    def test_the_declarations_are_immutable(self):
        """
        A contract that could be edited at runtime would let the planner and the
        executor disagree about what a capability does.
        """
        contract = CAPABILITY_CONTRACTS[0]
        with pytest.raises(Exception):
            contract.provider = "something else"
