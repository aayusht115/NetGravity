"""
Orchestrator — Workflow planner.

Turns an intent into an explicit dependency graph. Workflows are DATA
(`WORKFLOW_TEMPLATES`), not branching code, so the set of runnable workflows can
grow without the orchestrator core changing.

The planner also decides what NOT to run. A resilience query needs no scenario
solve; a state query needs no REI sweep. Running every engine on every request
would waste solver time and shared LLM budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from netgravity.orchestrator.exceptions import PlanningFailureError
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.plans import ExecutionPlan, PlanStep
from netgravity.orchestrator.schemas.requests import Intent, IntentResolution

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical capability names
# ---------------------------------------------------------------------------

CAP_LOAD_NETWORK   = "network.load_snapshot"
CAP_CREATE_SCEN    = "scenario.create"
CAP_VALIDATE_SCEN  = "scenario.validate"
CAP_OPTIMIZE       = "optimization.solve"
CAP_OPTIMIZE_SCEN  = "optimization.solve_scenario"
CAP_KPI            = "kpi.summarise"
CAP_REI            = "resilience.assess"
CAP_RISK           = "risk.compute_rf"
CAP_INTERPRET_SIG  = "external.interpret_signal"
CAP_FORECAST       = "forecast.demand"
#: Scores a reported market signal against the guardrail policy. Named apart
#: from CAP_INTERPRET_SIG on purpose: the two never touch. One derives a
#: probability that feeds RF; this one attaches a relevance verdict from
#: `ingestion/guardrails/relevance.py` and produces nothing RF ever reads.
CAP_SCORE_MARKET   = "market.score_signal"
CAP_REASON         = "reasoning.synthesise"
CAP_GOVERN         = "governance.classify"

# --- declared, but NOT plan steps ------------------------------------------
# These three are real capabilities with real providers, and the capability
# catalogue describes them so the future planner can reason about what the
# system can do. None of them is scheduled in an `ExecutionPlan`, and the names
# live here only to keep every canonical capability name in one place rather
# than drifting apart across modules.
#
# Extraction runs BEFORE an execution exists — it is what produces the network a
# run is later pinned to. The twin projection runs AFTER the plan settles, from
# results the orchestrator has already composed. Signal routing is a gated stage
# inside the forecast handler with no independent entry point.
#
# The planner must not schedule any of them; `WORKFLOW_TEMPLATES` therefore
# never references these constants, and a test asserts that.
CAP_EXTRACT        = "extraction.parse"
CAP_TWIN_PUBLISH   = "twin.publish"
CAP_ROUTE_SIGNAL   = "signal.route_for_forecast"


@dataclass(frozen=True)
class WorkflowTemplate:
    """A named, reusable execution graph."""
    workflow_id: str
    intent: Intent
    description: str
    build: Callable[[IntentResolution], List[PlanStep]]


# ---------------------------------------------------------------------------
# Shared graph fragments
# ---------------------------------------------------------------------------

def _reason_and_govern(depends_on: List[str]) -> List[PlanStep]:
    """
    Terminal pair on every workflow.

    EVERY dependency here is SOFT, deliberately. Reasoning explains whatever
    evidence exists — losing one analytic input degrades the narrative but must
    never suppress it. Governance is the safety gate and must ALWAYS produce a
    verdict; a missing input makes it more conservative, never absent.

    Reasoning is additionally `optional`, so its own failure does not fail the
    run (and makes the reason → govern edge soft automatically).
    """
    analytic = list(depends_on)
    return [
        PlanStep(
            step_id="reason",
            capability=CAP_REASON,
            description="Synthesise available deterministic results into an explanation.",
            depends_on=analytic,
            soft_depends_on=analytic,
            optional=True,
        ),
        PlanStep(
            step_id="govern",
            capability=CAP_GOVERN,
            description="Apply deterministic governance rules to available evidence.",
            depends_on=[*analytic, "reason"],
            soft_depends_on=[*analytic, "reason"],
        ),
    ]


def _build_network_state(_: IntentResolution) -> List[PlanStep]:
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="optimize", capability=CAP_OPTIMIZE,
                 description="Evaluate the observed network state.",
                 depends_on=["load"]),
        PlanStep(step_id="kpi", capability=CAP_KPI,
                 description="Project KPIs.", depends_on=["optimize"]),
        *_reason_and_govern(["optimize", "kpi"]),
    ]


def _build_scenario_analysis(resolution: IntentResolution) -> List[PlanStep]:
    """
    Single-scenario what-if.

    KPI and REI both depend only on the scenario solve, so they form one
    dependency layer and execute concurrently.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="baseline", capability=CAP_OPTIMIZE,
                 description="Solve the observed baseline for comparison.",
                 depends_on=["load"]),
        PlanStep(step_id="create_scenario", capability=CAP_CREATE_SCEN,
                 description="Materialise the hypothetical scenario network.",
                 depends_on=["load"],
                 params={"scenario_index": 0}),
        PlanStep(step_id="validate_scenario", capability=CAP_VALIDATE_SCEN,
                 description="Validate scenario overrides against the real network.",
                 depends_on=["create_scenario"]),
        PlanStep(step_id="optimize_scenario", capability=CAP_OPTIMIZE_SCEN,
                 description="Re-optimise the scenario network.",
                 # validate_scenario is HARD: solving an unvalidated scenario
                 # would produce numbers nobody should trust.
                 # baseline is SOFT: without it we lose the delta, not the solve.
                 depends_on=["validate_scenario", "baseline"],
                 soft_depends_on=["baseline"]),
        PlanStep(step_id="kpi", capability=CAP_KPI,
                 description="Project scenario KPIs.",
                 depends_on=["optimize_scenario"]),
        PlanStep(step_id="rei", capability=CAP_REI,
                 description="Assess facility resilience exposure.",
                 depends_on=["load"], optional=True),
        *_reason_and_govern(["optimize_scenario", "kpi", "rei"]),
    ]


