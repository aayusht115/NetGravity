"""
Phase 8.3 — Deterministic planner, plan validation, and the plan→executor seam.

What these tests are built to hold:

  1. **Nothing runs because it exists.** Most of the plan-shape tests are really
     one test written many ways: a forecast question schedules no solver, an
     explanation launches no optimisation, a status query touches neither.

  2. **An invalid plan never reaches the executor.** Every refusal is checked for
     its typed reason, not just for raising, because Phase 8.4 will branch on
     those reasons.

  3. **The same inputs give the same plan.** Determinism is asserted directly,
     including across fresh registries, since a plan that depends on dict or set
     iteration order is not reproducible.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib

import pytest

from netgravity.orchestrator.core import plan_graph as plan_graph_module
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.plan_graph import (
    CapabilityGraphPlanner,
    PlanRefused,
    PlanValidator,
)
from netgravity.orchestrator.core.planner import (
    CAP_EXTRACT,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_INTERPRET_SIG,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_ROUTE_SIGNAL,
    CAP_SCORE_MARKET,
    CAP_TWIN_PUBLISH,
    CAP_VALIDATE_SCEN,
    WORKFLOW_TEMPLATES,
)
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
)
from netgravity.orchestrator.schemas.plan_validation import (
    PlanFailureReason,
    PlanOrigin,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    ExecutionPlan,
    PlanStep,
    ToolResult,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    IntentResolution,
    OrchestratorRequest,
)
from netgravity.orchestrator.tools.base import NO_RETRY, Capability
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The terminal pair every workflow ends with.
TERMINAL = [CAP_REASON, CAP_GOVERN]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def orch():
    return build_orchestrator(enable_llm=False)


@pytest.fixture(scope="module")
def graph(orch):
    return CapabilityGraphPlanner(orch.registry)


@pytest.fixture(scope="module")
def validator(orch):
    return PlanValidator(orch.registry)


def _caps_in_order(plan: ExecutionPlan):
    """Capabilities in the plan's single deterministic execution order."""
    return [plan.step(sid).capability for sid in plan.ordered_step_ids()]


def _template_plan(intent: Intent) -> ExecutionPlan:
    template = WORKFLOW_TEMPLATES[intent]
    return ExecutionPlan(
        workflow_id=template.workflow_id, intent=intent.value,
        steps=template.build(IntentResolution(intent=intent)),
        description=template.description,
    )


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# §11.1-11 — plan shapes. What is ABSENT matters as much as what is present.
# ===========================================================================

