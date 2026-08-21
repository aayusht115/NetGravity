"""
Orchestrator — Tool interface, retry policy and the capability adapter layer.

Every unit of work the orchestrator can invoke — deterministic engine or
model-backed agent — is a Tool behind one interface. The core executes Tools;
it never imports an engine directly and never lets a model call business logic.

This is the adapter layer the LLM boundary depends on: a model can propose a
capability by name, but the registry decides whether it exists, the validator
decides whether the inputs are legal, and authorization decides whether this
actor may run it.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, Protocol, runtime_checkable

from netgravity.orchestrator.exceptions import (
    EngineTimeoutError,
    FailureClass,
    OrchestratorError,
)
from netgravity.orchestrator.schemas.plans import (
    ExecutionMode,
    ToolRequest,
    ToolResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from netgravity.orchestrator.core.execution_context import ExecutionContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """
    Bounded, observable retry behaviour.

    Only failures classified RETRYABLE are ever re-attempted. Infeasibility,
    validation failures and authorization failures are never retried — they are
    deterministic outcomes, and re-running them wastes time and budget while
    producing the same answer.
    """
    max_attempts: int = 1              # 1 = no retry
    backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with optional full jitter, for 1-based attempt."""
        raw = self.backoff_seconds * (self.backoff_multiplier ** max(0, attempt - 1))
        capped = min(raw, self.max_backoff_seconds)
        if self.jitter:
            return random.uniform(0.0, capped)
        return capped

    def should_retry(self, failure_class: FailureClass, attempt: int) -> bool:
        return failure_class == FailureClass.RETRYABLE and attempt < self.max_attempts


NO_RETRY = RetryPolicy(max_attempts=1)
STANDARD_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=0.5)
EXTERNAL_RETRY = RetryPolicy(max_attempts=3, backoff_seconds=1.0, max_backoff_seconds=10.0)


@runtime_checkable
class Tool(Protocol):
    """Anything the orchestrator can execute."""
    name: str

    async def execute(
        self,
        context: "ExecutionContext",
        request: ToolRequest,
    ) -> ToolResult:
        ...


#: Handler signature for function-backed capabilities.
Handler = Callable[["ExecutionContext", ToolRequest], Awaitable[Dict[str, Any]]]


@dataclass
class Capability:
    """
    Declarative description of one unit of work.

    The registry stores these; the planner reads them to build plans; the
    executor runs them. Adding a new agent or engine means registering one of
    these — the orchestrator core does not change.
    """
    name: str
    handler: Handler
    description: str = ""
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC

    # Capability names this one needs results from. Used to validate plans.
    dependencies: tuple = ()
    # Advisory: may this run alongside its layer peers?
    parallel_safe: bool = True

    timeout_seconds: float = 120.0
    retry_policy: RetryPolicy = field(default_factory=lambda: NO_RETRY)

    # A failure here degrades the run rather than failing it.
    optional: bool = False
    # Roles permitted to invoke it. Empty means "any authenticated actor".
    required_roles: tuple = ()

    input_schema: Optional[type] = None
    output_schema: Optional[type] = None

    @property
    def is_deterministic(self) -> bool:
        return self.execution_mode == ExecutionMode.DETERMINISTIC


class CapabilityTool:
    """
    Adapter turning a `Capability` into an executable Tool.

    Owns timeout enforcement, retry, error classification and execution
    metadata, so no handler has to reimplement them and every capability
    behaves consistently under failure.
    """

    def __init__(self, capability: Capability) -> None:
        self.capability = capability
        self.name = capability.name

    async def execute(
        self,
        context: "ExecutionContext",
        request: ToolRequest,
    ) -> ToolResult:
        cap = self.capability
        started = time.perf_counter()
        attempt = 0
        last_error: Optional[OrchestratorError] = None

        while attempt < max(1, cap.retry_policy.max_attempts):
            attempt += 1
            try:
                output = await asyncio.wait_for(
                    cap.handler(context, request),
                    timeout=cap.timeout_seconds,
                )
                return ToolResult(
                    capability=cap.name,
                    success=True,
                    output=output or {},
                    duration_seconds=round(time.perf_counter() - started, 4),
                    attempts=attempt,
                    execution_mode=cap.execution_mode,
                )

            except asyncio.TimeoutError:
                last_error = EngineTimeoutError(
                    f"Capability '{cap.name}' exceeded its {cap.timeout_seconds}s timeout.",
                    context={"capability": cap.name, "attempt": attempt},
                )
            except OrchestratorError as exc:
                last_error = exc
            except asyncio.CancelledError:  # pragma: no cover - cooperative cancel
                raise
            except Exception as exc:  # noqa: BLE001 - classified, never swallowed
                from netgravity.orchestrator.exceptions import EngineFailureError
                last_error = EngineFailureError(
                    f"Capability '{cap.name}' raised {type(exc).__name__}: {exc}",
                    context={"capability": cap.name, "attempt": attempt},
                    cause=exc,
                )

            if not cap.retry_policy.should_retry(last_error.failure_class, attempt):
                break

            delay = cap.retry_policy.delay_for(attempt)
            logger.warning(
                "orchestrator.tool.retry capability=%s attempt=%d code=%s delay=%.2fs %s",
                cap.name, attempt, last_error.code.value, delay, context.correlation(),
            )
            await asyncio.sleep(delay)

        assert last_error is not None
        logger.error(
            "orchestrator.tool.failed capability=%s attempts=%d code=%s class=%s %s",
            cap.name, attempt, last_error.code.value,
            last_error.failure_class.value, context.correlation(),
        )
        return ToolResult(
            capability=cap.name,
            success=False,
            output={},
            error_code=last_error.code.value,
            error_message=last_error.message,
            failure_class=last_error.failure_class.value,
            duration_seconds=round(time.perf_counter() - started, 4),
            attempts=attempt,
            execution_mode=cap.execution_mode,
            metadata={"error_context": last_error.context},
        )
