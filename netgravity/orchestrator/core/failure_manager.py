"""
Orchestrator — Failure Management and Recovery Policy.

Phase 8.4 establishes the FailureManager above the single-shot CapabilityExecutor.

ARCHITECTURE & FLOW:
    User Request
         ↓
    Orchestrator
         ↓
    Deterministic WorkflowPlanner
         ↓
    ExecutionPlan
         ↓
    FAILURE MANAGER (FailureManager)
         ↓
    CapabilityExecutor (Single-shot seam)
         ↓
    Specialist Capability
         ↓
    AgentResult
         ↓
    ExecutionContext
         ↓
    Orchestrator

CORE RESPONSIBILITIES:
  1. Failure Classification: Uses existing AgentStatus / FailureClass to distinguish
     transient technical failures from deterministic business outcomes (e.g. MILP
     infeasibility), missing data, and invalid outputs.
  2. Bounded Retries: Re-executes only explicitly retryable failures within max_attempts.
     Every attempt is chronologically recorded in ExecutionContext (attempt 1 is never
     overwritten by attempt 2).
  3. Dependency Failure Propagation: When a HARD prerequisite fails, dependent steps are
     marked BLOCKED with explicit NOT_RUN evidence status. Dependent steps are NEVER
     invoked with zero or fabricated data. SOFT dependencies degrade gracefully.
  4. Rerouting: If a capability fails and the CapabilityRegistry contains an explicitly
     registered alternative, executes the alternative while recording the primary failure.
  5. Escalation: When autonomous recovery is not possible, creates a structured
     EscalationOutcome detailing blocked capabilities, available evidence, and human actions.
  6. Bounded Circuit Breaker: Guards shared external dependencies (LLM gateway) to prevent
     cascading failures and quota waste.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from netgravity.orchestrator.audit import events
from netgravity.orchestrator.core.circuit_breaker import CircuitBreaker
from netgravity.orchestrator.core.executor import CapabilityExecutor
from netgravity.orchestrator.exceptions import FailureClass
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.agent_result import AgentError, AgentResult
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    EvidenceStatus,
    ExecutionPlan,
    PlanStep,
    StepStatus,
    UnavailableEvidence,
)
from netgravity.orchestrator.schemas.recovery import (
    EscalationOutcome,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
)

logger = logging.getLogger(__name__)


class FailureManager:
    """
    Coordinates execution of plans and steps with recovery, retries, rerouting,
    dependency blocking, and escalation.

    Does NOT execute capabilities directly — delegates all capability execution
    to `CapabilityExecutor`.
    """

    def __init__(
        self,
        executor: CapabilityExecutor,
        registry: CapabilityRegistry,
        *,
        policy: Optional[RecoveryPolicy] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        self.executor = executor
        self.registry = registry
        self.policy = policy or RecoveryPolicy()
        self.circuit_breaker = circuit_breaker or CircuitBreaker(name="shared_llm_gateway")

    # ==================================================================
    # 1. Failure Classification
    # ==================================================================

    def classify_failure(self, result: AgentResult) -> AgentStatus:
        """
        Classify the outcome of an execution.

        Returns one of:
          - SUCCESS
          - PARTIAL
          - RETRYABLE_FAILURE
          - NON_RETRYABLE_FAILURE
          - INVALID_OUTPUT
          - INSUFFICIENT_EVIDENCE
        """
        return result.status

    # ==================================================================
    # 2. Recovery Decision Policy
    # ==================================================================

    def decide_policy(
        self,
        step: PlanStep,
        result: AgentResult,
        attempt: int,
        context: Any,
        plan: Optional[ExecutionPlan] = None,
    ) -> RecoveryDecision:
        """
        Determine the recovery action for a capability outcome.

        Decisions:
          - CONTINUE: result is usable
          - RETRY: transient failure within attempt budget
          - REROUTE: primary failed and valid registered alternative exists
          - ESCALATE: retries exhausted or unrecoverable condition
          - BLOCK: hard failure blocking dependents
        """
        if result.is_usable:
            return RecoveryDecision(
                action=RecoveryAction.CONTINUE,
                reason="Capability executed successfully or degraded safely.",
                attempt_number=attempt,
            )

        first_error = result.errors[0] if result.errors else None
        error_code = first_error.code if first_error else None
        error_message = first_error.message if first_error else result.status.value

        # Check retry eligibility
        is_retryable = self.policy.is_retryable(result.status, error_code)
        can_retry_more = attempt < self.policy.max_attempts

        # Check circuit breaker constraint for LLM-backed capabilities
        capability_contract = None
        if self.registry.has_contract(step.capability):
            capability_contract = self.registry.contract(step.capability)
        is_llm_backed = capability_contract.llm_backed if capability_contract else False

        if is_llm_backed and not self.circuit_breaker.can_execute():
            # Circuit breaker is OPEN - cannot retry external call
            is_retryable = False

        if is_retryable and can_retry_more:
            delay = self.policy.delay_for(attempt)
            return RecoveryDecision(
                action=RecoveryAction.RETRY,
                reason=f"Transient failure ({error_code or result.status.value}); scheduling attempt {attempt + 1}",
                attempt_number=attempt,
                delay_seconds=delay,
            )

        # Retry not possible or exhausted. Check for registered alternative capability (Reroute)
        if self.policy.enable_rerouting:
            alternatives = self.registry.get_alternatives(step.capability)
            # Filter out alternatives that are already tried or not executable
            valid_alternatives = [
                alt for alt in alternatives
                if alt != step.capability and (self.registry.has(alt) or self.registry.has_contract(alt))
            ]
            if valid_alternatives:
                target_alt = valid_alternatives[0]
                return RecoveryDecision(
                    action=RecoveryAction.REROUTE,
                    reason=f"Primary capability '{step.capability}' failed ({error_message}); rerouting to alternative '{target_alt}'",
                    target_capability=target_alt,
                    attempt_number=attempt,
                )

        # Compute blocked downstream capabilities if plan is present
        blocked_downstream: List[str] = []
        if plan is not None:
            for s in plan.steps:
                if step.step_id in s.depends_on:
                    dep_type = plan.dependency_type(step.step_id, s)
                    if dep_type.value == "HARD":
                        blocked_downstream.append(s.capability)

        # Escalation
        available_ev = {
            cap: status.value
            for cap, status in context.capability_status.items()
            if status in (AgentStatus.SUCCESS, AgentStatus.PARTIAL)
        }
        human_action = self._recommend_human_action(step.capability, error_code, error_message)

        escalation = EscalationOutcome(
            capability=step.capability,
            execution_id=context.execution_id,
            reason=f"Capability '{step.capability}' failed after {attempt} attempt(s): {error_message}",
            failed_attempts=attempt,
            blocked_downstream_capabilities=blocked_downstream,
            available_evidence=available_ev,
            recommended_human_action=human_action,
            metadata={"error_code": error_code, "status": result.status.value},
        )

        return RecoveryDecision(
            action=RecoveryAction.ESCALATE,
            reason=f"Autonomous recovery exhausted for '{step.capability}': {error_message}",
            attempt_number=attempt,
            escalation=escalation,
        )

    def _recommend_human_action(
        self, capability: str, error_code: Optional[str], error_message: str
    ) -> str:
        """Provide domain-appropriate guidance for human operators."""
        if error_code == "SOLVER_INFEASIBLE":
            return "Relax facility capacity constraints or add lane connections in scenario configuration."
        if error_code == "MISSING_DATA":
            return "Supply missing input datasets (demand, facilities, or lanes) through the ingestion portal."
        if error_code == "STALE_SNAPSHOT":
            return "Re-run analysis against the current active network snapshot."
        if error_code == "VALIDATION_FAILURE":
            return "Inspect network topology for referential integrity or boundary violations."
        if error_code in ("LLM_FAILURE", "CIRCUIT_BREAKER_OPEN"):
            return "Verify LLM text gateway endpoint availability and token quota budget."
        return f"Review capability '{capability}' execution trace and error logs."

    # ==================================================================
    # 3. Single-Step Execution with Recovery
    # ==================================================================

    async def execute_step_with_recovery(
        self,
        step: PlanStep,
        context: Any,
        plan: Optional[ExecutionPlan] = None,
        *,
        upstream: Optional[Dict[str, Any]] = None,
        unavailable: Optional[Dict[str, UnavailableEvidence]] = None,
    ) -> AgentResult:
        """
        Execute one step through CapabilityExecutor, applying retry and reroute policies.
        """
        current_capability = step.capability
        attempt = 0
        last_result: Optional[AgentResult] = None
        is_rerouted = False
        original_capability = step.capability

        # Contract inspection for circuit breaking
        contract = None
        if self.registry.has_contract(current_capability):
            contract = self.registry.contract(current_capability)
        is_llm = contract.llm_backed if contract else False

        # Check authorization if capability has required roles
        if self.registry.has(current_capability):
            cap_obj = self.registry.get(current_capability)
            if cap_obj.required_roles and hasattr(context, "actor") and context.actor.role.value not in cap_obj.required_roles:
                from netgravity.orchestrator.exceptions import AuthorizationError
                raise AuthorizationError(
                    f"Actor role {context.actor.role.value} may not invoke capability "
                    f"'{current_capability}'. Allowed: {list(cap_obj.required_roles)}",
                    context={"capability": current_capability},
                )

        while attempt < self.policy.max_attempts:
            attempt += 1

            # Check circuit breaker before external call
            if is_llm and self.policy.enable_circuit_breaker:
                if not self.circuit_breaker.can_execute():
                    logger.warning(
                        "orchestrator.circuit_breaker.fast_fail capability=%s attempt=%d state=OPEN",
                        current_capability, attempt,
                    )
                    cb_result = AgentResult.insufficient_evidence(
                        current_capability,
                        reason=f"Circuit breaker for '{self.circuit_breaker.name}' is OPEN; fast-failing external call",
                        status=EvidenceStatus.UNAVAILABLE,
                        agent=contract.provider if contract else "",
                        execution_id=context.execution_id,
                    )
                    # Force status to RETRYABLE_FAILURE to signify transient breaker state
                    cb_result = AgentResult(
                        capability=current_capability,
                        status=AgentStatus.RETRYABLE_FAILURE,
                        errors=[AgentError(
                            code="CIRCUIT_BREAKER_OPEN",
                            message=f"Circuit breaker '{self.circuit_breaker.name}' is OPEN",
                            failure_class="RETRYABLE",
                        )],
                        execution_id=context.execution_id,
                    )
                    context.record_attempt(
                        step.step_id,
                        current_capability,
                        attempt,
                        cb_result.status,
                        error_code="CIRCUIT_BREAKER_OPEN",
                        error_message="Circuit breaker is OPEN",
                        failure_class="RETRYABLE",
                        is_reroute=is_rerouted,
                        rerouted_from=original_capability if is_rerouted else None,
                    )
                    decision = self.decide_policy(step, cb_result, attempt, context, plan)
                    if decision.action == RecoveryAction.REROUTE and decision.target_capability:
                        current_capability = decision.target_capability
                        is_rerouted = True
                        is_llm = False
                        continue
                    if decision.escalation:
                        context.record_escalation(decision.escalation)
                    return cb_result

            # Execute single-shot via the executor seam
            result = await self.executor.execute(
                current_capability,
                context,
                params=dict(step.params),
                upstream=upstream,
                unavailable=unavailable,
                step_id=step.step_id,
                record=True,
            )
            last_result = result

            # Record observable attempt in ExecutionContext
            first_err = result.errors[0] if result.errors else None
            context.record_attempt(
                step.step_id,
                current_capability,
                attempt,
                result.status,
                duration_seconds=result.provenance.duration_seconds,
                error_code=first_err.code if first_err else None,
                error_message=first_err.message if first_err else None,
                failure_class=first_err.failure_class if first_err else None,
                is_reroute=is_rerouted,
                rerouted_from=original_capability if is_rerouted else None,
            )

            # Evaluate policy decision
            decision = self.decide_policy(step, result, attempt, context, plan)

            if decision.action == RecoveryAction.CONTINUE:
                if is_llm and self.policy.enable_circuit_breaker:
                    self.circuit_breaker.record_success()
                if is_rerouted:
                    context.add_warning(
                        f"Step '{step.step_id}' rerouted to '{current_capability}' after '{original_capability}' failed."
                    )
                return result

            if is_llm and self.policy.enable_circuit_breaker:
                self.circuit_breaker.record_failure(
                    failure_class=first_err.failure_class if first_err else "RETRYABLE",
                    error_code=first_err.code if first_err else None,
                )

            if decision.action == RecoveryAction.RETRY:
                if decision.delay_seconds > 0:
                    logger.info(
                        "orchestrator.recovery.retry capability=%s attempt=%d delay=%.2fs",
                        current_capability, attempt, decision.delay_seconds,
                    )
                    await asyncio.sleep(decision.delay_seconds)
                continue

            if decision.action == RecoveryAction.REROUTE and decision.target_capability:
                target_alt = decision.target_capability
                logger.warning(
                    "orchestrator.recovery.reroute from=%s to=%s step=%s",
                    current_capability, target_alt, step.step_id,
                )
                alt_result = await self.executor.execute(
                    target_alt,
                    context,
                    params=dict(step.params),
                    upstream=upstream,
                    unavailable=unavailable,
                    step_id=step.step_id,
                    record=True,
                )
                alt_err = alt_result.errors[0] if alt_result.errors else None
                context.record_attempt(
                    step.step_id,
                    target_alt,
                    attempt + 1,
                    alt_result.status,
                    duration_seconds=alt_result.provenance.duration_seconds,
                    error_code=alt_err.code if alt_err else None,
                    error_message=alt_err.message if alt_err else None,
                    failure_class=alt_err.failure_class if alt_err else None,
                    is_reroute=True,
                    rerouted_from=current_capability,
                )
                if alt_result.is_usable:
                    context.add_warning(
                        f"Step '{step.step_id}' rerouted from '{current_capability}' to '{target_alt}' after failure."
                    )
                    return alt_result

                # Alternative also failed -> escalate
                esc_outcome = EscalationOutcome(
                    capability=target_alt,
                    execution_id=context.execution_id,
                    reason=f"Alternative capability '{target_alt}' also failed for step '{step.step_id}' after '{current_capability}' failed: {alt_err.message if alt_err else 'Unknown'}",
                    failed_attempts=attempt + 1,
                    recommended_human_action=self._recommend_human_action(target_alt, alt_err.code if alt_err else None, alt_err.message if alt_err else "Failed"),
                )
                context.record_escalation(esc_outcome)
                return alt_result

            if decision.action == RecoveryAction.ESCALATE:
                if decision.escalation is not None:
                    context.record_escalation(decision.escalation)
                return result

            # Block or other non-retryable
            return result

        assert last_result is not None
        return last_result

    # ==================================================================
    # 4. Plan-Level Execution Loop
    # ==================================================================

    async def execute_plan(
        self,
        plan: ExecutionPlan,
        context: Any,
        trace: Optional[Any] = None,
    ) -> None:
        """
        Execute an entire ExecutionPlan layer by layer with failure management.

        Enforces:
          - HARD unmet dependencies -> StepStatus.BLOCKED (never executed)
          - SOFT unmet dependencies -> StepStatus.RUNNING with degraded unavailable evidence
          - Solver infeasibility -> Mathematical outcome, immediately stops plan execution
          - Retries, rerouting and escalations are managed transparently
        """
        layers = plan.execution_layers()

        for layer_index, layer in enumerate(layers):
            runnable: List[str] = []

            for step_id in layer:
                step = plan.step(step_id)
                assert step is not None

                resolution = plan.classify_dependencies(
                    step, set(context.completed_steps),
                )

                if not resolution.runnable:
                    step.status = StepStatus.BLOCKED
                    context.record_blocked(step_id, step.capability, resolution.blocking)
                    if trace is not None:
                        trace.record(
                            events.STEP_BLOCKED,
                            step_id=step_id,
                            capability=step.capability,
                            blocked_by=resolution.blocking,
                        )
                        trace.record(
                            events.EVIDENCE_UNAVAILABLE,
                            step_id=step_id,
                            capability=step.capability,
                            status="NOT_RUN",
                            reason=f"hard dependency failed: {resolution.blocking}",
                        )
                    logger.warning(
                        "orchestrator.step.blocked step=%s hard_deps_failed=%s %s",
                        step_id, resolution.blocking, context.correlation(),
                    )
                    continue

                if resolution.is_degraded:
                    missing_caps = [
                        (plan.step(d).capability if plan.step(d) else d)
                        for d in resolution.degraded
                    ]
                    context.add_warning(
                        f"step '{step_id}' running with degraded evidence; unavailable: "
                        f"{', '.join(missing_caps)}"
                    )
                    if trace is not None:
                        trace.record(
                            events.STEP_DEGRADED,
                            step_id=step_id,
                            capability=step.capability,
                            soft_deps_failed=resolution.degraded,
                            missing_capabilities=missing_caps,
                        )
                    logger.info(
                        "orchestrator.step.degraded step=%s soft_deps_failed=%s %s",
                        step_id, resolution.degraded, context.correlation(),
                    )

                runnable.append(step_id)

            if not runnable:
                continue

            logger.info(
                "orchestrator.layer.start layer=%d steps=%s %s",
                layer_index, runnable, context.correlation(),
            )
            if trace is not None:
                for step_id in runnable:
                    step = plan.step(step_id)
                    assert step is not None
                    trace.record(
                        events.STEP_STARTED,
                        step_id=step_id,
                        capability=step.capability,
                        layer=layer_index,
                    )

            # Launch runnable steps in the current layer
            tasks = []
            for sid in runnable:
                s = plan.step(sid)
                assert s is not None
                upstream = {
                    dep: context.step_output(dep)
                    for dep in s.depends_on
                    if context.step_output(dep) is not None
                }
                unavailable = dict(context.unavailable_evidence)
                tasks.append(self.execute_step_with_recovery(
                    s, context, plan=plan, upstream=upstream, unavailable=unavailable,
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for step_id, outcome in zip(runnable, results):
                step = plan.step(step_id)
                assert step is not None

                if isinstance(outcome, BaseException):
                    step.status = StepStatus.FAILED
                    context.record_error(
                        "ENGINE_FAILURE",
                        f"Step '{step_id}' raised {type(outcome).__name__}: {outcome}",
                        FailureClass.NON_RETRYABLE.value,
                    )
                    if trace is not None:
                        trace.record(
                            events.STEP_EXCEPTION,
                            step_id=step_id,
                            capability=step.capability,
                            error_type=type(outcome).__name__,
                            error=str(outcome),
                        )
                        trace.record(
                            events.STEP_FAILED,
                            step_id=step_id,
                            capability=step.capability,
                            code="ENGINE_FAILURE",
                        )
                    continue

                first_error = outcome.errors[0] if outcome.errors else None
                step.status = (
                    StepStatus.COMPLETED if outcome.is_usable else StepStatus.FAILED
                )

                if trace is not None:
                    trace.record_tool(
                        step_id=step_id,
                        capability=outcome.capability,
                        success=outcome.is_usable,
                        duration_seconds=outcome.provenance.duration_seconds,
                        attempts=outcome.provenance.attempts,
                        execution_mode=outcome.provenance.execution_mode.value,
                        error_code=first_error.code if first_error else None,
                        error_message=first_error.message if first_error else None,
                    )

                if outcome.is_usable:
                    if trace is not None:
                        trace.engine_results[outcome.capability] = (
                            context.output_of(outcome.capability) or {}
                        )
                        trace.record(
                            events.STEP_COMPLETED,
                            step_id=step_id,
                            capability=outcome.capability,
                            status=outcome.status.value,
                            duration_seconds=round(outcome.provenance.duration_seconds, 4),
                        )
                    continue

                # Record failure details
                if trace is not None:
                    trace.record(
                        events.STEP_FAILED,
                        step_id=step_id,
                        capability=outcome.capability,
                        code=first_error.code if first_error else outcome.status.value,
                        status=outcome.status.value,
                        optional=step.optional,
                    )
                    evidence = context.unavailable_evidence.get(outcome.capability)
                    trace.record(
                        events.EVIDENCE_UNAVAILABLE,
                        step_id=step_id,
                        capability=outcome.capability,
                        status=evidence.status.value if evidence else "UNAVAILABLE",
                        reason=(evidence.reason if evidence else (first_error.message if first_error else "")) or "",
                    )

                if first_error is not None and first_error.code == "SOLVER_INFEASIBLE":
                    # Mathematical finding: halt run and record infeasibility
                    context.audit_metadata["infeasible_step"] = step_id
                    context.audit_metadata["infeasible_detail"] = (
                        first_error.context or outcome.metadata.get("error_context", {})
                    )
                    if trace is not None:
                        trace.record(
                            events.SOLVER_INFEASIBLE,
                            step_id=step_id,
                            capability=outcome.capability,
                        )
                    return


__all__ = [
    "FailureManager",
]
