"""
Orchestrator — LLM Plan Proposal Contract.

Defines the typed boundary between the LLM planner and the deterministic
orchestration layer.

BOUNDARY PRINCIPLES
───────────────────
1. The LLM planner proposes WHAT TO RUN (capability selection, dependency ordering).
2. It NEVER computes or dictates WHAT THE SYSTEM FOUND (authoritative numbers,
   costs, REI, RF, MILP solutions, forecast predictions).
3. Every proposal is an unvalidated proposition until approved by `PlanValidator`.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from netgravity.orchestrator.schemas.plan_validation import PlanOrigin
from netgravity.orchestrator.schemas.plans import ExecutionPlan, PlanStep

#: Prohibited domain outcome keys that a planner proposal must NEVER contain.
FORBIDDEN_DOMAIN_KEYS = frozenset({
    "business_network_cost",
    "total_cost",
    "transport_cost",
    "facility_cost",
    "holding_cost",
    "shortage_cost",
    "rei",
    "resilience_exposure_index",
    "risk_factor",
    "rf",
    "milp_solution",
    "optimal_flow",
    "forecast_values",
    "governance_verdict",
})


class ProposedPlanStep(BaseModel):
    """A proposed execution step from an LLM planner."""
    step_id: str
    capability: str
    description: str = ""
    depends_on: List[str] = Field(default_factory=list)
    soft_depends_on: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)
    optional: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def forbid_domain_calculations_in_params(self) -> ProposedPlanStep:
        lowered_params = {k.lower() for k in self.params}
        forbidden = lowered_params & FORBIDDEN_DOMAIN_KEYS
        if forbidden:
            raise ValueError(
                f"Proposed step '{self.step_id}' contains domain calculation output {sorted(forbidden)} in params. "
                f"The LLM planner may only propose parameters and graph structure, never calculation findings."
            )
        return self


class PlanProposal(BaseModel):
    """
    A structured plan proposed by an LLM (or mock) planner.

    This proposal is unvalidated until passed through `PlanValidator.validate()`.
    """
    proposal_id: str = Field(default_factory=lambda: f"prop_{uuid.uuid4().hex[:12]}")
    workflow_id: Optional[str] = None
    intent: Optional[str] = None
    steps: List[ProposedPlanStep] = Field(default_factory=list)
    reasoning: str = ""
    planner_source: PlanOrigin = PlanOrigin.MOCK_LLM
    confidence: float = 1.0
    raw_model_output: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def validate_proposal_safety(self) -> PlanProposal:
        # Verify no steps contain unauthorized calculation results
        for step in self.steps:
            if not step.step_id or not step.capability:
                raise ValueError("Every proposed step must define both step_id and capability.")
        return self

    @property
    def capabilities(self) -> List[str]:
        return [s.capability for s in self.steps]


def plan_proposal_to_execution_plan(
    proposal: PlanProposal,
    origin: Optional[PlanOrigin] = None,
) -> ExecutionPlan:
    """
    Convert an unvalidated PlanProposal into a typed ExecutionPlan for validation.

    Does NOT validate the plan — that is the explicit responsibility of
    `PlanValidator.assert_valid()` or `PlanValidator.validate()`.
    """
    steps = []
    for s in proposal.steps:
        # If soft_depends_on contains items not in depends_on, ensure they are in depends_on
        deps = list(s.depends_on)
        for sd in s.soft_depends_on:
            if sd not in deps:
                deps.append(sd)
        soft_deps = [sd for sd in s.soft_depends_on if sd in deps]

        steps.append(
            PlanStep(
                step_id=s.step_id,
                capability=s.capability,
                description=s.description,
                depends_on=deps,
                soft_depends_on=soft_deps,
                params=dict(s.params),
                optional=s.optional,
            )
        )
    return ExecutionPlan(
        plan_id=proposal.proposal_id,
        workflow_id=proposal.workflow_id or "wf_proposed",
        intent=proposal.intent or "PROPOSED",
        steps=steps,
        description=proposal.reasoning,
        origin=origin or proposal.planner_source,
    )
