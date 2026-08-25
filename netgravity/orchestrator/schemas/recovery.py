"""
Orchestrator — Recovery, retry policy, and escalation schemas.

Phase 8.4 introduces the failure-management layer above the single-shot
`CapabilityExecutor`. This module defines the typed vocabulary for:
  - Failure classification and recovery decisions (RETRY, REROUTE, BLOCK, ESCALATE, CONTINUE)
  - Observable step execution attempt records
  - Bounded circuit breaker state and statistics
  - Formal escalation outcomes for unrecoverable or safety-critical conditions
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from netgravity.orchestrator.schemas.plans import AgentStatus


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RecoveryAction(str, Enum):
    """
    Decisions the FailureManager can make when evaluating a capability outcome.

    CONTINUE   Execution succeeded or degraded safely; proceed with next steps.
    RETRY      Transient failure; re-execute the same capability within budget.
    REROUTE    Primary failed; execute an explicitly declared alternative capability.
    BLOCK      Hard prerequisite failed; prevent downstream dependents from running.
    ESCALATE   Unrecoverable condition; stop autonomous execution and report to operator.
    """
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    REROUTE = "REROUTE"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class StepAttemptRecord(BaseModel):
    """
    Observable chronological record of a single execution attempt of a step.

    Preserves attempt number, outcome, duration, and error details so that
    retried executions never overwrite earlier failed attempts.
    """
    step_id: str
    capability: str
    attempt: int
    status: AgentStatus
    duration_seconds: float = 0.0
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    failure_class: Optional[str] = None
    is_reroute: bool = False
    rerouted_from: Optional[str] = None
    timestamp: str = Field(default_factory=_utc_now)
    context: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class EscalationOutcome(BaseModel):
    """
    Structured record when NetGravity cannot safely continue autonomously.

    Surfaces the failed capability, reasons, attempt history, blocked downstream
    dependencies, available evidence, and recommended human operator actions.
    """
    capability: str
    execution_id: str
    reason: str
    failed_attempts: int = 1
    blocked_downstream_capabilities: List[str] = Field(default_factory=list)
    available_evidence: Dict[str, str] = Field(default_factory=dict)
    recommended_human_action: str = ""
    timestamp: str = Field(default_factory=_utc_now)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryDecision(BaseModel):
    """
    Policy determination made by the FailureManager for one execution outcome.
    """
    action: RecoveryAction
    reason: str
    target_capability: Optional[str] = None
    attempt_number: int = 1
    delay_seconds: float = 0.0
    escalation: Optional[EscalationOutcome] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class CircuitState(str, Enum):
    """States of a bounded circuit breaker for external dependencies."""
    CLOSED = "CLOSED"         # Normal operation: all calls permitted
    OPEN = "OPEN"             # Tripped: fast-fail calls without external invocation
    HALF_OPEN = "HALF_OPEN"   # Cooldown elapsed: allow single probe call


class CircuitBreakerStats(BaseModel):
    """Observable metrics of a circuit breaker."""
    name: str
    state: CircuitState
    failure_count: int
    success_count: int
    last_failure_time: Optional[float] = None
    last_state_change: float = 0.0
    total_tripped_count: int = 0

    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryPolicy(BaseModel):
    """
    Bounded policy configuration governing retries, rerouting, and circuit breaking.

    Guarantees:
      - Max attempts are strictly bounded.
      - Non-retryable domain outcomes (e.g. MILP infeasibility, validation errors)
        are never retried.
      - Circuit breaker fast-fails when external service outages occur.
    """
    max_attempts: int = 3
    backoff_seconds: float = 0.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0
    jitter: bool = False

    retryable_statuses: Tuple[AgentStatus, ...] = (
        AgentStatus.RETRYABLE_FAILURE,
    )
    retryable_error_codes: Tuple[str, ...] = (
        "ENGINE_TIMEOUT",
        "LLM_FAILURE",
        "NETWORK_ERROR",
        "TRANSIENT_SERVICE_ERROR",
        "RATE_LIMIT_429",
        "GATEWAY_500",
        "GATEWAY_502",
        "CIRCUIT_OPEN",
    )

    non_retryable_statuses: Tuple[AgentStatus, ...] = (
        AgentStatus.NON_RETRYABLE_FAILURE,
        AgentStatus.INVALID_OUTPUT,
        AgentStatus.INSUFFICIENT_EVIDENCE,
    )
    non_retryable_error_codes: Tuple[str, ...] = (
        "SOLVER_INFEASIBLE",
        "VALIDATION_FAILURE",
        "CAPABILITY_NOT_FOUND",
        "MISSING_DATA",
        "AUTHORIZATION_FAILURE",
        "INVALID_REQUEST",
        "STALE_SNAPSHOT",
    )

    enable_rerouting: bool = True
    enable_circuit_breaker: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True)

    def is_retryable(self, status: AgentStatus, error_code: Optional[str] = None) -> bool:
        """
        Check if an outcome is retryable according to explicit policy rules.
        """
        if error_code in self.non_retryable_error_codes:
            return False
        if status in self.non_retryable_statuses:
            return False
        if status in self.retryable_statuses:
            return True
        if error_code in self.retryable_error_codes:
            return True
        return False

    def delay_for(self, attempt: int) -> float:
        """Calculate exponential backoff for attempt >= 1."""
        if self.backoff_seconds <= 0.0:
            return 0.0
        raw = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        return min(raw, self.max_backoff_seconds)


__all__ = [
    "CircuitBreakerStats",
    "CircuitState",
    "EscalationOutcome",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
    "StepAttemptRecord",
]
