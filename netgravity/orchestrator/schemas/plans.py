"""
Orchestrator — Execution plan, tool and result schemas.

A plan is an explicit dependency graph of steps. Workflow structure is DATA,
not Python control flow, so it can be inspected, validated, audited and
executed with dependency-aware parallelism.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecutionMode(str, Enum):
    """Whether a capability's output is reproducible from its inputs."""
    DETERMINISTIC = "DETERMINISTIC"   # engines: MILP, KPI, REI, RF
    PROBABILISTIC = "PROBABILISTIC"   # model-backed: intent, reasoning


class AgentStatus(str, Enum):
    """
    Outcome of one capability execution, as the orchestrator must read it.

    Defined here, beside `StepStatus` and `EvidenceStatus`, because it belongs to
    the same result layer and `ToolResult` needs it without importing the
    envelope that wraps it. `AgentResult` in `schemas/agent_result.py` re-exports
    it as the public name.

    The distinctions are not cosmetic — each one calls for a different response:

    SUCCESS                Complete, valid, every declared input present.

    PARTIAL                Usable output, knowingly incomplete: some sub-unit
                           failed, or a SOFT dependency was unavailable. The
                           caller may use it PROVIDED it is told what is missing,
                           which is why `unavailable` travels alongside.

    RETRYABLE_FAILURE      Transient. Re-running could succeed. Reported in this
                           phase; retry logic is deliberately not built yet.

    NON_RETRYABLE_FAILURE  Deterministic failure. Re-running produces the same
                           answer, so retrying only wastes solver time and
                           shared model budget. Infeasibility lives here: the
                           solver PROVED there is no solution, which is a real
                           finding rather than a fault.

    INVALID_OUTPUT         It ran and produced something the validators refused.
                           Worse than missing output, because it looks usable.

    INSUFFICIENT_EVIDENCE  Never attempted, or not computable, because required
                           inputs were absent. Nothing malfunctioned. Kept apart
                           from the failure statuses so a gap in the data is not
                           reported as a broken engine — and, above all, so it
                           is never reported as a value of zero.
    """
    SUCCESS               = "SUCCESS"
    PARTIAL               = "PARTIAL"
    RETRYABLE_FAILURE     = "RETRYABLE_FAILURE"
    NON_RETRYABLE_FAILURE = "NON_RETRYABLE_FAILURE"
    INVALID_OUTPUT        = "INVALID_OUTPUT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class StepStatus(str, Enum):
    PENDING   = "PENDING"
    RUNNING   = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED    = "FAILED"
    BLOCKED   = "BLOCKED"     # a HARD dependency failed — cannot safely run
    SKIPPED   = "SKIPPED"     # deliberately not run
    CANCELLED = "CANCELLED"


class DependencyType(str, Enum):
    """
    How critical one step's output is to a dependent step.

    HARD  The dependent step cannot safely execute without it. If the upstream
          step fails, the dependent is BLOCKED.
          Example: scenario validation → MILP. Solving an unvalidated scenario
          would produce numbers nobody should trust.

    SOFT  The dependent step can operate on partial inputs. If the upstream step
          fails, the dependent still RUNS and is told, explicitly, that the
          evidence is unavailable.
          Example: REI → reasoning. Losing exposure analysis degrades the
          narrative; it does not invalidate the cost figures already computed.

    A SOFT dependency never means "pretend it succeeded". The dependent receives
    the failure as `unavailable_evidence`, so it can distinguish a real zero
    from a missing measurement.
    """
    HARD = "HARD"
    SOFT = "SOFT"


