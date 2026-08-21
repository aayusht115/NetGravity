"""
NetGravity Orchestrator — Exception Taxonomy
=============================================

Every failure the control plane can encounter is an explicit, classified type.
Nothing is silently swallowed.

Classification drives behaviour:

    RETRYABLE       transient; the retry policy may re-attempt it
    NON_RETRYABLE   deterministic failure; re-running changes nothing
    REQUIRES_HUMAN  the run cannot proceed without a person

The most important distinction in this file is that **solver infeasibility is
NOT retryable**. An infeasible network is a mathematical fact, not a transient
fault; retrying it burns time and produces the same answer. It is surfaced as a
result, with an explanation, rather than retried.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional


class FailureClass(str, Enum):
    """How the orchestrator should react to a failure."""
    RETRYABLE      = "RETRYABLE"
    NON_RETRYABLE  = "NON_RETRYABLE"
    REQUIRES_HUMAN = "REQUIRES_HUMAN"


class ErrorCode(str, Enum):
    """Canonical failure codes surfaced in responses and audit records."""
    INVALID_REQUEST          = "INVALID_REQUEST"
    INVALID_SCENARIO         = "INVALID_SCENARIO"
    MISSING_DATA             = "MISSING_DATA"
    STALE_SNAPSHOT           = "STALE_SNAPSHOT"
    ENGINE_TIMEOUT           = "ENGINE_TIMEOUT"
    ENGINE_FAILURE           = "ENGINE_FAILURE"
    SOLVER_INFEASIBLE        = "SOLVER_INFEASIBLE"
    DEPENDENCY_FAILURE       = "DEPENDENCY_FAILURE"
    LLM_FAILURE              = "LLM_FAILURE"
    EXTERNAL_SIGNAL_FAILURE  = "EXTERNAL_SIGNAL_FAILURE"
    VALIDATION_FAILURE       = "VALIDATION_FAILURE"
    GOVERNANCE_FAILURE       = "GOVERNANCE_FAILURE"
    AUTHORIZATION_FAILURE    = "AUTHORIZATION_FAILURE"
    CAPABILITY_NOT_FOUND     = "CAPABILITY_NOT_FOUND"
    PLANNING_FAILURE         = "PLANNING_FAILURE"
    ILLEGAL_STATE_TRANSITION = "ILLEGAL_STATE_TRANSITION"


class OrchestratorError(Exception):
    """
    Base class for every orchestrator failure.

    Carries the canonical code, the failure class that drives retry/escalation,
    and structured context for the audit trail.
    """
    code: ErrorCode = ErrorCode.ENGINE_FAILURE
    failure_class: FailureClass = FailureClass.NON_RETRYABLE

    def __init__(
        self,
        message: str,
        *,
        context: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: Dict[str, Any] = dict(context or {})
        self.cause = cause

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code.value,
            "failure_class": self.failure_class.value,
            "message": self.message,
            "context": self.context,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.code.value}: {self.message})"


# ---------------------------------------------------------------------------
# Request / validation failures — never retryable
# ---------------------------------------------------------------------------

class InvalidRequestError(OrchestratorError):
    code = ErrorCode.INVALID_REQUEST
    failure_class = FailureClass.NON_RETRYABLE


class ValidationFailureError(OrchestratorError):
    code = ErrorCode.VALIDATION_FAILURE
    failure_class = FailureClass.NON_RETRYABLE


class InvalidScenarioError(OrchestratorError):
    code = ErrorCode.INVALID_SCENARIO
    failure_class = FailureClass.NON_RETRYABLE


class MissingDataError(OrchestratorError):
    """A required input is absent. A person must supply it."""
    code = ErrorCode.MISSING_DATA
    failure_class = FailureClass.REQUIRES_HUMAN


class StaleSnapshotError(OrchestratorError):
    """
    An execution referenced a network snapshot that is no longer current.

    Results from incompatible network versions must never be combined, so the
    run stops and is re-planned or escalated rather than silently continuing.
    """
    code = ErrorCode.STALE_SNAPSHOT
    failure_class = FailureClass.REQUIRES_HUMAN


# ---------------------------------------------------------------------------
# Engine failures
# ---------------------------------------------------------------------------

class EngineTimeoutError(OrchestratorError):
    """An engine exceeded its configured timeout. Transient — may be retried."""
    code = ErrorCode.ENGINE_TIMEOUT
    failure_class = FailureClass.RETRYABLE


class EngineFailureError(OrchestratorError):
    code = ErrorCode.ENGINE_FAILURE
    failure_class = FailureClass.RETRYABLE


class SolverInfeasibleError(OrchestratorError):
    """
    The MILP proved no feasible solution exists.

    Explicitly NON_RETRYABLE: infeasibility is a mathematical property of the
    model, not a transient fault. Re-solving produces the same answer. The
    orchestrator reports it as an outcome with diagnostics.
    """
    code = ErrorCode.SOLVER_INFEASIBLE
    failure_class = FailureClass.NON_RETRYABLE


class DependencyFailureError(OrchestratorError):
    """A step could not run because an upstream step failed."""
    code = ErrorCode.DEPENDENCY_FAILURE
    failure_class = FailureClass.NON_RETRYABLE


class CapabilityNotFoundError(OrchestratorError):
    code = ErrorCode.CAPABILITY_NOT_FOUND
    failure_class = FailureClass.NON_RETRYABLE


class PlanningFailureError(OrchestratorError):
    code = ErrorCode.PLANNING_FAILURE
    failure_class = FailureClass.NON_RETRYABLE


class IllegalStateTransitionError(OrchestratorError):
    code = ErrorCode.ILLEGAL_STATE_TRANSITION
    failure_class = FailureClass.NON_RETRYABLE


# ---------------------------------------------------------------------------
# LLM / external failures
# ---------------------------------------------------------------------------

class LLMFailureError(OrchestratorError):
    """
    The language gateway failed.

    Retryable by default, but the orchestrator is designed to DEGRADE rather
    than fail: deterministic results remain valid without the LLM, so callers
    generally fall back to rule-based intent parsing and template reasoning.
    """
    code = ErrorCode.LLM_FAILURE
    failure_class = FailureClass.RETRYABLE


class LLMNonRetryableError(LLMFailureError):
    """Auth, budget, oversized prompt, or malformed request — retrying is futile."""
    failure_class = FailureClass.NON_RETRYABLE


class ExternalSignalFailureError(OrchestratorError):
    code = ErrorCode.EXTERNAL_SIGNAL_FAILURE
    failure_class = FailureClass.RETRYABLE


# ---------------------------------------------------------------------------
# Governance / authorization
# ---------------------------------------------------------------------------

class GovernanceFailureError(OrchestratorError):
    code = ErrorCode.GOVERNANCE_FAILURE
    failure_class = FailureClass.REQUIRES_HUMAN


class AuthorizationError(OrchestratorError):
    """
    The actor is not permitted to perform this action.

    Never retryable, and never overridable by an LLM: the model can request an
    action, but only the authorization layer grants it.
    """
    code = ErrorCode.AUTHORIZATION_FAILURE
    failure_class = FailureClass.NON_RETRYABLE
