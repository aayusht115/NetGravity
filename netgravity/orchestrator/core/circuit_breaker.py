"""
Orchestrator — Bounded Circuit Breaker for external dependencies.

Phase 8.4 introduces bounded circuit breaker protection for shared external
resources (such as the text-generation LLM gateway).

States:
  CLOSED     Normal operation. Calls are allowed through.
  OPEN       Failure threshold exceeded. Fast-fails calls immediately without
             invoking the external network or consuming quota.
  HALF_OPEN  Cooldown elapsed. Allows a bounded probe attempt to test recovery.

Design rules:
  1. Only transient service/network failures increment the circuit trip counter.
     Deterministic business outcomes (MILP infeasibility, validation errors)
     are not infrastructure outages and NEVER trip the circuit breaker.
  2. The circuit breaker is observable and reports statistics.
  3. Reset/recovery is automatic via bounded cooldown probing.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from netgravity.orchestrator.schemas.recovery import (
    CircuitBreakerStats,
    CircuitState,
)

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    Thread-safe, bounded circuit breaker.
    """

    def __init__(
        self,
        name: str = "shared_llm_gateway",
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        half_open_probe_limit: int = 1,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout_seconds = max(0.1, recovery_timeout_seconds)
        self.half_open_probe_limit = max(1, half_open_probe_limit)

        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_failure_time: Optional[float] = None
        self._last_state_change: float = time.monotonic()
        self._total_tripped_count: int = 0
        self._probe_count: int = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._check_cooldown_unlocked()
            return self._state

    def _check_cooldown_unlocked(self) -> None:
        """Transition from OPEN to HALF_OPEN if cooldown has elapsed."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_state_change
            if elapsed >= self.recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._last_state_change = time.monotonic()
                self._probe_count = 0
                logger.info(
                    "circuit_breaker.transitioned name=%s from=OPEN to=HALF_OPEN elapsed=%.2fs",
                    self.name, elapsed,
                )

    def can_execute(self) -> bool:
        """
        Determine if an execution may proceed through this circuit breaker.

        Returns True if CLOSED or if HALF_OPEN within probe budget.
        Returns False if OPEN.
        """
        with self._lock:
            self._check_cooldown_unlocked()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                if self._probe_count < self.half_open_probe_limit:
                    self._probe_count += 1
                    return True
                return False
            # OPEN
            return False

    def record_success(self) -> None:
        """
        Record a successful execution through the dependency.
        """
        with self._lock:
            self._consecutive_successes += 1
            if self._state == CircuitState.HALF_OPEN:
                # Probe succeeded, reset to CLOSED
                self._state = CircuitState.CLOSED
                self._consecutive_failures = 0
                self._last_state_change = time.monotonic()
                self._probe_count = 0
                logger.info(
                    "circuit_breaker.transitioned name=%s from=HALF_OPEN to=CLOSED after successful probe",
                    self.name,
                )
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    def record_failure(
        self,
        failure_class: str = "RETRYABLE",
        *,
        error_code: Optional[str] = None,
        is_infrastructure_failure: bool = True,
    ) -> None:
        """
        Record a failure event.

        Only infrastructure/transient/retryable errors count toward tripping
        the circuit breaker. Domain-level math findings do not trip it.
        """
        if not is_infrastructure_failure or failure_class == "NON_RETRYABLE":
            # Domain / deterministic validation failure does not trip the circuit
            return

        with self._lock:
            now = time.monotonic()
            self._last_failure_time = now
            self._consecutive_failures += 1
            self._consecutive_successes = 0

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed, immediately trip back to OPEN
                self._state = CircuitState.OPEN
                self._last_state_change = now
                self._total_tripped_count += 1
                self._probe_count = 0
                logger.warning(
                    "circuit_breaker.probe_failed name=%s back_to=OPEN code=%s",
                    self.name, error_code,
                )
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._last_state_change = now
                    self._total_tripped_count += 1
                    logger.warning(
                        "circuit_breaker.tripped name=%s failures=%d threshold=%d state=OPEN",
                        self.name, self._consecutive_failures, self.failure_threshold,
                    )

    def trip_open(self, reason: str = "") -> None:
        """Manually force the circuit breaker to OPEN."""
        with self._lock:
            self._state = CircuitState.OPEN
            self._last_state_change = time.monotonic()
            self._total_tripped_count += 1
            logger.warning("circuit_breaker.manually_tripped name=%s reason=%s", self.name, reason)

    def reset(self) -> None:
        """Reset the circuit breaker to CLOSED with 0 failures."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._last_state_change = time.monotonic()
            self._probe_count = 0

    def stats(self) -> CircuitBreakerStats:
        """Get snapshot of circuit breaker statistics."""
        with self._lock:
            self._check_cooldown_unlocked()
            return CircuitBreakerStats(
                name=self.name,
                state=self._state,
                failure_count=self._consecutive_failures,
                success_count=self._consecutive_successes,
                last_failure_time=self._last_failure_time,
                last_state_change=self._last_state_change,
                total_tripped_count=self._total_tripped_count,
            )


__all__ = [
    "CircuitBreaker",
]