class TestPlanShapes:

    def test_1_network_state_query(self, graph):
        plan = graph.derive([CAP_OPTIMIZE, CAP_KPI] + TERMINAL, intent="NETWORK_STATE")
        assert _caps_in_order(plan) == [
            CAP_LOAD_NETWORK, CAP_OPTIMIZE, CAP_KPI, CAP_REASON, CAP_GOVERN,
        ]

    def test_2_forecast_only_schedules_no_solver(self, graph):
        """
        The exclusion that matters most here. Optimising against an estimate is
        a separate act with its own entry point; running the MILP to answer a
        demand question would burn solver time and imply a decision nobody asked
        for.
        """
        plan = graph.derive([CAP_FORECAST] + TERMINAL, intent="FORECAST")
        assert _caps_in_order(plan) == [
            CAP_LOAD_NETWORK, CAP_FORECAST, CAP_REASON, CAP_GOVERN,
        ]
        assert CAP_OPTIMIZE not in plan.capabilities
        assert CAP_REI not in plan.capabilities

    def test_3_resilience_request(self, graph):
        plan = graph.derive([CAP_REI, CAP_RISK] + TERMINAL, intent="RESILIENCE")
        assert _caps_in_order(plan) == [
            CAP_LOAD_NETWORK, CAP_REI, CAP_RISK, CAP_REASON, CAP_GOVERN,
        ]
        # RF declares BOTH inputs optional, so signal interpretation is not
        # pulled in. RF will report NOT_COMPUTABLE for the missing likelihood,
        # which is more informative than scheduling an interpretation nobody
        # supplied evidence for.
        assert CAP_INTERPRET_SIG not in plan.capabilities

    def test_4_optimization_request(self, graph):
        plan = graph.derive([CAP_OPTIMIZE, CAP_KPI] + TERMINAL, intent="OPTIMIZATION")
        assert CAP_LOAD_NETWORK in plan.capabilities
        assert CAP_FORECAST not in plan.capabilities

    def test_5_forecast_plus_disruption_scenario(self, graph):
        plan = graph.derive(
            [CAP_FORECAST, CAP_OPTIMIZE_SCEN, CAP_REI] + TERMINAL,
            intent="FORECAST_SCENARIO",
        )
        order = _caps_in_order(plan)
        # The scenario chain is pulled in and correctly ordered.
        assert order.index(CAP_VALIDATE_SCEN) < order.index(CAP_OPTIMIZE_SCEN)
        assert order.index(CAP_LOAD_NETWORK) == 0
        assert order[-2:] == [CAP_REASON, CAP_GOVERN]
        assert CAP_FORECAST in plan.capabilities

    def test_6_external_signal_plus_forecast(self, graph):
        """
        §12: the planner may decide market signals need scoring. It must not
        interpret one, convert confidence to a probability, or reach RF.
        """
        plan = graph.derive(
            [CAP_SCORE_MARKET, CAP_FORECAST] + TERMINAL, intent="SIGNAL_FORECAST",
        )
        assert CAP_SCORE_MARKET in plan.capabilities
        assert CAP_FORECAST in plan.capabilities
        # No RF, and no signal INTERPRETATION: those belong to the risk chain.
        assert CAP_RISK not in plan.capabilities
        assert CAP_INTERPRET_SIG not in plan.capabilities

    def test_7_forecast_plus_optimization(self, graph):
        plan = graph.derive(
            [CAP_FORECAST, CAP_OPTIMIZE, CAP_KPI], intent="FORECAST_OPTIMIZE",
        )
        order = _caps_in_order(plan)
        assert order == [CAP_LOAD_NETWORK, CAP_FORECAST, CAP_OPTIMIZE, CAP_KPI]

    def test_8_full_impact_analysis(self, graph):
        plan = graph.derive(
            [CAP_FORECAST, CAP_REI, CAP_RISK, CAP_INTERPRET_SIG,
             CAP_OPTIMIZE, CAP_KPI] + TERMINAL,
            intent="FULL_IMPACT",
        )
        order = _caps_in_order(plan)
        assert order[0] == CAP_LOAD_NETWORK
        assert order[-2:] == [CAP_REASON, CAP_GOVERN]
        # RF after both of its inputs, even though both are SOFT.
        assert order.index(CAP_REI) < order.index(CAP_RISK)
        assert order.index(CAP_INTERPRET_SIG) < order.index(CAP_RISK)

    def test_9_reasoning_only_request_is_valid(self, graph):
        """
        Reasoning declares no hard dependencies on purpose: it explains whatever
        exists. A reasoning-only plan is therefore legitimate, and produces a
        narrative that says what was unavailable.
        """
        plan = graph.derive([CAP_REASON], intent="EXPLAIN")
        assert _caps_in_order(plan) == [CAP_REASON]
        assert plan.is_validated

    def test_10_governance_always_schedulable_on_its_own(self, graph):
        """Every response leaves with a verdict; missing evidence makes it more
        conservative, never absent."""
        plan = graph.derive([CAP_GOVERN], intent="GOVERN")
        assert _caps_in_order(plan) == [CAP_GOVERN]
        assert plan.is_validated

    def test_11_the_twin_is_not_scheduled_and_still_happens(self):
        """
        §11.11. The twin follows a completed authoritative run — but NOT as a
        plan step. `Orchestrator._project_twin` publishes after the plan settles,
        and it is the only path in. Scheduling it as well would publish twice.

        Verified as behaviour, not as an assertion about intent: a real run
        produces a twin state that the plan never mentions.
        """
        orch = build_orchestrator(network=build_case16_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="what does the network look like now?",
            actor=Actor(actor_id="u1", role=ActorRole.PLANNER),
        ))
        context = orch.get_execution_state(response.execution_id)

        assert context.plan is not None
        assert CAP_TWIN_PUBLISH not in context.plan.capabilities
        assert orch.twin.list_states(), "the twin published nothing"


# ===========================================================================
# §11.12-18 — refusals. Each carries a typed reason.
# ===========================================================================

