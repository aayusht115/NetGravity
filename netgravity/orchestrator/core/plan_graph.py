"""
Orchestrator — Deterministic plan derivation and plan validation.

Two components, both deterministic, neither executing anything.

`PlanValidator` answers "may this plan run?" It is the gate in front of the
executor, and it runs on EVERY plan — hand-written template or derived — so a
malformed graph is refused before any capability is invoked rather than failing
partway through.

`CapabilityGraphPlanner` answers "which capabilities does this goal need, and in
what order?" It closes the dependency graph over a requested set of goals using
the contracts registered in Phase 8.1, and orders the result.

WHY DERIVATION DOES NOT REPLACE THE TEMPLATES
---------------------------------------------
The ten hand-written workflows encode deliberate EXCLUSIONS, and the reasoning
behind them is real domain judgement that no dependency graph contains:

  * a forecast question runs no solver — optimising against an estimate is a
    separate act with its own entry point
  * a market-intelligence message runs no forecast — a signal reaching the
    forecaster by virtue of having been mentioned is precisely the routing
    decision the orchestrator exists to make
  * an explanation runs no optimization — it must not launch a fresh solve
    nobody asked for

A graph closure knows what a capability NEEDS. It cannot know what a question
should be refused. So derivation is goal-driven: it adds required dependencies
and nothing else, and the templates remain authoritative for the intents they
cover. Both paths produce the same typed, validated `ExecutionPlan`.

NO PLANNING DECISION IS PROBABILISTIC
-------------------------------------
No model call, no network access, no clock, no randomness, no set iteration
order leaking into output. Same goals plus same context always yield the same
plan, byte for byte, and a test asserts it.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional, Sequence, Set, TYPE_CHECKING

from netgravity.orchestrator.exceptions import PlanningFailureError
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.capability import CapabilityContract
from netgravity.orchestrator.schemas.plan_validation import (
    PlanFailureReason,
    PlanOrigin,
    PlanValidation,
    PlanViolation,
)
from netgravity.orchestrator.schemas.plans import (
    AgentStatus,
    ExecutionPlan,
    PlanStep,
)

if TYPE_CHECKING:  # pragma: no cover
    from netgravity.orchestrator.core.execution_context import ExecutionContext

logger = logging.getLogger(__name__)

#: Statuses that mean a capability's result is present and usable, so the
#: planner need not schedule it again.
_SATISFIED = (AgentStatus.SUCCESS, AgentStatus.PARTIAL)


class PlanRefused(PlanningFailureError):
    """
    A plan that must not execute.

    Subclasses the existing `PlanningFailureError` so every current handler of
    planning failures still catches it, and carries the typed `PlanValidation`
    so a caller — Phase 8.4, in particular — can branch on the reason rather
    than parse a message.
    """

    def __init__(self, message: str, validation: PlanValidation, **kw) -> None:
        super().__init__(message, **kw)
        self.validation = validation

    @property
    def reasons(self):
        return self.validation.reasons()


class PlanValidator:
    """
    The gate in front of the executor.

    Checks structure and permission only. It never asks whether a plan is a
    GOOD answer to the question — that is the planner's business, and a
    validator that second-guessed it would become a second planner.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def validate(
        self,
        plan: ExecutionPlan,
        *,
        context: Optional["ExecutionContext"] = None,
    ) -> PlanValidation:
        """
        Check one plan and return the verdict.

        Collects EVERY violation rather than stopping at the first. A planning
        bug that produced three problems should be reported as three problems;
        fixing them one round-trip at a time is how a caller ends up guessing.
        """
        violations: List[PlanViolation] = []
        notes: List[str] = []

        # --- structural: ids ------------------------------------------
        seen: Set[str] = set()
        for step in plan.steps:
            if step.step_id in seen:
                violations.append(PlanViolation(
                    reason=PlanFailureReason.DUPLICATE_STEP,
                    step_id=step.step_id, capability=step.capability,
                    detail=(f"step id '{step.step_id}' appears more than once; "
                            f"results would collide in the execution record"),
                ))
            seen.add(step.step_id)

        # --- structural: acyclic and orderable ------------------------
        # Delegated to the plan's own `validate_dag`, which already detects
        # unknown dependencies and cycles. Reused rather than reimplemented.
        try:
            plan.validate_dag()
        except PlanningFailureError as exc:
            message = str(exc)
            reason = (PlanFailureReason.DEPENDENCY_CYCLE if "cycle" in message.lower()
                      else PlanFailureReason.INVALID_ORDERING)
            violations.append(PlanViolation(reason=reason, detail=message))

        # --- per-step: exists, plannable, inputs ----------------------
        for step in plan.steps:
            if not self.registry.has(step.capability):
                violations.append(PlanViolation(
                    reason=PlanFailureReason.UNKNOWN_CAPABILITY,
                    step_id=step.step_id, capability=step.capability,
                    detail=(f"'{step.capability}' is not registered; "
                            f"registered: {self.registry.names()}"),
                ))
                continue

            contract = (self.registry.contract(step.capability)
                        if self.registry.has_contract(step.capability) else None)
            if contract is None:
                notes.append(
                    f"step '{step.step_id}' runs '{step.capability}', which has "
                    f"no contract; its inputs cannot be checked before execution"
                )
                continue

            if not contract.is_plannable:
                violations.append(PlanViolation(
                    reason=PlanFailureReason.NOT_PLANNABLE,
                    step_id=step.step_id, capability=step.capability,
                    detail=(f"'{step.capability}' is {contract.invocation.value} "
                            f"and may not appear as an independent plan step; it "
                            f"is reached through "
                            f"{contract.host_capability or 'the component that owns it'}"),
                ))

            missing_inputs = tuple(contract.missing_inputs(step.params))
            if missing_inputs:
                violations.append(PlanViolation(
                    reason=PlanFailureReason.UNSATISFIABLE_INPUT,
                    step_id=step.step_id, capability=step.capability,
                    detail=(f"'{step.capability}' declares required input(s) "
                            f"{list(missing_inputs)}, which this step does not "
                            f"supply"),
                    missing=missing_inputs,
                ))

        # --- required dependencies ------------------------------------
        violations.extend(self._check_dependencies(plan, context, notes))

        # --- emptiness ------------------------------------------------
        if not plan.steps:
            violations.append(PlanViolation(
                reason=PlanFailureReason.EMPTY_PLAN,
                detail="the plan contains no steps",
            ))

        validation = PlanValidation(
            checked=True,
            violations=violations,
            already_satisfied=self._already_satisfied(plan, context),
            notes=notes,
        )
        logger.info(
            "orchestrator.plan.validated plan=%s steps=%d valid=%s reasons=%s",
            plan.plan_id, len(plan.steps), validation.valid,
            [r.value for r in validation.reasons()],
        )
        return validation

    def assert_valid(
        self,
        plan: ExecutionPlan,
        *,
        context: Optional["ExecutionContext"] = None,
    ) -> ExecutionPlan:
        """
        Attach the verdict to the plan, or refuse it.

        Raises:
            PlanRefused: the plan must not execute. No step is dropped and no
                partial plan is returned — silently pruning an invalid plan
                would execute something nobody designed.
        """
        validation = self.validate(plan, context=context)
        plan.validation = validation
        if not validation.valid:
            raise PlanRefused(
                f"Execution plan '{plan.plan_id}' for intent '{plan.intent}' was "
                f"refused: {validation.summary()}",
                validation,
                context={"plan_id": plan.plan_id, "intent": plan.intent,
                         "reasons": [r.value for r in validation.reasons()]},
            )
        return plan

    # ------------------------------------------------------------------

    def _check_dependencies(
        self,
        plan: ExecutionPlan,
        context: Optional["ExecutionContext"],
        notes: List[str],
    ) -> List[PlanViolation]:
        """
        Verify each step's REQUIRED contract dependencies can be met.

        Satisfaction is checked three ways, in order, and any one is enough:

          1. another step in this plan provides the capability
          2. another step provides a capability in the same DOMAIN — this is
             what lets `kpi.summarise`, whose contract can only name
             `optimization.solve`, be satisfied by `optimization.solve_scenario`
             in a scenario workflow. Both are OPTIMIZATION, and the KPI step
             genuinely does not care which solve produced the result.
          3. the context already holds a usable result from an earlier run

        `optional_dependencies` are excluded before any of this: those providers
        report their own absences, and RF reporting NOT_COMPUTABLE is strictly
        more informative than a plan refusing to run.
        """
        violations: List[PlanViolation] = []

        provided: Set[str] = set(plan.capabilities)
        domains_provided: Set[str] = set()
        for capability in sorted(provided):
            if self.registry.has_contract(capability):
                domains_provided.add(self.registry.contract(capability).domain.value)

        for step in sorted(plan.steps, key=lambda s: s.step_id):
            if not self.registry.has_contract(step.capability):
                continue
            contract = self.registry.contract(step.capability)

            unmet: List[str] = []
            for dependency in contract.required_dependencies:
                if dependency in provided:
                    continue
                if self.registry.has_contract(dependency):
                    if self.registry.contract(dependency).domain.value in domains_provided:
                        notes.append(
                            f"step '{step.step_id}' needs '{dependency}'; satisfied "
                            f"by another provider of the same domain in this plan"
                        )
                        continue
                if context is not None and \
                        context.capability_outcome(dependency) in _SATISFIED:
                    notes.append(
                        f"step '{step.step_id}' needs '{dependency}'; already "
                        f"satisfied in this execution context"
                    )
                    continue
                unmet.append(dependency)

            if unmet:
                violations.append(PlanViolation(
                    reason=PlanFailureReason.MISSING_HARD_DEPENDENCY,
                    step_id=step.step_id, capability=step.capability,
                    detail=(f"'{step.capability}' requires {sorted(unmet)}, which "
                            f"no step in this plan provides and the context does "
                            f"not already hold"),
                    missing=tuple(sorted(unmet)),
                ))

        return violations

    @staticmethod
    def _already_satisfied(
        plan: ExecutionPlan, context: Optional["ExecutionContext"],
    ) -> tuple:
        if context is None:
            return ()
        return tuple(sorted(
            capability for capability, status in context.capability_status.items()
            if status in _SATISFIED and capability not in plan.capabilities
        ))


