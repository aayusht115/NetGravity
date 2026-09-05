"""
Orchestrator — Offline and Live LLM Planner Layer.

Provides:
  1. `LLMPlannerProtocol`: The shared typed interface for all LLM planners.
  2. `MockPlanner`: Deterministic, offline mock planner for local testing
     with 14 mock scenarios and zero external API calls.
  3. `LiveLLMPlanner`: Production-ready planner interfacing with the existing
     `LLMGateway` for tomorrow's live validation, with a HARD limit of 15 calls.

BOUNDARY PRINCIPLES
───────────────────
- Planners only PROPOSE plans (`PlanProposal`).
- Planners NEVER execute capabilities, compute domain numbers, or mutate state.
- Every proposed plan must pass through `PlanValidator` before execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from netgravity.orchestrator.agents.llm_gateway import LLMGateway, extract_json
from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
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
    CAP_SCORE_MARKET,
    CAP_VALIDATE_SCEN,
)
from netgravity.orchestrator.exceptions import LLMFailureError, LLMNonRetryableError
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.plan_validation import PlanOrigin
from netgravity.orchestrator.schemas.planner_contract import (
    PlanProposal,
    ProposedPlanStep,
)
from netgravity.orchestrator.schemas.requests import (
    Intent,
    IntentResolution,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

MAX_LIVE_PLANNER_CALLS = 15

#: Capabilities whose failure DEGRADES an answer rather than destroying it.
#:
#: Taken from the deterministic templates in `orchestrator/core/planner.py`,
#: which have always marked exactly these `optional=True`: they enrich an
#: analysis, they are not the analysis. `_settle` already honours the flag —
#: "Optional steps failing is degradation, not failure" — but a proposed step
#: defaults to `optional=False`, and the model never set it. So one enrichment
#: step failing HONESTLY ("no observed demand history is available for this
#: network, so no forecast can be produced") took the whole execution to FAILED
#: and returned "The analysis produced no narrative result", discarding the
#: resilience, risk, optimization, KPI and governance results that had all
#: succeeded beside it.
#:
#: Applied in code rather than asked for in the prompt, and applied in BOTH
#: directions: a model may not promote an enrichment step to essential, and may
#: not demote a core step to optional. What runs is the model's proposal; what
#: is load-bearing is the system's policy.
ENRICHMENT_CAPABILITIES = frozenset({
    "reasoning.synthesise",
    "forecast.demand",
    "market.score_signal",
    "external.interpret_signal",
})


def _salvage_truncated_plan(raw_output: str) -> Optional[Dict[str, Any]]:
    """
    Recover the completed steps from a reply the model ran out of room to finish.

    The gateway caps output tokens, so a long plan can arrive cut off mid-object:

        {"workflow_id":"wf_1","steps":[{"step_id":"s1","capability":"a"},
         {"step_id":"s2","capa

    Every step before the cut is intact and usable. Discarding all of them —
    which is what a plain `json.loads` failure does — throws away a valid plan
    because its tail is missing, and the shorter plan is still put to the
    deterministic validator before anything runs.

    Nothing is invented: the last, incomplete object is DROPPED rather than
    guessed at, and a step whose `capability` did not survive the cut is not a
    step. Returns None when nothing whole can be recovered.
    """
    if not raw_output:
        return None
    # The OUTERMOST brace. Seeking `{"` instead finds the first step object,
    # which puts the `"steps"` key behind the cursor and loses the array.
    start = raw_output.find("{")
    if start == -1:
        return None
    body = raw_output[start:]

    # Every complete {...} object inside the steps array, in order.
    steps_at = body.find('"steps"')
    if steps_at == -1:
        return None
    array_at = body.find("[", steps_at)
    if array_at == -1:
        return None

    steps: List[Dict[str, Any]] = []
    depth, obj_start, in_string, escaped = 0, None, False, False
    for i in range(array_at + 1, len(body)):
        ch = body[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start is not None:
                try:
                    step = json.loads(body[obj_start:i + 1])
                except ValueError:
                    obj_start = None
                    continue
                if isinstance(step, dict) and step.get("capability"):
                    steps.append(step)
                obj_start = None
        elif ch == "]" and depth == 0:
            break

    if not steps:
        return None

    workflow_id = "wf_live_proposed"
    match = re.search(r'"workflow_id"\s*:\s*"([^"]{1,80})"', body)
    if match:
        workflow_id = match.group(1)

    logger.warning(
        "orchestrator.llm_planner.salvaged_truncated_plan steps=%d chars=%d",
        len(steps), len(raw_output),
    )
    return {
        "workflow_id": workflow_id,
        "steps": steps,
        "reasoning": "Recovered from a truncated model reply; "
                     "incomplete trailing step discarded.",
    }


@runtime_checkable
class LLMPlannerProtocol(Protocol):
    """The narrow provider interface for plan proposal generation."""

    async def propose_plan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        """Propose an execution plan for the resolved intent."""
        ...

    async def propose_replan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Any,
        trigger_reason: str = "",
    ) -> PlanProposal:
        """Propose a replanned execution plan following a dynamic trigger."""
        ...

    def propose_plan_sync(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        """Synchronous wrapper for plan proposal."""
        ...


class MockPlanner:
    """
    Deterministic, offline mock planner for local verification.

    Guarantees:
      - 0 external API calls
      - 100% deterministic outputs for identical inputs
      - Support for 14 required test scenarios
    """

    def __init__(
        self,
        registry: Optional[CapabilityRegistry] = None,
        simulated_scenario: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.simulated_scenario = simulated_scenario
        self.call_count = 0

    def set_scenario(self, scenario: Optional[str]) -> None:
        """Dynamically configure mock scenario for adversarial testing."""
        self.simulated_scenario = scenario

    def propose_plan_sync(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        return asyncio.run(self.propose_plan(request, resolution, context))

    async def propose_plan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        self.call_count += 1
        scenario = self.simulated_scenario or self._infer_scenario(request, resolution)

        logger.info(
            "orchestrator.mock_planner.proposing scenario=%s intent=%s call_count=%d",
            scenario, resolution.intent.value, self.call_count,
        )

        # ------------------------------------------------------------------
        # Adversarial / Error Simulation Scenarios
        # ------------------------------------------------------------------
        if scenario == "LLM_UNAVAILABLE":
            raise LLMNonRetryableError("Simulated offline LLM gateway — unavailable.")

        if scenario == "RETRYABLE_PLANNER_FAILURE":
            raise LLMFailureError("Simulated transient rate limit on LLM planner (HTTP 429).")

        if scenario == "MALFORMED_PLAN":
            # Return proposal with illegal blank step fields
            return PlanProposal(
                workflow_id="wf_malformed",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="", capability=""),
                ],
                reasoning="Malformed proposal for testing.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "EMPTY_PLAN":
            return PlanProposal(
                workflow_id="wf_empty",
                intent=resolution.intent.value,
                steps=[],
                reasoning="Empty proposal for testing.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "INVALID_CAPABILITY":
            return PlanProposal(
                workflow_id="wf_invalid_cap",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="s1", capability="nonexistent.magic_solver"),
                ],
                reasoning="Proposal containing unknown capability.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "INVALID_DEPENDENCY":
            return PlanProposal(
                workflow_id="wf_cyclic",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="step_a", capability=CAP_LOAD_NETWORK, depends_on=["step_b"]),
                    ProposedPlanStep(step_id="step_b", capability=CAP_OPTIMIZE, depends_on=["step_a"]),
                ],
                reasoning="Proposal containing cyclic dependency.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "FORBIDDEN_RF_PATH":
            # Attempt to run risk.compute_rf directly without prerequisites
            return PlanProposal(
                workflow_id="wf_forbidden_rf",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="rf_step", capability=CAP_RISK),
                ],
                reasoning="Proposal attempting unvalidated direct RF calculation.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "NON_PLANNABLE_CAPABILITY":
            return PlanProposal(
                workflow_id="wf_non_plannable",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="twin_step", capability="twin.publish"),
                ],
                reasoning="Proposal scheduling non-plannable twin service.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "FULL_NETWORK_IMPACT":
            return PlanProposal(
                workflow_id="wf_full_impact_proposed",
                intent=Intent.SCENARIO_ANALYSIS.value,
                steps=[
                    ProposedPlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                    ProposedPlanStep(step_id="score_signal", capability=CAP_SCORE_MARKET, depends_on=["load"], optional=True),
                    ProposedPlanStep(step_id="forecast", capability=CAP_FORECAST, depends_on=["load"], optional=True),
                    ProposedPlanStep(step_id="baseline", capability=CAP_OPTIMIZE, depends_on=["load"]),
                    ProposedPlanStep(step_id="create_scenario", capability=CAP_CREATE_SCEN, depends_on=["load"], params={"scenario_index": 0}),
                    ProposedPlanStep(step_id="validate_scenario", capability=CAP_VALIDATE_SCEN, depends_on=["create_scenario"]),
                    ProposedPlanStep(step_id="optimize_scenario", capability=CAP_OPTIMIZE_SCEN, depends_on=["validate_scenario"]),
                    ProposedPlanStep(step_id="resilience", capability=CAP_REI, depends_on=["optimize_scenario"], optional=True),
                    ProposedPlanStep(step_id="kpi", capability=CAP_KPI, depends_on=["optimize_scenario"]),
                    ProposedPlanStep(step_id="reason", capability=CAP_REASON, depends_on=["baseline", "optimize_scenario", "kpi", "resilience"], soft_depends_on=["baseline", "optimize_scenario", "kpi", "resilience"], optional=True),
                    ProposedPlanStep(step_id="govern", capability=CAP_GOVERN, depends_on=["baseline", "optimize_scenario", "kpi", "resilience", "reason"], soft_depends_on=["baseline", "optimize_scenario", "kpi", "resilience", "reason"]),
                ],
                reasoning="Propose multi-capability full network impact analysis.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        # ------------------------------------------------------------------
        # Functional Valid Scenarios (using registered templates)
        # ------------------------------------------------------------------
        from netgravity.orchestrator.core.planner import WORKFLOW_TEMPLATES
        template = WORKFLOW_TEMPLATES.get(resolution.intent)
        if template is not None:
            template_steps = template.build(resolution)
            proposed_steps = [
                ProposedPlanStep(
                    step_id=s.step_id,
                    capability=s.capability,
                    description=s.description,
                    depends_on=list(s.depends_on),
                    soft_depends_on=list(s.soft_depends_on),
                    params=dict(s.params),
                    optional=s.optional,
                )
                for s in template_steps
            ]
            return PlanProposal(
                workflow_id=template.workflow_id,
                intent=resolution.intent.value,
                steps=proposed_steps,
                reasoning=f"Propose standard graph structure for intent {resolution.intent.value}.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        # Default fallback proposal
        return PlanProposal(
            workflow_id="wf_default_proposed",
            intent=resolution.intent.value,
            steps=[
                ProposedPlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                ProposedPlanStep(step_id="reason", capability=CAP_REASON, depends_on=["load"], soft_depends_on=["load"], optional=True),
                ProposedPlanStep(step_id="govern", capability=CAP_GOVERN, depends_on=["load", "reason"], soft_depends_on=["load", "reason"]),
            ],
            reasoning="Default minimal proposal.",
            planner_source=PlanOrigin.MOCK_LLM,
        )

    async def propose_replan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Any,
        trigger_reason: str = "",
    ) -> PlanProposal:
        """Propose an adapted plan in response to a dynamic runtime trigger."""
        self.call_count += 1
        scenario = self.simulated_scenario

        if scenario == "REPLAN_REPEATED_CYCLE":
            # Return identical proposal to trigger cycle guard
            return PlanProposal(
                workflow_id="wf_cycle_attempt",
                intent=resolution.intent.value,
                steps=[
                    ProposedPlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                    ProposedPlanStep(step_id="forecast", capability=CAP_FORECAST, depends_on=["load"]),
                    ProposedPlanStep(step_id="reason", capability=CAP_REASON, depends_on=["forecast"], soft_depends_on=["forecast"]),
                    ProposedPlanStep(step_id="govern", capability=CAP_GOVERN, depends_on=["forecast", "reason"], soft_depends_on=["forecast", "reason"]),
                ],
                reasoning="Proposal identical to initial plan to verify cycle detection.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        if scenario == "REPLAN_MALFORMED":
            return PlanProposal(
                workflow_id="wf_replan_malformed",
                intent=resolution.intent.value,
                steps=[ProposedPlanStep(step_id="", capability="")],
                reasoning="Malformed replan proposal for fallback testing.",
                planner_source=PlanOrigin.MOCK_LLM,
            )

        # Default material demand expansion replan
        return PlanProposal(
            workflow_id="wf_replanned_material_demand",
            intent=resolution.intent.value,
            steps=[
                ProposedPlanStep(step_id="load", capability=CAP_LOAD_NETWORK),
                ProposedPlanStep(step_id="forecast", capability=CAP_FORECAST, depends_on=["load"]),
                ProposedPlanStep(step_id="optimize", capability=CAP_OPTIMIZE, depends_on=["load"]),
                ProposedPlanStep(step_id="resilience", capability=CAP_REI, depends_on=["load"], optional=True),
                ProposedPlanStep(step_id="kpi", capability=CAP_KPI, depends_on=["optimize"]),
                ProposedPlanStep(step_id="reason", capability=CAP_REASON, depends_on=["optimize", "kpi", "resilience"], soft_depends_on=["optimize", "kpi", "resilience"], optional=True),
                ProposedPlanStep(step_id="govern", capability=CAP_GOVERN, depends_on=["optimize", "kpi", "resilience", "reason"], soft_depends_on=["optimize", "kpi", "resilience", "reason"]),
            ],
            reasoning=f"Adaptive replan proposal for trigger: {trigger_reason}",
            planner_source=PlanOrigin.MOCK_LLM,
        )

    def _infer_scenario(self, request: OrchestratorRequest, resolution: IntentResolution) -> str:
        text = (request.input or "").lower()
        if "expansion" in text and "recommend" in text:
            return "FULL_NETWORK_IMPACT"
        if "current state" in text or "what does the network look like" in text:
            return "NETWORK_STATE"
        if "forecast" in text:
            return "FORECAST"
        if "what happens if" in text or "increase" in text or "close" in text:
            if "fail" in text or "disrupt" in text or "exposed" in text:
                return "RESILIENCE"
            return "SCENARIO_ANALYSIS"
        return resolution.intent.value


class LiveLLMPlanner:
    """
    Live OpenAI / Text Gateway planner for tomorrow's validation.

    Guarantees:
      - Uses existing LLMGateway without creating a second client
      - Enforces a HARD max limit of 15 calls
      - Validates output structure defensively
    """

    def __init__(
        self,
        gateway: LLMGateway,
        registry: CapabilityRegistry,
        max_calls: int = MAX_LIVE_PLANNER_CALLS,
    ) -> None:
        self.gateway = gateway
        self.registry = registry
        self.max_calls = max_calls
        self.calls_attempted = 0
        self.calls_successful = 0
        self.calls_failed = 0

    def propose_plan_sync(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        return asyncio.run(self.propose_plan(request, resolution, context))

    async def propose_plan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Optional[Any] = None,
    ) -> PlanProposal:
        if self.calls_attempted >= self.max_calls:
            raise LLMNonRetryableError(
                f"Live planner call quota exhausted: reached maximum of {self.max_calls} calls."
            )

        if not self.gateway.available:
            raise LLMNonRetryableError(
                f"LLM Gateway is not available: {self.gateway.unavailable_reason()}"
            )

        prompt = self._build_prompt(request, resolution)
        self.calls_attempted += 1

        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.gateway.generate(prompt, purpose="live_plan_proposal")
            )
            self.calls_successful += 1
        except Exception as exc:
            self.calls_failed += 1
            raise LLMFailureError(f"Live planner request failed: {exc}") from exc

        return self._parse_response(resp.output, resolution)

    async def propose_replan(
        self,
        request: OrchestratorRequest,
        resolution: IntentResolution,
        context: Any,
        trigger_reason: str = "",
    ) -> PlanProposal:
        """Propose an adapted plan in response to a dynamic runtime trigger."""
        if self.calls_attempted >= self.max_calls:
            raise LLMNonRetryableError(
                f"Phase 8.5/8.6 hard call limit of {self.max_calls} reached ({self.calls_attempted} attempted)."
            )
        if not self.gateway.available:
            raise LLMNonRetryableError(
                f"LLM Gateway is not available: {self.gateway.unavailable_reason()}"
            )

        prompt = (
            f"{self._build_prompt(request, resolution)}\n\n"
            f"DYNAMIC REPLAN TRIGGER:\n"
            f"{trigger_reason}\n"
            f"Propose an updated capability graph taking into account the observed findings above."
        )
        self.calls_attempted += 1

        try:
            resp = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self.gateway.generate(prompt, purpose="live_replan_proposal")
            )
            self.calls_successful += 1
        except Exception as exc:
            self.calls_failed += 1
            raise LLMFailureError(f"Live planner replan request failed: {exc}") from exc

        return self._parse_response(resp.output, resolution)

    def _build_prompt(self, request: OrchestratorRequest, resolution: IntentResolution) -> str:
        """
        Ask for the smallest plan that is still a plan.

        Why this prompt is terse, measured rather than assumed
        ─────────────────────────────────────────────────────
        The gateway caps OUTPUT at 2,000 tokens and the backing model bills its
        own internal reasoning to that same allowance — the constraint already
        documented on `ReasoningAgent._llm`, which hit it and was rewritten.
        This prompt was never given the same treatment, and it sat just inside
        the limit: on a simple request it returned 1,015 output tokens and
        parsed; across the five requests of the Phase 8.8 end-to-end run it
        truncated every time, mid-string, in the middle of a `description`:

            "description": "Pin and verify the current obse

        Truncated JSON does not parse, so `propose_plan` raised, and every
        execution fell back to the deterministic planner. The LLM planner had
        therefore never once produced a plan — and nothing said so, because
        falling back is the designed behaviour and it is silent.

        Two changes, each measured on the same request:

          * `description` is not requested. Nothing consumes
            `ProposedPlanStep.description` — it defaults to "" and no caller
            reads it — so it was pure output budget spent on prose, and it was
            the field truncation landed in.
          * the capability list is ids and one line each, not an indented JSON
            dump of the full contracts.

        Same request, same model: 1,015 output tokens -> 539, and the reply is
        a complete five-step graph. Halving the output is what moves the call
        off the edge of the cap, which is the actual failure.

        `params` stays, because the validator inspects it
        (`ProposedPlanStep.forbid_domain_calculations_in_params`) and a step may
        legitimately carry a facility id. It is declared optional so the model
        omits it when empty rather than emitting `"params": {}` thirteen times.
        """
        # Only capabilities this request can actually support.
        #
        # `scenario.create` materialises a hypothetical network FROM a concrete
        # specification — which facility, what change — and that specification
        # can only come from the user. Offered unconditionally, the model
        # planned it for any request that sounded like a change:
        #
        #   "A major disruption is affecting Mumbai. Assess the impact..."
        #
        # names no scenario, so the step failed MISSING_DATA ("the request did
        # not describe a concrete scenario"), and the whole execution settled
        # FAILED with "The analysis produced no narrative result" — throwing
        # away the resilience and risk assessments that had already succeeded.
        #
        # Whether a specification exists is a fact about the resolved intent,
        # not a judgement, so it is read rather than guessed. When there is
        # none, the three scenario capabilities are withheld and the reason is
        # stated, so the model plans the analysis it CAN run instead of one it
        # cannot.
        has_scenario_spec = bool(getattr(resolution, "scenarios", None))
        withheld = set() if has_scenario_spec else {
            "scenario.create", "scenario.validate", "optimization.solve_scenario",
        }
        capabilities = "\n".join(
            f"- {c.capability_id}"
            + (f" (needs: {', '.join(c.dependencies)})" if c.dependencies else "")
            for c in self.registry.contracts()
            if c.is_plannable and c.capability_id not in withheld
        )
        scenario_note = "" if has_scenario_spec else (
            "\nThis request names no concrete scenario, so no hypothetical "
            "network can be built from it. Plan the analysis of the OBSERVED "
            "network only.\n"
        )
        return (
            "You are the NetGravity Orchestrator plan proposer.\n"
            "Output ONLY compact JSON. No markdown, no code fences, no preamble, "
            "no explanation outside the JSON.\n"
            "Schema (omit optional keys when empty):\n"
            '{"workflow_id":"str","steps":['
            '{"step_id":"s1","capability":"<id from the list>",'
            '"depends_on":["s0"],"params":{},"optional":false}'
            '],"reasoning":"<=15 words"}\n"'
            "Rules:\n"
            "- every `capability` MUST be copied exactly from the list below;\n"
            "- `depends_on` names earlier step_ids only;\n"
            "- do not invent capabilities, and do not compute or assert any "
            "figure — steps name work to run, nothing more.\n"
            f"{scenario_note}\n"
            f"Capabilities:\n{capabilities}\n\n"
            f"Request: {request.input}\n"
            f"Intent: {resolution.intent.value}\n"
        )

    def _parse_response(self, raw_output: str, resolution: IntentResolution) -> PlanProposal:
        payload = extract_json(raw_output)
        if not payload or not isinstance(payload, dict):
            repaired = raw_output.strip()
            for fix in ('"}', '"]}', '"]}}', '}'):
                try:
                    candidate = extract_json(repaired + fix)
                    if candidate and isinstance(candidate, dict) and "steps" in candidate:
                        payload = candidate
                        break
                except Exception:
                    pass

        if not payload or not isinstance(payload, dict):
            payload = _salvage_truncated_plan(raw_output)

        if not payload or not isinstance(payload, dict):
            raise LLMFailureError(f"Live planner returned unparseable output: {raw_output[:200]}")

        raw_steps = payload.get("steps", [])
        steps: List[ProposedPlanStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            capability = str(s.get("capability", ""))
            steps.append(
                ProposedPlanStep(
                    step_id=str(s.get("step_id", "")),
                    capability=capability,
                    description=str(s.get("description", "")),
                    depends_on=list(s.get("depends_on", [])),
                    soft_depends_on=list(s.get("soft_depends_on", [])),
                    params=dict(s.get("params", {})),
                    # Decided by the capability, not by the model — see
                    # ENRICHMENT_CAPABILITIES.
                    optional=capability in ENRICHMENT_CAPABILITIES,
                )
            )

        return PlanProposal(
            workflow_id=payload.get("workflow_id", "wf_live_proposed"),
            intent=resolution.intent.value,
            steps=steps,
            reasoning=payload.get("reasoning", "Live OpenAI generated proposal"),
            planner_source=PlanOrigin.LLM,
            raw_model_output=raw_output,
        )