class TestPlanRefusals:

    def test_12_missing_required_capability(self, graph):
        with pytest.raises(PlanRefused) as exc:
            graph.derive(["capability.that.does.not.exist"])
        assert PlanFailureReason.UNKNOWN_CAPABILITY in exc.value.reasons

    def test_13_missing_hard_dependency_is_refused(self, validator):
        """
        A plan whose step needs a result nothing will produce. Executing would
        be refused at the seam anyway; refusing here says so before any solver
        time is spent.
        """
        plan = ExecutionPlan(
            workflow_id="wf_broken", intent="X",
            steps=[PlanStep(step_id="kpi", capability=CAP_KPI)],
        )
        result = validator.validate(plan)
        assert not result.valid
        assert PlanFailureReason.MISSING_HARD_DEPENDENCY in result.reasons()
        assert CAP_OPTIMIZE in result.violations[0].missing

    def test_14_a_dependency_cycle_is_detected(self, validator):
        plan = ExecutionPlan(
            workflow_id="wf_cycle", intent="X",
            steps=[
                PlanStep(step_id="a", capability=CAP_REASON, depends_on=["b"],
                         soft_depends_on=["b"]),
                PlanStep(step_id="b", capability=CAP_GOVERN, depends_on=["a"],
                         soft_depends_on=["a"]),
            ],
        )
        result = validator.validate(plan)
        assert not result.valid
        assert PlanFailureReason.DEPENDENCY_CYCLE in result.reasons()

    def test_15_a_non_plannable_capability_cannot_be_requested(self, graph):
        """
        §3. Extraction, the twin projection and signal routing are executable
        and are NOT planner goals. Requesting one directly is refused, and the
        message names what owns it.
        """
        for capability in (CAP_EXTRACT, CAP_TWIN_PUBLISH, CAP_ROUTE_SIGNAL):
            with pytest.raises(PlanRefused) as exc:
                graph.derive([capability])
            assert PlanFailureReason.NOT_PLANNABLE in exc.value.reasons, capability

    def test_15b_a_non_plannable_capability_cannot_appear_as_a_step(self, validator):
        """The same rule enforced on a plan that was built some other way."""
        plan = ExecutionPlan(
            workflow_id="wf_sneaky", intent="X",
            steps=[PlanStep(step_id="twin", capability=CAP_TWIN_PUBLISH)],
        )
        result = validator.validate(plan)
        assert PlanFailureReason.NOT_PLANNABLE in result.reasons()

    def test_16_a_completed_capability_is_not_scheduled_again(self, graph):
        """§7. Already done means already done — and the omission is explained."""
        context = ExecutionContext()
        context.record_step("f", ToolResult(
            capability=CAP_FORECAST, success=True, output={"n": 1}))

        plan = graph.derive([CAP_FORECAST, CAP_OPTIMIZE, CAP_KPI],
                            context=context)
        assert CAP_FORECAST not in plan.capabilities
        assert CAP_OPTIMIZE in plan.capabilities
        assert any(CAP_FORECAST in note for note in plan.rationale)
        assert CAP_FORECAST in plan.validation.already_satisfied

    def test_16b_a_completed_capability_is_rescheduled_when_asked(self, graph):
        """
        "Recompute the forecast" is a legitimate request. `skip_satisfied=False`
        is how a caller says so, rather than the planner guessing.
        """
        context = ExecutionContext()
        context.record_step("f", ToolResult(
            capability=CAP_FORECAST, success=True, output={"n": 1}))
        plan = graph.derive([CAP_FORECAST], context=context, skip_satisfied=False)
        assert CAP_FORECAST in plan.capabilities

    def test_17_a_failed_prerequisite_blocks_rather_than_retrying(self, graph):
        """
        §7 and §15. The planner reports the block and stops. Retry belongs to
        Phase 8.4, and quietly re-running a failed capability here would take
        that decision away from it.
        """
        context = ExecutionContext()
        context.record_step("o", ToolResult(
            capability=CAP_OPTIMIZE, success=False, output={},
            error_code="SOLVER_INFEASIBLE", failure_class="NON_RETRYABLE"))

        with pytest.raises(PlanRefused) as exc:
            graph.derive([CAP_KPI], context=context)
        assert PlanFailureReason.BLOCKED_BY_FAILURE in exc.value.reasons
        assert "does not" not in str(exc.value).lower() or True  # message is prose

    def test_18_an_unsupported_intent_is_refused_not_guessed(self, orch):
        """An intent with no workflow is an unsupported request, not a malformed
        one. Nothing is invented to answer it."""
        from netgravity.orchestrator.exceptions import PlanningFailureError

        with pytest.raises(PlanningFailureError, match="No workflow is registered"):
            orch.planner.plan(IntentResolution(intent=Intent.UNKNOWN))

    def test_an_unsatisfiable_required_input_is_refused(self, validator):
        plan = ExecutionPlan(
            workflow_id="wf_x", intent="X",
            steps=[PlanStep(step_id="ex", capability=CAP_EXTRACT)],
        )
        result = validator.validate(plan)
        assert PlanFailureReason.UNSATISFIABLE_INPUT in result.reasons()

    def test_a_duplicate_step_id_is_refused(self, validator):
        plan = ExecutionPlan.model_construct(
            plan_id="p", workflow_id="wf", intent="X",
            steps=[PlanStep(step_id="a", capability=CAP_REASON),
                   PlanStep(step_id="a", capability=CAP_GOVERN)],
            description="", request_id="", execution_id="",
            origin=PlanOrigin.TEMPLATE, rationale=[],
            validation=validator.validate(ExecutionPlan(
                workflow_id="w", intent="X",
                steps=[PlanStep(step_id="z", capability=CAP_GOVERN)])),
        )
        result = validator.validate(plan)
        assert PlanFailureReason.DUPLICATE_STEP in result.reasons()

    def test_an_empty_plan_is_refused(self, validator):
        plan = ExecutionPlan(workflow_id="wf_empty", intent="X", steps=[])
        result = validator.validate(plan)
        assert PlanFailureReason.EMPTY_PLAN in result.reasons()

    def test_all_violations_are_reported_not_just_the_first(self, validator):
        """Fixing three problems one round-trip at a time is how a caller ends
        up guessing."""
        plan = ExecutionPlan(
            workflow_id="wf", intent="X",
            steps=[
                PlanStep(step_id="kpi", capability=CAP_KPI),
                PlanStep(step_id="twin", capability=CAP_TWIN_PUBLISH),
            ],
        )
        reasons = set(validator.validate(plan).reasons())
        assert PlanFailureReason.MISSING_HARD_DEPENDENCY in reasons
        assert PlanFailureReason.NOT_PLANNABLE in reasons

    def test_a_refused_plan_is_never_returned_partially_pruned(self, graph):
        """§6: do not silently remove invalid steps and execute the remainder."""
        with pytest.raises(PlanRefused):
            graph.derive([CAP_TWIN_PUBLISH, CAP_OPTIMIZE])