class PlanStep(BaseModel):
    """
    One node in the execution DAG.

    `depends_on` defines TOPOLOGY (what must be attempted first).
    `soft_depends_on` overlays CRITICALITY on those same edges.

    Two lists rather than a list of edge objects because ordering is identical
    either way, and keeping `depends_on` a plain list of ids leaves the DAG
    algorithms and every existing caller unchanged.
    """
    step_id: str
    capability: str
    description: str = ""
    depends_on: List[str] = Field(default_factory=list)
    params: Dict[str, Any] = Field(default_factory=dict)

    # Subset of `depends_on` whose failure must NOT block this step.
    # Edges from a step marked `optional` are SOFT automatically, so this is
    # only needed to soften an edge from a non-optional step.
    soft_depends_on: List[str] = Field(default_factory=list)

    # When True a failure here does not fail the run, and every edge OUT of this
    # step is SOFT by default. Used for advisory capabilities such as narrative
    # reasoning, whose absence degrades presentation but not truth.
    optional: bool = False

    status: StepStatus = StepStatus.PENDING

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def soft_deps_must_be_declared(self) -> "PlanStep":
        stray = [d for d in self.soft_depends_on if d not in self.depends_on]
        if stray:
            raise ValueError(
                f"Step '{self.step_id}' lists soft_depends_on {stray} that are not in "
                f"depends_on {self.depends_on}. Criticality can only be declared for a "
                f"dependency that actually exists."
            )
        return self


class ExecutionPlan(BaseModel):
    """
    A validated, acyclic execution graph.

    Built by the planner from an intent; never assembled ad hoc inside the
    orchestrator loop.
    """
    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    workflow_id: str
    intent: str
    steps: List[PlanStep] = Field(default_factory=list)
    description: str = ""

    model_config = ConfigDict(extra="forbid")

    def step(self, step_id: str) -> Optional[PlanStep]:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def dependency_type(self, upstream_id: str, dependent: PlanStep) -> DependencyType:
        """
        Criticality of the edge `upstream_id → dependent`.

        SOFT when the dependent explicitly softens it, or when the upstream step
        is itself `optional` (an advisory step's output cannot be mandatory for
        anyone). HARD otherwise — the safe default.
        """
        if upstream_id in dependent.soft_depends_on:
            return DependencyType.SOFT
        upstream = self.step(upstream_id)
        if upstream is not None and upstream.optional:
            return DependencyType.SOFT
        return DependencyType.HARD

    def classify_dependencies(
        self, dependent: PlanStep, satisfied: Set[str],
    ) -> "DependencyResolution":
        """
        Split a step's unmet dependencies into blocking and degrading.

        Args:
            dependent: The step about to be considered for execution.
            satisfied: Step ids that completed successfully.

        Returns:
            DependencyResolution — `runnable` is False only when a HARD
            dependency is unmet.
        """
        hard_unmet: List[str] = []
        soft_unmet: List[str] = []
        for dep in dependent.depends_on:
            if dep in satisfied:
                continue
            if self.dependency_type(dep, dependent) == DependencyType.SOFT:
                soft_unmet.append(dep)
            else:
                hard_unmet.append(dep)
        return DependencyResolution(
            step_id=dependent.step_id,
            blocking=hard_unmet,
            degraded=soft_unmet,
        )

    def step_ids(self) -> Set[str]:
        return {s.step_id for s in self.steps}

    def validate_dag(self) -> None:
        """
        Verify the plan is a well-formed DAG.

        Raises:
            PlanningFailureError: duplicate ids, unknown dependency, or a cycle.
        """
        from netgravity.orchestrator.exceptions import PlanningFailureError

        ids: List[str] = [s.step_id for s in self.steps]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise PlanningFailureError(
                f"Execution plan has duplicate step ids: {dupes}",
                context={"plan_id": self.plan_id},
            )

        known = set(ids)
        for s in self.steps:
            unknown = [d for d in s.depends_on if d not in known]
            if unknown:
                raise PlanningFailureError(
                    f"Step '{s.step_id}' depends on unknown step(s): {unknown}",
                    context={"plan_id": self.plan_id},
                )

        # Depth-first cycle detection.
        WHITE, GREY, BLACK = 0, 1, 2
        colour = {i: WHITE for i in ids}
        deps = {s.step_id: list(s.depends_on) for s in self.steps}

        def visit(node: str, trail: List[str]) -> None:
            colour[node] = GREY
            for dep in deps[node]:
                if colour[dep] == GREY:
                    cycle = trail + [node, dep]
                    raise PlanningFailureError(
                        f"Execution plan contains a dependency cycle: {' -> '.join(cycle)}",
                        context={"plan_id": self.plan_id},
                    )
                if colour[dep] == WHITE:
                    visit(dep, trail + [node])
            colour[node] = BLACK

        for i in ids:
            if colour[i] == WHITE:
                visit(i, [])

    def execution_layers(self) -> List[List[str]]:
        """
        Group steps into dependency layers.

        Every step within a layer is independent of the others in that layer
        and may execute concurrently. Layer N+1 starts only once layer N has
        settled.
        """
        self.validate_dag()
        remaining = {s.step_id: set(s.depends_on) for s in self.steps}
        layers: List[List[str]] = []
        done: Set[str] = set()

        while remaining:
            ready = sorted(sid for sid, deps in remaining.items() if deps <= done)
            if not ready:  # pragma: no cover - validate_dag already rules this out
                from netgravity.orchestrator.exceptions import PlanningFailureError
                raise PlanningFailureError(
                    "Execution plan cannot be layered; unresolved dependencies remain.",
                    context={"remaining": sorted(remaining)},
                )
            layers.append(ready)
            for sid in ready:
                remaining.pop(sid)
            done |= set(ready)

        return layers


