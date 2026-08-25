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
from netgravity.orchestrator.schemas.agent_result import AgentResult
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionPlan,
    StepStatus,
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
    # The RF-eligible signal: a discrete hazard with a stated likelihood. Feeds
    # `RF = P + REI - P*REI` and nothing else.
    external_signal: Optional[ExternalSignal] = None
    # Structured market-intelligence signals awaiting a routing decision,
    # whatever route they arrived by: document extraction, a structured feed, or
    # a chat turn. Deliberately a SEPARATE field from `external_signal` above:
    # these carry no probability and can never reach RF, and that one carries a
    # probability and can never reach a forecast. One field holding both would
    # be the first step to conflating them.
    #
    # MUTATED IN PLACE by `market.score_signal`, which attaches each signal's
    # guardrail verdict — the same pattern `external_signal` already uses for
    # `interpret_signal`. Readers downstream therefore see scored signals if
    # that step ran and unscored ones if it did not; `passed_guardrail` is the
    # field to check, never presence.
    market_signals: List[Any] = field(default_factory=list)
    # What the orchestrator decided about each of them, for the audit trail.
    signal_routing: Optional[Any] = None

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

    # Outcome per CAPABILITY, as the orchestrator must read it.
    #
    # The step lists above are keyed by STEP id, which is the right key for
    # executing a plan — the same capability can legitimately appear as two
    # steps (a baseline solve and a scenario solve). But a planner asks
    # capability-shaped questions: "do we have resilience evidence yet?" It
    # should not have to know which step ids happened to carry it.
    #
    # Derived state, written only by `record_step` and the record_* methods
    # below, so it cannot disagree with the step lists. Not a second store:
    # nothing is kept here that is not already implied by `step_results`.
    capability_status: Dict[str, AgentStatus] = field(default_factory=dict)

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
    # The AUTHORITATIVE typed network-state contracts, keyed by capability.
    # Same rationale as `rei_registry`: `engine_results` holds a flattened
    # projection that discards per-facility utilisation and per-lane flow, and
    # nothing can reconstruct those from it. The Digital Twin projection reads
    # these, so what a viewer sees is the engine's own output rather than a
    # re-derivation from summary figures.
    network_states: Dict[str, Any] = field(default_factory=dict)
    # Handles to Digital Twin states published for this run.
    twin_refs: List[Any] = field(default_factory=list)
    # The AUTHORITATIVE typed `ForecastResult`. Same rationale as `rei_registry`
    # above: `engine_results` holds a flattened projection for transport, and
    # rebuilding a result from it would lose per-series calculation status —
    # making a series that FAILED indistinguishable from one nobody asked for.
    forecast_result: Optional[Any] = None
    # The typed `ExtractionResult`, when extraction was executed through the
    # capability seam. Same rationale as the fields above: `engine_results`
    # holds a flattened projection, and rebuilding a result from it would lose
    # the per-finding validation detail that distinguishes "accepted with
    # warnings" from "rejected".
    #
    # Usually None. Extraction normally runs BEFORE an execution exists — it
    # produces the network a run is later pinned to — so most runs never touch
    # this. It is populated only when a caller executes `extraction.parse`
    # against a context of its own.
    extraction_result: Optional[Any] = None
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

    def _record_capability_status(self, capability: str, incoming: AgentStatus) -> None:
        """
        Fold one step outcome into the capability-level view.

        A capability can be carried by several steps — a comparison run solves
        two scenarios through the same capability. When those steps disagree,
        neither extreme is honest: reporting SUCCESS hides a failed solve, and
        reporting failure discards a solve that worked. PARTIAL is the accurate
        answer, and it is exactly what PARTIAL exists to say.

        A repeat SUCCESS after a failure does clear it, matching the existing
        behaviour of `unavailable_evidence` on the retry and approval-resume
        paths: the capability has since produced a good result for that step.
        """
        previous = self.capability_status.get(capability)
        if previous is None or previous == incoming:
            self.capability_status[capability] = incoming
            return

        succeeded = {AgentStatus.SUCCESS, AgentStatus.PARTIAL}
        if previous in succeeded and incoming not in succeeded:
            # Something worked and something did not.
            self.capability_status[capability] = AgentStatus.PARTIAL
        else:
            self.capability_status[capability] = incoming

    def record_step(self, step_id: str, result: ToolResult) -> None:
        """Attach a step outcome and update progress bookkeeping."""
        self.step_results[step_id] = result
        # Explicit status wins; otherwise it is derived from the same evidence
        # the boolean and the error code already carry, so nothing new is
        # inferred here.
        self._record_capability_status(
            result.capability,
            result.status or AgentResult.classify(
                success=result.success,
                error_code=result.error_code,
                failure_class=result.failure_class,
            ),
        )
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
        # `setdefault`, not assignment. A step that FAILED has already recorded
        # its own classification a moment ago, and overwriting it here would
        # turn every engine fault into "the inputs were missing" — losing the
        # retryable/non-retryable distinction that this record exists to
        # preserve. Only a capability with no outcome at all is INSUFFICIENT.
        self.capability_status.setdefault(capability, AgentStatus.INSUFFICIENT_EVIDENCE)

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

    # ------------------------------------------------------------------
    # Capability-level view
    # ------------------------------------------------------------------
    #
    # Everything here is DERIVED from `capability_status`, `step_results` and the
    # plan. No new state is stored, so these views cannot drift from the step
    # bookkeeping they summarise.

    def completed_capabilities(self) -> List[str]:
        """Capabilities that produced usable output, complete or partial."""
        return sorted(
            c for c, s in self.capability_status.items()
            if s in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)
        )

    def failed_capabilities(self) -> List[str]:
        """
        Capabilities that produced nothing usable.

        Includes INSUFFICIENT_EVIDENCE and INVALID_OUTPUT: from a consumer's
        point of view all three mean "do not read a number from this". The
        status itself still distinguishes why, which is what governance and the
        narrative need.
        """
        return sorted(
            c for c, s in self.capability_status.items()
            if s not in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)
        )

    def pending_capabilities(self) -> List[str]:
        """
        Capabilities the plan still expects and that have not settled.

        Computed from the plan each time rather than maintained as a list — a
        stored copy is one more thing that can fall out of step with the steps
        it describes. With no plan yet, `required_capabilities` is used, which is
        what the planner fills in before a plan exists.
        """
        if self.plan is None:
            return sorted(
                set(self.required_capabilities) - set(self.capability_status)
            )
        expected = {
            s.capability for s in self.plan.steps
            if s.status not in (StepStatus.SKIPPED, StepStatus.CANCELLED)
        }
        return sorted(expected - set(self.capability_status))

    def capability_outcome(self, capability: str) -> Optional[AgentStatus]:
        """Recorded outcome for one capability, or None if it never ran."""
        return self.capability_status.get(capability)

    def typed_output(self, capability: str, authoritative_field: str) -> Any:
        """
        The AUTHORITATIVE typed result for a capability.

        `authoritative_field` comes from the capability's contract, so the
        mapping from capability to field is declared in exactly one place. This
        method deliberately does not keep its own copy of that mapping — a
        second copy is how the two come to disagree.

        Reads the typed field, never `engine_results`. That dict holds a
        flattened projection for transport: rebuilding a registry or a forecast
        from it would default a FAILED node's status to OK and make a series
        nobody could compute indistinguishable from one nobody asked for.
        """
        if not authoritative_field:
            return None
        value = getattr(self, authoritative_field, None)
        if isinstance(value, dict):
            # Capability-keyed containers such as `network_states`. Prefer this
            # capability's own entry; a scenario solve stores under a
            # scenario-qualified key, so fall back to the sole entry when there
            # is exactly one and no ambiguity about which run it describes.
            if capability in value:
                return value[capability]
            return next(iter(value.values())) if len(value) == 1 else None
        return value

    def agent_result(
        self,
        capability: str,
        *,
        authoritative_field: str = "",
        agent: str = "",
    ) -> AgentResult:
        """
        Express one capability's outcome through the common contract.

        A VIEW over what is already recorded, built on demand. The context keeps
        storing what it always stored; this only presents it in the shape the
        future planner reads, which is why introducing the contract changed no
        behaviour.

        Returns an INSUFFICIENT_EVIDENCE result for a capability that never ran
        — never an empty success, and never a zero.
        """
        result = next(
            (r for r in reversed(list(self.step_results.values()))
             if r.capability == capability),
            None,
        )
        if result is None:
            missing = self.unavailable_evidence.get(capability)
            return AgentResult.insufficient_evidence(
                capability,
                reason=(missing.reason if missing else "capability did not run in this execution"),
                status=(missing.status if missing else EvidenceStatus.NOT_RUN),
                agent=agent,
                execution_id=self.execution_id,
            )

        status = self.capability_status.get(capability)
        output = self.typed_output(capability, authoritative_field)
        # A PARTIAL or SUCCESS must carry output. When the typed field is empty
        # — the capability keeps no domain object, or its field was never set —
        # fall back to the flattened projection so the envelope's invariant
        # holds without inventing anything: this is the same dict every existing
        # consumer already reads.
        if output is None and result.success:
            output = self.engine_results.get(capability)
        if output is None and status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL):
            return AgentResult.insufficient_evidence(
                capability,
                reason="capability reported success but recorded no output",
                status=EvidenceStatus.INVALID,
                agent=agent,
                execution_id=self.execution_id,
            )

        return AgentResult.from_tool_result(
            result,
            output=output,
            agent=agent,
            execution_id=self.execution_id,
            snapshot_id=self.baseline_snapshot_id,
            scenario_id=self.scenario_id,
            unavailable={
                k: v for k, v in self.unavailable_evidence.items() if k == capability
            },
            complete=(status != AgentStatus.PARTIAL),
        )

    def capability_provenance(self) -> Dict[str, Dict[str, Any]]:
        """
        Per-capability provenance, for the audit trail and the twin.

        Says which snapshot and scenario each outcome describes, so two results
        pinned to different data versions can never be presented as evidence
        about the same network.
        """
        return {
            capability: {
                "status": status.value,
                "snapshot_id": self.baseline_snapshot_id,
                "scenario_id": self.scenario_id,
                "execution_id": self.execution_id,
                "authoritative": next(
                    (r.execution_mode.value == "DETERMINISTIC"
                     for r in self.step_results.values()
                     if r.capability == capability),
                    False,
                ),
            }
            for capability, status in sorted(self.capability_status.items())
        }

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
            market_signals=list(request.market_signals),
            baseline_snapshot_id=request.network_snapshot_id or current_snapshot_id,
            llm_enabled=not request.disable_llm,
        )
