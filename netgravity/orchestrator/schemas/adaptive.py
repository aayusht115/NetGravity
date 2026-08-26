"""
Orchestrator — Adaptive execution schemas, observation models, and decision contracts.

Phase 8.6 establishes the adaptive closed-loop orchestration layer above the
existing CapabilityExecutor and FailureManager.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from netgravity.orchestrator.schemas.plans import AgentStatus
from netgravity.orchestrator.schemas.planner_contract import PlanProposal
from netgravity.orchestrator.schemas.recovery import EscalationOutcome


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AdaptiveAction(str, Enum):
    """
    Action determined by the deterministic AdaptiveDecisionPolicy after observing a step result.

    CONTINUE    Proceed to the next scheduled step in the active approved plan.
    REPLAN      Domain finding or missing analysis requires dynamic plan restructuring.
    RETRY       Transient failure; re-execute capability (handled via FailureManager).
    REROUTE     Primary capability failed; execute alternative (handled via FailureManager).
    BLOCK       Hard dependency failed; prevent downstream execution.
    ESCALATE    Unrecoverable failure or loop guard tripped; escalate to operator.
    TERMINATE   Workflow completed early or safely halted at terminal condition.
    """
    CONTINUE  = "CONTINUE"
    REPLAN    = "REPLAN"
    RETRY     = "RETRY"
    REROUTE   = "REROUTE"
    BLOCK     = "BLOCK"
    ESCALATE  = "ESCALATE"
    TERMINATE = "TERMINATE"


class ResultObservation(BaseModel):
    """
    Structured, deterministic interpretation of one step's execution result.

    Inspects typed AgentResult fields and ExecutionContext evidence without
    inferring numbers from unstructured text.
    """
    step_id: str
    capability: str
    status: AgentStatus
    is_usable: bool
    domain_outcome: str = "STANDARD_SUCCESS"
    summary: str = ""
    requires_replanning: bool = False
    requires_human_escalation: bool = False
    skip_downstream: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=_utc_now)

    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveDecision(BaseModel):
    """
    Policy determination made by AdaptiveDecisionPolicy for the next orchestration step.
    """
    action: AdaptiveAction
    reason: str
    step_id: Optional[str] = None
    capability: Optional[str] = None
    replan_proposal: Optional[PlanProposal] = None
    escalation: Optional[EscalationOutcome] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=_utc_now)

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplanRecord(BaseModel):
    """
    Chronological, immutable record of one plan restructuring event.
    """
    replan_index: int
    trigger_step_id: str
    trigger_capability: str
    trigger_reason: str
    previous_plan_id: str
    new_plan_id: str
    plan_signature: str
    approved: bool
    timestamp: str = Field(default_factory=_utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveExecutionConfig(BaseModel):
    """
    Configurable guardrails and thresholds for adaptive closed-loop execution.
    """
    max_execution_steps: int = 25
    max_replans: int = 3
    enable_materiality_branching: bool = False
    material_forecast_threshold: float = 0.15  # >=15% demand increase is material

    model_config = ConfigDict(extra="forbid", frozen=True)