def _build_scenario_comparison(resolution: IntentResolution) -> List[PlanStep]:
    """
    Independent scenarios compared against one baseline.

    Each scenario gets its own create/validate/solve chain with no cross-links,
    so they are fully isolated and land in the same dependency layer — the
    planner-level expression of "scenarios must not contaminate each other".
    """
    steps: List[PlanStep] = [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="baseline", capability=CAP_OPTIMIZE,
                 description="Solve the observed baseline for comparison.",
                 depends_on=["load"]),
    ]
    solve_ids: List[str] = []
    count = max(1, len(resolution.scenarios))

    for idx in range(count):
        create_id = f"create_scenario_{idx}"
        validate_id = f"validate_scenario_{idx}"
        solve_id = f"optimize_scenario_{idx}"
        steps.extend([
            PlanStep(step_id=create_id, capability=CAP_CREATE_SCEN,
                     description=f"Materialise scenario {idx}.",
                     depends_on=["load"], params={"scenario_index": idx}),
            PlanStep(step_id=validate_id, capability=CAP_VALIDATE_SCEN,
                     description=f"Validate scenario {idx}.",
                     depends_on=[create_id], params={"scenario_index": idx}),
            PlanStep(step_id=solve_id, capability=CAP_OPTIMIZE_SCEN,
                     description=f"Re-optimise scenario {idx}.",
                     depends_on=[validate_id, "baseline"],
                     params={"scenario_index": idx}),
        ])
        solve_ids.append(solve_id)

    steps.extend(_reason_and_govern(solve_ids))
    return steps


def _build_resilience_query(_: IntentResolution) -> List[PlanStep]:
    """No scenario solve — the REI registry already answers exposure questions."""
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="rei", capability=CAP_REI,
                 description="Assess and rank facility exposure.",
                 depends_on=["load"]),
        *_reason_and_govern(["rei"]),
    ]


def _build_external_event(_: IntentResolution) -> List[PlanStep]:
    """
    External signal → exposure → RF.

    RF needs both P (from the signal) and REI (from the network), so it waits on
    both; those two are independent of each other and run concurrently.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        # Both are CONTRIBUTING inputs here, not the deliverable: if either is
        # unavailable the honest outcome is RF = NOT_COMPUTABLE reported
        # alongside the surviving severity and exposure analysis, not a failed
        # run. (Contrast the resilience query, where REI *is* the answer and its
        # loss is a genuine failure.)
        PlanStep(step_id="interpret_signal", capability=CAP_INTERPRET_SIG,
                 description="Interpret the external event; derive P only if defensible.",
                 depends_on=["load"], optional=True),
        PlanStep(step_id="rei", capability=CAP_REI,
                 description="Assess network exposure (REI).",
                 depends_on=["load"], optional=True),
        PlanStep(step_id="risk", capability=CAP_RISK,
                 description="Combine P and REI into RF deterministically.",
                 # Both SOFT: RF needs a defensible P and a valid REI, and the
                 # correct answer when either is absent is RF = NOT_COMPUTABLE,
                 # reported explicitly. Blocking here would hide that.
                 depends_on=["interpret_signal", "rei"],
                 soft_depends_on=["interpret_signal", "rei"]),
        *_reason_and_govern(["rei", "risk", "interpret_signal"]),
    ]


def _build_explanation(_: IntentResolution) -> List[PlanStep]:
    """
    "Why is Delhi DC high risk?" — answered from evidence that already exists.

    Deliberately contains NO `optimization.solve` step. An explanation must not
    launch a fresh optimization the user did not ask for; it reads the REI
    registry (served from cache when the network has not materially changed, so
    zero MILP solves) and combines it with any external signal already attached
    to the request.

    The graph is otherwise the external-event graph minus signal interpretation:
    interpretation is an act of gathering NEW external evidence, which an
    explanation query has no mandate to do. A signal supplied WITH the request
    is already on the execution context and is used.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="rei", capability=CAP_REI,
                 description="Retrieve facility exposure (REI), cache-served when valid.",
                 depends_on=["load"], optional=True),
        PlanStep(step_id="risk", capability=CAP_RISK,
                 description="Combine any available P with REI deterministically.",
                 depends_on=["rei"], soft_depends_on=["rei"]),
        *_reason_and_govern(["rei", "risk"]),
    ]


