"""
Orchestrator — Common agent execution contract.

Every specialist the orchestrator can invoke reports through one shape:
`AgentResult[T]`. The point is not uniformity for its own sake — it is that a
future planner must be able to read an outcome and decide what to do next
WITHOUT knowing which specialist produced it.

Two rules shape the design.

First, the domain result stays authoritative. `AgentResult` is a typed envelope
around `ForecastResult`, `FacilityResilienceRegistry`, `OptimizationResult` and
the rest — it never flattens them into a dictionary and never re-derives a
number. `AgentResult[ForecastResult]` carries the forecast itself, so nothing
downstream has to reconstruct per-series calculation status from a summary.

Second, absence is never zero. The status enum separates the ways a run can fail
to hand over trustworthy output, and the model REFUSES to hold output alongside
a failing status. A caller cannot accidentally read `0` out of a result that
means "we could not measure this" — the field is None and the status says why.

The statuses are derived from evidence the codebase already produces
(`FailureClass`, `ErrorCode`, `EvidenceStatus`, `DependencyResolution`), so this
module unifies existing semantics rather than inventing a second vocabulary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionMode,
    ToolResult,
    UnavailableEvidence,
)

#: The authoritative domain payload. Deliberately unbounded: the envelope must
#: not constrain what an engine is allowed to return.
T = TypeVar("T")


#: Statuses that mean "there is no output you may trust". The model enforces
#: `output is None` for these, which is what stops a failure from being read as
#: a legitimate zero.
NO_OUTPUT_STATUSES = frozenset({
    AgentStatus.RETRYABLE_FAILURE,
    AgentStatus.NON_RETRYABLE_FAILURE,
    AgentStatus.INVALID_OUTPUT,
    AgentStatus.INSUFFICIENT_EVIDENCE,
})

#: Statuses that carry output a caller may use. PARTIAL is included on purpose:
#: incomplete evidence is still evidence, provided the caller is told.
USABLE_STATUSES = frozenset({
    AgentStatus.SUCCESS,
    AgentStatus.PARTIAL,
})

#: Error codes meaning "the inputs were not there", as distinct from "the work
#: was attempted and broke". Nothing malfunctioned in these cases, so reporting
#: them as a failure would misdirect whoever reads the trace.
_EVIDENCE_CODES = frozenset({"MISSING_DATA", "DEPENDENCY_FAILURE"})

#: Error codes meaning the specialist ran and produced something the validators
#: rejected. The output exists but must not be consumed.
_INVALID_CODES = frozenset({"VALIDATION_FAILURE"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentError(BaseModel):
    """One structured failure, kept machine-readable for the audit trail."""
    code: str
    message: str
    #: RETRYABLE / NON_RETRYABLE / REQUIRES_HUMAN, from `FailureClass`. Carried
    #: verbatim rather than re-inferred, so retry policy in a later phase reads
    #: the engine's own classification.
    failure_class: str = ""
    context: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ResultProvenance(BaseModel):
    """
    Where a result came from and which data version it describes.

    Immutable identifiers only. This is what makes two results comparable: a
    forecast pinned to one snapshot and an REI sweep pinned to another are not
    evidence about the same network, and the envelope has to make that visible.
    """
    capability: str = ""
    #: Class or module that actually did the work, e.g. "ForecastingService".
    provider: str = ""
    execution_id: str = ""
    snapshot_id: Optional[str] = None
    scenario_id: Optional[str] = None
    #: DETERMINISTIC output is reproducible from its inputs; PROBABILISTIC is
    #: not, and may not be treated as authoritative.
    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    duration_seconds: float = 0.0
    attempts: int = 1
    recorded_at: str = Field(default_factory=_utc_now)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def is_authoritative(self) -> bool:
        """True only for reproducible output. Narrative text is never this."""
        return self.execution_mode == ExecutionMode.DETERMINISTIC


class AgentResult(BaseModel, Generic[T]):
    """
    The single execution contract the future orchestrator reads.

    Generic in the domain payload so the type survives the trip:
    `AgentResult[ForecastResult]`, not `AgentResult` wrapping a loose dict.
    """
    capability: str
    status: AgentStatus

    #: The authoritative domain object. None whenever `status` forbids output.
    output: Optional[T] = None

    #: Which specialist produced this, for the trace.
    agent: str = ""
    execution_id: str = ""

    errors: List[AgentError] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    #: Why output is missing or incomplete, keyed by capability. Populated for
    #: PARTIAL and INSUFFICIENT_EVIDENCE so a caller can tell which input was
    #: absent instead of guessing.
    unavailable: Dict[str, UnavailableEvidence] = Field(default_factory=dict)

    provenance: ResultProvenance = Field(default_factory=ResultProvenance)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def output_must_match_status(self) -> "AgentResult[T]":
        """
        Refuse the shapes that would let a caller misread the result.

        A failing status holding output invites `result.output or 0`. A SUCCESS
        holding nothing invites the opposite mistake. Both are rejected at
        construction, so the impossible states cannot reach a consumer.
        """
        if self.status in NO_OUTPUT_STATUSES and self.output is not None:
            raise ValueError(
                f"AgentResult for '{self.capability}' has status "
                f"{self.status.value} but carries output. A result that cannot "
                f"be trusted must not present a value: the caller would read it "
                f"as a measurement. Put the rejected payload in `metadata` if "
                f"it is needed for diagnosis."
            )
        if self.status in USABLE_STATUSES and self.output is None:
            raise ValueError(
                f"AgentResult for '{self.capability}' has status "
                f"{self.status.value} but no output. Use INSUFFICIENT_EVIDENCE "
                f"when there was nothing to compute, or a failure status when "
                f"the attempt broke."
            )
        if self.status in NO_OUTPUT_STATUSES and not self.errors and not self.unavailable:
            raise ValueError(
                f"AgentResult for '{self.capability}' has status "
                f"{self.status.value} but records neither an error nor missing "
                f"evidence. An unexplained failure cannot be acted on."
            )
        return self

    # ------------------------------------------------------------------
    # Predicates the planner will branch on
    # ------------------------------------------------------------------

    @property
    def is_success(self) -> bool:
        """Complete and valid. Not true for PARTIAL."""
        return self.status == AgentStatus.SUCCESS

    @property
    def is_usable(self) -> bool:
        """Output exists and may be consumed, complete or not."""
        return self.status in USABLE_STATUSES

    @property
    def is_failure(self) -> bool:
        """The attempt did not yield trustworthy output, for any reason."""
        return self.status in NO_OUTPUT_STATUSES

    @property
    def is_retryable(self) -> bool:
        """
        Whether re-running could plausibly succeed.

        Reported here; ACTED ON nowhere in this phase. Retry orchestration is
        deliberately not implemented yet.
        """
        return self.status == AgentStatus.RETRYABLE_FAILURE

    def require(self) -> T:
        """
        Return the output or raise.

        For callers that genuinely cannot proceed without it. Preferred over
        `result.output or <default>`, which is how a missing measurement turns
        into a fabricated number.
        """
        if self.output is None:
            detail = "; ".join(e.message for e in self.errors) or self.status.value
            raise ValueError(
                f"Capability '{self.capability}' produced no usable output "
                f"({self.status.value}): {detail}"
            )
        return self.output

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @staticmethod
    def classify(
        *,
        success: bool,
        error_code: Optional[str] = None,
        failure_class: Optional[str] = None,
        validation_errors: Sequence[str] = (),
        degraded: Sequence[str] = (),
        unavailable: Sequence[str] = (),
        complete: bool = True,
    ) -> AgentStatus:
        """
        Map existing failure evidence onto one status.

        Kept a pure function so the classification is testable on its own and
        identical everywhere. It reads only signals the codebase already
        produces; nothing here is a new judgement about severity.
        """
        if not success:
            if error_code in _INVALID_CODES or validation_errors:
                return AgentStatus.INVALID_OUTPUT
            if error_code in _EVIDENCE_CODES:
                return AgentStatus.INSUFFICIENT_EVIDENCE
            if failure_class == "RETRYABLE":
                return AgentStatus.RETRYABLE_FAILURE
            return AgentStatus.NON_RETRYABLE_FAILURE

        # Succeeded, but validation may still reject what it produced. A
        # rejected result is worse than a missing one, because it looks usable.
        if validation_errors:
            return AgentStatus.INVALID_OUTPUT
        if not complete or degraded or unavailable:
            return AgentStatus.PARTIAL
        return AgentStatus.SUCCESS

    @classmethod
    def from_tool_result(
        cls,
        result: ToolResult,
        *,
        output: Optional[T] = None,
        agent: str = "",
        execution_id: str = "",
        snapshot_id: Optional[str] = None,
        scenario_id: Optional[str] = None,
        validation_errors: Sequence[str] = (),
        degraded: Sequence[str] = (),
        unavailable: Optional[Dict[str, UnavailableEvidence]] = None,
        complete: bool = True,
    ) -> "AgentResult[T]":
        """
        Build the envelope from what the executor already recorded.

        The existing `ToolResult` remains the executor's own contract; this is a
        view over it. Nothing about execution changes — which is the point, in a
        phase that must not alter behaviour.

        `output` is the AUTHORITATIVE typed object, passed in by whoever holds it
        (the execution context). It is never reconstructed from `result.output`,
        the flattened transport projection.
        """
        missing = dict(unavailable or {})
        status = result.status or cls.classify(
            success=result.success,
            error_code=result.error_code,
            failure_class=result.failure_class,
            validation_errors=validation_errors,
            degraded=degraded,
            unavailable=list(missing),
            complete=complete,
        )

        errors: List[AgentError] = []
        if not result.success:
            errors.append(AgentError(
                code=result.error_code or "ENGINE_FAILURE",
                message=result.error_message or "capability failed without a message",
                failure_class=result.failure_class or "",
                context=dict(result.metadata.get("error_context") or {}),
            ))
        for message in validation_errors:
            errors.append(AgentError(
                code="VALIDATION_FAILURE",
                message=message,
                failure_class="NON_RETRYABLE",
            ))

        warnings = [f"degraded: missing upstream '{d}'" for d in degraded]

        # A status that forbids output must not silently keep it. Dropping the
        # payload here is intentional: it is unusable by definition, and
        # `metadata` preserves it for diagnosis.
        metadata = dict(result.metadata)
        if status in NO_OUTPUT_STATUSES:
            if result.output:
                metadata["rejected_output"] = result.output
            output = None
            if not errors and not missing:
                errors.append(AgentError(
                    code=result.error_code or "UNSPECIFIED",
                    message=f"capability '{result.capability}' produced no usable output",
                    failure_class=result.failure_class or "",
                ))

        return cls(
            capability=result.capability,
            status=status,
            output=output,
            agent=agent,
            execution_id=execution_id,
            errors=errors,
            warnings=warnings,
            unavailable=missing,
            provenance=ResultProvenance(
                capability=result.capability,
                provider=agent,
                execution_id=execution_id,
                snapshot_id=snapshot_id,
                scenario_id=scenario_id,
                execution_mode=result.execution_mode,
                duration_seconds=result.duration_seconds,
                attempts=result.attempts,
            ),
            metadata=metadata,
        )

    @classmethod
    def insufficient_evidence(
        cls,
        capability: str,
        *,
        reason: str,
        status: EvidenceStatus = EvidenceStatus.NOT_RUN,
        agent: str = "",
        execution_id: str = "",
    ) -> "AgentResult[T]":
        """
        A capability that could not be attempted for want of inputs.

        Separate from a failure constructor on purpose. "Nobody could compute
        this" and "this broke" call for different responses, and collapsing them
        is how a gap in the evidence becomes an apparent engine fault.
        """
        return cls(
            capability=capability,
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            output=None,
            agent=agent,
            execution_id=execution_id,
            unavailable={capability: UnavailableEvidence(
                capability=capability, status=status, reason=reason,
            )},
            provenance=ResultProvenance(
                capability=capability, provider=agent, execution_id=execution_id,
            ),
        )


__all__ = [
    "AgentError",
    "AgentResult",
    "AgentStatus",
    "NO_OUTPUT_STATUSES",
    "ResultProvenance",
    "USABLE_STATUSES",
]