class DependencyResolution(BaseModel):
    """Outcome of checking one step's dependencies against completed work."""
    step_id: str
    #: Unmet HARD dependencies. Non-empty ⇒ the step must not run.
    blocking: List[str] = Field(default_factory=list)
    #: Unmet SOFT dependencies. The step runs, but with evidence missing.
    degraded: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def runnable(self) -> bool:
        return not self.blocking

    @property
    def is_degraded(self) -> bool:
        return bool(self.degraded)


# ---------------------------------------------------------------------------
# Tool interface payloads
# ---------------------------------------------------------------------------

class EvidenceStatus(str, Enum):
    """Why a piece of expected evidence is not present."""
    UNAVAILABLE = "UNAVAILABLE"   # upstream step failed
    TIMEOUT     = "TIMEOUT"       # upstream step exceeded its timeout
    INVALID     = "INVALID"       # upstream produced output that failed validation
    NOT_RUN     = "NOT_RUN"       # upstream was blocked or never scheduled


class UnavailableEvidence(BaseModel):
    """
    An explicit record that evidence is MISSING.

    Exists so a downstream component can distinguish `value = 0` from
    `value = unavailable`. Zero is never used as a stand-in for absent data.
    """
    capability: str
    status: EvidenceStatus = EvidenceStatus.UNAVAILABLE
    reason: str = ""
    step_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ToolRequest(BaseModel):
    """Input handed to a capability handler."""
    capability: str
    params: Dict[str, Any] = Field(default_factory=dict)
    # Outputs of already-completed steps, keyed by step id.
    upstream: Dict[str, Any] = Field(default_factory=dict)
    # Expected-but-missing evidence, keyed by capability name. A handler that
    # reads this can degrade honestly instead of assuming a default.
    unavailable: Dict[str, UnavailableEvidence] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    """
    Output of a capability handler.

    Always structured; handlers never return bare dictionaries into the core.
    """
    capability: str
    success: bool
    output: Dict[str, Any] = Field(default_factory=dict)

    #: Optional finer-grained outcome. `success` stays the executor's own
    #: contract and every existing caller keeps reading it; this only lets a
    #: handler say something the boolean cannot — that a result is PARTIAL, or
    #: that the inputs were absent rather than the work broken.
    #:
    #: Left None by default, in which case `AgentResult.classify` derives the
    #: status from `success`, `error_code` and `failure_class`. So no handler is
    #: obliged to change, and none did in this phase.
    status: Optional[AgentStatus] = None

    error_code: Optional[str] = None
    error_message: Optional[str] = None
    failure_class: Optional[str] = None

    duration_seconds: float = 0.0
    attempts: int = 1
    # DETERMINISTIC results are reproducible; PROBABILISTIC ones are not.
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
