"""
Orchestrator — Deterministic governance and authorization.

Governance answers one question: *may this be acted on, and by whom?*

Two properties are non-negotiable:

1. **Rules are deterministic and configurable.** No language model produces a
   governance verdict. A model may propose an action; these rules decide.

2. **REI is never the sole determinant.** A structurally significant action —
   closing a facility — requires a human even when exposure is low and the
   economics look excellent, because irreversibility, not cost, is what makes
   it consequential. Rule ordering below enforces that explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from netgravity.orchestrator.schemas.actions import (
    ActionClassification,
    ActionType,
    ApprovalRequest,
    ApprovalStatus,
    GovernanceDecision,
)
from netgravity.orchestrator.schemas.requests import Actor, ActorRole

logger = logging.getLogger(__name__)


#: Actions that permanently change the physical network. Always human-decided.
STRUCTURAL_ACTIONS: Set[ActionType] = {
    ActionType.CLOSE_FACILITY,
    ActionType.OPEN_FACILITY,
}

#: Reversible operational actions — candidates for automation.
OPERATIONAL_ACTIONS: Set[ActionType] = {
    ActionType.REROUTE_FLOW,
}

#: Actions with no effect on observed state.
ANALYTICAL_ACTIONS: Set[ActionType] = {
    ActionType.NONE,
    ActionType.REPORT,
    ActionType.CREATE_SCENARIO,
}


class EvidenceState(str, Enum):
    """
    Why a piece of required evidence cannot support an autonomous decision.

    Every one of these means "we could not establish this fact". None of them
    means "this fact is zero", and none means "risk is high".
    """
    UNAVAILABLE      = "UNAVAILABLE"       # the producing step did not run or failed
    FAILED           = "FAILED"            # the producing step errored
    STALE            = "STALE"             # produced, but against a different snapshot
    NOT_COMPUTABLE   = "NOT_COMPUTABLE"    # attempted, and explicitly refused
    GROUNDING_FAILED = "GROUNDING_FAILED"  # narrative figures could not be grounded


#: Actions whose AUTONOMY depends on risk evidence being present and valid.
#:
#: This is the action-awareness the missing-evidence rule needs. The question it
#: answers is NOT "is this action risky?" but "would a human justify running this
#: unattended by pointing at risk evidence?" — because if so, that justification
#: evaporates when the evidence does.
#:
#: Excluded, deliberately:
#:   NONE             nothing is proposed, so nothing can be justified.
#:   CREATE_SCENARIO  hypothetical by construction; it cannot touch observed
#:                    state, so no evidence is load-bearing for its safety.
#:
#: REPORT is INCLUDED. A report is the tier that carries risk findings to a
#: decision-maker, and emitting one unattended as though it were a complete
#: assessment — when the exposure analysis behind it failed — is exactly the
#: "absence of evidence read as absence of risk" failure. Note that including it
#: constrains only AUTONOMY: the report is still produced and still delivered,
#: it simply no longer clears itself for unattended action.
ACTIONS_REQUIRING_RISK_EVIDENCE: Set[ActionType] = {
    ActionType.REPORT,
    ActionType.REROUTE_FLOW,
    ActionType.CHANGE_CAPACITY,
    ActionType.OPEN_FACILITY,
    ActionType.CLOSE_FACILITY,
}


@dataclass
class GovernancePolicy:
    """
    Configurable thresholds. Every value is explicit and auditable; none is
    inferred at runtime.
    """
    #: Cost impact (%) above which even an operational action needs approval.
    cost_impact_approval_pct: float = 5.0
    #: Cost impact (%) above which a person must decide.
    cost_impact_human_pct: float = 20.0
    #: Unserved-demand fraction above which a person must decide.
    unserved_demand_human_rate: float = 0.02
    #: RF at or above which a person must decide.
    risk_factor_human: float = 0.8
    #: RF at or above which approval is required.
    risk_factor_approval: float = 0.5
    #: Reasoning confidence below which automation is withheld.
    min_confidence_for_auto: str = "HIGH"
    #: Infeasible results always escalate.
    infeasible_requires_human: bool = True
    #: Capabilities whose absence removes evidence a decision would rely on.
    #: Missing any of these withholds automation for non-analytical actions.
    critical_evidence_capabilities: List[str] = field(
        default_factory=lambda: [
            "resilience.assess",      # REI — exposure
            "risk.compute_rf",        # RF — combined risk
            "optimization.solve",     # cost/feasibility
            "optimization.solve_scenario",
        ]
    )
    #: A failed numeric-grounding check means the narrative cannot be trusted
    #: to describe the numbers. Withhold automation.
    grounding_failure_requires_approval: bool = True
    #: Action types whose autonomy depends on risk evidence. Overrides
    #: `ACTIONS_REQUIRING_RISK_EVIDENCE` when set, so an operator can declare a
    #: genuinely low-stakes action autonomous without resilience evidence —
    #: explicitly, in policy, rather than by accident of rule ordering.
    actions_requiring_risk_evidence: Optional[List[ActionType]] = None
    #: Roles allowed to approve.
    approver_roles: List[str] = field(
        default_factory=lambda: [ActorRole.APPROVER.value, ActorRole.ADMIN.value]
    )


_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


class ActionClassifier:
    """
    Maps deterministic evidence to an action classification.

    Rules are evaluated in strict precedence order, most restrictive first, and
    every rule that fires is recorded on the decision so the verdict can be
    explained after the fact.

    PRECEDENCE
    ──────────
        R0   nothing proposed                  → NO_ACTION
        R1   infeasible                        → HUMAN_ONLY
        R2   structural action                 → HUMAN_ONLY   (before any threshold)
        R3   degraded data quality             → HUMAN_ONLY
        R4   service loss                      → HUMAN_ONLY
        R5   severe cost impact                → HUMAN_ONLY
        R6   high combined risk                → HUMAN_ONLY
        R7   analytical output                 → AUTO *candidate* ─┐
        R7B  required risk evidence unresolved → APPROVAL_REQUIRED │ evidence
        R7C  numeric grounding failed          → APPROVAL_REQUIRED │ constraints
        R7   settlement of the candidate       → AUTO_ACTION     ◄─┘
        R8   moderate risk                     → APPROVAL_REQUIRED
        R9   material cost impact              → APPROVAL_REQUIRED
        R10  low narrative confidence          → APPROVAL_REQUIRED
        R11  reversible, low-impact            → AUTO_ACTION
        R12  default                           → APPROVAL_REQUIRED

    The R7 candidate/settlement split is the whole of the missing-evidence fix.
    Two properties it must preserve, both asserted by tests:

      * **Absence of evidence never increases autonomy.** R7 cannot reach AUTO
        past an unresolved evidence constraint.
      * **It is action-aware, not a blanket override.** R7B applies only to
        actions that would have leaned on risk evidence for their autonomy
        (`ACTIONS_REQUIRING_RISK_EVIDENCE`), so a hypothetical scenario is not
        escalated for lacking a measurement that could not make it unsafe.

    Two things this rule deliberately does NOT do: it does not treat missing
    evidence as high risk, and it does not block information delivery. A report
    whose exposure analysis failed is still produced and still returned; it
    simply no longer clears itself for unattended action.
    """

    def __init__(self, policy: Optional[GovernancePolicy] = None) -> None:
        self.policy = policy or GovernancePolicy()

    def classify(
        self,
        *,
        action_type: ActionType,
        is_feasible: bool = True,
        cost_impact_pct: Optional[float] = None,
        unserved_demand_rate: Optional[float] = None,
        rei: Optional[float] = None,
        risk_factor: Optional[float] = None,
        confidence: str = "LOW",
        data_quality_ok: bool = True,
        reversible: Optional[bool] = None,
        missing_evidence: Optional[Dict[str, str]] = None,
        grounding_failed: bool = False,
        unresolved_evidence: Optional[Dict[str, str]] = None,
    ) -> GovernanceDecision:
        """
        Produce a governance verdict from deterministic evidence.

        `rei` is accepted and recorded, but note it never *alone* decides —
        see the structural-action rule.

        THE MISSING-EVIDENCE POLICY
        ───────────────────────────
        Formally, for an action A requiring evidence E for autonomous execution:

            E.state ∈ {UNAVAILABLE, FAILED, STALE, NOT_COMPUTABLE,
                       GROUNDING_FAILED}   ⟹   AUTO_ACTION is prohibited.

        The verdict falls to the next conservative tier the action's own
        classification allows — APPROVAL_REQUIRED, or HUMAN_ONLY where a
        stricter rule already applies.

        This says **autonomy cannot be justified**. It does NOT say risk is
        high, and it does not substitute a value: `rei` and `risk_factor` stay
        `None` throughout. Absent risk information makes this system more
        conservative, never less — but the decision records *why*, so nobody
        reads "we could not measure it" as "we measured it and it was bad".

        Args:
            missing_evidence:    capability → reason, for steps that failed or
                                 never ran.
            unresolved_evidence: evidence key → `EvidenceState`, for evidence
                                 that WAS produced but cannot be relied on —
                                 principally a stale or not-computable RF, which
                                 no failed step would otherwise reveal.
        """
        p = self.policy
        triggered: List[str] = []
        missing_evidence = dict(missing_evidence or {})
        unresolved_evidence = dict(unresolved_evidence or {})
        missing_critical = sorted(
            cap for cap in missing_evidence if cap in p.critical_evidence_capabilities
        )

        # Which actions need risk evidence to justify running unattended.
        requires_evidence_actions = (
            set(p.actions_requiring_risk_evidence)
            if p.actions_requiring_risk_evidence is not None
            else ACTIONS_REQUIRING_RISK_EVIDENCE
        )
        action_requires_evidence = action_type in requires_evidence_actions

        # The combined evidence picture, as states rather than prose.
        evidence_status: Dict[str, str] = {
            cap: EvidenceState.UNAVAILABLE.value for cap in missing_critical
        }
        evidence_status.update(unresolved_evidence)
        if grounding_failed and p.grounding_failure_requires_approval:
            evidence_status["reasoning.grounding"] = EvidenceState.GROUNDING_FAILED.value

        evaluated: Dict[str, Any] = {
            "action_type": action_type.value,
            "is_feasible": is_feasible,
            "cost_impact_pct": cost_impact_pct,
            "unserved_demand_rate": unserved_demand_rate,
            "rei": rei,
            "risk_factor": risk_factor,
            "confidence": confidence,
            "data_quality_ok": data_quality_ok,
            "missing_evidence": missing_evidence,
            "missing_critical_evidence": missing_critical,
            "unresolved_evidence": unresolved_evidence,
            "grounding_failed": grounding_failed,
            "action_requires_risk_evidence": action_requires_evidence,
        }

        if reversible is None:
            reversible = action_type not in STRUCTURAL_ACTIONS

        def decide(
            classification: ActionClassification,
            reason: str,
            *,
            blocked_by_missing_evidence: bool = False,
        ) -> GovernanceDecision:
            needs_approval = classification == ActionClassification.APPROVAL_REQUIRED
            logger.info(
                "orchestrator.governance.decision action=%s classification=%s rules=%s "
                "blocked_by_missing_evidence=%s",
                action_type.value, classification.value, triggered,
                blocked_by_missing_evidence,
            )
            return GovernanceDecision(
                action_type=action_type,
                classification=classification,
                reason=reason,
                triggered_rules=list(triggered),
                governing_rule=triggered[-1] if triggered else None,
                evaluated=evaluated,
                blocked_by_missing_evidence=blocked_by_missing_evidence,
                evidence_status=dict(evidence_status),
                requires_approval=needs_approval,
                eligible_approver_roles=list(p.approver_roles) if needs_approval else [],
            )

        # --- Rule 0: nothing proposed -------------------------------------
        if action_type == ActionType.NONE:
            triggered.append("R0_NO_ACTION_PROPOSED")
            return decide(ActionClassification.NO_ACTION, "No action was proposed by this run.")

        # --- Rule 1: infeasible results cannot authorise anything ---------
        if p.infeasible_requires_human and not is_feasible:
            triggered.append("R1_INFEASIBLE")
            return decide(
                ActionClassification.HUMAN_ONLY,
                "The network is infeasible under this configuration, so no automated "
                "action can be authorised. A person must review the constraint conflict.",
            )

        # --- Rule 2: structural actions are ALWAYS human ------------------
        # Deliberately evaluated before any REI / RF / cost threshold. This is
        # the rule that stops a low-REI, low-cost facility closure from being
        # automated: irreversibility, not exposure, governs here.
        if action_type in STRUCTURAL_ACTIONS:
            triggered.append("R2_STRUCTURAL_ACTION")
            return decide(
                ActionClassification.HUMAN_ONLY,
                f"'{action_type.value}' is a structurally significant, effectively "
                f"irreversible network change. It requires human decision regardless "
                f"of REI, risk factor or cost impact.",
            )

        # --- Rule 3: degraded data quality --------------------------------
        if not data_quality_ok:
            triggered.append("R3_DATA_QUALITY")
            return decide(
                ActionClassification.HUMAN_ONLY,
                "Input data quality is degraded; automated action is withheld.",
            )

        # --- Rule 4: service loss -----------------------------------------
        if (unserved_demand_rate is not None
                and unserved_demand_rate > p.unserved_demand_human_rate):
            triggered.append("R4_UNSERVED_DEMAND")
            return decide(
                ActionClassification.HUMAN_ONLY,
                f"Unserved demand {unserved_demand_rate:.2%} exceeds the "
                f"{p.unserved_demand_human_rate:.2%} threshold; customer service impact "
                f"requires human judgement.",
            )

        # --- Rule 5: severe economic impact -------------------------------
        if cost_impact_pct is not None and cost_impact_pct >= p.cost_impact_human_pct:
            triggered.append("R5_COST_IMPACT_HUMAN")
            return decide(
                ActionClassification.HUMAN_ONLY,
                f"Cost impact {cost_impact_pct:.2f}% meets the "
                f"{p.cost_impact_human_pct:.2f}% human-decision threshold.",
            )

        # --- Rule 6: high combined risk -----------------------------------
        if risk_factor is not None and risk_factor >= p.risk_factor_human:
            triggered.append("R6_RISK_FACTOR_HUMAN")
            return decide(
                ActionClassification.HUMAN_ONLY,
                f"Risk factor {risk_factor:.3f} meets the {p.risk_factor_human:.3f} "
                f"human-decision threshold.",
            )

        # --- Rule 7: analytical output is safe — as a CANDIDATE -------------
        # Scenario creation is included: a scenario is hypothetical by
        # construction and cannot touch observed state.
        #
        # PRECEDENCE (the R7/R7B fix). This rule previously RETURNED here, which
        # short-circuited the evidence rules below and let an analytical action
        # reach AUTO_ACTION while the exposure analysis behind it had failed —
        # so losing evidence made the system MORE autonomous, the exact inversion
        # the architecture exists to prevent. R7 now records a *candidate*
        # verdict and falls through; the evidence constraints get to speak first,
        # and the candidate settles at `R7 settlement` below only if they do not
        # object.
        analytical_candidate = action_type in ANALYTICAL_ACTIONS
        if analytical_candidate:
            triggered.append("R7_ANALYTICAL_ONLY")

        # --- Rule 7b: required risk evidence is unavailable ------------------
        # Action-aware, not a blanket override: it applies only where the action
        # would have leaned on risk evidence to justify running unattended.
        # `CREATE_SCENARIO` is exempt because nothing about a hypothetical can be
        # made unsafe by a missing measurement.
        if action_requires_evidence and (missing_critical or unresolved_evidence):
            triggered.append("R7B_MISSING_CRITICAL_EVIDENCE")
            parts = [f"{cap} ({missing_evidence[cap]})" for cap in missing_critical]
            parts += [f"{key} ({state})" for key, state in sorted(unresolved_evidence.items())]
            return decide(
                ActionClassification.APPROVAL_REQUIRED,
                f"Autonomous execution is not permitted because required risk evidence "
                f"is unavailable: {'; '.join(parts)}. This is a statement about the "
                f"EVIDENCE, not about the risk: exposure and risk factor remain UNKNOWN "
                f"rather than zero, and no value has been substituted for them.",
                blocked_by_missing_evidence=True,
            )

        # --- Rule 7c: numeric grounding failed ------------------------------
        # Also evaluated before the analytical candidate settles: a narrative
        # whose figures could not be grounded cannot justify unattended action,
        # and a report is precisely a vehicle for those figures.
        if (grounding_failed and p.grounding_failure_requires_approval
                and action_requires_evidence):
            triggered.append("R7C_GROUNDING_FAILED")
            return decide(
                ActionClassification.APPROVAL_REQUIRED,
                "Numeric claims in the generated narrative could not be grounded "
                "against authoritative deterministic values. The explanation cannot "
                "be relied upon, so automated action is withheld.",
                blocked_by_missing_evidence=True,
            )

        # --- Rule 7 settlement: the analytical candidate stands --------------
        # Reached only when no evidence constraint objected. Settling HERE — and
        # not further down — preserves the pre-existing precedence exactly: an
        # analytical action still bypasses R8–R10, as it always did.
        if analytical_candidate:
            return decide(
                ActionClassification.AUTO_ACTION,
                f"'{action_type.value}' produces analysis only and does not modify "
                f"observed network state.",
            )

        # --- Rule 8: moderate risk needs approval -------------------------
        if risk_factor is not None and risk_factor >= p.risk_factor_approval:
            triggered.append("R8_RISK_FACTOR_APPROVAL")
            return decide(
                ActionClassification.APPROVAL_REQUIRED,
                f"Risk factor {risk_factor:.3f} meets the {p.risk_factor_approval:.3f} "
                f"approval threshold.",
            )

        # --- Rule 9: material cost impact needs approval ------------------
        if cost_impact_pct is not None and cost_impact_pct >= p.cost_impact_approval_pct:
            triggered.append("R9_COST_IMPACT_APPROVAL")
            return decide(
                ActionClassification.APPROVAL_REQUIRED,
                f"Cost impact {cost_impact_pct:.2f}% meets the "
                f"{p.cost_impact_approval_pct:.2f}% approval threshold.",
            )

        # --- Rule 10: low narrative confidence ----------------------------
        if (_CONFIDENCE_RANK.get(confidence.upper(), 0)
                < _CONFIDENCE_RANK.get(p.min_confidence_for_auto.upper(), 2)):
            triggered.append("R10_LOW_CONFIDENCE")
            return decide(
                ActionClassification.APPROVAL_REQUIRED,
                f"Analysis confidence is {confidence.upper()}, below the "
                f"{p.min_confidence_for_auto} required for automated action.",
            )

        # --- Rule 11: reversible, low-impact, high-confidence -------------
        if action_type in OPERATIONAL_ACTIONS and reversible:
            triggered.append("R11_REVERSIBLE_LOW_IMPACT")
            return decide(
                ActionClassification.AUTO_ACTION,
                f"'{action_type.value}' is reversible, low-impact and high-confidence.",
            )

        triggered.append("R12_DEFAULT_APPROVAL")
        return decide(
            ActionClassification.APPROVAL_REQUIRED,
            "No rule authorised automatic execution; defaulting to approval. "
            "The governance default is deliberately conservative.",
        )


class AuthorizationService:
    """
    Enforces who may do what.

    A language model can request anything; it reaches this layer as a *proposal*
    and is refused here if the actor lacks the right. There is no code path from
    model output to an authorised action that bypasses this check.
    """

    #: role → actions that role may initiate without further approval
    ROLE_PERMISSIONS: Dict[ActorRole, Set[ActionType]] = {
        ActorRole.VIEWER:   set(ANALYTICAL_ACTIONS),
        ActorRole.SYSTEM:   set(ANALYTICAL_ACTIONS),
        ActorRole.PLANNER:  set(ANALYTICAL_ACTIONS) | set(OPERATIONAL_ACTIONS)
                            | {ActionType.CHANGE_CAPACITY},
        ActorRole.APPROVER: set(ANALYTICAL_ACTIONS) | set(OPERATIONAL_ACTIONS)
                            | {ActionType.CHANGE_CAPACITY},
        ActorRole.ADMIN:    set(ANALYTICAL_ACTIONS) | set(OPERATIONAL_ACTIONS)
                            | {ActionType.CHANGE_CAPACITY},
    }

    def can_perform(self, actor: Actor, action_type: ActionType) -> bool:
        """
        Whether `actor` may initiate `action_type` directly.

        Note no role — not even ADMIN — is permitted to initiate a structural
        action directly. Those must go through the human-decision path.
        """
        return action_type in self.ROLE_PERMISSIONS.get(actor.role, set())

    def can_approve(self, actor: Actor, policy: GovernancePolicy) -> bool:
        return actor.role.value in policy.approver_roles

    def authorize(self, actor: Actor, action_type: ActionType) -> None:
        """
        Raises:
            AuthorizationError: actor may not initiate this action.
        """
        from netgravity.orchestrator.exceptions import AuthorizationError

        if not self.can_perform(actor, action_type):
            raise AuthorizationError(
                f"Actor '{actor.actor_id}' with role {actor.role.value} is not "
                f"permitted to initiate '{action_type.value}'.",
                context={"actor": actor.actor_id, "role": actor.role.value,
                         "action": action_type.value},
            )


class ApprovalManager:
    """
    Creates and resolves approval requests.

    An approval pins the exact execution, scenario version and snapshot it was
    raised against, so a later decision cannot silently apply to data other than
    what was reviewed.
    """

    def __init__(self, policy: Optional[GovernancePolicy] = None) -> None:
        self.policy = policy or GovernancePolicy()
        self.authorization = AuthorizationService()

    def create_request(
        self,
        *,
        execution_id: str,
        decision: GovernanceDecision,
        summary: str,
        scenario_id: Optional[str] = None,
        scenario_version: Optional[int] = None,
        baseline_snapshot_id: Optional[str] = None,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            execution_id=execution_id,
            action_type=decision.action_type,
            classification=decision.classification,
            summary=summary,
            scenario_id=scenario_id,
            scenario_version=scenario_version,
            baseline_snapshot_id=baseline_snapshot_id,
            requested_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(
            "orchestrator.governance.approval_requested approval_id=%s execution_id=%s action=%s",
            req.approval_id, execution_id, decision.action_type.value,
        )
        return req

    def decide(
        self,
        approval: ApprovalRequest,
        *,
        actor: Actor,
        approved: bool,
        note: str = "",
    ) -> ApprovalRequest:
        """
        Record a human decision.

        Raises:
            AuthorizationError: actor is not an eligible approver.
            GovernanceFailureError: the request is no longer pending.
        """
        from netgravity.orchestrator.exceptions import (
            AuthorizationError,
            GovernanceFailureError,
        )

        if not self.authorization.can_approve(actor, self.policy):
            raise AuthorizationError(
                f"Actor '{actor.actor_id}' with role {actor.role.value} may not approve "
                f"actions. Eligible roles: {self.policy.approver_roles}.",
                context={"actor": actor.actor_id, "role": actor.role.value},
            )

        if approval.status != ApprovalStatus.PENDING:
            raise GovernanceFailureError(
                f"Approval '{approval.approval_id}' is already {approval.status.value}; "
                f"it cannot be decided again.",
                context={"approval_id": approval.approval_id},
            )

        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        approval.decided_at = datetime.now(timezone.utc).isoformat()
        approval.decided_by = actor.actor_id
        approval.decision_note = note
        logger.info(
            "orchestrator.governance.approval_decided approval_id=%s status=%s by=%s",
            approval.approval_id, approval.status.value, actor.actor_id,
        )
        return approval
