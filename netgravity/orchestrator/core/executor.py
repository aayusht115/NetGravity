"""
Orchestrator — The capability execution seam.

Before this, invoking a capability meant touching three places: `CapabilityTool`
ran the handler and classified errors, `Orchestrator._run_step` checked
authorization and assembled the request, and `Orchestrator._execute_plan` gated
dependencies and recorded the outcome. Anything wanting to run one capability had
to reproduce all three, or reach past them into a service directly — which is
what extraction, the twin projection and signal routing each did.

`CapabilityExecutor` is the one seam. Every invocation goes through the same nine
steps, in the same order, and lands the same way in the same place.

WHAT IT IS NOT
--------------
Not a second executor. Invocation is still `CapabilityTool.execute` — timeout
enforcement, error classification and the existing bounded retry policies live
there, untouched. This wraps that with the checks and the normalisation that
previously had no home.

Not a planner. It runs the capability it is given. It never chooses what runs
next, never reorders, never substitutes a provider, and never schedules a
dependency. Given a capability whose inputs are not satisfied it REFUSES and
says so — deciding what to do about that belongs to the caller.

Not a retry loop. It adds no retry, no fallback, no escalation and no circuit
breaker. Where a capability already carries a `RetryPolicy` that policy still
applies inside `CapabilityTool`, exactly as before; the attempt count is
reported through provenance so the caller can see it happened.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from netgravity.orchestrator.exceptions import (
    CapabilityNotFoundError,
    OrchestratorError,
)
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.agent_result import (
    AgentError,
    AgentResult,
    ResultProvenance,
)
from netgravity.orchestrator.schemas.capability import CapabilityContract
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionMode,
    ToolRequest,
    ToolResult,
    UnavailableEvidence,
)

if TYPE_CHECKING:  # pragma: no cover
    from netgravity.orchestrator.core.execution_context import ExecutionContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightRefusal:
    """
    Why a capability was not invoked.

    A refusal is not a failure: nothing ran, so nothing broke. Kept as its own
    type so the reason survives into the `AgentResult` verbatim instead of being
    flattened into a message string.
    """
    reason: str
    #: What was missing, keyed by the capability or input that is absent.
    missing: Dict[str, str]
    evidence_status: EvidenceStatus = EvidenceStatus.NOT_RUN


class CapabilityExecutor:
    """
    Executes exactly one capability, and records exactly one outcome.

    Construct with the registry the orchestrator already built. The executor
    holds no state of its own: everything it records goes into the
    `ExecutionContext` it is handed, which remains the single execution store.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    # ==================================================================
    # The seam
    # ==================================================================

    async def execute(
        self,
        capability_id: str,
        context: "ExecutionContext",
        *,
        params: Optional[Dict[str, Any]] = None,
        upstream: Optional[Dict[str, Any]] = None,
        unavailable: Optional[Dict[str, UnavailableEvidence]] = None,
        step_id: Optional[str] = None,
        record: bool = True,
    ) -> AgentResult:
        """
        Run one capability and return its standardised outcome.

        The nine steps, in order:

          1. resolve the capability from the registry
          2. refuse if it is not registered
          3. validate declared inputs
          4. verify required dependencies are satisfied
          5. invoke the registered implementation via `CapabilityTool`
          6. normalise the outcome into an `AgentResult`
          7. validate the output against the declared contract
          8. record the execution on the `ExecutionContext`, exactly once
          9. return the `AgentResult`

        Steps 1-4 are preflight. A refusal there means the handler was never
        called, so the result is INSUFFICIENT_EVIDENCE rather than a failure —
        and `record=True` still records it, because "we did not run this, and
        here is why" is exactly the kind of absence a caller must not have to
        infer.

        Args:
            capability_id: Declared capability to run.
            context: The run this execution belongs to. Mutated: this is where
                the outcome is recorded.
            params: Handler parameters, becoming `ToolRequest.params`.
            upstream: Outputs of already-completed steps, keyed by step id.
                Assembled by the caller, which knows the plan topology.
            unavailable: Expected-but-missing evidence to hand the handler, so it
                can degrade honestly rather than inferring a default.
            step_id: Plan step this execution belongs to, when it has one.
                Defaults to the capability id for a direct invocation, so a
                standalone execution is still recorded under a stable key.
            record: Set False only to evaluate a capability without touching the
                run's state. Off the recording path nothing is written.

        Returns:
            `AgentResult`. Never raises for a capability-level failure — the
            failure IS the return value, which is what lets a caller inspect it
            instead of catching it.
        """
        started = time.perf_counter()
        effective_step = step_id or capability_id
        request_params = dict(params or {})

        # ---- 1 & 2. resolve --------------------------------------------
        try:
            capability = self.registry.get(capability_id)
        except CapabilityNotFoundError as exc:
            # The one case that is a caller error rather than a data gap: asking
            # for something that does not exist is a mistake to surface, not an
            # absence to work around. Still returned rather than raised, so a
            # caller iterating capabilities is not derailed by one bad name.
            return self._refused(
                capability_id, context, effective_step,
                status=AgentStatus.NON_RETRYABLE_FAILURE,
                error=AgentError(
                    code="CAPABILITY_NOT_FOUND",
                    message=exc.message,
                    failure_class="NON_RETRYABLE",
                    context={"requested": capability_id},
                ),
                record=record,
                duration=time.perf_counter() - started,
            )

        contract = capability.contract or (
            self.registry.contract(capability_id)
            if self.registry.has_contract(capability_id) else None
        )

        # ---- 3 & 4. preflight ------------------------------------------
        refusal = (
            self._check_inputs(contract, request_params)
            or self._check_dependencies(contract, context, step_id)
        )
        if refusal is not None:
            logger.info(
                "orchestrator.executor.refused capability=%s reason=%s missing=%s %s",
                capability_id, refusal.reason, sorted(refusal.missing),
                context.correlation(),
            )
            return self._insufficient(
                capability_id, contract, context, effective_step, refusal,
                record=record, duration=time.perf_counter() - started,
            )

        # ---- 5. invoke -------------------------------------------------
        # Straight through the EXISTING tool. Timeout, error classification and
        # any pre-existing retry policy are its business, unchanged.
        tool = self.registry.tool(capability_id)
        try:
            tool_result = await tool.execute(
                context,
                ToolRequest(
                    capability=capability_id,
                    params=request_params,
                    upstream=dict(upstream or {}),
                    unavailable=dict(unavailable or {}),
                ),
            )
        except OrchestratorError as exc:
            # `CapabilityTool` classifies almost everything itself; this covers
            # a fault raised on the way in. Never swallowed silently.
            tool_result = ToolResult(
                capability=capability_id, success=False, output={},
                error_code=exc.code.value, error_message=exc.message,
                failure_class=exc.failure_class.value,
                duration_seconds=round(time.perf_counter() - started, 4),
                execution_mode=capability.execution_mode,
                metadata={"error_context": exc.context},
            )

        # ---- 6 & 7. normalise, then validate the output ----------------
        validation_errors = self._validate_output(contract, tool_result, context)
        agent_result = self._normalise(
            tool_result, contract, context, effective_step, validation_errors,
        )

        # ---- 8. record, exactly once -----------------------------------
        if record:
            self._record(context, effective_step, tool_result, agent_result)

        logger.info(
            "orchestrator.executor.executed capability=%s status=%s attempts=%d "
            "duration=%.4fs %s",
            capability_id, agent_result.status.value,
            agent_result.provenance.attempts, agent_result.provenance.duration_seconds,
            context.correlation(),
        )
        # ---- 9 ---------------------------------------------------------
        return agent_result

    # ==================================================================
    # 3. Input validation
    # ==================================================================

    @staticmethod
    def _check_inputs(
        contract: Optional[CapabilityContract], params: Dict[str, Any],
    ) -> Optional[PreflightRefusal]:
        """
        Refuse before invoking when a declared input is absent.

        A present-but-None value counts as absent. Passing None through as
        though it were a supplied value is precisely how a missing input becomes
        a default deep inside a handler.
        """
        if contract is None:
            return None
        missing = {
            key: "required input was not supplied"
            for key in contract.required_inputs
            if params.get(key) is None
        }
        if not missing:
            return None
        return PreflightRefusal(
            reason=(f"required input(s) {sorted(missing)} were not supplied to "
                    f"'{contract.capability_id}'"),
            missing=missing,
            evidence_status=EvidenceStatus.NOT_RUN,
        )

    # ==================================================================
    # 4. Dependency validation
    # ==================================================================

    def _check_dependencies(
        self,
        contract: Optional[CapabilityContract],
        context: "ExecutionContext",
        step_id: Optional[str],
    ) -> Optional[PreflightRefusal]:
        """
        Answer one question: are this capability's required dependencies
        satisfied right now?

        Not scheduling. Nothing is queued, ordered or run to satisfy a gap — the
        executor reports the gap and stops.

        Criticality comes from the declaration, never from a guess.
        `required_dependencies` excludes everything the provider handles the
        absence of itself, which is why RF still runs on one input and reports
        NOT_COMPUTABLE for the other rather than being refused here.

        When the execution belongs to a plan step, the plan's own HARD/SOFT
        classification is consulted as well, since a plan may soften an edge for
        one particular workflow. The two are combined by intersection: an edge
        must be required by BOTH the contract and the plan to block. Anything
        else would let this check contradict a decision the plan already made.
        """
        if contract is None:
            return None

        required = list(contract.required_dependencies)
        if not required:
            return None

        plan = context.plan
        step = plan.step(step_id) if (plan is not None and step_id) else None
        if step is not None:
            # The plan may soften an edge this contract calls required. Respect
            # it: the plan describes one concrete workflow, and it is the more
            # specific statement.
            softened = set(step.soft_depends_on)
            soft_capabilities = {
                s.capability for s in plan.steps if s.step_id in softened
            }
            for upstream_id in step.depends_on:
                upstream = plan.step(upstream_id)
                if upstream is not None and upstream.optional:
                    soft_capabilities.add(upstream.capability)
            required = [c for c in required if c not in soft_capabilities]

        missing: Dict[str, str] = {}
        for dependency in required:
            outcome = context.capability_outcome(dependency)
            if outcome in (AgentStatus.SUCCESS, AgentStatus.PARTIAL):
                continue
            missing[dependency] = (
                f"{outcome.value}" if outcome is not None else "has not run"
            )

        if not missing:
            return None
        return PreflightRefusal(
            reason=(f"required dependency/dependencies "
                    f"{sorted(missing)} of '{contract.capability_id}' are not "
                    f"satisfied: "
                    + "; ".join(f"{k} {v}" for k, v in sorted(missing.items()))),
            missing=missing,
            evidence_status=EvidenceStatus.NOT_RUN,
        )

    # ==================================================================
    # 7. Output validation
    # ==================================================================

    def _validate_output(
        self,
        contract: Optional[CapabilityContract],
        tool_result: ToolResult,
        context: "ExecutionContext",
    ) -> List[str]:
        """
        Check what came back against what was declared.

        Deliberately narrow. This checks CONTRACT CONFORMANCE — did the
        capability produce the type it promised — and nothing else.

        It does NOT escalate the warnings that `ResultValidator` already
        produces inside the optimization and REI handlers. Those are advisory by
        design: a KPI slightly outside an expected band is worth flagging and is
        not grounds for discarding a solved network. Turning them into
        INVALID_OUTPUT here would change behaviour the existing tests correctly
        pin, and would suppress results that are fine.

        Returns:
            Human-readable violations. Empty means conformant. A non-empty list
            makes the result INVALID_OUTPUT, so it must only contain reasons the
            output genuinely may not be consumed.
        """
        if contract is None or not tool_result.success:
            return []

        violations: List[str] = []

        # A handler that reports success and returns nothing at all has not
        # produced a result. Checked only when the contract says it should have
        # produced a typed one, so capabilities that legitimately return an
        # empty projection are unaffected.
        typed = (
            context.typed_output(contract.capability_id, contract.authoritative_field)
            if contract.authoritative_field else None
        )
        if contract.authoritative_field and typed is None and not tool_result.output:
            violations.append(
                f"reported success but produced neither a typed "
                f"'{contract.output_type}' on context.{contract.authoritative_field} "
                f"nor any output projection"
            )

        # When both a declared output type and a typed result exist, they must
        # agree. A capability quietly returning a different type is the failure
        # mode that a dictionary-shaped envelope could never catch.
        #
        # Skipped where the field holds an identifier by design — a pinned
        # snapshot id is a `str` and comparing it to "NetworkSnapshot" would
        # reject a correct result. The contract says which fields those are
        # rather than the validator inferring it.
        if typed is not None and contract.output_type \
                and not contract.authoritative_is_reference:
            actual = type(typed).__name__
            if actual != contract.output_type and not self._is_declared_container(
                typed, contract.output_type
            ):
                violations.append(
                    f"declared output type '{contract.output_type}' but produced "
                    f"'{actual}'"
                )

        return violations

    @staticmethod
    def _is_declared_container(typed: Any, declared: str) -> bool:
        """
        Allow a container of the declared type.

        Several capabilities legitimately record a list — `market_signals` holds
        `MarketIntelligenceSignal` records, `twin_refs` holds handles — and the
        contract names the element type because that is the useful thing to
        declare. A container whose members match is conformant.
        """
        if isinstance(typed, (list, tuple)):
            if not typed:
                return True
            return all(type(item).__name__ == declared for item in typed)
        return False

    # ==================================================================
    # 6. Normalisation
    # ==================================================================

    def _normalise(
        self,
        tool_result: ToolResult,
        contract: Optional[CapabilityContract],
        context: "ExecutionContext",
        step_id: str,
        validation_errors: Sequence[str],
    ) -> AgentResult:
        """
        Turn one tool outcome into the standard envelope.

        Reads only evidence the capability itself produced — `success`, its
        `error_code`, its `failure_class`, the declared contract, and the
        context's record of missing evidence. No status is inferred from the
        mere presence of an exception, and none is invented here.

        The typed authoritative object is attached from the context field the
        contract names, so a consumer receives `ForecastResult` or
        `FacilityResilienceRegistry` itself rather than the flattened projection
        that would have lost per-series and per-node status.
        """
        typed = (
            context.typed_output(tool_result.capability, contract.authoritative_field)
            if contract is not None and contract.authoritative_field else None
        )
        output: Any = typed if typed is not None else (tool_result.output or None)

        relevant = {
            capability: evidence
            for capability, evidence in context.unavailable_evidence.items()
            if contract is not None and capability in contract.dependencies
        }
        # A tolerated-but-absent dependency makes the result PARTIAL: the output
        # is real and usable, and it was computed on less than the full picture.
        # Saying SUCCESS here would be the one dishonest option.
        degraded = sorted(relevant)

        return AgentResult.from_tool_result(
            tool_result,
            output=output,
            agent=(contract.provider if contract else ""),
            execution_id=context.execution_id,
            snapshot_id=context.baseline_snapshot_id,
            scenario_id=context.scenario_id,
            validation_errors=list(validation_errors),
            degraded=degraded,
            unavailable=relevant,
            complete=not degraded,
        )

    # ==================================================================
    # 8. Recording
    # ==================================================================

    @staticmethod
    def _record(
        context: "ExecutionContext",
        step_id: str,
        tool_result: ToolResult,
        agent_result: AgentResult,
    ) -> None:
        """
        Record the execution on the existing context. Once.

        `record_step` is the single write path for execution state and stays so
        — it updates the step lists, the capability status, the engine-results
        projection and the missing-evidence map together, which is what keeps
        them from disagreeing.

        The normalised status is written onto the `ToolResult` first, so what the
        context records and what the caller receives cannot diverge. In
        particular a result the executor rejected as INVALID_OUTPUT is recorded
        as invalid, rather than as the success the handler believed it was.
        """
        recorded = tool_result.model_copy(update={"status": agent_result.status})
        context.record_step(step_id, recorded)

        if agent_result.status is AgentStatus.INVALID_OUTPUT:
            # The handler thought it succeeded, so `record_step` will not have
            # registered any absence. Say plainly that the evidence is unusable:
            # a consumer must not read the projection that is still sitting in
            # `engine_results` from the handler's own point of view.
            context.engine_results.pop(tool_result.capability, None)
            context.record_unavailable(
                tool_result.capability,
                reason="; ".join(e.message for e in agent_result.errors)
                       or "output failed contract validation",
                status=EvidenceStatus.INVALID,
                step_id=step_id,
            )

    # ==================================================================
    # Refusal constructors
    # ==================================================================

    def _insufficient(
        self,
        capability_id: str,
        contract: Optional[CapabilityContract],
        context: "ExecutionContext",
        step_id: str,
        refusal: PreflightRefusal,
        *,
        record: bool,
        duration: float,
    ) -> AgentResult:
        """A capability that was never invoked because its inputs were absent."""
        result: AgentResult = AgentResult(
            capability=capability_id,
            status=AgentStatus.INSUFFICIENT_EVIDENCE,
            output=None,
            agent=(contract.provider if contract else ""),
            execution_id=context.execution_id,
            unavailable={
                capability_id: UnavailableEvidence(
                    capability=capability_id,
                    status=refusal.evidence_status,
                    reason=refusal.reason,
                    step_id=step_id,
                ),
                **{
                    name: UnavailableEvidence(
                        capability=name,
                        status=refusal.evidence_status,
                        reason=detail,
                    )
                    for name, detail in refusal.missing.items()
                },
            },
            provenance=ResultProvenance(
                capability=capability_id,
                provider=(contract.provider if contract else ""),
                execution_id=context.execution_id,
                snapshot_id=context.baseline_snapshot_id,
                scenario_id=context.scenario_id,
                execution_mode=(contract.execution_mode if contract
                                else ExecutionMode.DETERMINISTIC),
                duration_seconds=round(duration, 4),
                attempts=0,
            ),
            metadata={"preflight": "refused", "missing": sorted(refusal.missing)},
        )
        if record:
            # Recorded even though nothing ran. An execution that was refused is
            # part of the run's history, and leaving it out would make the gap
            # something a reader has to notice rather than read.
            context.record_unavailable(
                capability_id,
                reason=refusal.reason,
                status=refusal.evidence_status,
                step_id=step_id,
            )
        return result

    def _refused(
        self,
        capability_id: str,
        context: "ExecutionContext",
        step_id: str,
        *,
        status: AgentStatus,
        error: AgentError,
        record: bool,
        duration: float,
    ) -> AgentResult:
        """A capability that could not be resolved at all."""
        result: AgentResult = AgentResult(
            capability=capability_id,
            status=status,
            output=None,
            execution_id=context.execution_id,
            errors=[error],
            provenance=ResultProvenance(
                capability=capability_id,
                execution_id=context.execution_id,
                snapshot_id=context.baseline_snapshot_id,
                duration_seconds=round(duration, 4),
                attempts=0,
            ),
        )
        if record:
            context.record_error(error.code, error.message, error.failure_class,
                                 capability=capability_id, step_id=step_id)
            context.record_unavailable(
                capability_id, reason=error.message,
                status=EvidenceStatus.NOT_RUN, step_id=step_id,
            )
        return result


__all__ = ["CapabilityExecutor", "PreflightRefusal"]
