"""
Orchestrator — Execution context.

One strongly typed object carried through an entire run. It is the single
source of truth for *what* is executing, *against which data version*, and
*how far it has got*.

Snapshot and scenario references are immutable identifiers. Every downstream
engine call knows exactly which network snapshot and scenario version it is
operating on, which is what makes stale-state detection and audit possible.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.orchestrator.core.execution_state import (
    ExecutionState,
    assert_transition,
    is_terminal,
)
from netgravity.orchestrator.schemas.actions import (
    ApprovalRequest,
    GovernanceDecision,
)
from netgravity.orchestrator.schemas.plans import (
    EvidenceStatus,
    ExecutionPlan,
    ToolResult,
    UnavailableEvidence,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ExternalSignal,
    Intent,
    IntentResolution,
    OrchestratorRequest,
)
from netgravity.orchestrator.schemas.risk import ReasoningResult, RiskAssessment
from netgravity.schemas.results import FacilityResilienceRegistry


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StateTransition:
    """One recorded move through the state machine."""
    from_state: str
    to_state: str
    at: str
    note: str = ""


@dataclass
class ExecutionContext:
    """
    Mutable execution record for a single orchestrator run.

    A dataclass rather than a Pydantic model because it is a live working
    object mutated throughout the run; the API-facing contracts
    (`FinalResponse`) are the validated, serialisable surface.
    """

    # --- identity ---
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    request_id: str = ""
    parent_execution_id: Optional[str] = None
    actor: Actor = field(default_factory=Actor)
    created_at: str = field(default_factory=_utc_now)
    _started_monotonic: float = field(default_factory=time.perf_counter, repr=False)

    # --- what was asked ---
    raw_input: str = ""
    intent: Intent = Intent.UNKNOWN
    intent_resolution: Optional[IntentResolution] = None
    external_signal: Optional[ExternalSignal] = None

    # --- immutable data references ---
    # The observed network snapshot this run is pinned to. Never changes once
    # set; if the live snapshot moves, the run goes STALE rather than drifting.
    baseline_snapshot_id: Optional[str] = None
    # Hypothetical overlay, when the run analyses a scenario.
    scenario_id: Optional[str] = None
    scenario_version: Optional[int] = None
    # Several scenarios for comparison runs.
    scenario_ids: List[str] = field(default_factory=list)

    # --- plan & progress ---
    workflow_id: Optional[str] = None
    plan: Optional[ExecutionPlan] = None
    current_state: ExecutionState = ExecutionState.RECEIVED
    state_history: List[StateTransition] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    completed_steps: List[str] = field(default_factory=list)
    failed_steps: List[str] = field(default_factory=list)
    skipped_steps: List[str] = field(default_factory=list)
    blocked_steps: List[str] = field(default_factory=list)

    # Evidence that was expected but is MISSING, keyed by capability name.
    # Populated whenever a step fails, is blocked, or produces invalid output.
    # Downstream consumers read this to tell "no data" from "a value of zero".
    unavailable_evidence: Dict[str, UnavailableEvidence] = field(default_factory=dict)

    # --- outputs ---
    # step_id -> ToolResult
    step_results: Dict[str, ToolResult] = field(default_factory=dict)
    # Deterministic engine outputs, keyed by capability name.
    engine_results: Dict[str, Any] = field(default_factory=dict)
    # The AUTHORITATIVE REI batch, in its typed form. `engine_results` holds a
    # flattened projection of it for transport; consumers that need per-node
    # calculation status, failure reasons or snapshot provenance read this
    # instead. Reconstructing a registry from the flattened dict would silently
    # default a FAILED node's status to OK.
    rei_registry: Optional[FacilityResilienceRegistry] = None
    risk_results: Optional[RiskAssessment] = None
    reasoning: Optional[ReasoningResult] = None
    governance_result: Optional[GovernanceDecision] = None
    approval_request: Optional[ApprovalRequest] = None

    # --- diagnostics ---
    errors: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    audit_metadata: Dict[str, Any] = field(default_factory=dict)

    # Set when the run must not perform model calls.
    llm_enabled: bool = True

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def transition(self, target: ExecutionState, note: str = "") -> None:
        """
        Move to `target`, enforcing the legal-transition table.

        Raises:
            IllegalStateTransitionError
        """
        assert_transition(self.current_state, target)
        self.state_history.append(StateTransition(
            from_state=self.current_state.value,
            to_state=target.value,
            at=_utc_now(),
            note=note,
        ))
        self.current_state = target

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.current_state)

    @property
    def duration_seconds(self) -> float:
        return round(time.perf_counter() - self._started_monotonic, 4)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_step(self, step_id: str, result: ToolResult) -> None:
        """Attach a step outcome and update progress bookkeeping."""
        self.step_results[step_id] = result
        if result.success:
            if step_id not in self.completed_steps:
                self.completed_steps.append(step_id)
            self.engine_results[result.capability] = result.output
            # A previously-missing capability that later succeeded is no longer
            # missing (relevant on retry/resume paths).
            self.unavailable_evidence.pop(result.capability, None)
        else:
            if step_id not in self.failed_steps:
                self.failed_steps.append(step_id)
            self.errors.append({
                "step_id": step_id,
                "capability": result.capability,
                "code": result.error_code,
                "message": result.error_message,
                "failure_class": result.failure_class,
            })
            status = (
                EvidenceStatus.TIMEOUT if result.error_code == "ENGINE_TIMEOUT"
                else EvidenceStatus.INVALID if result.error_code == "VALIDATION_FAILURE"
                else EvidenceStatus.UNAVAILABLE
            )
            self.record_unavailable(
                result.capability,
                reason=result.error_message or "step failed",
                status=status,
                step_id=step_id,
            )

    def record_unavailable(
        self,
        capability: str,
        *,
        reason: str,
        status: EvidenceStatus = EvidenceStatus.UNAVAILABLE,
        step_id: Optional[str] = None,
    ) -> None:
        """
        Record that expected evidence is missing.

        Never substitutes a default value. Consumers see the absence explicitly.
        """
        self.unavailable_evidence[capability] = UnavailableEvidence(
            capability=capability, status=status, reason=reason, step_id=step_id,
        )

    def record_skip(self, step_id: str, reason: str) -> None:
        if step_id not in self.skipped_steps:
            self.skipped_steps.append(step_id)
        self.warnings.append(f"step '{step_id}' skipped: {reason}")

    def record_blocked(self, step_id: str, capability: str, blocking: List[str]) -> None:
        """Record a step that could not run because a HARD dependency failed."""
        if step_id not in self.blocked_steps:
            self.blocked_steps.append(step_id)
        reason = f"blocked by failed required dependency: {', '.join(blocking)}"
        self.warnings.append(f"step '{step_id}' {reason}")
        self.record_unavailable(
            capability, reason=reason, status=EvidenceStatus.NOT_RUN, step_id=step_id,
        )

    def missing_evidence(self) -> Dict[str, str]:
        """Capability → human-readable reason, for governance and reporting."""
        return {
            cap: f"{ev.status.value}: {ev.reason}"
            for cap, ev in self.unavailable_evidence.items()
        }

    def record_error(self, code: str, message: str, failure_class: str = "", **ctx: Any) -> None:
        self.errors.append({
            "code": code,
            "message": message,
            "failure_class": failure_class,
            **ctx,
        })

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def output_of(self, capability: str) -> Optional[Dict[str, Any]]:
        """Successful output of a capability, if it ran."""
        return self.engine_results.get(capability)

    def step_output(self, step_id: str) -> Optional[Dict[str, Any]]:
        res = self.step_results.get(step_id)
        return res.output if (res and res.success) else None

    @property
    def is_hypothetical(self) -> bool:
        """True when this run analysed a scenario rather than observed state."""
        return bool(self.scenario_id or self.scenario_ids)

    def correlation(self) -> Dict[str, Any]:
        """Correlation identifiers attached to every structured log line."""
        return {
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "scenario_id": self.scenario_id,
            "workflow_id": self.workflow_id,
            "snapshot_id": self.baseline_snapshot_id,
        }

    def step_summaries(self) -> List[Dict[str, Any]]:
        """Compact per-step trace for the API response."""
        out: List[Dict[str, Any]] = []
        for step in (self.plan.steps if self.plan else []):
            res = self.step_results.get(step.step_id)
            out.append({
                "step_id": step.step_id,
                "capability": step.capability,
                "status": step.status.value,
                "duration_seconds": round(res.duration_seconds, 4) if res else 0.0,
                "attempts": res.attempts if res else 0,
                "error": res.error_code if (res and not res.success) else None,
            })
        return out

    @classmethod
    def from_request(
        cls,
        request: OrchestratorRequest,
        current_snapshot_id: Optional[str] = None,
    ) -> "ExecutionContext":
        """
        Build a context from an inbound request.

        An explicitly pinned `network_snapshot_id` ALWAYS wins over the current
        snapshot. Silently substituting the current one would defeat the point
        of pinning — the caller would believe it was analysing one data version
        while the engines used another, and stale-state detection could never
        fire.
        """
        return cls(
            request_id=request.request_id,
            actor=request.actor,
            raw_input=request.input,
            external_signal=request.external_signal,
            baseline_snapshot_id=request.network_snapshot_id or current_snapshot_id,
            llm_enabled=not request.disable_llm,
        )