def _build_status(_: IntentResolution) -> List[PlanStep]:
    """
    "How many warehouses do we have?" — answered from the digital twin.

    Contains NO solver step, by construction. A count of facilities does not
    require an optimum, and running one to answer it would burn solver time for
    nothing. Questions that genuinely need cost or service resolve to
    NETWORK_STATE_QUERY instead, which does solve.

    Governance still runs: every response leaves with a verdict.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Read the observed network snapshot."),
        *_reason_and_govern(["load"]),
    ]


def _build_forecast(_: IntentResolution) -> List[PlanStep]:
    """
    "What will demand look like next quarter?" — estimate, and stop there.

    Contains NO solver step, by construction. A demand estimate is not an
    optimisation, and running the MILP to answer a forecast question would burn
    solver time and imply a network decision nobody asked for. Optimising
    against a forecast is a separate act with its own entry point — see
    `Orchestrator.build_forecast_scenario`, which materialises the forecast as
    a hypothetical scenario and lets the ordinary scenario machinery solve it.

    The forecast step is `optional`, so an environment with no forecasting
    capability registered still runs and reports the absence rather than
    failing. That is the same degradation the rest of the plan uses, and it is
    how this workflow behaved before a forecaster existed at all.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Read the observed network snapshot."),
        PlanStep(step_id="forecast", capability=CAP_FORECAST,
                 description="Estimate future demand from observed history.",
                 depends_on=["load"], optional=True),
        *_reason_and_govern(["forecast"]),
    ]


def _build_market_intelligence(_: IntentResolution) -> List[PlanStep]:
    """
    Record a stated market change as context. No solve, and no scenario.

    WHY THIS WORKFLOW DOES SO LITTLE
        A market signal is evidence, not an instruction. It says diesel moved;
        it does not say what this network should do about it. Turning "diesel
        is up 6%" into a re-optimisation would answer a question nobody asked,
        against inputs nobody changed — every lane rate in the snapshot is
        still the contracted rate, so the solve would return the baseline and
        present it as a response to the news.

        Worse, editing the rates first would be a language model changing a
        number the MILP treats as fact. The architecture's governing rule
        forbids exactly that: the engines are the only sources of numeric
        truth.

        So the signal is loaded against the network (so a reader can see which
        facilities and lanes it plausibly touches), SCORED against the
        guardrail policy, explained, and governed. If it warrants a what-if, a
        person asks for one — and that request arrives as SCENARIO_ANALYSIS,
        with the quantity they chose, through the workflow built to govern
        structural change.

    Note what is ALSO absent: no forecast step. A market signal may legitimately
    inform a forecast, but that happens on the forecast workflow, where
    `ExternalSignalRouter` decides whether the signal is eligible. Wiring
    forecasting in here would let a signal reach the Forecasting Agent by
    virtue of having been mentioned, which is the routing decision the
    orchestrator exists to make.

    "score_signal" is OPTIONAL and SOFT-depended-on by reason/govern, the same
    treatment `interpret_signal` gets in `wf_external_event`: a message with no
    signal to score (or a scoring failure) degrades the narrative, it does not
    fail the run.

    Governance still runs. Every response leaves with a verdict.
    """
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Read the observed network snapshot, so the "
                             "signal can be read against real facilities."),
        PlanStep(step_id="score_signal", capability=CAP_SCORE_MARKET,
                 description="Score the reported market change against the "
                             "guardrail policy.",
                 depends_on=["load"], optional=True),
        *_reason_and_govern(["load", "score_signal"]),
    ]


def _build_optimization(_: IntentResolution) -> List[PlanStep]:
    return [
        PlanStep(step_id="load", capability=CAP_LOAD_NETWORK,
                 description="Load the pinned observed network snapshot."),
        PlanStep(step_id="optimize", capability=CAP_OPTIMIZE,
                 description="Optimise the network.", depends_on=["load"],
                 params={"mode": "CURRENT_FOOTPRINT_OPTIMIZATION"}),
        PlanStep(step_id="kpi", capability=CAP_KPI,
                 description="Project KPIs.", depends_on=["optimize"]),
        *_reason_and_govern(["optimize", "kpi"]),
    ]


