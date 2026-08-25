"""
Orchestrator — Adapters onto the common result contract.

Three capabilities are real but are not plan steps, so they never pass through
`CapabilityTool` and never produce a `ToolResult`: extraction runs before an
execution exists, the Digital Twin projection runs after the plan settles, and
signal routing is a gated stage inside the forecast handler. Each already has a
perfectly good domain result, and each expresses success in its own vocabulary.

These functions TRANSLATE those vocabularies into `AgentStatus`. That is all
they do. No algorithm moved here, no threshold is applied here, and no status is
invented — every mapping below is a restatement of a decision the specialist had
already made and recorded.

The domain object stays authoritative and is carried through untouched, so a
caller that wants `ExtractionResult.validation_results` or a twin state's
per-facility detail reads it off `AgentResult.output` exactly as before.

Adapters, not wrappers: nothing calls a specialist from here. Each takes a
result that already exists.
"""

from __future__ import annotations

from typing import Any, List, Optional

from netgravity.orchestrator.schemas.agent_result import (
    AgentError,
    AgentResult,
    ResultProvenance,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionMode,
    UnavailableEvidence,
)

#: Capability ids, imported lazily inside each function to keep this module free
#: of an import cycle through the planner.


def _provenance(
    capability: str,
    provider: str,
    execution_id: str,
    snapshot_id: Optional[str] = None,
    scenario_id: Optional[str] = None,
    mode: ExecutionMode = ExecutionMode.DETERMINISTIC,
) -> ResultProvenance:
    return ResultProvenance(
        capability=capability,
        provider=provider,
        execution_id=execution_id,
        snapshot_id=snapshot_id,
        scenario_id=scenario_id,
        execution_mode=mode,
    )


def extraction_to_agent_result(
    result: Any,
    *,
    execution_id: str = "",
) -> AgentResult:
    """
    Express an `ExtractionResult` through the common contract.

    The mapping preserves a distinction extraction went to some trouble to make:

      ACCEPTED               -> SUCCESS
      WARNING                -> PARTIAL   usable, with findings attached
      HUMAN_REVIEW_REQUIRED  -> PARTIAL   usable ONLY after a person confirms;
                                          the warning says so, and governance —
                                          not this adapter — decides what that
                                          means for the action
      REJECTED               -> INVALID_OUTPUT

    REJECTED becomes INVALID_OUTPUT rather than a failure because nothing broke:
    the pipeline ran correctly and refused the data. Calling that a failure would
    point an operator at the extractor instead of at the file.

    HUMAN_REVIEW_REQUIRED is deliberately NOT collapsed into REJECTED. Rejection
    means the data cannot be used; review means it might be, once confirmed. The
    contract keeps both usable-with-a-caveat, and the caveat travels in
    `warnings` where a caller cannot miss it.
    """
    from netgravity.orchestrator.core.planner import CAP_EXTRACT
    from netgravity.orchestrator.schemas.extraction import (
        ExtractionStatus,
        ValidationSeverity,
    )

    mapping = {
        ExtractionStatus.ACCEPTED: AgentStatus.SUCCESS,
        ExtractionStatus.WARNING: AgentStatus.PARTIAL,
        ExtractionStatus.HUMAN_REVIEW_REQUIRED: AgentStatus.PARTIAL,
        ExtractionStatus.REJECTED: AgentStatus.INVALID_OUTPUT,
    }
    status = mapping[result.status]

    errors: List[AgentError] = []
    warnings: List[str] = []
    for finding in result.validation_results:
        severity = getattr(finding.severity, "value", str(finding.severity))
        if finding.severity == ValidationSeverity.ERROR:
            errors.append(AgentError(
                code=finding.code, message=finding.message,
                failure_class="NON_RETRYABLE", context=dict(finding.where or {}),
            ))
        else:
            warnings.append(f"{severity}: {finding.code}: {finding.message}")

    if status == AgentStatus.INVALID_OUTPUT and not errors:
        # The invariant requires an explanation for an unusable result, and a
        # rejection without a recorded finding would be exactly the silent
        # refusal this contract exists to prevent.
        errors.append(AgentError(
            code="EXTRACTION_REJECTED",
            message="Extraction rejected the source; see validation_results.",
            failure_class="NON_RETRYABLE",
        ))

    if result.status == ExtractionStatus.HUMAN_REVIEW_REQUIRED:
        warnings.insert(0, (
            "extraction requires human review before this data is used; "
            "PARTIAL means usable-once-confirmed, not confirmed"
        ))

    return AgentResult(
        capability=CAP_EXTRACT,
        status=status,
        output=result if status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL) else None,
        agent="ExtractionParsingAgent",
        execution_id=execution_id,
        errors=errors,
        warnings=warnings,
        provenance=_provenance(
            CAP_EXTRACT, "ExtractionParsingAgent", execution_id,
            snapshot_id=result.snapshot_id,
        ),
        metadata={"ingestion_id": result.ingestion_id,
                  "extraction_status": result.status.value},
    )


