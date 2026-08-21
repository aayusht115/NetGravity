"""
Orchestrator — Governance, action and response schemas.

Governance is deterministic and configurable. A language model may *propose* an
action; only these rules decide whether it can be taken, and only the
authorization layer decides whether this actor may take it.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from netgravity.orchestrator.schemas.risk import ReasoningResult


class ActionType(str, Enum):
    """
    What the run is proposing to do.

    Structural reversibility is the key axis. Analysis is free; closing a
    facility is not, regardless of how favourable the numbers look.
    """
    NONE              = "NONE"                # pure analysis, nothing proposed
    REPORT            = "REPORT"              # produce a report/recommendation
    CREATE_SCENARIO   = "CREATE_SCENARIO"     # hypothetical only, never touches observed state
    REROUTE_FLOW      = "REROUTE_FLOW"        # operational, reversible
    CHANGE_CAPACITY   = "CHANGE_CAPACITY"     # tactical, semi-reversible
    OPEN_FACILITY     = "OPEN_FACILITY"       # structural, capital
    CLOSE_FACILITY    = "CLOSE_FACILITY"      # structural, effectively irreversible


class ActionClassification(str, Enum):
    """The governance verdict."""
    AUTO_ACTION       = "AUTO_ACTION"         # may execute without a human
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"   # needs an approver
    HUMAN_ONLY        = "HUMAN_ONLY"          # a person must decide and act
    NO_ACTION         = "NO_ACTION"           # nothing to do


class GovernanceDecision(BaseModel):
    """
    Deterministic authorisation verdict, with the rule that produced it.

    `reason` and `triggered_rules` exist so a decision can always be explained
    and reproduced — "why did NetGravity require approval here?" must have a
    concrete answer.
    """
    action_type: ActionType = ActionType.NONE
    classification: ActionClassification = ActionClassification.NO_ACTION
    reason: str = ""
    triggered_rules: List[str] = Field(default_factory=list)
    #: The single rule that settled this verdict. `triggered_rules` records
    #: everything that fired along the way; this names the one that decided.
    governing_rule: Optional[str] = None

    # Evidence the rules were evaluated against (recorded for audit).
    evaluated: Dict[str, Any] = Field(default_factory=dict)

    #: True when autonomous execution was withheld because required risk
    #: evidence was unavailable, stale or ungrounded — NOT because measured risk
    #: was high. The two are different findings and must not be conflated: the
    #: first says "we could not establish the facts", the second says "we did,
    #: and they are alarming".
    blocked_by_missing_evidence: bool = False
    #: Evidence key → state (UNAVAILABLE / FAILED / STALE / NOT_COMPUTABLE /
    #: GROUNDING_FAILED). Populated whenever required evidence is unresolved.
    evidence_status: Dict[str, str] = Field(default_factory=dict)

    requires_approval: bool = False
    approval_request_id: Optional[str] = None
    # Roles permitted to approve, when approval is required.
    eligible_approver_roles: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


class ApprovalStatus(str, Enum):
    PENDING  = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED  = "EXPIRED"


class ApprovalRequest(BaseModel):
    """
    A pending human decision.

    Pins the exact execution, scenario and snapshot it refers to, so approving
    later cannot silently apply to different data than was reviewed.
    """
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    execution_id: str
    action_type: ActionType
    classification: ActionClassification
    summary: str = ""

    # Immutable context — the approval is only valid for exactly this state.
    scenario_id: Optional[str] = None
    scenario_version: Optional[int] = None
    baseline_snapshot_id: Optional[str] = None

    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: Optional[str] = None
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    decision_note: str = ""

    model_config = ConfigDict(extra="forbid")


class FinalResponse(BaseModel):
    """
    The machine-readable result of one orchestrator run.

    Deliberately flat and self-describing: a caller should never need to reach
    into engine internals to understand what happened, and should always be
    able to tell observed fact from hypothesis.
    """
    execution_id: str
    request_id: str
    status: str                       # terminal ExecutionState value
    intent: str = "UNKNOWN"

    # Snapshot / scenario provenance
    network_snapshot_id: Optional[str] = None
    scenario_id: Optional[str] = None
    scenario_version: Optional[int] = None
    # True whenever the results describe a hypothetical network.
    is_hypothetical: bool = False

    summary: str = ""
    results: Dict[str, Any] = Field(default_factory=dict)
    risk: Optional[Dict[str, Any]] = None
    reasoning: Optional[ReasoningResult] = None
    governance: Optional[GovernanceDecision] = None
    approval: Optional[ApprovalRequest] = None

    errors: List[Dict[str, Any]] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    # Correlation + timing for observability.
    duration_seconds: float = 0.0
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    # Compact per-step trace; the full trace lives in the audit record.
    steps: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")
