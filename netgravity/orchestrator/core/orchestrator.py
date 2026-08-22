"""
Orchestrator — The control plane.

Coordinates the full lifecycle:

    RECEIVE → UNDERSTAND → CLASSIFY → PLAN → VALIDATE → EXECUTE
            → COLLECT → CALCULATE → REASON → GOVERN → RESPOND → AUDIT

What this class does NOT do is as important as what it does. It contains no
optimization mathematics, no cost arithmetic, no REI logic and no risk formula.
It decides *what* runs, *in what order*, *against which data version*, and *what
may happen next* — then delegates every calculation to an authoritative engine.

Layer execution is dependency-aware: steps whose dependencies are satisfied form
a layer and run concurrently via `asyncio.gather`, with blocking solver work
dispatched to a thread pool by the engine adapters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.schemas.network import CanonicalNetwork

from netgravity.orchestrator.agents.llm_gateway import LLMGateway
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.audit.audit_logger import AuditLogger, ExecutionTrace
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.core.planner import WorkflowPlanner
from netgravity.orchestrator.exceptions import (
    FailureClass,
    OrchestratorError,
    SolverInfeasibleError,
    StaleSnapshotError,
)
from netgravity.orchestrator.governance.action_classifier import (
    ActionClassifier,
    ApprovalManager,
    AuthorizationService,
    GovernancePolicy,
)
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.schemas.actions import (
    ActionClassification,
    ActionType,
    FinalResponse,
)
from netgravity.orchestrator.schemas.plans import StepStatus, ToolRequest
from netgravity.orchestrator.schemas.requests import (
    Intent,
    IntentResolution,
    OrchestratorRequest,
)
from netgravity.orchestrator.state.stores import (
    ExecutionStateStore,
    ScenarioStore,
    SnapshotManager,
)
from netgravity.orchestrator.validation.validators import (
    RequestValidator,
    SnapshotValidator,
)

logger = logging.getLogger(__name__)


#: `EvidenceStatus` (control plane) → `ValueStatus` (twin). The twin reports the
#: same absences the orchestrator recorded rather than inventing a second
#: vocabulary for missing data.
_EVIDENCE_TO_VALUE_STATUS: Dict[str, Any] = {}

#: Terminal execution state → the twin status for a run that produced nothing.
_TERMINAL_TO_TWIN_STATUS: Dict[str, Any] = {}


def _init_twin_status_maps() -> None:
    """
    Populate the status maps on first use.

    Deferred so importing the orchestrator core does not pull in the twin
    schemas, keeping the dependency one-directional: the twin knows nothing
    about the orchestrator, and the orchestrator reaches for it only when it
    actually publishes.
    """
    if _EVIDENCE_TO_VALUE_STATUS:
        return
    from netgravity.orchestrator.schemas.plans import EvidenceStatus
    from netgravity.orchestrator.schemas.twin import TwinCalculationStatus, ValueStatus

    _EVIDENCE_TO_VALUE_STATUS.update({
        EvidenceStatus.UNAVAILABLE.value: ValueStatus.UNAVAILABLE,
        EvidenceStatus.NOT_RUN.value: ValueStatus.NOT_COMPUTED,
        EvidenceStatus.TIMEOUT.value: ValueStatus.FAILED,
        EvidenceStatus.INVALID.value: ValueStatus.FAILED,
    })
    _TERMINAL_TO_TWIN_STATUS.update({
        ExecutionState.INFEASIBLE.value: TwinCalculationStatus.INFEASIBLE,
        ExecutionState.STALE.value: TwinCalculationStatus.STALE,
        ExecutionState.FAILED.value: TwinCalculationStatus.FAILED,
        ExecutionState.REQUIRES_HUMAN.value: TwinCalculationStatus.PARTIAL,
        ExecutionState.REQUIRES_APPROVAL.value: TwinCalculationStatus.PARTIAL,
        ExecutionState.COMPLETED.value: TwinCalculationStatus.PARTIAL,
        ExecutionState.CANCELLED.value: TwinCalculationStatus.PARTIAL,
    })


class Orchestrator:
    """
    NetGravity's control plane.

    Construct via `netgravity.orchestrator.build_orchestrator()`, which wires
    the default capability set.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistry,
        snapshots: SnapshotManager,
        scenarios: ScenarioStore,
        state_store: Optional[ExecutionStateStore] = None,
        audit: Optional[AuditLogger] = None,
        gateway: Optional[LLMGateway] = None,
        governance_policy: Optional[GovernancePolicy] = None,
        twin: Optional["DigitalTwinService"] = None,
    ) -> None:
        from netgravity.orchestrator.twin.service import DigitalTwinService

        self.registry = registry
        self.snapshots = snapshots
        self.scenarios = scenarios
        self.state_store = state_store or ExecutionStateStore()
        self.audit = audit or AuditLogger()
        self.gateway = gateway
        # The Digital Twin. Held by the orchestrator because the orchestrator is
        # its sole upstream integration point — no engine has a reference to it,
        # and none can acquire one without passing through here.
        self.twin = twin if twin is not None else DigitalTwinService()
        _init_twin_status_maps()

        self.planner = WorkflowPlanner(registry)
        self.request_validator = RequestValidator()
        self.snapshot_validator = SnapshotValidator()

        self.policy = governance_policy or GovernancePolicy()
        self.classifier = ActionClassifier(self.policy)
        self.authorization = AuthorizationService()
        self.approvals = ApprovalManager(self.policy)

        # Populated by the registry wiring so capability handlers can reach
        # shared services without importing the orchestrator (avoiding cycles).
        self.services: Dict[str, Any] = {}

    # ==================================================================
    # Entry points
    # ==================================================================

    def run_sync(self, request: OrchestratorRequest) -> FinalResponse:
        """Synchronous wrapper for Flask handlers, scripts and tests."""
        return asyncio.run(self.run(request))

    async def run(self, request: OrchestratorRequest) -> FinalResponse:
        """
        Execute one request end to end.

        Never raises: every failure is captured, classified, recorded on the
        context and returned as a structured response. A control plane that
        throws is a control plane that loses its audit trail.
        """
        # ---- IDEMPOTENCY ------------------------------------------------
        existing = self.state_store.find_by_request_id(request.request_id)
        if existing is not None:
            logger.info(
                "orchestrator.request.deduplicated request_id=%s execution_id=%s",
                request.request_id, existing.execution_id,
            )
            return self._build_response(existing, deduplicated=True)

        # ---- RECEIVE ----------------------------------------------------
        context = ExecutionContext.from_request(request, self.snapshots.current_id)
        self.state_store.put(context)
        trace = self.audit.start(context)

        try:
            await self._execute_lifecycle(request, context, trace)
        except OrchestratorError as exc:
            self._fail(context, exc)
        except Exception as exc:  # noqa: BLE001 - never lose the trail
            logger.exception("orchestrator.unexpected_error execution_id=%s", context.execution_id)
            context.record_error("UNEXPECTED_ERROR", f"{type(exc).__name__}: {exc}",
                                 FailureClass.NON_RETRYABLE.value)
            if not context.is_terminal:
                context.transition(ExecutionState.FAILED, note=str(exc)[:200])
        finally:
            # ---- PROJECT (Digital Twin) ---------------------------------
            # In `finally` so a stale, failed or infeasible run is represented
            # too. Publishing nothing on those paths would leave the previous
            # state on screen, and a viewer would see a healthy network with no
            # sign that the run behind it collapsed.
            self._project_twin(context, trace)
            self.audit.finish(trace, context)

        return self._build_response(context)

    # ==================================================================
    # Lifecycle
    # ==================================================================

    async def _execute_lifecycle(
        self,
        request: OrchestratorRequest,
        context: ExecutionContext,
        trace: ExecutionTrace,
    ) -> None:
        # ---- VALIDATE REQUEST -------------------------------------------
        self.request_validator.validate(request)

        # ---- UNDERSTAND / CLASSIFY --------------------------------------
        context.transition(ExecutionState.UNDERSTANDING)
        resolution = await self._understand(request, context)
        context.intent = resolution.intent
        context.intent_resolution = resolution
        trace.interpreted_intent = resolution.intent.value
        trace.intent_source = resolution.source
        trace.intent_confidence = resolution.confidence
        trace.record(events.INTENT_RESOLVED, intent=resolution.intent.value,
                     source=resolution.source, confidence=resolution.confidence)

        if resolution.intent == Intent.UNKNOWN:
            context.add_warning(
                "The request could not be classified into a known workflow."
            )
            context.record_error(
                "INVALID_REQUEST",
                f"Unable to determine intent. {resolution.rationale}",
                FailureClass.REQUIRES_HUMAN.value,
            )
            context.transition(ExecutionState.REQUIRES_HUMAN,
                               note="intent could not be determined")
            return

        # ---- PLAN --------------------------------------------------------
        plan = self.planner.plan(resolution)
        context.plan = plan
        context.workflow_id = plan.workflow_id
        context.required_capabilities = [s.capability for s in plan.steps]
        trace.workflow_id = plan.workflow_id
        trace.plan_steps = [
            {"step_id": s.step_id, "capability": s.capability,
             "depends_on": list(s.depends_on), "optional": s.optional}
            for s in plan.steps
        ]
        context.transition(ExecutionState.PLANNED)
        trace.record(events.PLAN_BUILT, steps=len(plan.steps))
        trace.record(
            events.WORKFLOW_STARTED,
            intent=plan.intent,
            steps=[s.step_id for s in plan.steps],
            layers=[len(layer) for layer in plan.execution_layers()],
        )

        # ---- VALIDATE ----------------------------------------------------
        context.transition(ExecutionState.VALIDATING)
        self.snapshot_validator.validate_freshness(self.snapshots, context.baseline_snapshot_id)
        snapshot = self.snapshots.get(context.baseline_snapshot_id or "")
        trace.data_version = snapshot.data_version
        trace.record(events.SNAPSHOT_VALIDATED, snapshot_id=snapshot.snapshot_id,
                     data_version=snapshot.data_version)

        # ---- EXECUTE / COLLECT / CALCULATE / REASON ----------------------
        context.transition(ExecutionState.RUNNING)
        await self._execute_plan(context, trace)

        # ---- GOVERN ------------------------------------------------------
        self._govern(context, trace)

        # ---- terminal state ---------------------------------------------
        self._settle(context)

    # ------------------------------------------------------------------
    # UNDERSTAND
    # ------------------------------------------------------------------

    async def _understand(
        self, request: OrchestratorRequest, context: ExecutionContext,
    ) -> IntentResolution:
        """Resolve intent, preferring an explicit one over interpretation."""
        if request.explicit_intent is not None:
            return IntentResolution(
                intent=request.explicit_intent,
                confidence=1.0,
                source="explicit",
                scenarios=list(request.explicit_scenarios),
                rationale="Intent supplied explicitly by the caller.",
            )

        if request.external_signal is not None and not (request.input or "").strip():
            return IntentResolution(
                intent=Intent.EXTERNAL_EVENT,
                confidence=1.0,
                source="explicit",
                rationale="A structured external signal was supplied.",
            )

        intent_agent = self.services.get("intent_agent")
        if intent_agent is None:
            return IntentResolution(
                intent=Intent.UNKNOWN, source="rules",
                rationale="No intent agent is registered.",
            )

        snapshot = self.snapshots.get(context.baseline_snapshot_id or "")
        known_ids = [
            f.id for f in snapshot.network.facilities
            if f.role.value not in ("MARKET", "CUSTOMER")
        ]

        resolution = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: intent_agent.resolve(
                request.input,
                known_facility_ids=known_ids,
                allow_llm=context.llm_enabled,
            ),
        )
        if resolution.raw_model_output:
            self.audit.get(context.execution_id).record_llm(  # type: ignore[union-attr]
                "intent", resolution.source, resolution.raw_model_output,
            )
        return resolution

    # ------------------------------------------------------------------
    # EXECUTE — dependency-aware, layer-parallel
    # ------------------------------------------------------------------

    async def _execute_plan(self, context: ExecutionContext, trace: ExecutionTrace) -> None:
        """
        Run the plan layer by layer.

        Steps in a layer are independent by construction, so they are launched
        together.

        Dependency criticality decides what a failure does:

          HARD unmet → the dependent is BLOCKED. It cannot run safely.
          SOFT unmet → the dependent RUNS, and is handed explicit
                       `unavailable_evidence` for what is missing.

        A soft failure therefore degrades the run rather than collapsing the
        rest of the graph — losing REI must not cost us the reasoning and
        governance that the surviving MILP and KPI results can still support.
        """
        assert context.plan is not None
        layers = context.plan.execution_layers()

        for layer_index, layer in enumerate(layers):
            runnable: List[str] = []

            for step_id in layer:
                step = context.plan.step(step_id)
                assert step is not None

                resolution = context.plan.classify_dependencies(
                    step, set(context.completed_steps),
                )

                if not resolution.runnable:
                    step.status = StepStatus.BLOCKED
                    context.record_blocked(step_id, step.capability, resolution.blocking)
                    trace.record(events.STEP_BLOCKED, step_id=step_id,
                                 capability=step.capability,
                                 blocked_by=resolution.blocking)
                    trace.record(events.EVIDENCE_UNAVAILABLE, step_id=step_id,
                                 capability=step.capability, status="NOT_RUN",
                                 reason=f"hard dependency failed: {resolution.blocking}")
                    logger.warning(
                        "orchestrator.step.blocked step=%s hard_deps_failed=%s %s",
                        step_id, resolution.blocking, context.correlation(),
                    )
                    continue

                if resolution.is_degraded:
                    # Runs, but honestly: the missing pieces are named.
                    missing_caps = [
                        (context.plan.step(d).capability if context.plan.step(d) else d)
                        for d in resolution.degraded
                    ]
                    context.add_warning(
                        f"step '{step_id}' running with degraded evidence; unavailable: "
                        f"{', '.join(missing_caps)}"
                    )
                    trace.record(events.STEP_DEGRADED, step_id=step_id,
                                 capability=step.capability,
                                 soft_deps_failed=resolution.degraded,
                                 missing_capabilities=missing_caps)
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
            for step_id in runnable:
                step = context.plan.step(step_id)
                assert step is not None
                trace.record(events.STEP_STARTED, step_id=step_id,
                             capability=step.capability, layer=layer_index)

            results = await asyncio.gather(
                *(self._run_step(context, sid) for sid in runnable),
                return_exceptions=True,
            )

            for step_id, outcome in zip(runnable, results):
                step = context.plan.step(step_id)
                assert step is not None

                if isinstance(outcome, BaseException):
                    step.status = StepStatus.FAILED
                    context.record_error(
                        "ENGINE_FAILURE",
                        f"Step '{step_id}' raised {type(outcome).__name__}: {outcome}",
                        FailureClass.NON_RETRYABLE.value,
                    )
                    trace.record(events.STEP_EXCEPTION, step_id=step_id,
                                 capability=step.capability,
                                 error_type=type(outcome).__name__,
                                 error=str(outcome))
                    trace.record(events.STEP_FAILED, step_id=step_id,
                                 capability=step.capability, code="ENGINE_FAILURE")
                    continue

                context.record_step(step_id, outcome)
                step.status = StepStatus.COMPLETED if outcome.success else StepStatus.FAILED
                trace.record_tool(
                    step_id=step_id,
                    capability=outcome.capability,
                    success=outcome.success,
                    duration_seconds=outcome.duration_seconds,
                    attempts=outcome.attempts,
                    execution_mode=outcome.execution_mode.value,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message,
                )

                if outcome.success:
                    trace.engine_results[outcome.capability] = outcome.output
                    trace.record(events.STEP_COMPLETED, step_id=step_id,
                                 capability=outcome.capability,
                                 duration_seconds=round(outcome.duration_seconds, 4))
                    continue

                # Every failure — optional or not — removes evidence something
                # downstream might have relied on. Record the absence explicitly
                # so a reader never has to infer it from a missing key.
                trace.record(events.STEP_FAILED, step_id=step_id,
                             capability=outcome.capability, code=outcome.error_code,
                             optional=step.optional)
                evidence = context.unavailable_evidence.get(outcome.capability)
                trace.record(
                    events.EVIDENCE_UNAVAILABLE, step_id=step_id,
                    capability=outcome.capability,
                    status=evidence.status.value if evidence else "UNAVAILABLE",
                    reason=(evidence.reason if evidence else outcome.error_message) or "",
                )

                if outcome.error_code == "SOLVER_INFEASIBLE":
                    # An outcome, not a fault. Stop the run and report it —
                    # never retried, never dressed up as an error.
                    context.audit_metadata["infeasible_step"] = step_id
                    context.audit_metadata["infeasible_detail"] = outcome.metadata.get(
                        "error_context", {}
                    )
                    trace.record(events.SOLVER_INFEASIBLE, step_id=step_id,
                                 capability=outcome.capability)
                    return

    async def _run_step(self, context: ExecutionContext, step_id: str) -> Any:
        """Execute one step through its capability tool."""
        assert context.plan is not None
        step = context.plan.step(step_id)
        assert step is not None
        step.status = StepStatus.RUNNING

        capability = self.registry.get(step.capability)

        # Capability-level authorization: a model can name a capability, but it
        # only runs if this actor's role permits it.
        if capability.required_roles and context.actor.role.value not in capability.required_roles:
            from netgravity.orchestrator.exceptions import AuthorizationError
            raise AuthorizationError(
                f"Actor role {context.actor.role.value} may not invoke capability "
                f"'{step.capability}'. Allowed: {list(capability.required_roles)}",
                context={"capability": step.capability},
            )

        tool = self.registry.tool(step.capability)
        upstream = {
            dep: context.step_output(dep)
            for dep in step.depends_on
            if context.step_output(dep) is not None
        }
        # Hand the handler the missing pieces explicitly, so it can degrade
        # honestly instead of inferring a default from an absent key.
        unavailable = {
            cap: evidence
            for cap, evidence in context.unavailable_evidence.items()
        }
        return await tool.execute(
            context,
            ToolRequest(
                capability=step.capability,
                params=dict(step.params),
                upstream=upstream,
                unavailable=unavailable,
            ),
        )

    # ------------------------------------------------------------------
    # PROJECT — Digital Twin
    # ------------------------------------------------------------------

    def _project_twin(self, context: ExecutionContext, trace: ExecutionTrace) -> None:
        """
        Publish this run's network state to the Digital Twin.

        The ONLY path into the twin. No engine reaches it: MILP, REI and RF all
        report here first, and the orchestrator composes what they produced into
        one authoritative payload. A test asserts the absence of the direct
        paths, because an architecture rule nobody checks is a comment.

        Never raises. The twin is a representation layer, and a failure to draw
        the picture must not fail the analysis that produced it — the run
        carries a warning instead.
        """
        from netgravity.orchestrator.schemas.twin import (
            TwinCalculationStatus,
            TwinStateType,
            UnavailableValue,
            ValueStatus,
        )
        from netgravity.orchestrator.twin.builder import (
            build_twin_state,
            build_unavailable_state,
        )

        snapshot_id = context.baseline_snapshot_id
        if not snapshot_id:
            # No snapshot was ever pinned, so there is no network for a state to
            # describe. Nothing to publish, and nothing lost by not publishing.
            return

        try:
            unavailable = [
                UnavailableValue(
                    field=f"engine.{cap}",
                    status=_EVIDENCE_TO_VALUE_STATUS.get(
                        ev.status.value, ValueStatus.UNAVAILABLE,
                    ),
                    reason=ev.reason,
                    capability=cap,
                )
                for cap, ev in sorted(context.unavailable_evidence.items())
            ]

            published: List[Any] = []

            # ---- the observed / optimized state --------------------------
            baseline_state = context.network_states.get("optimization.solve")
            if baseline_state is not None:
                state_type = (
                    TwinStateType.BASELINE if not baseline_state.is_hypothetical
                    else TwinStateType.OPTIMIZED
                )
                published.append(self.twin.update(build_twin_state(
                    snapshot_id=snapshot_id,
                    state_type=state_type,
                    network_state=baseline_state,
                    execution_id=context.execution_id,
                    rei_registry=context.rei_registry,
                    risk_assessment=context.risk_results,
                    unavailable=unavailable,
                )))

            # ---- scenario states, one per scenario -----------------------
            # Each is published independently and compressed against the
            # baseline by the service. Scenario B's record never touches
            # scenario A's.
            for key, scenario_state in sorted(context.network_states.items()):
                if not key.startswith("scenario:"):
                    continue
                scenario_id = key.split(":", 1)[1]
                record = None
                try:
                    record = self.scenarios.get(scenario_id)
                except Exception:  # noqa: BLE001 - scenario store is advisory here
                    pass

                published.append(self.twin.update(build_twin_state(
                    snapshot_id=snapshot_id,
                    state_type=TwinStateType.SCENARIO,
                    network_state=scenario_state,
                    execution_id=context.execution_id,
                    scenario_id=scenario_id,
                    scenario_version=(record.version if record else None),
                    parent_snapshot_id=(record.parent_snapshot_id if record else None),
                    scenario_overrides=(list(record.overrides) if record else []),
                    rei_registry=context.rei_registry,
                    risk_assessment=context.risk_results,
                    unavailable=unavailable,
                )))

            # ---- nothing solved -----------------------------------------
            # An empty state, explicitly. `kpis` stays None: a TwinKPIs of
            # zeros would describe a network that ran perfectly for free.
            if not published:
                status = _TERMINAL_TO_TWIN_STATUS.get(
                    context.current_state.value, TwinCalculationStatus.FAILED,
                )
                if not unavailable:
                    unavailable = [UnavailableValue(
                        field="network_state",
                        status=ValueStatus.UNAVAILABLE,
                        reason=(
                            f"the run ended {context.current_state.value} without "
                            f"producing a network state"
                        ),
                    )]
                try:
                    snapshot = self.snapshots.get(snapshot_id)
                except OrchestratorError:
                    # The snapshot itself is gone — the state can still name it,
                    # which is more use to a reader than publishing nothing.
                    snapshot = None
                published.append(self.twin.update(build_unavailable_state(
                    snapshot_id=snapshot_id,
                    state_type=TwinStateType.OPTIMIZED,
                    calculation_status=status,
                    unavailable=unavailable,
                    execution_id=context.execution_id,
                    data_version=(snapshot.data_version if snapshot else None),
                    network_id=(snapshot.network_id if snapshot else None),
                    rei_registry=context.rei_registry,
                    risk_assessment=context.risk_results,
                )))

            context.twin_refs = published
            for ref in published:
                trace.record(
                    events.TWIN_STATE_PUBLISHED,
                    state_id=ref.state_id,
                    state_type=ref.state_type.value,
                    snapshot_id=ref.snapshot_id,
                    scenario_id=ref.scenario_id,
                    calculation_status=ref.calculation_status.value,
                    n_facilities=ref.n_facilities,
                    n_flows=ref.n_flows,
                )

        except Exception as exc:  # noqa: BLE001 - representation must not break analysis
            logger.exception(
                "orchestrator.twin.projection_failed %s", context.correlation(),
            )
            context.add_warning(
                f"The Digital Twin state could not be published "
                f"({type(exc).__name__}: {exc}). The analysis results below are "
                f"unaffected; only the visual representation is missing."
            )

    # ------------------------------------------------------------------
    # GOVERN
    # ------------------------------------------------------------------

    def _govern(self, context: ExecutionContext, trace: ExecutionTrace) -> None:
        """
        Apply deterministic governance.

        Runs even when the governance capability did not (for example after an
        infeasible solve), because no response may leave without a verdict.
        """
        if context.governance_result is not None:
            trace.record(events.GOVERNANCE_APPLIED,
                         classification=context.governance_result.classification.value)
            return

        evidence = self._collect_evidence(context)
        decision = self.classifier.classify(
            action_type=self._infer_action_type(context),
            is_feasible=evidence["is_feasible"],
            cost_impact_pct=evidence["cost_impact_pct"],
            unserved_demand_rate=evidence["unserved_demand_rate"],
            rei=evidence["rei"],
            risk_factor=evidence["risk_factor"],
            confidence=(context.reasoning.confidence if context.reasoning else "LOW"),
            data_quality_ok=evidence["data_quality_ok"],
            missing_evidence=evidence["missing_evidence"],
            unresolved_evidence=evidence["unresolved_evidence"],
            grounding_failed=evidence["grounding_failed"],
        )
        context.governance_result = decision

        if decision.classification == ActionClassification.APPROVAL_REQUIRED:
            approval = self.approvals.create_request(
                execution_id=context.execution_id,
                decision=decision,
                summary=(context.reasoning.summary if context.reasoning else ""),
                scenario_id=context.scenario_id,
                scenario_version=context.scenario_version,
                baseline_snapshot_id=context.baseline_snapshot_id,
            )
            decision.approval_request_id = approval.approval_id
            context.approval_request = approval
            self.state_store.put_approval(approval)

        trace.record(events.GOVERNANCE_DECISION,
                     classification=decision.classification.value,
                     action_type=decision.action_type.value,
                     rules=decision.triggered_rules,
                     governing_rule=decision.governing_rule,
                     rei=evidence["rei"], risk_factor=evidence["risk_factor"],
                     missing_evidence=sorted(evidence["missing_evidence"]),
                     evidence_status=decision.evidence_status,
                     blocked_by_missing_evidence=decision.blocked_by_missing_evidence,
                     grounding_failed=evidence["grounding_failed"])
        trace.record(events.GOVERNANCE_APPLIED,
                     classification=decision.classification.value,
                     rules=decision.triggered_rules)

    def _collect_evidence(self, context: ExecutionContext) -> Dict[str, Any]:
        """Gather the deterministic facts governance rules evaluate."""
        scenario_out = (context.output_of("optimization.solve_scenario")
                        or context.output_of("optimization.solve") or {})
        rei_out = context.output_of("resilience.assess") or {}

        is_feasible = bool(scenario_out.get("is_feasible", True))
        if context.audit_metadata.get("infeasible_step"):
            is_feasible = False

        cost_impact_pct = scenario_out.get("business_cost_delta_pct")
        if cost_impact_pct is not None:
            cost_impact_pct = abs(float(cost_impact_pct))

        risk_factor = None
        if context.risk_results is not None:
            risk_factor = context.risk_results.max_risk_factor

        # Evidence a governance decision would normally rely on. Anything listed
        # here is genuinely MISSING — never defaulted to zero, which would read
        # as "measured, and it was fine".
        missing_evidence = context.missing_evidence()

        grounding_failed = bool(
            context.reasoning is not None
            and getattr(context.reasoning, "grounding_status", "") == "GROUNDING_FAILED"
        )

        return {
            "is_feasible": is_feasible,
            "cost_impact_pct": cost_impact_pct,
            "unserved_demand_rate": scenario_out.get("unserved_demand_rate"),
            "rei": rei_out.get("max_rei"),
            "risk_factor": risk_factor,
            "data_quality_ok": scenario_out.get("reconciliation_is_closed", True) is not False,
            "missing_evidence": missing_evidence,
            "unresolved_evidence": self._unresolved_risk_evidence(context),
            "grounding_failed": grounding_failed,
        }

    @staticmethod
    def _unresolved_risk_evidence(context: ExecutionContext) -> Dict[str, str]:
        """
        Evidence that WAS produced but cannot be relied on.

        `missing_evidence` only sees steps that failed. A stale REI is invisible
        to it: `resilience.assess` succeeds, returns a perfectly valid batch, and
        RF then refuses it because it belongs to a different snapshot. Without
        this, governance would treat that run as fully evidenced.

        Two refusals are deliberately NOT treated as evidence failures, because
        both imply no event was ever asserted — so there is nothing missing:

          NO_EVENT_PROBABILITY  nobody claimed an event. Escalating here would
                                penalise every ordinary resilience query that
                                happens to have no live incident attached.
          NO_INPUTS             neither P nor REI. Since P is absent, this is
                                the same non-assertion case. When REI is ALSO
                                genuinely broken, its step has failed and
                                `missing_evidence` already carries it, so no
                                protection is lost by omitting it here.

        What remains are the cases where an event WAS asserted and the evidence
        to assess it could not be established.
        """
        from netgravity.orchestrator.governance.action_classifier import EvidenceState
        from netgravity.orchestrator.schemas.risk import RFNotComputableReason

        FAILURE_REASONS = {
            RFNotComputableReason.STALE_REI: EvidenceState.STALE,
            RFNotComputableReason.NO_REI: EvidenceState.UNAVAILABLE,
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE: EvidenceState.NOT_COMPUTABLE,
            RFNotComputableReason.INVALID_INPUT: EvidenceState.NOT_COMPUTABLE,
        }

        assessment = context.risk_results
        if assessment is None or assessment.results:
            # No RF was attempted, or at least one RF genuinely computed.
            return {}

        states = {
            FAILURE_REASONS[row.not_computable_reason].value
            for row in assessment.not_computable
            if row.not_computable_reason in FAILURE_REASONS
        }
        if not states:
            return {}
        return {"risk.compute_rf": sorted(states)[0]}

    def _infer_action_type(self, context: ExecutionContext) -> ActionType:
        """
        Map the run to the action it implies.

        A scenario that closes a facility implies CLOSE_FACILITY even though the
        run itself only *analysed* it — that is precisely what forces the
        human-only verdict for structurally significant changes.
        """
        if context.audit_metadata.get("infeasible_step"):
            return ActionType.REPORT

        resolution = context.intent_resolution
        if resolution and resolution.scenarios:
            from netgravity.orchestrator.schemas.requests import ScenarioActionType
            actions = {s.action for s in resolution.scenarios}
            if ScenarioActionType.CLOSE_FACILITY in actions:
                return ActionType.CLOSE_FACILITY
            if ScenarioActionType.OPEN_FACILITY in actions:
                return ActionType.OPEN_FACILITY
            if ScenarioActionType.CHANGE_CAPACITY in actions:
                return ActionType.CHANGE_CAPACITY
            if ScenarioActionType.SHIFT_VOLUME in actions:
                return ActionType.REROUTE_FLOW

        # EXPLANATION included: an explanation query produces a report, and a
        # report is governed. Omitting it left explanation runs classified NONE,
        # which short-circuits at R0 and bypasses the evidence rules entirely.
        if context.intent in (Intent.RESILIENCE_QUERY, Intent.EXTERNAL_EVENT,
                              Intent.NETWORK_STATE_QUERY, Intent.OPTIMIZATION_REQUEST,
                              Intent.EXPLANATION):
            return ActionType.REPORT
        return ActionType.NONE

    # ------------------------------------------------------------------
    # Settle / fail
    # ------------------------------------------------------------------

    def _settle(self, context: ExecutionContext) -> None:
        """Move to the correct terminal state."""
        if context.is_terminal:
            return

        if context.audit_metadata.get("infeasible_step"):
            context.transition(ExecutionState.INFEASIBLE,
                               note="solver proved no feasible solution")
            return

        # A run fails when a REQUIRED step failed or was blocked. Optional steps
        # failing is degradation, not failure — that is the point of soft
        # dependencies.
        #
        # Checked BEFORE the governance verdict: a governance decision describes
        # an action, and if the analysis that action rests on did not complete,
        # the run is broken regardless of what the verdict says. Reporting
        # REQUIRES_HUMAN would imply a usable result exists.
        mandatory_failed = [
            s.step_id for s in (context.plan.steps if context.plan else [])
            if s.status in (StepStatus.FAILED, StepStatus.BLOCKED) and not s.optional
        ]
        if mandatory_failed:
            context.transition(ExecutionState.FAILED,
                               note=f"required steps failed or blocked: {mandatory_failed}")
            return

        decision = context.governance_result
        if decision is not None:
            if decision.classification == ActionClassification.HUMAN_ONLY:
                context.transition(ExecutionState.REQUIRES_HUMAN, note=decision.reason[:200])
                return
            if decision.classification == ActionClassification.APPROVAL_REQUIRED:
                context.transition(ExecutionState.REQUIRES_APPROVAL, note=decision.reason[:200])
                return

        context.transition(ExecutionState.COMPLETED)

    def _fail(self, context: ExecutionContext, exc: OrchestratorError) -> None:
        """Record a control-plane failure and land in the right terminal state."""
        context.record_error(exc.code.value, exc.message, exc.failure_class.value,
                             **exc.context)
        logger.error(
            "orchestrator.execution.failed code=%s class=%s %s",
            exc.code.value, exc.failure_class.value, context.correlation(),
        )
        if context.is_terminal:
            return

        if isinstance(exc, StaleSnapshotError):
            context.transition(ExecutionState.STALE, note=exc.message[:200])
        elif isinstance(exc, SolverInfeasibleError):
            context.transition(ExecutionState.INFEASIBLE, note=exc.message[:200])
        elif exc.failure_class == FailureClass.REQUIRES_HUMAN:
            context.transition(ExecutionState.REQUIRES_HUMAN, note=exc.message[:200])
        else:
            context.transition(ExecutionState.FAILED, note=exc.message[:200])

    # ------------------------------------------------------------------
    # RESPOND
    # ------------------------------------------------------------------

    def _build_response(
        self, context: ExecutionContext, *, deduplicated: bool = False,
    ) -> FinalResponse:
        scenario_out = (context.output_of("optimization.solve_scenario")
                        or context.output_of("optimization.solve") or {})
        rei_out = context.output_of("resilience.assess") or {}
        kpi_out = context.output_of("kpi.summarise") or {}

        results: Dict[str, Any] = {}
        if scenario_out:
            results["network"] = scenario_out
        if kpi_out:
            results["kpis"] = kpi_out
        if rei_out:
            results["resilience"] = rei_out
        for cap, out in context.engine_results.items():
            if cap.startswith("optimization.solve_scenario_"):
                results.setdefault("scenarios", {})[cap] = out

        summary = context.reasoning.summary if context.reasoning else ""
        if not summary and context.current_state == ExecutionState.INFEASIBLE:
            summary = (
                "The network has no feasible solution under this configuration. "
                "No cost or service figures are reported, because none exist."
            )

        warnings = list(context.warnings)
        if deduplicated:
            warnings.append(
                f"Duplicate request_id '{context.request_id}': returning the original "
                f"execution rather than re-running it."
            )

        return FinalResponse(
            execution_id=context.execution_id,
            request_id=context.request_id,
            status=context.current_state.value,
            intent=context.intent.value,
            network_snapshot_id=context.baseline_snapshot_id,
            scenario_id=context.scenario_id,
            scenario_version=context.scenario_version,
            is_hypothetical=context.is_hypothetical,
            summary=summary,
            results=results,
            risk=(context.risk_results.model_dump() if context.risk_results else None),
            reasoning=context.reasoning,
            governance=context.governance_result,
            approval=context.approval_request,
            twin_states=[ref.model_dump(mode="json") for ref in context.twin_refs],
            errors=list(context.errors),
            warnings=warnings,
            duration_seconds=context.duration_seconds,
            started_at=context.created_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
            steps=context.step_summaries(),
        )

    # ==================================================================
    # Approval resumption
    # ==================================================================

    def resolve_approval(
        self,
        approval_id: str,
        *,
        actor: Any,
        approved: bool,
        note: str = "",
    ) -> FinalResponse:
        """
        Record a human decision and resume the ORIGINAL execution.

        The approval pins the scenario version and snapshot it was raised
        against. If the observed network has moved on since, the execution goes
        STALE rather than acting on a decision made about different data.
        """
        from netgravity.orchestrator.exceptions import GovernanceFailureError

        approval = self.state_store.get_approval(approval_id)
        if approval is None:
            raise GovernanceFailureError(
                f"Approval '{approval_id}' not found.",
                context={"approval_id": approval_id},
            )

        context = self.state_store.get(approval.execution_id)
        if context is None:
            raise GovernanceFailureError(
                f"Execution '{approval.execution_id}' for approval '{approval_id}' "
                f"is no longer available.",
                context={"approval_id": approval_id},
            )

        self.approvals.decide(approval, actor=actor, approved=approved, note=note)

        try:
            if approval.baseline_snapshot_id:
                self.snapshots.assert_fresh(approval.baseline_snapshot_id)
        except StaleSnapshotError as exc:
            context.record_error(exc.code.value, exc.message, exc.failure_class.value)
            if not context.is_terminal or context.current_state == ExecutionState.REQUIRES_APPROVAL:
                context.transition(ExecutionState.STALE,
                                   note="snapshot changed while awaiting approval")
            return self._build_response(context)

        if context.current_state == ExecutionState.REQUIRES_APPROVAL:
            context.transition(
                ExecutionState.COMPLETED if approved else ExecutionState.CANCELLED,
                note=f"approval {approval.status.value} by {actor.actor_id}",
            )
        return self._build_response(context)

    # ==================================================================
    # Introspection
    # ==================================================================

    def register_network(self, network: CanonicalNetwork, *, label: str = "") -> str:
        """Register an observed network and return its snapshot id."""
        return self.snapshots.register(network, label=label).snapshot_id

    def capabilities(self) -> List[Dict[str, Any]]:
        return self.registry.describe()

    def workflows(self) -> List[Dict[str, str]]:
        return self.planner.available_workflows()

    def get_trace(self, execution_id: str) -> Optional[ExecutionTrace]:
        return self.audit.get(execution_id)

    def health(self) -> Dict[str, Any]:
        """Control-plane health, including degraded-mode visibility."""
        return {
            "status": "ok",
            "capabilities": len(self.registry),
            "workflows": len(self.workflows()),
            "snapshots": self.snapshots.list_ids(),
            "current_snapshot": self.snapshots.current_id,
            "scenarios": len(self.scenarios.list_ids()),
            "llm": (self.gateway.stats() if self.gateway
                    else {"available": False, "reason": "no gateway configured"}),
        }