class CapabilityGraphPlanner:
    """
    Derives a plan from goals plus the registered capability contracts.

    Goal-driven, deliberately. It adds a capability ONLY because something asked
    for it or because something asked for depends on it. Nothing is scheduled
    for being available, which is the failure mode section 4 warns about:
    running forecasting, resilience, optimization and governance on every
    request because they exist.
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry
        self.validator = PlanValidator(registry)

    def derive(
        self,
        goals: Sequence[str],
        *,
        intent: str = "DERIVED",
        workflow_id: str = "wf_derived",
        description: str = "",
        context: Optional["ExecutionContext"] = None,
        params: Optional[Dict[str, Dict[str, object]]] = None,
        skip_satisfied: bool = True,
        validate: bool = True,
    ) -> ExecutionPlan:
        """
        Build an ordered plan that reaches every goal.

        Args:
            goals: Capability ids the request needs. Order is irrelevant — the
                dependency graph decides execution order — but duplicates and
                already-included dependencies are harmless.
            context: When given, capabilities it already holds a usable result
                for are omitted (see `skip_satisfied`), and their absence is
                recorded in the plan's rationale rather than left implicit.
            params: Per-capability handler parameters.
            skip_satisfied: Omit capabilities already completed in `context`.
                Set False to force a fresh run of everything — which is what a
                request explicitly asking to recompute needs.
            validate: Attach the verdict, refusing an invalid plan. Off only for
                tests that want to inspect a deliberately broken graph.

        Raises:
            PlanRefused: a goal is unknown or not plannable, or the resulting
                plan fails validation.
        """
        rationale: List[str] = []
        requested = self._unique(goals)

        # --- goals must exist and be selectable -----------------------
        problems: List[PlanViolation] = []
        for goal in requested:
            if not self.registry.has_contract(goal):
                problems.append(PlanViolation(
                    reason=PlanFailureReason.UNKNOWN_CAPABILITY,
                    capability=goal,
                    detail=(f"'{goal}' is not a declared capability; declared: "
                            f"{[c.capability_id for c in self.registry.contracts()]}"),
                ))
                continue
            contract = self.registry.contract(goal)
            if not contract.is_plannable:
                problems.append(PlanViolation(
                    reason=PlanFailureReason.NOT_PLANNABLE,
                    capability=goal,
                    detail=(f"'{goal}' is {contract.invocation.value} and cannot "
                            f"be requested as a planning goal; it is reached "
                            f"through "
                            f"{contract.host_capability or 'the component that owns it'}"),
                ))
        if problems:
            validation = PlanValidation(checked=True, violations=problems)
            raise PlanRefused(
                f"Cannot plan for the requested goals: {validation.summary()}",
                validation, context={"goals": list(requested)},
            )

        # --- close over REQUIRED dependencies -------------------------
        needed = self._close(requested, rationale)

        # --- drop what the context already provides -------------------
        satisfied: List[str] = []
        if context is not None and skip_satisfied:
            for capability in list(needed):
                if context.capability_outcome(capability) in _SATISFIED:
                    needed.remove(capability)
                    satisfied.append(capability)
            for capability in sorted(satisfied):
                rationale.append(
                    f"'{capability}' not scheduled: this execution already holds "
                    f"a usable result "
                    f"({context.capability_outcome(capability).value})"
                )

        # --- a failed prerequisite blocks; it is not retried ----------
        if context is not None:
            blocked = sorted(
                capability for capability in needed
                if context.capability_outcome(capability) is not None
                and context.capability_outcome(capability) not in _SATISFIED
            )
            if blocked:
                validation = PlanValidation(
                    checked=True,
                    violations=[PlanViolation(
                        reason=PlanFailureReason.BLOCKED_BY_FAILURE,
                        capability=capability,
                        detail=(f"'{capability}' already failed in this execution "
                                f"({context.capability_outcome(capability).value}); "
                                f"the planner reports the block and stops rather "
                                f"than re-running it — retry policy belongs to "
                                f"the failure-management layer, not here"),
                    ) for capability in blocked],
                )
                raise PlanRefused(
                    f"Cannot plan: {validation.summary()}",
                    validation, context={"blocked": blocked},
                )

        # --- order, deterministically ---------------------------------
        ordered = self._order(needed)
        steps = self._build_steps(ordered, needed, params or {})

        plan = ExecutionPlan(
            workflow_id=workflow_id,
            intent=intent,
            steps=steps,
            description=description or (
                f"Derived plan reaching {sorted(requested)}."
            ),
            origin=PlanOrigin.CAPABILITY_GRAPH,
            rationale=rationale,
        )
        if context is not None:
            plan.request_id = context.request_id
            plan.execution_id = context.execution_id

        if validate:
            self.validator.assert_valid(plan, context=context)
            plan.validation = plan.validation.model_copy(
                update={"already_satisfied": tuple(sorted(satisfied))}
            )

        logger.info(
            "orchestrator.plan.derived goals=%s steps=%d skipped=%s",
            list(requested), len(plan.steps), satisfied,
        )
        return plan

    # ------------------------------------------------------------------

    @staticmethod
    def _unique(values: Iterable[str]) -> List[str]:
        """Order-preserving de-duplication, so output never depends on set order."""
        seen: Set[str] = set()
        out: List[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                out.append(value)
        return out

    def _close(self, goals: Sequence[str], rationale: List[str]) -> List[str]:
        """
        Add every REQUIRED dependency of everything requested, transitively.

        Breadth-first over a sorted frontier so the result does not depend on
        dictionary or set iteration order. Optional dependencies are excluded:
        adding them would schedule work the provider is explicitly built to do
        without.
        """
        needed = self._unique(goals)
        frontier = list(needed)
        while frontier:
            current = frontier.pop(0)
            if not self.registry.has_contract(current):
                continue
            contract = self.registry.contract(current)
            for dependency in contract.required_dependencies:
                if dependency in needed:
                    continue
                needed.append(dependency)
                frontier.append(dependency)
                rationale.append(
                    f"'{dependency}' added: required by '{current}'"
                )
            for dependency in contract.optional_dependencies:
                if dependency not in needed:
                    rationale.append(
                        f"'{dependency}' NOT added: '{current}' declares it "
                        f"optional and reports its absence itself"
                    )
        return needed

    def _order(self, capabilities: Sequence[str]) -> List[str]:
        """
        A single deterministic order.

        Kahn's algorithm over the required-dependency edges INSIDE this set,
        taking the alphabetically first ready capability at each step. Two
        independent capabilities therefore always come out in the same order,
        which is what makes a derived plan reproducible.

        This produces a sequential order on purpose. The layering that would
        permit concurrency is still available on the plan via
        `execution_layers()`; acting on it belongs to a later phase.
        """
        # Terminal capabilities are held back and appended by rank. They declare
        # no hard dependencies on purpose, so the graph cannot place them last —
        # ordering by dependencies alone puts them FIRST, which would have
        # governance ruling on evidence that did not exist yet.
        terminal = sorted(
            (c for c in capabilities if self._terminal_rank(c) > 0),
            key=lambda c: (self._terminal_rank(c), c),
        )
        members = {c for c in capabilities if self._terminal_rank(c) == 0}
        pending: Dict[str, Set[str]] = {}
        for capability in sorted(members):
            deps = set()
            if self.registry.has_contract(capability):
                deps = {
                    d for d in self.registry.contract(capability).required_dependencies
                    if d in members
                }
            pending[capability] = deps

        ordered: List[str] = []
        done: Set[str] = set()
        while pending:
            ready = sorted(c for c, deps in pending.items() if deps <= done)
            if not ready:
                # Only reachable from a cycle among REQUIRED dependencies. The
                # catalogue is checked for cycles at import, so this means the
                # registry was modified at runtime.
                remaining = sorted(pending)
                validation = PlanValidation(checked=True, violations=[PlanViolation(
                    reason=PlanFailureReason.DEPENDENCY_CYCLE,
                    detail=(f"required dependencies among {remaining} cannot be "
                            f"ordered; there is a cycle"),
                    missing=tuple(remaining),
                )])
                raise PlanRefused(
                    f"Cannot order the plan: {validation.summary()}",
                    validation, context={"remaining": remaining},
                )
            for capability in ready:
                ordered.append(capability)
                pending.pop(capability)
            done |= set(ready)
        return ordered + terminal

    def _build_steps(
        self,
        ordered: Sequence[str],
        members: Sequence[str],
        params: Dict[str, Dict[str, object]],
    ) -> List[PlanStep]:
        """
        One step per capability, carrying its declared contract.

        Step ids are derived from the capability id, so the same plan always
        names its steps the same way and a reader can find a step without
        consulting a mapping.
        """
        member_set = set(members)
        ids = {capability: self._step_id(capability) for capability in ordered}
        steps: List[PlanStep] = []
        # Position in the final order, so a terminal step can depend on
        # everything that precedes it without depending on anything after.
        position = {capability: i for i, capability in enumerate(ordered)}

        for capability in ordered:
            contract: Optional[CapabilityContract] = (
                self.registry.contract(capability)
                if self.registry.has_contract(capability) else None
            )
            depends_on = sorted(
                ids[d] for d in (contract.required_dependencies if contract else ())
                if d in member_set and d in ids
            )
            # Optional dependencies present in this plan become SOFT edges, so
            # the executor hands the provider an explicit absence rather than
            # blocking it — the same treatment the templates give RF.
            soft = sorted(
                ids[d] for d in (contract.optional_dependencies if contract else ())
                if d in member_set and d in ids
            )
            # A terminal capability takes a SOFT edge to every step before it.
            # SOFT, not HARD, and deliberately: reasoning explains whatever
            # evidence exists, and governance must return a verdict even when an
            # input is missing. This is exactly what `_reason_and_govern` does in
            # the hand-written templates, derived rather than repeated.
            if self._terminal_rank(capability) > 0:
                preceding = sorted(
                    ids[c] for c in ordered
                    if position[c] < position[capability]
                )
                soft = sorted(set(soft) | set(preceding))

            steps.append(PlanStep(
                step_id=ids[capability],
                capability=capability,
                description=(contract.description if contract else ""),
                depends_on=sorted(set(depends_on) | set(soft)),
                soft_depends_on=soft,
                params=dict(params.get(capability, {})),
                optional=bool(contract and not contract.is_authoritative),
                required_inputs=list(contract.required_inputs) if contract else [],
                optional_inputs=list(contract.optional_dependencies) if contract else [],
                expected_output=(contract.output_type if contract else ""),
                domain=(contract.domain.value if contract else ""),
                timeout_seconds=None,
                execution_mode=(contract.execution_mode if contract else None),
            ))
        return steps

    def _terminal_rank(self, capability: str) -> int:
        """Declared terminal rank, or 0 when the capability is not terminal."""
        if not self.registry.has_contract(capability):
            return 0
        return self.registry.contract(capability).terminal_rank

    @staticmethod
    def _step_id(capability: str) -> str:
        """`optimization.solve_scenario` -> `optimization_solve_scenario`."""
        return capability.replace(".", "_")


__all__ = ["CapabilityGraphPlanner", "PlanRefused", "PlanValidator"]
