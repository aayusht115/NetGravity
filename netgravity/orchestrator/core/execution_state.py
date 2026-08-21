"""
Orchestrator — Explicit execution state machine.

Workflow state is DATA with legal transitions, not implicit Python control
flow. Every move is validated and recorded, so "what was this run doing when it
stopped?" always has an answer.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet


class ExecutionState(str, Enum):
    """
    Lifecycle states of one orchestrator run.

    Terminal states are the only legal resting places:
      COMPLETED / FAILED / INFEASIBLE / REQUIRES_APPROVAL /
      REQUIRES_HUMAN / CANCELLED / STALE
    """
    RECEIVED          = "RECEIVED"
    UNDERSTANDING     = "UNDERSTANDING"
    PLANNED           = "PLANNED"
    VALIDATING        = "VALIDATING"
    RUNNING           = "RUNNING"
    WAITING           = "WAITING"            # blocked on an external dependency

    # --- terminal ---
    COMPLETED         = "COMPLETED"
    FAILED            = "FAILED"
    INFEASIBLE        = "INFEASIBLE"         # solver proved no feasible solution
    REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
    REQUIRES_HUMAN    = "REQUIRES_HUMAN"
    CANCELLED         = "CANCELLED"
    STALE             = "STALE"              # snapshot moved under the run


TERMINAL_STATES: FrozenSet[ExecutionState] = frozenset({
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.INFEASIBLE,
    ExecutionState.REQUIRES_APPROVAL,
    ExecutionState.REQUIRES_HUMAN,
    ExecutionState.CANCELLED,
    ExecutionState.STALE,
})

#: Every state may fail, be cancelled, go stale, or escalate to a human.
_UNIVERSAL_EXITS: FrozenSet[ExecutionState] = frozenset({
    ExecutionState.FAILED,
    ExecutionState.CANCELLED,
    ExecutionState.STALE,
    ExecutionState.REQUIRES_HUMAN,
})

LEGAL_TRANSITIONS: Dict[ExecutionState, FrozenSet[ExecutionState]] = {
    ExecutionState.RECEIVED: frozenset({
        ExecutionState.UNDERSTANDING,
    }) | _UNIVERSAL_EXITS,

    ExecutionState.UNDERSTANDING: frozenset({
        ExecutionState.PLANNED,
    }) | _UNIVERSAL_EXITS,

    ExecutionState.PLANNED: frozenset({
        ExecutionState.VALIDATING,
    }) | _UNIVERSAL_EXITS,

    ExecutionState.VALIDATING: frozenset({
        ExecutionState.RUNNING,
    }) | _UNIVERSAL_EXITS,

    ExecutionState.RUNNING: frozenset({
        ExecutionState.WAITING,
        ExecutionState.COMPLETED,
        ExecutionState.INFEASIBLE,
        ExecutionState.REQUIRES_APPROVAL,
    }) | _UNIVERSAL_EXITS,

    ExecutionState.WAITING: frozenset({
        ExecutionState.RUNNING,
        ExecutionState.COMPLETED,
        ExecutionState.REQUIRES_APPROVAL,
    }) | _UNIVERSAL_EXITS,

    # An approval decision resumes the SAME execution rather than starting an
    # uncontrolled new one.
    ExecutionState.REQUIRES_APPROVAL: frozenset({
        ExecutionState.RUNNING,
        ExecutionState.COMPLETED,
        ExecutionState.CANCELLED,
        ExecutionState.FAILED,
        ExecutionState.STALE,
    }),

    # Remaining terminal states are absorbing.
    ExecutionState.COMPLETED:      frozenset(),
    ExecutionState.FAILED:         frozenset(),
    ExecutionState.INFEASIBLE:     frozenset(),
    ExecutionState.REQUIRES_HUMAN: frozenset(),
    ExecutionState.CANCELLED:      frozenset(),
    ExecutionState.STALE:          frozenset(),
}


def is_terminal(state: ExecutionState) -> bool:
    return state in TERMINAL_STATES


def can_transition(current: ExecutionState, target: ExecutionState) -> bool:
    """True when moving current → target is permitted."""
    if current == target:
        return False
    return target in LEGAL_TRANSITIONS.get(current, frozenset())


def assert_transition(current: ExecutionState, target: ExecutionState) -> None:
    """
    Raise unless current → target is a legal move.

    Raises:
        IllegalStateTransitionError
    """
    from netgravity.orchestrator.exceptions import IllegalStateTransitionError

    if not can_transition(current, target):
        allowed = sorted(s.value for s in LEGAL_TRANSITIONS.get(current, frozenset()))
        raise IllegalStateTransitionError(
            f"Illegal execution state transition {current.value} -> {target.value}. "
            f"Allowed from {current.value}: {allowed or ['(terminal)']}",
            context={"from": current.value, "to": target.value},
        )
