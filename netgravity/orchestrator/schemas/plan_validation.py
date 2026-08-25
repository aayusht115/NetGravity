"""
Orchestrator — Typed planning outcomes.

A plan that fails validation must not reach the executor, and the reason must be
machine-readable rather than a sentence in a log. Phase 8.4 will build failure
management on top of these reasons, so each one names a distinct situation that
calls for a distinct response.

Everything here is data. Nothing in this module inspects a plan or decides
anything — see `core/plan_graph.py` for the checks that produce it.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field


class PlanOrigin(str, Enum):
    """
    Where a plan's structure came from.

    Recorded on every plan because it changes how much the structure can be
    trusted to be intentional. A template was written by a person who decided
    what to leave OUT; a derived plan closed a dependency graph and knows only
    what was asked for.
    """
    #: Hand-written `WorkflowTemplate`. Encodes deliberate exclusions.
    TEMPLATE = "TEMPLATE"
    #: Derived from capability contracts by closing required dependencies.
    CAPABILITY_GRAPH = "CAPABILITY_GRAPH"


class PlanFailureReason(str, Enum):
    """
    Why a plan was refused.

    Each value is a different problem with a different owner, which is why they
    are not collapsed into one "invalid plan":

    NO_WORKFLOW_FOR_INTENT   Nothing knows how to answer this kind of request.
                             The request is unsupported — not malformed.

    UNKNOWN_CAPABILITY       A step names something the registry does not hold.
                             A planning bug, or a capability that was removed.

    NOT_PLANNABLE            The capability exists and is executable, but a
                             planner may not select it independently. Service
                             and embedded capabilities are reached through the
                             thing that owns them.

    MISSING_HARD_DEPENDENCY  A step needs a result that no step in this plan
                             will produce and the context does not already
                             hold. Executing would refuse at the seam anyway;
                             refusing here says so before any work is done.

    UNSATISFIABLE_INPUT      A declared required input has no supplied value.

    DEPENDENCY_CYCLE         The graph cannot be ordered.

    INVALID_ORDERING         A step is placed before something it depends on.

    DUPLICATE_STEP           Two steps share an id, so results would collide.

    EMPTY_PLAN               Nothing to do. Distinguished from a failure: it
                             usually means every capability the request needed
                             is already satisfied in this context.

    BLOCKED_BY_FAILURE       A prerequisite already FAILED in this context. The
                             planner does not retry it — Phase 8.4 owns that
                             decision — so it reports the block and stops.
    """
    NO_WORKFLOW_FOR_INTENT  = "NO_WORKFLOW_FOR_INTENT"
    UNKNOWN_CAPABILITY      = "UNKNOWN_CAPABILITY"
    NOT_PLANNABLE           = "NOT_PLANNABLE"
    MISSING_HARD_DEPENDENCY = "MISSING_HARD_DEPENDENCY"
    UNSATISFIABLE_INPUT     = "UNSATISFIABLE_INPUT"
    DEPENDENCY_CYCLE        = "DEPENDENCY_CYCLE"
    INVALID_ORDERING        = "INVALID_ORDERING"
    DUPLICATE_STEP          = "DUPLICATE_STEP"
    EMPTY_PLAN              = "EMPTY_PLAN"
    BLOCKED_BY_FAILURE      = "BLOCKED_BY_FAILURE"


class PlanViolation(BaseModel):
    """One specific thing wrong with a plan."""
    reason: PlanFailureReason
    #: Step the problem is attached to, when it belongs to one.
    step_id: Optional[str] = None
    capability: Optional[str] = None
    detail: str = ""
    #: What was missing, when the reason is about absence.
    missing: Tuple[str, ...] = ()

    model_config = ConfigDict(extra="forbid", frozen=True)

    def __str__(self) -> str:  # pragma: no cover - diagnostic convenience
        where = f" at step '{self.step_id}'" if self.step_id else ""
        return f"{self.reason.value}{where}: {self.detail}"


class PlanValidation(BaseModel):
    """
    The verdict on one plan.

    Carried ON the plan, so anything holding a plan can see whether it was
    checked and what was found. An unvalidated plan is distinguishable from a
    valid one — `checked` is False until a validator has run, and the executor
    is never handed a plan that has not been through this.
    """
    checked: bool = False
    violations: List[PlanViolation] = Field(default_factory=list)
    #: Capabilities the planner deliberately did NOT schedule because the
    #: context already holds a usable result. Recorded so a reader can tell
    #: "skipped, already done" from "never considered".
    already_satisfied: Tuple[str, ...] = ()
    #: Human-readable notes on why the plan looks the way it does.
    notes: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def valid(self) -> bool:
        """True only when a check has actually run and found nothing."""
        return self.checked and not self.violations

    def reasons(self) -> Tuple[PlanFailureReason, ...]:
        return tuple(v.reason for v in self.violations)

    def summary(self) -> str:
        if not self.checked:
            return "plan has not been validated"
        if not self.violations:
            return "plan is valid"
        return "; ".join(str(v) for v in self.violations)


__all__ = [
    "PlanFailureReason",
    "PlanOrigin",
    "PlanValidation",
    "PlanViolation",
]