def twin_state_to_agent_result(
    state: Any,
    *,
    execution_id: str = "",
) -> AgentResult:
    """
    Express a published `DigitalTwinState` through the common contract.

    The twin publishes in every condition on purpose — refusing to draw a failed
    run would leave a viewer looking at a stale picture with no sign anything had
    gone wrong. So the mapping keeps FAILED, INFEASIBLE and STALE states
    representable while making clear no figure may be read off them:

      COMPLETE    -> SUCCESS
      PARTIAL     -> PARTIAL
      INFEASIBLE  -> NON_RETRYABLE_FAILURE   the solver PROVED no solution;
                                             re-running gives the same answer
      STALE       -> NON_RETRYABLE_FAILURE   the snapshot moved under the run
      FAILED      -> NON_RETRYABLE_FAILURE

    A state that is published but carries no usable numbers therefore arrives
    with `output=None`, and the state itself is preserved in `metadata` so the
    viewer layer can still render the explicitly-empty picture.
    """
    from netgravity.orchestrator.core.planner import CAP_TWIN_PUBLISH
    from netgravity.orchestrator.schemas.twin import TwinCalculationStatus

    mapping = {
        TwinCalculationStatus.COMPLETE: AgentStatus.SUCCESS,
        TwinCalculationStatus.PARTIAL: AgentStatus.PARTIAL,
        TwinCalculationStatus.INFEASIBLE: AgentStatus.NON_RETRYABLE_FAILURE,
        TwinCalculationStatus.STALE: AgentStatus.NON_RETRYABLE_FAILURE,
        TwinCalculationStatus.FAILED: AgentStatus.NON_RETRYABLE_FAILURE,
    }
    calc_status = state.calculation_status
    status = mapping[calc_status]
    usable = status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)

    errors: List[AgentError] = []
    if not usable:
        errors.append(AgentError(
            code=f"TWIN_{calc_status.value}",
            message=(f"Twin state {state.state_id} published as "
                     f"{calc_status.value}; it carries no usable engine result."),
            failure_class="NON_RETRYABLE",
        ))

    # `DigitalTwinState.unavailable` is a list of `UnavailableValue` keyed by
    # dotted FIELD path; the contract's map is keyed by CAPABILITY. Both
    # vocabularies already exist and neither is wrong, so the translation keys on
    # the capability the twin recorded and keeps the field path in the reason —
    # nothing is dropped, and a value with no known cause is still reported.
    unavailable = {}
    for value in list(getattr(state, "unavailable", []) or []):
        key = value.capability or value.field
        detail = value.reason or f"{value.field} was not available"
        unavailable[key] = UnavailableEvidence(
            capability=key,
            status=EvidenceStatus.UNAVAILABLE,
            reason=f"{value.field}: {detail}",
        )

    metadata: dict = {"state_id": state.state_id,
                      "calculation_status": calc_status.value}
    if not usable:
        metadata["published_state"] = state.state_id

    return AgentResult(
        capability=CAP_TWIN_PUBLISH,
        status=status,
        output=state if usable else None,
        agent="DigitalTwinService",
        execution_id=execution_id,
        errors=errors,
        unavailable=unavailable,
        provenance=_provenance(
            CAP_TWIN_PUBLISH, "DigitalTwinService", execution_id,
            snapshot_id=getattr(state, "snapshot_id", None),
            scenario_id=getattr(state, "scenario_id", None),
        ),
        metadata=metadata,
    )


def routing_decision_to_agent_result(
    decision: Any,
    *,
    execution_id: str = "",
) -> AgentResult:
    """
    Express a `SignalRoutingDecision` through the common contract.

    Routing always SUCCEEDS at deciding, even when it routes nothing — a refusal
    is a correct answer, not a failure, and every signal offered appears in
    `records` either way. So:

      any signal accepted   -> SUCCESS
      signals offered, none accepted
                            -> PARTIAL, with each refusal named in `warnings`
      nothing offered       -> SUCCESS with an empty decision

    PARTIAL rather than SUCCESS in the middle case because a caller that asked
    for enrichment and received none should be told, and rather than a failure
    because the router did its job.

    Note what this result is NOT. The confidence score behind these outcomes
    governs forecast eligibility only. It is not an event probability and can
    never reach `RF = P + REI - P*REI`; that number comes from
    `external.interpret_signal`, a different capability in a different domain.
    """
    from netgravity.orchestrator.core.planner import CAP_ROUTE_SIGNAL

    accepted = list(getattr(decision, "accepted", []) or [])
    records = list(getattr(decision, "records", []) or [])

    warnings = [
        f"signal '{r.signal_id}' not routed: {r.outcome.value}"
             + (f" ({r.reason})" if r.reason else "")
        for r in records if not r.routed
    ]

    status = (
        AgentStatus.SUCCESS if accepted or not records
        else AgentStatus.PARTIAL
    )

    return AgentResult(
        capability=CAP_ROUTE_SIGNAL,
        status=status,
        output=decision,
        agent="ExternalSignalRouter",
        execution_id=execution_id,
        warnings=warnings,
        provenance=_provenance(CAP_ROUTE_SIGNAL, "ExternalSignalRouter", execution_id),
        metadata={"n_accepted": len(accepted), "n_offered": len(records),
                  "outcomes": decision.outcome_counts()
                  if hasattr(decision, "outcome_counts") else {}},
    )


__all__ = [
    "extraction_to_agent_result",
    "routing_decision_to_agent_result",
    "twin_state_to_agent_result",
]