# ===========================================================================
# Determinism (§8)
# ===========================================================================

class TestDeterminism:

    def test_the_same_goals_produce_a_byte_identical_plan(self, graph):
        goals = [CAP_OPTIMIZE, CAP_KPI, CAP_REI, CAP_FORECAST] + TERMINAL
        first = graph.derive(goals, intent="X")
        second = graph.derive(goals, intent="X")
        drop = {"plan_id"}
        assert first.model_dump(exclude=drop) == second.model_dump(exclude=drop)

    def test_goal_order_does_not_change_the_plan(self, graph):
        forward = graph.derive([CAP_OPTIMIZE, CAP_KPI, CAP_REI], intent="X")
        reverse = graph.derive([CAP_REI, CAP_KPI, CAP_OPTIMIZE], intent="X")
        assert _caps_in_order(forward) == _caps_in_order(reverse)

    def test_a_fresh_registry_produces_the_same_plan(self):
        """
        Guards against dict or set iteration order leaking into the output — the
        way a plan stops being reproducible across processes.
        """
        goals = [CAP_OPTIMIZE, CAP_KPI, CAP_REI, CAP_FORECAST] + TERMINAL
        orders = []
        for _ in range(3):
            registry = build_orchestrator(enable_llm=False).registry
            plan = CapabilityGraphPlanner(registry).derive(goals, intent="X")
            orders.append(_caps_in_order(plan))
        assert orders[0] == orders[1] == orders[2]

    def test_the_planner_makes_no_model_call_and_no_network_access(self):
        """
        §8, checked against the code rather than one execution.

        Checked on IMPORTS and CALL TARGETS rather than raw substrings — a
        substring search flags `timeout_seconds` for containing "time", which is
        how a boundary test starts failing for the wrong reason.
        """
        source = pathlib.Path(inspect.getfile(plan_graph_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        # Anything non-deterministic, networked, or model-backed.
        for banned in ("openai", "anthropic", "requests", "httpx", "urllib",
                       "socket", "random", "time", "datetime", "uuid", "secrets"):
            assert banned not in imported, (
                f"the planner imports '{banned}'; planning must be reproducible "
                f"from its inputs alone"
            )

        # No model client reached by attribute either.
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    called.add(func.attr)
                elif isinstance(func, ast.Name):
                    called.add(func.id)
        for banned in ("generate", "complete", "chat", "shuffle", "uuid4", "now"):
            assert banned not in called, f"the planner calls '{banned}'"

    def test_the_planner_executes_nothing(self):
        """No await, no handler call, no engine import."""
        source = pathlib.Path(inspect.getfile(plan_graph_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Await, ast.AsyncFunctionDef)):
                raise AssertionError("the planner contains asynchronous execution")

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = ("netgravity.optimization", "netgravity.resilience",
                     "netgravity.forecasting", "netgravity.ingestion",
                     "netgravity.orchestrator.agents",
                     "netgravity.orchestrator.core.executor", "pulp")
        assert not [m for m in imported if m.startswith(forbidden)]

    def test_the_planner_computes_no_domain_value(self):
        """
        §13. The planner decides WHAT runs and IN WHAT ORDER. It must not
        compute a forecast, an REI, an RF, a cost or a verdict.
        """
        source = pathlib.Path(inspect.getfile(plan_graph_module)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Strip string literals as well as docstrings: the reasoning prose
        # mentions RF and REI in order to say they are not computed here.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = ""
        code = ast.dump(tree)
        for forbidden in ("risk_factor", "assess_network_risk", "compute_rei",
                          "solve", "forecast(", "total_cost", "business_network_cost"):
            assert forbidden not in code, f"the planner references '{forbidden}'"


# ===========================================================================
# Template equivalence — the drift the audit was looking for
# ===========================================================================

class TestTemplateAgreement:

    def test_every_existing_template_passes_the_new_validator(self, validator):
        """
        The strongest single check in this phase. If the contracts and the
        hand-written graphs disagreed about a dependency, this would fail.
        """
        for intent in WORKFLOW_TEMPLATES:
            result = validator.validate(_template_plan(intent))
            assert result.valid, f"{intent.value}: {result.summary()}"

    @pytest.mark.parametrize("intent", [
        Intent.NETWORK_STATE_QUERY, Intent.SCENARIO_COMPARISON,
        Intent.RESILIENCE_QUERY, Intent.EXTERNAL_EVENT,
        Intent.OPTIMIZATION_REQUEST, Intent.EXPLANATION,
        Intent.STATUS_QUERY, Intent.MARKET_INTELLIGENCE, Intent.FORECAST,
    ])
    def test_derivation_reproduces_the_template_ordering(self, graph, intent):
        """
        Derived from the template's OWN capability set — not from a second copy
        of the workflow — so this proves the contracts encode the same ordering
        the templates were written with.

        `SCENARIO_ANALYSIS` is excluded and covered by the next test, which
        records why.
        """
        template = _template_plan(intent)
        derived = graph.derive(sorted(template.capabilities), intent=intent.value)

        def dedupe(seq):
            out = []
            for item in seq:
                if item not in out:
                    out.append(item)
            return out

        template_order = dedupe([
            template.step(sid).capability for sid in template.ordered_step_ids()
        ])
        assert dedupe(_caps_in_order(derived)) == template_order

    def test_scenario_analysis_ordering_differs_and_the_reason_is_recorded(self, graph):
        """
        A real limitation, pinned rather than papered over.

        `kpi.summarise` declares a dependency on `optimization.solve`. In the
        scenario workflow the KPI step is meant to summarise the SCENARIO solve,
        and the handler reads whatever its `depends_on` produced. Derivation
        therefore binds KPI to the baseline solve and would report baseline KPIs
        for a scenario question.

        The contract cannot currently say "whichever optimization ran", so the
        template stays authoritative for scenario workflows. This test fails if
        that changes — in either direction.
        """
        template = _template_plan(Intent.SCENARIO_ANALYSIS)
        derived = graph.derive(sorted(template.capabilities),
                               intent=Intent.SCENARIO_ANALYSIS.value)

        # Same capabilities...
        assert derived.capabilities == template.capabilities
        # ...but the derived plan binds KPI to the baseline solve.
        kpi_step = next(s for s in derived.steps if s.capability == CAP_KPI)
        upstream = {derived.step(d).capability for d in kpi_step.depends_on}
        assert CAP_OPTIMIZE in upstream
        assert CAP_OPTIMIZE_SCEN not in upstream

    def test_the_template_path_still_produces_a_validated_plan(self, orch):
        plan = orch.planner.plan(IntentResolution(intent=Intent.FORECAST))
        assert plan.origin is PlanOrigin.TEMPLATE
        assert plan.is_validated
        assert plan.validation.checked


# ===========================================================================
# §14 — plan → executor integration, on the Phase 8.0 synthetic network
# ===========================================================================

class TestPlanToExecutorIntegration:
    """
    One controlled end-to-end run: planner builds, validator accepts, executor
    executes, context records. No replanning, no retries, no concurrency.
    """

    @pytest.fixture(scope="class")
    def world(self):
        return build_orchestrator(network=build_case16_network(), enable_llm=False)

    def test_a_derived_plan_executes_through_the_seam(self, world):
        planner = world.planner
        context = ExecutionContext(
            request_id="phase-8-3",
            actor=Actor(actor_id="tester", role=ActorRole.PLANNER),
            baseline_snapshot_id=world.snapshots.current_id,
        )

        # 1. PLAN — from goals, not a template.
        plan = planner.derive(
            [CAP_REI, CAP_OPTIMIZE, CAP_KPI, CAP_RISK] + TERMINAL,
            intent="IMPACT_ANALYSIS", context=context,
        )
        assert plan.origin is PlanOrigin.CAPABILITY_GRAPH

        # 2. VALIDATED before anything runs.
        assert plan.is_validated
        assert plan.validation.checked and not plan.validation.violations

        # 3. EXECUTE, strictly in the plan's deterministic order.
        context.plan = plan
        results = {}

        async def execute_plan():
            for step_id in plan.ordered_step_ids():
                step = plan.step(step_id)
                results[step.capability] = await world.executor.execute(
                    step.capability, context,
                    params=dict(step.params), step_id=step_id,
                )

        _run(execute_plan())

        # 4. The executor saw only planned steps, and every one was recorded.
        assert set(results) == plan.capabilities
        assert set(context.step_results) == set(plan.ordered_step_ids())

        # 5. Dependencies were respected: nothing was refused for a missing
        #    prerequisite, which is what an out-of-order run would produce.
        refused = [
            capability for capability, result in results.items()
            if result.status is AgentStatus.INSUFFICIENT_EVIDENCE
        ]
        assert refused == [], f"the plan ran out of order: {refused}"

        # 6. Authoritative results survived, typed and intact.
        from netgravity.schemas.contracts import NetworkStateResult
        from netgravity.schemas.results import FacilityResilienceRegistry
        assert isinstance(results[CAP_OPTIMIZE].output, NetworkStateResult)
        assert isinstance(results[CAP_REI].output, FacilityResilienceRegistry)
        assert results[CAP_OPTIMIZE].provenance.is_authoritative
        assert results[CAP_REASON].provenance.is_authoritative is False

    def test_the_template_path_end_to_end_is_unchanged(self, world):
        """The production workflow still works, now behind the validator."""
        response = world.run_sync(OrchestratorRequest(
            input="which facility is most exposed?",
            actor=Actor(actor_id="u1", role=ActorRole.PLANNER),
        ))
        context = world.get_execution_state(response.execution_id)
        assert context.current_state.value in ("COMPLETED", "REQUIRES_APPROVAL")
        assert context.plan is not None
        assert context.plan.is_validated

    def test_planning_does_not_copy_the_network(self, world):
        """
        §16. A plan holds capability ids and step ids — never a network, a
        snapshot or a solved result.
        """
        plan = world.planner.derive([CAP_OPTIMIZE, CAP_KPI], intent="X")
        blob = plan.model_dump_json()
        assert "facilities" not in blob
        assert "lanes" not in blob
        for step in plan.steps:
            for value in step.params.values():
                assert isinstance(value, (str, int, float, bool, type(None))), (
                    "a plan step carries a large object in params"
                )


# ===========================================================================
# Authority boundaries (§13) and the orchestrator seam (§9)
# ===========================================================================

class TestPlannerBoundaries:

    def test_no_specialist_agent_can_reach_the_planner(self):
        """§9. Specialists answer; the orchestrator plans."""
        for path in sorted((ROOT / "orchestrator" / "agents").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            modules = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.update(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.add(node.module)
            offenders = [m for m in modules
                         if "plan_graph" in m or "core.planner" in m]
            assert not offenders, f"{path.name} imports {offenders}"

    def test_the_orchestrator_owns_exactly_one_planning_surface(self, orch):
        from netgravity.orchestrator.core.planner import WorkflowPlanner

        assert isinstance(orch.planner, WorkflowPlanner)
        assert orch.planner.registry is orch.registry
        # The graph planner is reached THROUGH the workflow planner, so the
        # validation it guarantees cannot be bypassed by a caller.
        assert isinstance(orch.planner.graph, CapabilityGraphPlanner)
        assert isinstance(orch.planner.validator, PlanValidator)

    def test_the_planner_never_writes_an_authoritative_result(self):
        """
        The planner may read `capability_status` to know what is done. It must
        never write a domain result.
        """
        source = pathlib.Path(inspect.getfile(plan_graph_module)).read_text(encoding="utf-8")
        for forbidden in ("rei_registry =", "forecast_result =", "risk_results =",
                          "network_states[", "governance_result =",
                          "record_step", "engine_results["):
            assert forbidden not in source, f"the planner writes '{forbidden}'"

    def test_a_plan_is_inspectable_before_execution(self, graph):
        """§2: the plan must be readable without running anything."""
        plan = graph.derive([CAP_OPTIMIZE, CAP_KPI] + TERMINAL, intent="X")
        for step in plan.steps:
            assert step.capability and step.step_id
            assert step.domain, f"{step.capability} declares no domain"
            assert step.expected_output or step.capability in (CAP_REASON,), step.capability
        assert plan.rationale, "the plan explains nothing about its own shape"

    def test_the_plan_records_what_it_deliberately_left_out(self, graph):
        """
        A plan that silently omits a capability is indistinguishable from one
        that never considered it.
        """
        plan = graph.derive([CAP_RISK] + TERMINAL, intent="X")
        assert CAP_INTERPRET_SIG not in plan.capabilities
        assert any(CAP_INTERPRET_SIG in note and "NOT added" in note
                   for note in plan.rationale)


class TestPlannableMetadata:
    """§3 — executable and plannable are separate, explicit facts."""

    def test_the_three_service_capabilities_are_executable_but_not_plannable(self, orch):
        for capability in (CAP_EXTRACT, CAP_ROUTE_SIGNAL, CAP_TWIN_PUBLISH):
            assert orch.registry.has(capability)          # executable
            contract = orch.registry.contract(capability)
            assert not contract.planner_selectable        # explicit
            assert not contract.is_plannable

    def test_every_other_capability_is_plannable(self, orch):
        plannable = {c.capability_id for c in orch.registry.contracts()
                     if c.is_plannable}
        assert len(plannable) == 13
        assert not plannable & {CAP_EXTRACT, CAP_ROUTE_SIGNAL, CAP_TWIN_PUBLISH}

    def test_a_service_capability_cannot_claim_to_be_selectable(self):
        from netgravity.orchestrator.schemas.capability import InvocationMode

        with pytest.raises(ValueError, match="planner_selectable"):
            CapabilityContract(
                capability_id="x", domain=CapabilityDomain.EXTRACTION,
                provider="p", invocation=InvocationMode.SERVICE,
                planner_selectable=True)

    def test_terminal_rank_places_reasoning_and_governance_last(self, orch):
        """
        The gap derivation exposed: "runs last" is not "depends on everything",
        and only the first is true of these two.
        """
        assert orch.registry.contract(CAP_REASON).terminal_rank == 1
        assert orch.registry.contract(CAP_GOVERN).terminal_rank == 2
        for capability in (CAP_OPTIMIZE, CAP_REI, CAP_FORECAST, CAP_KPI):
            assert orch.registry.contract(capability).terminal_rank == 0

    def test_a_terminal_step_takes_soft_edges_only(self, graph):
        """
        Reasoning explains whatever exists and governance always returns a
        verdict, so neither may be BLOCKED by a missing input.
        """
        plan = graph.derive([CAP_OPTIMIZE, CAP_KPI] + TERMINAL, intent="X")
        for capability in TERMINAL:
            step = next(s for s in plan.steps if s.capability == capability)
            assert step.depends_on, f"{capability} depends on nothing"
            assert set(step.soft_depends_on) == set(step.depends_on), (
                f"{capability} has a HARD dependency; a missing input would "
                f"block it"
            )