WORKFLOW_TEMPLATES: Dict[Intent, WorkflowTemplate] = {
    Intent.NETWORK_STATE_QUERY: WorkflowTemplate(
        "wf_network_state", Intent.NETWORK_STATE_QUERY,
        "Evaluate and report the observed network state.", _build_network_state),
    Intent.SCENARIO_ANALYSIS: WorkflowTemplate(
        "wf_scenario_analysis", Intent.SCENARIO_ANALYSIS,
        "Analyse a single hypothetical scenario against the baseline.",
        _build_scenario_analysis),
    Intent.SCENARIO_COMPARISON: WorkflowTemplate(
        "wf_scenario_comparison", Intent.SCENARIO_COMPARISON,
        "Compare independent scenarios against one baseline.",
        _build_scenario_comparison),
    Intent.RESILIENCE_QUERY: WorkflowTemplate(
        "wf_resilience_query", Intent.RESILIENCE_QUERY,
        "Rank facilities by relative economic exposure.", _build_resilience_query),
    Intent.EXTERNAL_EVENT: WorkflowTemplate(
        "wf_external_event", Intent.EXTERNAL_EVENT,
        "Assess an external event: likelihood, exposure, combined risk.",
        _build_external_event),
    Intent.OPTIMIZATION_REQUEST: WorkflowTemplate(
        "wf_optimization", Intent.OPTIMIZATION_REQUEST,
        "Optimise the network within the current footprint.", _build_optimization),
    Intent.EXPLANATION: WorkflowTemplate(
        "wf_explanation", Intent.EXPLANATION,
        "Explain an existing result from evidence already computed; no new optimization.",
        _build_explanation),
    Intent.STATUS_QUERY: WorkflowTemplate(
        "wf_status", Intent.STATUS_QUERY,
        "Answer an inventory/count question from the digital twin; no solve.",
        _build_status),
    Intent.MARKET_INTELLIGENCE: WorkflowTemplate(
        "wf_market_intelligence", Intent.MARKET_INTELLIGENCE,
        "Record a stated market change as context; no solve, no scenario.",
        _build_market_intelligence),
    Intent.FORECAST: WorkflowTemplate(
        "wf_forecast", Intent.FORECAST,
        "Estimate future demand from observed history; no solve.",
        _build_forecast),
}


class WorkflowPlanner:
    """Builds and validates execution plans from resolved intents."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def plan(self, resolution: IntentResolution) -> ExecutionPlan:
        """
        Build a validated plan for an intent.

        Raises:
            PlanningFailureError: no template for the intent, the graph is
                malformed, or it references an unregistered capability.
        """
        template = WORKFLOW_TEMPLATES.get(resolution.intent)
        if template is None:
            raise PlanningFailureError(
                f"No workflow is registered for intent '{resolution.intent.value}'. "
                f"Available: {sorted(i.value for i in WORKFLOW_TEMPLATES)}",
                context={"intent": resolution.intent.value},
            )

        steps = template.build(resolution)
        plan = ExecutionPlan(
            workflow_id=template.workflow_id,
            intent=resolution.intent.value,
            steps=steps,
            description=template.description,
        )

        plan.validate_dag()
        self.registry.validate_plan_capabilities({s.capability for s in plan.steps})

        # Drop optional steps whose capability is not registered, so an
        # environment without (say) a reasoning agent still runs.
        retained: List[PlanStep] = []
        for step in plan.steps:
            if step.optional and not self.registry.has(step.capability):
                logger.info(
                    "orchestrator.plan.optional_step_dropped step=%s capability=%s",
                    step.step_id, step.capability,
                )
                continue
            retained.append(step)
        if len(retained) != len(plan.steps):
            dropped = {s.step_id for s in plan.steps} - {s.step_id for s in retained}
            for step in retained:
                step.depends_on = [d for d in step.depends_on if d not in dropped]
            plan.steps = retained
            plan.validate_dag()

        logger.info(
            "orchestrator.plan.built workflow=%s intent=%s steps=%d layers=%s",
            plan.workflow_id, plan.intent, len(plan.steps),
            [len(layer) for layer in plan.execution_layers()],
        )
        return plan

    @staticmethod
    def available_workflows() -> List[Dict[str, str]]:
        return [
            {"workflow_id": t.workflow_id, "intent": t.intent.value,
             "description": t.description}
            for t in WORKFLOW_TEMPLATES.values()
        ]
