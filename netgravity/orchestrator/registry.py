"""
Orchestrator — Default capability wiring.

This module is the seam where new agents and engines are plugged in. Adding a
Carbon Optimization Agent, Supplier Risk Agent, Inventory Agent or
Transportation Agent means writing a handler and appending one
`registry.register(Capability(...))` call here — the orchestrator core, planner
and executor are untouched.

Each handler is a small async function that pulls what it needs from the
execution context and delegates to an authoritative engine or agent.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from netgravity.orchestrator.agents.external_signal_agent import ExternalSignalAgent
from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.llm_gateway import LLMGateway
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.forecasting.service import ForecastingService
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.audit.audit_logger import AuditLogger
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_INTERPRET_SIG,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_VALIDATE_SCEN,
)
from netgravity.orchestrator.engines.deterministic import (
    KPIClient,
    OptimizationClient,
    REIClient,
    flatten_forecast_result,
    flatten_network_state,
    flatten_rei_registry,
    flatten_scenario_result,
)
from netgravity.orchestrator.engines.scenario_builder import ScenarioBuilder
from netgravity.orchestrator.exceptions import (
    InvalidScenarioError,
    MissingDataError,
)
from netgravity.orchestrator.governance.action_classifier import GovernancePolicy
from netgravity.orchestrator.risk.risk_assessment import assess_event_risk
from netgravity.orchestrator.risk.risk_factor import assess_network_risk, not_computable
from netgravity.orchestrator.routing.capability_registry import CapabilityRegistry
from netgravity.orchestrator.routing.signal_router import ExternalSignalRouter
from netgravity.orchestrator.schemas.plans import ExecutionMode, ToolRequest
from netgravity.orchestrator.schemas.risk import RFNotComputableReason, RiskAssessment
from netgravity.orchestrator.state.stores import ExecutionStateStore, ScenarioStore, SnapshotManager
from netgravity.orchestrator.tools.base import (
    NO_RETRY,
    STANDARD_RETRY,
    Capability,
    RetryPolicy,
)
from netgravity.orchestrator.validation.validators import ResultValidator, ScenarioValidator
from netgravity.schemas.network import CanonicalNetwork, OptimizationMode

logger = logging.getLogger(__name__)


def build_orchestrator(
    *,
    network: Optional[CanonicalNetwork] = None,
    gateway: Optional[LLMGateway] = None,
    governance_policy: Optional[GovernancePolicy] = None,
    enable_llm: bool = True,
    history_provider: Optional[Any] = None,
    signal_provider: Optional[Any] = None,
) -> Orchestrator:
    """
    Construct a fully wired orchestrator.

    Args:
        network:  Observed network to register as the initial snapshot.
        gateway:  LLM gateway. Defaults to one configured from the environment;
                  when no token is present it reports unavailable and the
                  orchestrator runs deterministically.
        governance_policy: Threshold overrides.
        enable_llm: False disables model calls entirely for this instance.
        history_provider: `snapshot -> (List[DemandTimeSeries], warnings)`.
            Supplies observed demand history to the forecasting capability.
            Defaults to None, in which case forecasting reports that no history
            is available rather than inventing one — a deployment that has not
            ingested transactional history genuinely cannot forecast, and
            saying so is the correct behaviour.
        signal_provider: `snapshot -> (List[MarketIntelligenceSignal], warnings)`.
            Supplies structured external signals from the Extraction Agent to
            the control plane. Supplying a signal OFFERS it for routing; the
            orchestrator still decides whether it may reach the forecaster.

    Returns:
        A ready Orchestrator.
    """
    registry = CapabilityRegistry()
    snapshots = SnapshotManager()
    scenarios = ScenarioStore()
    state_store = ExecutionStateStore()
    audit = AuditLogger()

    if gateway is None and enable_llm:
        gateway = LLMGateway()
    if not enable_llm:
        gateway = None

    orchestrator = Orchestrator(
        registry=registry,
        snapshots=snapshots,
        scenarios=scenarios,
        state_store=state_store,
        audit=audit,
        gateway=gateway,
        governance_policy=governance_policy,
    )

    optimization = OptimizationClient()
    kpi = KPIClient()
    rei = REIClient()
    builder = ScenarioBuilder()
    scenario_validator = ScenarioValidator()
    result_validator = ResultValidator()
    intent_agent = IntentAgent(gateway)
    reasoning_agent = ReasoningAgent(gateway)
    signal_agent = ExternalSignalAgent(gateway)

    orchestrator.services.update({
        "optimization": optimization,
        "kpi": kpi,
        "rei": rei,
        "scenario_builder": builder,
        "scenario_validator": scenario_validator,
        "result_validator": result_validator,
        "intent_agent": intent_agent,
        "reasoning_agent": reasoning_agent,
        "signal_agent": signal_agent,
        "forecasting": ForecastingService(),
        "history_provider": history_provider,
        # The routing decision layer. Lives on the orchestrator, never on
        # the Forecasting Agent.
        "signal_router": ExternalSignalRouter(),
        "signal_provider": signal_provider,
    })

    _register_defaults(orchestrator, registry)

    if network is not None:
        orchestrator.register_network(network, label="initial")

    logger.info(
        "orchestrator.built capabilities=%d llm_available=%s",
        len(registry), bool(gateway and gateway.available),
    )
    return orchestrator


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _register_defaults(orch: Orchestrator, registry: CapabilityRegistry) -> None:
    svc = orch.services

    # ---- network.load_snapshot --------------------------------------
    async def load_snapshot(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """Pin and verify the observed snapshot this run operates on."""
        snapshot = orch.snapshots.assert_fresh(ctx.baseline_snapshot_id or "")
        return {
            "snapshot_id": snapshot.snapshot_id,
            "network_id": snapshot.network_id,
            "data_version": snapshot.data_version,
            "is_hypothetical": False,
            "n_facilities": len(snapshot.network.facilities),
            "n_demands": len(snapshot.network.demands),
        }

    # ---- scenario.create ---------------------------------------------
    async def create_scenario(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Materialise a hypothetical network in the scenario store.

        Built from a deep copy of the observed snapshot and stored separately;
        observed state is never touched.
        """
        index = int(req.params.get("scenario_index", 0))
        resolution = ctx.intent_resolution
        if resolution is None or index >= len(resolution.scenarios):
            raise MissingDataError(
                f"No scenario specification at index {index}. The request did not "
                f"describe a concrete scenario (e.g. which facility to close).",
                context={"scenario_index": index},
            )

        spec = resolution.scenarios[index]
        snapshot = orch.snapshots.get(ctx.baseline_snapshot_id or "")

        svc["scenario_validator"].validate(spec, snapshot.network)

        scenario_net, overrides = await asyncio.get_running_loop().run_in_executor(
            None, lambda: svc["scenario_builder"].build(snapshot.network, spec),
        )
        record = orch.scenarios.create(
            parent_snapshot_id=snapshot.snapshot_id,
            network=scenario_net,
            label=spec.label or f"{spec.action.value} {', '.join(spec.facility_ids)}",
            overrides=overrides,
            created_by=ctx.actor.actor_id,
        )

        if index == 0:
            ctx.scenario_id = record.scenario_id
            ctx.scenario_version = record.version
        if record.scenario_id not in ctx.scenario_ids:
            ctx.scenario_ids.append(record.scenario_id)

        return {
            "scenario_id": record.scenario_id,
            "scenario_version": record.version,
            "parent_snapshot_id": record.parent_snapshot_id,
            "label": record.label,
            "overrides": list(record.overrides),
            "is_hypothetical": True,
            "source": record.source,
        }

    # ---- scenario.validate -------------------------------------------
    async def validate_scenario(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """Re-validate the materialised scenario before it reaches the solver."""
        created = _first_upstream(req, "scenario_id")
        if not created:
            raise InvalidScenarioError("No scenario was created to validate.")
        record = orch.scenarios.get(created)

        if not record.is_hypothetical:
            raise InvalidScenarioError(
                f"Scenario '{record.scenario_id}' is not marked hypothetical; refusing "
                f"to proceed. Scenario state must never be treated as observed.",
                context={"scenario_id": record.scenario_id},
            )
        if record.parent_snapshot_id != ctx.baseline_snapshot_id:
            raise InvalidScenarioError(
                f"Scenario '{record.scenario_id}' derives from snapshot "
                f"'{record.parent_snapshot_id}' but this execution is pinned to "
                f"'{ctx.baseline_snapshot_id}'.",
                context={"scenario_id": record.scenario_id},
            )

        orch.scenarios.set_status(record.scenario_id, "VALIDATED")
        return {
            "scenario_id": record.scenario_id,
            "scenario_version": record.version,
            "valid": True,
            "overrides": list(record.overrides),
        }

    # ---- optimization.solve -------------------------------------------
    async def solve_network(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        snapshot = orch.snapshots.get(ctx.baseline_snapshot_id or "")
        mode_name = req.params.get("mode")
        mode = OptimizationMode(mode_name) if mode_name else None
        state = await svc["optimization"].solve_result(snapshot.network, mode=mode)
        # Keep the TYPED contract for the Digital Twin projection. The flattened
        # dict below drops per-facility utilisation and per-lane flow, and no
        # consumer can recover them from it.
        ctx.network_states[req.capability] = state
        output = flatten_network_state(state)
        # The observed counterpart to the scenario stamp above: this result IS
        # current network state, and says so.
        output.update({
            "result_kind": "OBSERVED_RESULT",
            "is_hypothetical": False,
            "baseline_snapshot_id": snapshot.snapshot_id,
            "model_version": snapshot.network.config.model_version,
            "execution_id": ctx.execution_id,
        })
        for warning in svc["result_validator"].validate_optimization(output):
            ctx.add_warning(warning)
        return output

    # ---- optimization.solve_scenario ----------------------------------
    async def solve_scenario(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        scenario_id = _first_upstream(req, "scenario_id")
        if not scenario_id:
            raise MissingDataError("No validated scenario is available to solve.")
        record = orch.scenarios.get(scenario_id)

        baseline_state = None
        for payload in req.upstream.values():
            if isinstance(payload, dict) and payload.get("business_network_cost") is not None:
                baseline_state = payload
                break

        scenario_result = await svc["optimization"].solve_scenario_result(
            record.network,
            scenario_id=record.scenario_id,
            scenario_name=record.label,
            overrides=record.overrides,
        )
        # Typed contract retained for the twin; see solve_network above.
        # Keyed by scenario, NOT by capability: a comparison workflow runs
        # several steps under this one capability name, and a capability key
        # would leave only the last scenario standing.
        ctx.network_states[f"scenario:{record.scenario_id}"] = scenario_result.state
        output = flatten_scenario_result(scenario_result)

        # Deltas are computed here rather than inside the engine adapter so the
        # baseline reference stays explicit and auditable.
        if baseline_state:
            base_cost = baseline_state.get("business_network_cost")
            if base_cost:
                delta = round(output["business_network_cost"] - base_cost, 4)
                output["business_cost_delta"] = delta
                output["business_cost_delta_pct"] = round(delta / base_cost * 100.0, 6)
                output["baseline_business_cost"] = base_cost

        # Complete scenario provenance. A scenario result read on its own must
        # announce that it is hypothetical and say exactly which baseline,
        # overrides, model and execution produced it — otherwise it can be
        # mistaken for current network state, which is the failure mode §11
        # exists to prevent.
        output.update({
            "result_kind": "SCENARIO_RESULT",
            "is_hypothetical": True,
            "scenario_id": record.scenario_id,
            "scenario_version": record.version,
            "scenario_label": record.label,
            "scenario_overrides": list(record.overrides),
            "baseline_snapshot_id": record.parent_snapshot_id,
            "model_version": record.network.config.model_version,
            "execution_id": ctx.execution_id,
        })

        for warning in svc["result_validator"].validate_optimization(output):
            ctx.add_warning(warning)
        orch.scenarios.attach_results(record.scenario_id, "optimization", output)
        orch.scenarios.set_status(record.scenario_id, "SOLVED")
        return output

    # ---- kpi.summarise -------------------------------------------------
    async def summarise_kpis(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        source = next(
            (p for p in req.upstream.values()
             if isinstance(p, dict) and p.get("business_network_cost") is not None),
            {},
        )
        return await svc["kpi"].summarise(source)

    # ---- resilience.assess ---------------------------------------------
    async def assess_resilience(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        snapshot = orch.snapshots.get(ctx.baseline_snapshot_id or "")
        # Pass the orchestrator's snapshot identity down so the registry records
        # the SAME id the RF layer later validates against. Without this the
        # batch defaults to `data_version` and every RF is wrongly STALE_REI.
        registry_obj = await svc["rei"].assess_registry(
            snapshot.network, snapshot_id=snapshot.snapshot_id,
        )
        # The TYPED batch is what RF consumes. The flattened dict below is a
        # transport projection; rebuilding a registry from it would lose
        # per-node calculation status and quietly report a FAILED node as OK.
        ctx.rei_registry = registry_obj
        output = flatten_rei_registry(registry_obj)

        for warning in svc["result_validator"].validate_rei(output):
            ctx.add_warning(warning)

        trace = orch.audit.get(ctx.execution_id)
        if trace is not None:
            trace.record(
                events.REI_LOOKUP,
                step_id=req.capability,
                batch_id=registry_obj.batch_id,
                batch_status=registry_obj.batch_status.value,
                rei_snapshot_id=registry_obj.network_snapshot_id,
                served_from_cache=registry_obj.served_from_cache,
                milp_solves=registry_obj.n_milp_solves,
                nodes_assessed=registry_obj.n_facilities_assessed,
                nodes_failed=registry_obj.n_failed,
            )
        return output

    # ---- forecast.demand -------------------------------------------------
    async def forecast_demand(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Estimate future demand for the markets in the observed network.

        The Orchestrator decides WHEN this runs — the capability is reachable
        only from a plan step, and the forecast workflow contains no solver, so
        a demand question cannot trigger an optimisation.

        History comes from the `history_provider` service, which reads what the
        ingestion pipeline wrote to its staging zone. With no provider and no
        history supplied on the request there is nothing to forecast, and the
        step reports that rather than inventing a series.
        """
        from netgravity.forecasting.history import series_for_network
        from netgravity.forecasting.schemas import ForecastRequest, SelectionMode

        snapshot = orch.snapshots.get(ctx.baseline_snapshot_id or "")
        horizon = int(req.params.get("horizon", 1))

        pairs = {(d.market_id, d.product_id) for d in snapshot.network.demands}

        provider = svc.get("history_provider")
        history: List[Any] = []
        provider_warnings: List[str] = []
        if provider is not None:
            history, provider_warnings = provider(snapshot)
        for warning in provider_warnings:
            ctx.add_warning(f"forecast history: {warning}")

        matched, missing = series_for_network(history, pairs)
        if missing:
            ctx.add_warning(
                f"no observed history for {len(missing)} market-product pair(s): "
                f"{', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}. "
                f"They are reported unforecastable rather than defaulted."
            )

        if not matched:
            raise MissingDataError(
                "No observed demand history is available for this network, so no "
                "forecast can be produced. History reaches the forecaster through "
                "the ingestion staging zone; none was found for any market in the "
                "current snapshot.",
                context={"snapshot_id": snapshot.snapshot_id,
                         "markets_without_history": missing[:20]},
            )

        # ---- ROUTE external signals -------------------------------------
        # The orchestrator decides which extracted signals may inform this
        # forecast, and passes only those. The Forecasting Agent never reaches
        # for a signal itself; whatever is not handed to it here cannot
        # influence anything.
        #
        # Signals reach the context from the Extraction Agent, either supplied
        # on the request or fetched by the `signal_provider` service. Extraction
        # structures them; it does not decide they will be used.
        offered = list(ctx.market_signals)
        provider = svc.get("signal_provider")
        if provider is not None:
            provided, provider_signal_warnings = provider(snapshot)
            offered.extend(provided)
            for warning in provider_signal_warnings:
                ctx.add_warning(f"external signals: {warning}")

        routing = svc["signal_router"].route_for_forecast(
            offered,
            known_entity_ids={f.id for f in snapshot.network.facilities},
        )
        ctx.signal_routing = routing
        for record in routing.rejected:
            ctx.add_warning(
                f"external signal '{record.signal_id}' not routed to forecasting "
                f"({record.outcome.value}): {record.reason}"
            )

        routing_trace = orch.audit.get(ctx.execution_id)
        if routing.records and routing_trace is not None:
            routing_trace.record(
                events.SIGNALS_ROUTED, step_id=req.capability,
                accepted=len(routing.accepted),
                considered=len(routing.records),
                outcomes=routing.outcome_counts(),
                decisions=routing.audit_rows(),
            )

        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: svc["forecasting"].forecast(ForecastRequest(
                series=matched,
                horizon=horizon,
                snapshot_id=snapshot.snapshot_id,
                data_version=snapshot.data_version,
                network_id=snapshot.network_id,
                execution_id=ctx.execution_id,
                request_id=ctx.request_id,
                # ONLY what the orchestrator routed.
                signals=routing.accepted,
                enable_signal_enrichment=bool(routing.accepted),
                run_backtest=bool(req.params.get("run_backtest", True)),
                selection_mode=SelectionMode(
                    req.params.get("selection_mode", SelectionMode.PATTERN.value)
                ),
            )),
        )

        # The TYPED result is authoritative; the dict below is a transport
        # projection. Anything needing per-series status reads the former.
        ctx.forecast_result = result
        for warning in result.warnings:
            ctx.add_warning(f"forecast: {warning}")

        trace = orch.audit.get(ctx.execution_id)
        if trace is not None:
            trace.record(
                events.FORECAST_COMPLETED, step_id=req.capability,
                status=result.status.value, horizon=horizon,
                series=len(result.series),
                status_counts=result.status_counts(),
                engines=result.provenance.engines_used,
                signals_applied=result.provenance.signal_ids,
                model_version=result.provenance.model_version,
            )
        return flatten_forecast_result(result)

    # ---- external.interpret_signal --------------------------------------
    async def interpret_signal(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Interpret external evidence into a likelihood.

        Never merged into the network — the output is evidence with provenance,
        consumed only by the RF calculation.
        """
        snapshot = orch.snapshots.get(ctx.baseline_snapshot_id or "")
        known = [f.id for f in snapshot.network.facilities
                 if f.role.value not in ("MARKET", "CUSTOMER")]

        if ctx.external_signal is not None:
            signal = ctx.external_signal
        else:
            signal = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: svc["signal_agent"].interpret(
                    ctx.raw_input, known_facility_ids=known, allow_llm=ctx.llm_enabled,
                ),
            )
            ctx.external_signal = signal

        if signal.event_probability is None:
            ctx.add_warning(
                f"No defensible event probability could be established from the external "
                f"evidence (severity={signal.severity.value}, confidence={signal.confidence:.2f}). "
                f"RF will report NOT_COMPUTABLE. Severity and confidence are not "
                f"probabilities and are never substituted for one."
            )
        return signal.model_dump(mode="json")

    # ---- risk.compute_rf -------------------------------------------------
    async def compute_rf(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Combine P and REI deterministically. No model involvement.

        Every path through this handler ends in one of two states: an RF that
        was genuinely computed from two present inputs, or an explicit
        NOT_COMPUTABLE row naming what was missing. There is no third path that
        substitutes a value.
        """
        rei_out = ctx.output_of("resilience.assess") or {}
        registry = ctx.rei_registry

        # Both inputs are SOFT dependencies, so either may be genuinely absent.
        # Absent is reported as NOT_COMPUTABLE, never defaulted.
        rei_unavailable = "resilience.assess" in req.unavailable
        signal_unavailable = "external.interpret_signal" in req.unavailable
        probability = (ctx.external_signal.event_probability
                       if ctx.external_signal is not None else None)

        def finish(assessment) -> Dict[str, Any]:
            """Record the outcome once, on every exit path."""
            for warning in assessment.warnings:
                ctx.add_warning(f"risk: {warning}")
            ctx.risk_results = assessment

            trace = orch.audit.get(ctx.execution_id)
            if trace is not None:
                for row in assessment.results:
                    trace.record(
                        events.RF_CALCULATED, step_id=req.capability,
                        node_id=row.facility_id, event_probability=row.likelihood,
                        rei=row.rei, risk_factor=row.risk_factor, formula=row.formula,
                    )
                for row in assessment.not_computable:
                    trace.record(
                        events.RF_NOT_COMPUTABLE, step_id=req.capability,
                        node_id=row.facility_id,
                        reason=(row.not_computable_reason.value
                                if row.not_computable_reason else "UNKNOWN"),
                        event_probability=row.likelihood, rei=row.rei,
                    )
            return assessment.model_dump(mode="json")

        # ---- REI genuinely unavailable ----------------------------------
        # An empty assessment would be indistinguishable from "nothing at risk",
        # so an explicit NOT_COMPUTABLE row is emitted instead, carrying whatever
        # P we do have. REI stays None — never 0.
        if registry is None or rei_unavailable or not (rei_out.get("rei_by_facility") or {}):
            if rei_unavailable:
                reason = req.unavailable["resilience.assess"].reason
            elif registry is None:
                reason = "the REI step did not run"
            else:
                reason = "no exposure results were produced"

            detail = (
                f"Facility resilience exposure (REI) could not be obtained: {reason}. "
                f"Risk factor was therefore NOT computed. REI is UNKNOWN, not zero."
            )
            ctx.add_warning(f"RF NOT_COMPUTABLE for all entities: {detail}")

            assessment = RiskAssessment(
                results=[],
                not_computable=[not_computable(
                    (RFNotComputableReason.NO_INPUTS if probability is None
                     else RFNotComputableReason.NO_REI),
                    likelihood=probability,
                    facility_id=None,
                    provenance={"rei": f"unavailable:{reason}"},
                    note=detail,
                )],
                network_id=rei_out.get("network_id"),
                data_version=rei_out.get("data_version"),
                warnings=[detail],
            )
            return finish(assessment)

        # ---- No external event at all -------------------------------------
        # Exposure exists, but nothing says how likely anything is. RF across the
        # network is reported NOT_COMPUTABLE per facility with the reason.
        if ctx.external_signal is None:
            assessment = assess_network_risk(
                rei_by_facility=rei_out["rei_by_facility"],
                likelihood_by_facility={},
                network_id=rei_out.get("network_id"),
                data_version=rei_out.get("data_version"),
            )
            assessment.warnings.append(
                "No external signal was supplied, so no event probability exists "
                "and RF is NOT_COMPUTABLE. Severity and confidence are not "
                "probabilities and were not substituted."
            )
            return finish(assessment)

        # ---- Full path: event → node mapping → REI lookup → RF ------------
        # Node mapping, snapshot validation and RF all live in the deterministic
        # risk layer. The orchestrator coordinates; it does not compute.
        assessment = assess_event_risk(
            ctx.external_signal,
            registry,
            expected_snapshot_id=ctx.baseline_snapshot_id,
        )

        if signal_unavailable:
            assessment.warnings.append(
                "External signal interpretation was unavailable, so no event "
                "probability could be established."
            )
        return finish(assessment)

    # ---- reasoning.synthesise ---------------------------------------------
    async def synthesise(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Explain the deterministic results that ARE available.

        Receives partial evidence deliberately: whatever succeeded goes in
        `available_evidence`, and whatever is missing is named in
        `unavailable_evidence` so the narrative can say so rather than guess.
        """
        payload: Dict[str, Any] = {}
        scenario_out = (ctx.output_of("optimization.solve_scenario")
                        or ctx.output_of("optimization.solve"))
        if scenario_out:
            payload["network_state" if ctx.scenario_id is None else "scenario"] = scenario_out
            payload.setdefault("optimization", scenario_out)
        if ctx.output_of("kpi.summarise"):
            payload["kpis"] = ctx.output_of("kpi.summarise")
        if ctx.output_of("resilience.assess"):
            payload["rei"] = ctx.output_of("resilience.assess")
        # mode="json" so enums serialise to their values — these strings reach
        # the narrative, and "EventSeverity.SEVERE" is not something to show a
        # reader.
        if ctx.risk_results is not None:
            payload["risk"] = ctx.risk_results.model_dump(mode="json")
        if ctx.external_signal is not None:
            payload["external_evidence"] = ctx.external_signal.model_dump(mode="json")

        unavailable = {
            cap: {"status": ev.status.value, "reason": ev.reason}
            for cap, ev in ctx.unavailable_evidence.items()
        }

        result = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: svc["reasoning_agent"].reason(
                payload,
                unavailable_evidence=unavailable,
                allow_llm=ctx.llm_enabled,
                provenance={
                    "execution_id": ctx.execution_id,
                    "snapshot_id": ctx.baseline_snapshot_id or "",
                    "scenario_id": ctx.scenario_id or "",
                },
            ),
        )
        ctx.reasoning = result
        for warning in result.validation_warnings:
            ctx.add_warning(f"reasoning: {warning}")

        trace = orch.audit.get(ctx.execution_id)
        if trace is not None:
            trace.record(
                events.REASONING_COMPLETED, step_id=req.capability,
                source=result.source, confidence=result.confidence,
                evidence_cited=len(result.evidence),
                unavailable_evidence=sorted(unavailable),
            )
            # Grounding is not optional and not conditional — it runs on the
            # template path too, so this event is always emitted alongside.
            claims = result.grounded_claims or []
            trace.record(
                events.GROUNDING_COMPLETED, step_id=req.capability,
                grounding_status=result.grounding_status,
                claims_checked=len(claims),
                claims_grounded=sum(1 for c in claims
                                    if c.get("verdict") == "GROUNDED"),
                claims_contradicted=sum(1 for c in claims
                                        if c.get("verdict") == "CONTRADICTED"),
                claims_unsupported=sum(1 for c in claims
                                       if c.get("verdict") == "UNSUPPORTED"),
            )
        return result.model_dump()

    # ---- governance.classify ------------------------------------------------
    async def classify_action(ctx: ExecutionContext, req: ToolRequest) -> Dict[str, Any]:
        """
        Deterministic governance verdict.

        Delegates to the orchestrator's classifier so the same rules apply
        whether governance runs as a plan step or as the post-run safety net.
        """
        orch._govern(ctx, orch.audit.get(ctx.execution_id))  # noqa: SLF001
        assert ctx.governance_result is not None
        return ctx.governance_result.model_dump()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    registry.register_all([
        Capability(
            name=CAP_LOAD_NETWORK, handler=load_snapshot,
            description="Pin and verify the observed network snapshot for this run.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            timeout_seconds=15.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_CREATE_SCEN, handler=create_scenario,
            description="Materialise a hypothetical scenario network, isolated from observed state.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_LOAD_NETWORK,),
            timeout_seconds=30.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_VALIDATE_SCEN, handler=validate_scenario,
            description="Validate scenario overrides and provenance before solving.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_CREATE_SCEN,),
            timeout_seconds=15.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_OPTIMIZE, handler=solve_network,
            description="Solve the observed network with the authoritative MILP.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_LOAD_NETWORK,),
            timeout_seconds=300.0,
            # Retries cover transient engine faults only; infeasibility is
            # classified NON_RETRYABLE and is never re-attempted.
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0.5),
        ),
        Capability(
            name=CAP_OPTIMIZE_SCEN, handler=solve_scenario,
            description="Re-optimise a hypothetical scenario network.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_VALIDATE_SCEN,),
            timeout_seconds=300.0,
            retry_policy=RetryPolicy(max_attempts=2, backoff_seconds=0.5),
        ),
        Capability(
            name=CAP_KPI, handler=summarise_kpis,
            description="Project KPIs from an optimization result.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_OPTIMIZE,),
            timeout_seconds=30.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_REI, handler=assess_resilience,
            description="Assess facility resilience exposure (REI registry).",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_LOAD_NETWORK,),
            timeout_seconds=600.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_INTERPRET_SIG, handler=interpret_signal,
            description="Interpret external evidence into a likelihood with provenance.",
            execution_mode=ExecutionMode.PROBABILISTIC,
            dependencies=(CAP_LOAD_NETWORK,),
            timeout_seconds=120.0, retry_policy=STANDARD_RETRY,
        ),
        Capability(
            name=CAP_RISK, handler=compute_rf,
            description="Combine likelihood and exposure into RF deterministically.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_REI, CAP_INTERPRET_SIG),
            timeout_seconds=15.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_FORECAST, handler=forecast_demand,
            description="Estimate future demand from observed history (deterministic).",
            # DETERMINISTIC: the engines are numerical and reproducible. No
            # model call happens anywhere in the forecasting package — the
            # source repository's LLM signal path was not integrated.
            execution_mode=ExecutionMode.DETERMINISTIC,
            dependencies=(CAP_LOAD_NETWORK,),
            timeout_seconds=300.0, retry_policy=NO_RETRY,
        ),
        Capability(
            name=CAP_REASON, handler=synthesise,
            description="Explain deterministic results in natural language (advisory).",
            execution_mode=ExecutionMode.PROBABILISTIC,
            timeout_seconds=120.0, retry_policy=NO_RETRY, optional=True,
        ),
        Capability(
            name=CAP_GOVERN, handler=classify_action,
            description="Apply deterministic governance rules and classify the action.",
            execution_mode=ExecutionMode.DETERMINISTIC,
            timeout_seconds=15.0, retry_policy=NO_RETRY,
        ),
    ])


def _first_upstream(req: ToolRequest, key: str) -> Optional[str]:
    """First upstream payload carrying `key`."""
    for payload in req.upstream.values():
        if isinstance(payload, dict) and payload.get(key):
            return str(payload[key])
    return None
