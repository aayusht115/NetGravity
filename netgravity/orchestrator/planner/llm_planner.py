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

    def _build_prompt(self, request: OrchestratorRequest, resolution: IntentResolution) -> str:
        capabilities = [
            {"id": c.capability_id, "description": c.description, "dependencies": list(c.dependencies)}
            for c in self.registry.contracts()
            if c.is_plannable
        ]
        return (
            "You are the NetGravity Orchestrator Plan Proposal Agent.\n"
            "Your role is to propose an execution graph of capabilities to answer the user request.\n"
            "You MUST output valid JSON conforming to the following structure:\n"
            "{\n"
            '  "workflow_id": "string",\n'
            '  "reasoning": "string",\n'
            '  "steps": [\n'
            '    {\n'
            '      "step_id": "string",\n'
            '      "capability": "capability_id",\n'
            '      "description": "string",\n'
            '      "depends_on": ["step_id"],\n'
            '      "soft_depends_on": [],\n'
            '      "params": {},\n'
            '      "optional": false\n'
            '    }\n'
            '  ]\n'
            "}\n\n"
            f"Available Capabilities:\n{json.dumps(capabilities, indent=2)}\n\n"
            f"User Request: {request.input}\n"
            f"Resolved Intent: {resolution.intent.value}\n"
        )

    def _parse_response(self, raw_output: str, resolution: IntentResolution) -> PlanProposal:
        payload = extract_json(raw_output)
        if not payload or not isinstance(payload, dict):
            raise LLMFailureError(f"Live planner returned unparseable output: {raw_output[:200]}")

        raw_steps = payload.get("steps", [])
        steps: List[ProposedPlanStep] = []
        for s in raw_steps:
            if not isinstance(s, dict):
                continue
            steps.append(
                ProposedPlanStep(
                    step_id=str(s.get("step_id", "")),
                    capability=str(s.get("capability", "")),
                    description=str(s.get("description", "")),
                    depends_on=list(s.get("depends_on", [])),
                    soft_depends_on=list(s.get("soft_depends_on", [])),
                    params=dict(s.get("params", {})),
                    optional=bool(s.get("optional", False)),
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
