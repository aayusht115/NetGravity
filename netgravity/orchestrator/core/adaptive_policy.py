"""
Orchestrator — Deterministic Adaptive Decision Policy & Loop Guards.

Phase 8.6 decision engine. Coordinates:
  1. Result-driven workflow branching (material forecast growth -> scenario analysis).
  2. Replanning guardrails (max replans, max steps, repeated-plan cycle detection).
  3. Seamless integration with FailureManager for failure recovery.

INVARIANTS:
  - 100% deterministic: (context, result, plan) -> exact same decision.
  - Zero randomness, zero wall-clock dependency, zero LLM calls in the decision policy.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional, Set, Tuple

from netgravity.orchestrator.core.failure_manager import FailureManager
from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
    CAP_OPTIMIZE_SCEN,
    CAP_REI,
)
from netgravity.orchestrator.schemas.adaptive import (
    AdaptiveAction,
    AdaptiveDecision,
    AdaptiveExecutionConfig,
    ReplanRecord,
    ResultObservation,
)
from netgravity.orchestrator.schemas.agent_result import AgentResult
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    ExecutionPlan,
    PlanStep,
)
from netgravity.orchestrator.schemas.recovery import (
    EscalationOutcome,
    RecoveryAction,
)

logger = logging.getLogger(__name__)


class ReplanGuard:
    """
    Guards the adaptive execution loop against infinite loops, cycles, and unbounded steps.
    """

    def __init__(self, config: Optional[AdaptiveExecutionConfig] = None) -> None:
        self.config = config or AdaptiveExecutionConfig()

    def plan_signature(self, plan: ExecutionPlan) -> str:
        """
        Produce a deterministic canonical string representation of a plan's capability sequence.
        """
        return " -> ".join(f"{s.step_id}:{s.capability}" for s in plan.steps)

    def check_replan_eligibility(
        self,
        current_replan_count: int,
        proposed_plan: ExecutionPlan,
        past_plan_signatures: Set[str],
        total_steps_executed: int,
    ) -> Tuple[bool, str]:
        """
        Validate whether a replan request is legally permissible under guardrails.
        """
        # 1. Check max execution step budget
        if total_steps_executed >= self.config.max_execution_steps:
            return False, f"Maximum execution steps limit reached ({self.config.max_execution_steps})."

        # 2. Check max replans limit
        if current_replan_count >= self.config.max_replans:
            return False, f"Maximum replan limit reached ({self.config.max_replans})."

        # 3. Check for repeated-plan / cycle detection
        new_signature = self.plan_signature(proposed_plan)
        if new_signature in past_plan_signatures:
            return False, f"Repeated-plan cycle detected: signature '{new_signature}' has already executed."

        return True, "Replan permitted by guardrails."


class AdaptiveDecisionPolicy:
    """
    Deterministic next-action decision policy for the closed-loop orchestrator.
    """

    def __init__(
        self,
        failure_manager: FailureManager,
        config: Optional[AdaptiveExecutionConfig] = None,
    ) -> None:
        self.failure_manager = failure_manager
        self.config = config or AdaptiveExecutionConfig()
        self.guard = ReplanGuard(self.config)

    def decide(
        self,
        step: PlanStep,
        observation: ResultObservation,
        result: AgentResult,
        attempt: int,
        context: Any,
        current_plan: ExecutionPlan,
    ) -> AdaptiveDecision:
        """
        Map current step observation and context to a deterministic next action.
        """
        # ------------------------------------------------------------------
        # 1. Handle Domain Branching & Dynamic Replanning
        # ------------------------------------------------------------------
        if observation.domain_outcome == "MATERIAL_FORECAST_INCREASE" and self.config.enable_materiality_branching:
            # Check if current plan already contains downstream scenario analysis
            existing_capabilities = {s.capability for s in current_plan.steps}
            has_scenario_analysis = bool(
                existing_capabilities.intersection({CAP_CREATE_SCEN, CAP_OPTIMIZE_SCEN, CAP_REI, "resilience.assess"})
            )

            if not has_scenario_analysis:
                # Check replan limits
                replan_count = getattr(context, "replan_count", 0)
                if replan_count < self.config.max_replans:
                    return AdaptiveDecision(
                        action=AdaptiveAction.REPLAN,
                        reason=(
                            f"Material forecast increase detected in step '{step.step_id}' ({observation.summary}); "
                            f"replan required to trigger network scenario optimization and resilience stress-testing."
                        ),
                        step_id=step.step_id,
                        capability=step.capability,
                    )
                else:
                    return AdaptiveDecision(
                        action=AdaptiveAction.ESCALATE,
                        reason="Material forecast increase requires replanning but max replan limit was reached.",
                        step_id=step.step_id,
                        capability=step.capability,
                    )

        # ------------------------------------------------------------------
        # 2. Infeasible Optimization Handling
        # ------------------------------------------------------------------
        if observation.domain_outcome == "INFEASIBLE_OPTIMIZATION":
            esc = EscalationOutcome(
                capability=step.capability,
                execution_id=getattr(context, "execution_id", "unknown"),
                reason=f"Optimization in step '{step.step_id}' proved mathematically infeasible under network constraints.",
                failed_attempts=attempt,
                recommended_human_action="Review capacity constraints, supply limits, or lane availability in scenario parameters.",
            )
            return AdaptiveDecision(
                action=AdaptiveAction.ESCALATE,
                reason="Optimization infeasibility proved mathematically; halting downstream calculations.",
                step_id=step.step_id,
                capability=step.capability,
                escalation=esc,
            )

        # ------------------------------------------------------------------
        # 3. Handle Failures via FailureManager Policy
        # ------------------------------------------------------------------
        if not observation.is_usable or observation.status in (
            AgentStatus.RETRYABLE_FAILURE,
            AgentStatus.NON_RETRYABLE_FAILURE,
            AgentStatus.INSUFFICIENT_EVIDENCE,
            AgentStatus.INVALID_OUTPUT,
        ):
            recovery_decision = self.failure_manager.decide_policy(
                step, result, attempt, context, plan=current_plan,
            )

            if recovery_decision.action == RecoveryAction.RETRY:
                return AdaptiveDecision(
                    action=AdaptiveAction.RETRY,
                    reason=recovery_decision.reason,
                    step_id=step.step_id,
                    capability=step.capability,
                )
            elif recovery_decision.action == RecoveryAction.REROUTE:
                return AdaptiveDecision(
                    action=AdaptiveAction.REROUTE,
                    reason=recovery_decision.reason,
                    step_id=step.step_id,
                    capability=recovery_decision.target_capability or step.capability,
                )
            elif recovery_decision.action == RecoveryAction.BLOCK:
                return AdaptiveDecision(
                    action=AdaptiveAction.BLOCK,
                    reason=recovery_decision.reason,
                    step_id=step.step_id,
                    capability=step.capability,
                )
            elif recovery_decision.action == RecoveryAction.ESCALATE:
                if getattr(step, "optional", False):
                    return AdaptiveDecision(
                        action=AdaptiveAction.CONTINUE,
                        reason=f"Optional step '{step.step_id}' failed ({recovery_decision.reason}); continuing degraded execution.",
                        step_id=step.step_id,
                        capability=step.capability,
                    )
                return AdaptiveDecision(
                    action=AdaptiveAction.ESCALATE,
                    reason=recovery_decision.reason,
                    step_id=step.step_id,
                    capability=step.capability,
                    escalation=recovery_decision.escalation,
                )

        # ------------------------------------------------------------------
        # 4. Standard Continuation
        # ------------------------------------------------------------------
        return AdaptiveDecision(
            action=AdaptiveAction.CONTINUE,
            reason=f"Step '{step.step_id}' completed successfully; proceeding to next planned capability.",
            step_id=step.step_id,
            capability=step.capability,
        )
