"""
Orchestrator — The KPI Registry.

One access layer over metrics that already live in five different typed,
authoritative results: `NetworkStateResult` (MILP), `FacilityResilienceRegistry`
(REI), `RiskAssessment` (RF), `ForecastResult` (forecasting), and the Digital
Twin's own comparison machinery. Every method below either:

  1. WRAPS a value that already exists in one of those results, unchanged, or
  2. performs a DERIVED calculation using only already-authoritative inputs,
     each one documented at its call site with why it counts as "legitimate"
     rather than a new formula.

Nothing here recomputes a cost, a fill rate, an REI, an RF or a forecast. The
specialist engines listed in `docs/authoritative_kpi_architecture.md` §4 keep
sole ownership of their own arithmetic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING

from netgravity.orchestrator.metrics.thresholds import build_threshold_catalogue
from netgravity.orchestrator.schemas.kpi import (
    AuthoritativeEvidencePackage,
    EvidenceProvenance,
    KPIResult,
    KPIStatus,
    MetricScope,
    ScenarioMetricDelta,
    ThresholdSpec,
    TriggeredThreshold,
    UnavailableMetric,
)

if TYPE_CHECKING:  # pragma: no cover
    from netgravity.orchestrator.core.execution_context import ExecutionContext
    from netgravity.schemas.contracts import NetworkStateResult

#: Reasons a network-level metric is unavailable, explained once and reused —
#: consistent wording is part of making a gap traceable rather than merely
#: absent.
_NO_NETWORK_STATE = (
    "no optimization.solve or optimization.solve_scenario result is present "
    "in this execution"
)

#: Distance and intensity metrics computed by `NetworkKPIs`
#: (netgravity/metrics/kpis.py).
#:
#: Phase 9.1 recorded these as GAP-01: `compute_kpis()` produced them, but the
#: bridge to `NetworkStateResult` (netgravity/metrics/contracts.py) never copied
#: them, so nothing downstream of `ExecutionContext` could see them, and they
#: were reported as INSUFFICIENT_EVIDENCE rather than fabricated as zero.
#:
#: Phase 10.0 closed that gap: the five fields are now carried across the bridge
#: as `Optional[float]`. They are read here directly, and a None — meaning this
#: solve genuinely did not report the figure — still yields
#: INSUFFICIENT_EVIDENCE rather than a zero.
_DISTANCE_AND_INTENSITY_METRICS = {
    "weighted_avg_distance_km": ("km", "WEIGHTED_AVG_DISTANCE"),
    "inbound_avg_distance_km": ("km", "INBOUND_AVG_DISTANCE"),
    "outbound_avg_distance_km": ("km", "OUTBOUND_AVG_DISTANCE"),
    "carbon_per_unit": ("kg/unit", "CARBON_PER_UNIT"),
    "min_utilization_pct": ("%", "UTILIZATION"),
}

_MISSING_FIELD_REASON = (
    "NetworkKPIs.{field} was not reported by this solve, so no value reached "
    "NetworkStateResult. Not fabricated as zero."
)


def _single_network_state(context: "ExecutionContext") -> Optional[Any]:
    """
    The one `NetworkStateResult` this execution produced, if exactly one did.

    `ExecutionContext.network_states` is keyed by capability (or by
    `scenario:<id>` for a scenario solve), and most runs populate exactly one
    entry. Ambiguous when more than one is present — a caller comparing a
    baseline and a scenario must say which is which; see
    `KPIRegistry.scenario_comparison`.
    """
    states = context.network_states
    if len(states) == 1:
        return next(iter(states.values()))
    return states.get("optimization.solve")


#: Error codes the solver uses when it has PROVED that no feasible solution
#: exists, as distinct from failing to look.
_INFEASIBLE_CODES = ("SOLVER_INFEASIBLE", "INFEASIBLE", "SOLVER_UNBOUNDED")


def _infeasibility_reason(context: "ExecutionContext") -> Optional[str]:
    """
    The solver's reason, when this execution proved the network infeasible.

    Returns None when infeasibility was not established — in which case the
    absence of a network state genuinely is missing evidence rather than a
    result.
    """
    evidence = getattr(context, "unavailable_evidence", {}) or {}
    for capability in ("optimization.solve", "network.state"):
        record = evidence.get(capability)
        if record is None:
            continue
        reason = getattr(record, "reason", "") or ""
        status = getattr(getattr(record, "status", None), "value", "") or ""
        haystack = f"{reason} {status}".upper()
        if any(code in haystack for code in _INFEASIBLE_CODES):
            return reason or "the solver proved this network infeasible"

    for escalation in getattr(context, "escalations", []) or []:
        reason = getattr(escalation, "reason", "") or ""
        if "INFEASIB" in reason.upper():
            return reason
    return None


class KPIRegistry:
    """
    Stateless. Every method reads what it is given and returns typed
    `KPIResult`s (or a package of them); nothing is cached or stored between
    calls, so the registry cannot itself become a second source of truth.
    """

    def __init__(self) -> None:
        self._thresholds = build_threshold_catalogue()

    # ------------------------------------------------------------------
    # Thresholds
    # ------------------------------------------------------------------

    def thresholds(self) -> List[ThresholdSpec]:
        """The verified threshold catalogue. See `metrics/thresholds.py`."""
        return list(self._thresholds)

    def threshold_for(self, metric_id: str, severity: Optional[str] = None) -> Optional[ThresholdSpec]:
        matches = [t for t in self._thresholds if t.metric_id == metric_id]
        if severity is not None:
            matches = [t for t in matches if t.severity == severity]
        return matches[0] if matches else None

    def evaluate_thresholds(self, results: Sequence[KPIResult]) -> List[TriggeredThreshold]:
        """
        Which of the catalogue's thresholds fire against a set of results.

        Pure metadata comparison — `ThresholdSpec.evaluate` already refuses to
        fire on an unconfigured threshold or a missing value, so a metric that
        never computed cannot appear here.
        """
        triggered: List[TriggeredThreshold] = []
        for result in results:
            if not result.is_valid or not isinstance(result.value, (int, float)):
                continue
            for threshold in self._thresholds:
                if threshold.metric_id != result.metric_id:
                    continue
                if threshold.evaluate(float(result.value)):
                    triggered.append(TriggeredThreshold(
                        threshold=threshold, metric_id=result.metric_id,
                        value=float(result.value), entity_id=result.entity_id,
                    ))
        return triggered

    # ------------------------------------------------------------------
    # Network KPIs
    # ------------------------------------------------------------------

    def network_kpis(
        self, context: "ExecutionContext", *, key: Optional[str] = None,
    ) -> Dict[str, KPIResult]:
        """
        Network-level KPIs, wrapped verbatim from `NetworkStateResult`.

        Infeasibility is read from the result's OWN `is_feasible`/
        `solver_status` — never re-derived — and every cost/demand metric is
        reported INFEASIBLE rather than a zeroed `NetworkKPIs` being read as a
        genuine measurement (the engine's own zero-fill for infeasible results
        is a documented, verified behaviour of `compute_kpis`; this layer does
        not repeat it).
        """
        state = context.network_states.get(key) if key else _single_network_state(context)
        eid = context.execution_id

        # A solve that PROVED infeasibility is evidence, not an absence of it.
        # When the solver halts on SOLVER_INFEASIBLE no network state is
        # published, and reporting that as INSUFFICIENT_EVIDENCE ("we could not
        # find out") loses the one thing that was actually established ("no
        # solution exists"). The two are different answers and lead to
        # different actions, so the proved case is reported as INFEASIBLE with
        # the solver's own reason attached.
        proved_infeasible = _infeasibility_reason(context)

        def missing(metric_id: str, unit: str = "", scope: MetricScope = MetricScope.NETWORK) -> KPIResult:
            if proved_infeasible:
                return KPIResult(
                    metric_id=metric_id, value=None, unit=unit,
                    scope=scope, formula_id="SOLVER_INFEASIBLE",
                    source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id,
                    scenario_id=context.scenario_id,
                    execution_id=eid, status=KPIStatus.INFEASIBLE,
                    metadata={"reason": proved_infeasible},
                )
            return KPIResult.insufficient_evidence(
                metric_id, reason=_NO_NETWORK_STATE, scope=scope, unit=unit,
                source_capability="optimization.solve", execution_id=eid,
            )

        if state is None:
            ids = [
                "business_network_cost", "solver_objective", "shortage_penalty_cost",
                "facility_cost", "transport_cost", "handling_cost",
                "inventory_cost", "carbon_cost", "opening_cost", "closure_cost",
                "total_demand", "served_demand", "unserved_demand", "demand_fill_rate",
                "n_facilities_open", "n_facilities_closed",
                "avg_utilization_pct", "max_utilization_pct",
                "total_carbon_kg", "pct_demand_in_sla",
            ]
            return {mid: missing(mid) for mid in ids}

        infeasible = not state.is_feasible or state.solver_status.value == "INFEASIBLE"

        # When the strict model proved infeasible, the engine may have returned
        # a plan that serves as much as the network physically can and reports
        # the rest as unserved demand. Every KPI from such a run must say so:
        # 24.1% average utilisation on a network that is 23.6% short of its own
        # demand means something very different from the same figure on a fully
        # served one.
        relaxation = dict(getattr(state, "solve_relaxation", None) or {})
        relaxed_note: Dict[str, Any] = {}
        if relaxation.get("solve_relaxation"):
            relaxed_note = {
                "solve_relaxation": relaxation.get("solve_relaxation"),
                "strict_solve_status": relaxation.get("strict_solve_status"),
                "relaxation_reason": relaxation.get("relaxation_reason"),
                "unserved_demand": relaxation.get("unserved_demand"),
            }

        #: The shortage penalty is a solver device for deciding WHICH demand to
        #: strand when not all of it can be served. It is not a price anyone
        #: pays, and `business_network_cost` excludes it — but it is emitted in
        #: INR and would read as ₹8.7bn of cost on a dashboard, so it carries
        #: the warning with it.
        notional = {
            "shortage_penalty_cost": (
                "Notional. The per-unit shortage penalty is a solver device for "
                "ranking which demand to strand, not a cost the business incurs. "
                "It is excluded from business_network_cost, which is the figure "
                "to read as money."
            ),
        }
        if relaxed_note:
            # The objective the solver minimised, which on a relaxed run is
            # dominated by the same notional penalty. It is the right number for
            # auditing the solve and the wrong one for reading as spend.
            notional["solver_objective"] = (
                "Includes the notional shortage penalty, so on this run it is "
                "not a monetary total. business_network_cost is the spend."
            )

        def wrap(metric_id: str, value: Any, unit: str, formula_id: str) -> KPIResult:
            if infeasible:
                return KPIResult(
                    metric_id=metric_id, value=None, unit=unit, formula_id=formula_id,
                    source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, scenario_id=context.scenario_id,
                    execution_id=eid, status=KPIStatus.INFEASIBLE,
                    metadata={"reason": f"solver_status={state.solver_status.value}"},
                )
            meta: Dict[str, Any] = dict(relaxed_note)
            if metric_id in notional:
                meta["notional"] = notional[metric_id]
            return KPIResult(
                metric_id=metric_id, value=value, unit=unit, formula_id=formula_id,
                source_capability="optimization.solve",
                authoritative_owner="netgravity.optimization.milp",
                snapshot_id=context.baseline_snapshot_id, scenario_id=context.scenario_id,
                execution_id=eid, status=KPIStatus.VALID,
                input_evidence={"solver_status": state.solver_status.value},
                metadata=meta,
            )

        costs, demand = state.costs, state.demand
        results: Dict[str, KPIResult] = {
            "business_network_cost": wrap("business_network_cost", costs.business_network_cost,
                                         "INR", "BUSINESS_NETWORK_COST"),
            "solver_objective": wrap("solver_objective", costs.solver_objective,
                                     "INR", "MILP_OBJECTIVE"),
            "shortage_penalty_cost": wrap("shortage_penalty_cost", costs.shortage_penalty_cost,
                                         "INR", "SHORTAGE_PENALTY"),
            # The components that ADD UP to business_network_cost. They were
            # computed on every solve and carried on `CostBreakdown`, but the
            # registry exposed only the total — so the dashboard's cost
            # breakdown (fixed / transport / handling / inventory) had nothing
            # to read and rendered blank beneath a populated total. Each is the
            # business-cost layer's own figure, wrapped, not recomputed.
            "facility_cost": wrap("facility_cost", costs.facility_cost,
                                  "INR", "FACILITY_FIXED_COST"),
            "transport_cost": wrap("transport_cost", costs.transport_cost,
                                   "INR", "TRANSPORT_COST"),
            "handling_cost": wrap("handling_cost", costs.handling_cost,
                                  "INR", "HANDLING_COST"),
            "inventory_cost": wrap("inventory_cost", costs.inventory_cost,
                                   "INR", "INVENTORY_COST"),
            "carbon_cost": wrap("carbon_cost", costs.carbon_cost,
                                "INR", "CARBON_COST"),
            "opening_cost": wrap("opening_cost", costs.opening_cost,
                                 "INR", "FACILITY_OPENING_COST"),
            "closure_cost": wrap("closure_cost", costs.closure_cost,
                                 "INR", "FACILITY_CLOSURE_COST"),
            "total_demand": wrap("total_demand", demand.total_demand, "units", "DEMAND_TOTAL"),
            "served_demand": wrap("served_demand", demand.served_demand, "units", "DEMAND_SERVED"),
            "unserved_demand": wrap("unserved_demand", demand.unserved_demand, "units", "DEMAND_UNSERVED"),
            "demand_fill_rate": wrap("demand_fill_rate", demand.demand_fill_rate,
                                    "fraction", "FILL_RATE"),
            "n_facilities_open": wrap("n_facilities_open", len(state.open_facilities),
                                     "count", "FACILITY_COUNT"),
            "n_facilities_closed": wrap("n_facilities_closed", len(state.closed_facilities),
                                       "count", "FACILITY_COUNT"),
            "avg_utilization_pct": wrap("avg_utilization_pct", state.avg_utilization_pct,
                                      "%", "UTILIZATION_AVG"),
            "max_utilization_pct": wrap("max_utilization_pct", state.max_utilization_pct,
                                      "%", "UTILIZATION_MAX"),
            "total_carbon_kg": wrap("total_carbon_kg", state.total_carbon_kg,
                                   "kg", "CARBON_TOTAL"),
            # What the figures above COVER. Every cost and volume metric in this
            # block is a total across `periods_modelled` periods, and a twelve-
            # period total read as one period's cost is wrong by a factor of
            # twelve with nothing in the number to reveal it. The period count is
            # therefore an authoritative metric in its own right, and
            # `cost_per_period` is published so no consumer has to divide —
            # a divided KPI computed in a UI is a second, unowned cost engine.
            "periods_modelled": wrap("periods_modelled",
                                     getattr(state, "periods_modelled", 1),
                                     "count", "PLANNING_PERIODS"),
            "cost_per_period": wrap("cost_per_period",
                                    getattr(state, "cost_per_period", 0.0),
                                    "INR", "BUSINESS_COST_PER_PERIOD"),
        }
        if state.service is not None:
            results["pct_demand_in_sla"] = wrap(
                "pct_demand_in_sla", state.service.pct_demand_in_sla, "%", "SLA_PCT",
            )
        else:
            results["pct_demand_in_sla"] = KPIResult.insufficient_evidence(
                "pct_demand_in_sla",
                reason="no ServiceReport is present on this NetworkStateResult",
                source_capability="optimization.solve", execution_id=eid,
            )

        # Distance/intensity metrics, read verbatim from the state now that the
        # Phase 10.0 bridge carries them. A None is still INSUFFICIENT_EVIDENCE.
        for field, (unit, formula_id) in _DISTANCE_AND_INTENSITY_METRICS.items():
            value = getattr(state, field, None)
            if value is None:
                results[field] = KPIResult.insufficient_evidence(
                    field, reason=_MISSING_FIELD_REASON.format(field=field), unit=unit,
                    source_capability="optimization.solve", execution_id=eid,
                )
            else:
                results[field] = KPIResult(
                    metric_id=field, value=float(value), unit=unit,
                    scope=MetricScope.NETWORK, formula_id=formula_id,
                    source_capability="optimization.solve",
                    authoritative_owner="netgravity.metrics.kpis",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                    # These five are built here rather than through `wrap()`
                    # because they come from `NetworkKPIs` rather than the cost
                    # and demand summaries. They describe the same solve, so
                    # they carry the same relaxation note: an average distance
                    # measured over a plan that strands 23% of demand is not an
                    # average distance over the plan the client asked for.
                    metadata=dict(relaxed_note),
                )
        return results

    def facility_kpis(
        self, context: "ExecutionContext", *, key: Optional[str] = None,
    ) -> Dict[str, Dict[str, KPIResult]]:
        """Per-facility utilisation/throughput, wrapped from `FacilitySummary`."""
        state = context.network_states.get(key) if key else _single_network_state(context)
        if state is None:
            return {}
        eid = context.execution_id
        out: Dict[str, Dict[str, KPIResult]] = {}
        for fac in state.facilities:
            out[fac.facility_id] = {
                "utilization_pct": KPIResult(
                    metric_id="utilization_pct", value=fac.utilization_pct, unit="%",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="UTILIZATION", source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
                # Utilisation in the single busiest period. Over a horizon,
                # `utilization_pct` is an average, and an average is what hides
                # the month a site runs out of room — the specific thing a
                # multi-period model exists to find. Equal to the average on a
                # single-period solve, so this is never a second, disagreeing
                # answer to the same question.
                "peak_utilization_pct": KPIResult(
                    metric_id="peak_utilization_pct",
                    value=getattr(fac, "peak_utilization_pct", 0.0) or fac.utilization_pct,
                    unit="%",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="UTILIZATION_PEAK", source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
                "throughput_units": KPIResult(
                    metric_id="throughput_units", value=fac.throughput_units, unit="units",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="THROUGHPUT", source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
                # The same throughput on a per-period basis. `throughput_units`
                # is a horizon total; the capacity an upload states is per
                # period, and pairing the two would compare a year of volume
                # with a month of room. Published rather than left to a caller
                # to divide, so the ratio a screen shows is the same ratio
                # `utilization_pct` reports.
                "throughput_units_per_period": KPIResult(
                    metric_id="throughput_units_per_period",
                    value=getattr(fac, "throughput_units_per_period", 0.0)
                          or fac.throughput_units,
                    unit="units/period",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="THROUGHPUT_PER_PERIOD",
                    source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
                "is_open": KPIResult(
                    metric_id="is_open", value=fac.is_open, unit="bool",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="FACILITY_STATE", source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
                # Carried on `FacilitySummary` all along and never exposed, so
                # a caller could read a utilisation percentage but not the
                # capacity it was a percentage OF.
                "capacity_units": KPIResult(
                    metric_id="capacity_units", value=fac.capacity_units, unit="units",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="CAPACITY", source_capability="optimization.solve",
                    authoritative_owner="netgravity.optimization.milp",
                    snapshot_id=context.baseline_snapshot_id, execution_id=eid,
                ),
            }
        return out

    def flow_kpis(
        self, context: "ExecutionContext", *, key: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Per-lane solved volume and cost, wrapped from `FlowSummary`.

        The MILP decides how much moves down every lane and what that costs,
        and `NetworkStateResult.flows` has carried both since the contract was
        written — but nothing exposed them, so every corridor on the map and
        every row of a facility's lane table showed a null volume beneath a
        rate the upload had supplied. Wrapped, not recomputed: each figure is
        the solver's own, keyed by the lane it belongs to.
        """
        state = context.network_states.get(key) if key else _single_network_state(context)
        if state is None:
            return []
        return [
            {
                "origin_id": flow.origin_id,
                "destination_id": flow.destination_id,
                "flow_units": flow.flow_units,
                # The same volume per period, for pairing with a lane capacity
                # or a rate, both of which are per period. Equal to `flow_units`
                # on a single-period solve.
                "flow_units_per_period": getattr(
                    flow, "flow_units_per_period", 0.0) or flow.flow_units,
                "transport_cost": flow.transport_cost,
                "distance_km": flow.distance_km,
                "carbon_kg": flow.carbon_kg,
            }
            for flow in state.flows
        ]

    # ------------------------------------------------------------------
    # Resilience (REI) KPIs
    # ------------------------------------------------------------------

    def resilience_kpis(self, context: "ExecutionContext") -> Dict[str, KPIResult]:
        """Network-level REI summary, wrapped from `FacilityResilienceRegistry`."""
        reg = context.rei_registry
        eid = context.execution_id
        if reg is None:
            reason = context.unavailable_evidence.get("resilience.assess")
            return {
                "max_performance_impact": KPIResult.insufficient_evidence(
                    "max_performance_impact",
                    reason=(reason.reason if reason else "resilience.assess did not run"),
                    source_capability="resilience.assess", execution_id=eid,
                ),
            }
        return {
            "max_performance_impact": KPIResult(
                metric_id="max_performance_impact", value=reg.max_performance_impact,
                unit="INR", formula_id="PERFORMANCE_IMPACT",
                source_capability="resilience.assess",
                authoritative_owner="netgravity.resilience.rei",
                snapshot_id=reg.network_snapshot_id, execution_id=eid,
                status=(KPIStatus.VALID if reg.max_performance_impact is not None
                       else KPIStatus.NOT_COMPUTABLE),
                metadata=({} if reg.max_performance_impact is not None
                         else {"reason": f"rei_status={reg.rei_status.value}"}),
            ) if reg.max_performance_impact is not None else KPIResult.not_computable(
                "max_performance_impact", reason=f"rei_status={reg.rei_status.value}",
                source_capability="resilience.assess", execution_id=eid,
            ),
            "n_facilities_assessed": KPIResult(
                metric_id="n_facilities_assessed", value=reg.n_facilities_assessed,
                unit="count", formula_id="REI_BATCH_SIZE",
                source_capability="resilience.assess",
                authoritative_owner="netgravity.resilience.rei",
                snapshot_id=reg.network_snapshot_id, execution_id=eid,
            ),
            "n_infeasible": KPIResult(
                metric_id="n_infeasible", value=reg.n_infeasible, unit="count",
                formula_id="REI_INFEASIBLE_COUNT", source_capability="resilience.assess",
                authoritative_owner="netgravity.resilience.rei",
                snapshot_id=reg.network_snapshot_id, execution_id=eid,
            ),
        }

    def facility_resilience_kpis(self, context: "ExecutionContext") -> Dict[str, Dict[str, KPIResult]]:
        """Per-facility REI/cost-impact/risk-classification, wrapped from `FacilityResilienceResult`."""
        reg = context.rei_registry
        if reg is None:
            return {}
        eid = context.execution_id
        out: Dict[str, Dict[str, KPIResult]] = {}
        for row in reg.results:
            computed = row.rei_status.value == "COMPUTED" if hasattr(row.rei_status, "value") else False
            rei_result = (
                KPIResult(
                    metric_id="rei", value=row.rei, unit="ratio [0,1]",
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    formula_id="REI_NORMALIZATION", source_capability="resilience.assess",
                    authoritative_owner="netgravity.resilience.rei",
                    snapshot_id=reg.network_snapshot_id, execution_id=eid,
                    input_evidence={
                        "performance_impact": row.performance_impact,
                        "max_performance_impact": reg.max_performance_impact,
                        # The engine's own explanation, carried to whoever
                        # reports the number.
                        #
                        # A NEGATIVE performance impact — losing a facility
                        # makes the network cheaper — is a real result on a
                        # network whose baseline footprint is not optimal, and
                        # `rei.py` writes a full diagnostic saying so. That
                        # diagnostic stopped at the log: every consumer of this
                        # KPI got a figure that reads as nonsense with nothing
                        # attached to explain it.
                        "diagnostics": list(row.diagnostics or []),
                        "is_negative_impact": (row.performance_impact is not None
                                               and row.performance_impact < 0),
                        "unserved_demand_rate": getattr(
                            row, "unserved_demand_rate", None),
                    },
                ) if row.rei is not None else
                KPIResult.not_computable(
                    "rei", reason=(row.failure_reason or f"calculation_status={row.calculation_status.value}"),
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    source_capability="resilience.assess", execution_id=eid,
                )
            )
            cost_impact_result = (
                KPIResult(
                    metric_id="cost_impact_pct", value=row.cost_impact_pct, unit="%",
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    formula_id="COST_IMPACT_PCT", source_capability="resilience.assess",
                    authoritative_owner="netgravity.resilience.rei",
                    snapshot_id=reg.network_snapshot_id, execution_id=eid,
                ) if row.cost_impact_pct is not None else
                KPIResult.not_computable(
                    "cost_impact_pct", reason=(row.failure_reason or f"calculation_status={row.calculation_status.value}"),
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    source_capability="resilience.assess", execution_id=eid,
                )
            )
            out[row.facility_id] = {
                "rei": rei_result,
                "cost_impact_pct": cost_impact_result,
                "risk_classification": KPIResult(
                    metric_id="risk_classification", value=row.risk_classification.value,
                    unit="enum", scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    formula_id="RISK_CLASSIFICATION_RULES", source_capability="resilience.assess",
                    authoritative_owner="netgravity.resilience.rei",
                    snapshot_id=reg.network_snapshot_id, execution_id=eid,
                ),
            }
        return out

    # ------------------------------------------------------------------
    # Risk (RF) KPIs
    # ------------------------------------------------------------------

    def risk_kpis(self, context: "ExecutionContext") -> Dict[str, KPIResult]:
        """Network-level RF summary, wrapped from `RiskAssessment`."""
        assessment = context.risk_results
        eid = context.execution_id
        if assessment is None:
            reason = context.unavailable_evidence.get("risk.compute_rf")
            return {
                "max_risk_factor": KPIResult.insufficient_evidence(
                    "max_risk_factor",
                    reason=(reason.reason if reason else "risk.compute_rf did not run"),
                    source_capability="risk.compute_rf", execution_id=eid,
                ),
            }
        if assessment.max_risk_factor is None:
            return {
                "max_risk_factor": KPIResult.not_computable(
                    "max_risk_factor",
                    reason="no facility in this assessment produced a computed RF",
                    source_capability="risk.compute_rf", execution_id=eid,
                ),
            }
        return {
            "max_risk_factor": KPIResult(
                metric_id="max_risk_factor", value=assessment.max_risk_factor,
                unit="ratio [0,1]", formula_id="RF_COMPOUND",
                source_capability="risk.compute_rf",
                authoritative_owner="netgravity.orchestrator.risk.risk_factor",
                execution_id=eid, entity_id=assessment.highest_risk_entity,
            ),
        }

    def facility_risk_kpis(self, context: "ExecutionContext") -> Dict[str, Dict[str, KPIResult]]:
        """Per-facility RF/likelihood, wrapped from `RiskFactorResult`."""
        assessment = context.risk_results
        if assessment is None:
            return {}
        eid = context.execution_id
        out: Dict[str, Dict[str, KPIResult]] = {}
        for row in list(assessment.results) + list(assessment.not_computable):
            if row.facility_id is None:
                continue
            entry: Dict[str, KPIResult] = {}
            if row.is_computed:
                entry["risk_factor"] = KPIResult(
                    metric_id="risk_factor", value=row.risk_factor, unit="ratio [0,1]",
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    formula_id="RF_COMPOUND", source_capability="risk.compute_rf",
                    authoritative_owner="netgravity.orchestrator.risk.risk_factor",
                    execution_id=eid, input_evidence={"likelihood": row.likelihood, "rei": row.rei},
                )
            else:
                entry["risk_factor"] = KPIResult.not_computable(
                    "risk_factor",
                    reason=(row.not_computable_reason.value if row.not_computable_reason else "unknown"),
                    scope=MetricScope.FACILITY, entity_id=row.facility_id,
                    source_capability="risk.compute_rf", execution_id=eid,
                )
            out[row.facility_id] = entry
        return out

    # ------------------------------------------------------------------
    # Forecast metrics
    # ------------------------------------------------------------------

    def forecast_metrics(self, context: "ExecutionContext") -> Dict[str, KPIResult]:
        """Per-series forecast accuracy, wrapped from `ForecastResult`/`AccuracyMetrics`."""
        fc = context.forecast_result
        eid = context.execution_id
        if fc is None:
            reason = context.unavailable_evidence.get("forecast.demand")
            return {
                "forecast_status": KPIResult.insufficient_evidence(
                    "forecast_status",
                    reason=(reason.reason if reason else "forecast.demand did not run"),
                    source_capability="forecast.demand", execution_id=eid,
                ),
            }
        out: Dict[str, KPIResult] = {}
        for series in fc.series:
            key = f"{series.market_id}:{series.product_id}"
            if series.status.value != "OK" or series.accuracy is None:
                out[f"{key}.mase"] = KPIResult.not_computable(
                    "mase", reason=(series.reason or f"status={series.status.value}"),
                    scope=MetricScope.MARKET, entity_id=key,
                    source_capability="forecast.demand", execution_id=eid,
                )
                continue
            acc = series.accuracy
            out[f"{key}.mase"] = KPIResult(
                metric_id="mase", value=acc.mase, unit="ratio",
                scope=MetricScope.MARKET, entity_id=key,
                formula_id="MASE", source_capability="forecast.demand",
                authoritative_owner="netgravity.forecasting.validation",
                execution_id=eid,
            ) if acc.mase is not None else KPIResult.not_computable(
                "mase", reason="naive-1 benchmark MAE was ~0; MASE is undefined",
                scope=MetricScope.MARKET, entity_id=key,
                source_capability="forecast.demand", execution_id=eid,
            )
            out[f"{key}.mae"] = KPIResult(
                metric_id="mae", value=acc.mae, unit="units",
                scope=MetricScope.MARKET, entity_id=key,
                formula_id="MAE", source_capability="forecast.demand",
                authoritative_owner="netgravity.forecasting.validation",
                execution_id=eid,
            )
            out[f"{key}.wape"] = (
                KPIResult(
                    metric_id="wape", value=acc.wape, unit="ratio",
                    scope=MetricScope.MARKET, entity_id=key,
                    formula_id="WAPE", source_capability="forecast.demand",
                    authoritative_owner="netgravity.forecasting.validation",
                    execution_id=eid,
                ) if acc.wape is not None else KPIResult.not_computable(
                    "wape", reason="total actual demand was ~0 across the backtest folds",
                    scope=MetricScope.MARKET, entity_id=key,
                    source_capability="forecast.demand", execution_id=eid,
                )
            )
        return out

    # ------------------------------------------------------------------
    # Sustainability
    # ------------------------------------------------------------------

    def sustainability_kpis(self, context: "ExecutionContext") -> Dict[str, KPIResult]:
        """
        Carbon, grouped for a sustainability-facing consumer.

        Not a new computation: re-exposes `total_carbon_kg` and
        `carbon_per_unit` already produced by `network_kpis`, under the grouping
        the phase brief asks for. Both carry whatever status `network_kpis`
        assigned them, including INSUFFICIENT_EVIDENCE when a solve did not
        report the figure.
        """
        network = self.network_kpis(context)
        keys = ("total_carbon_kg", "carbon_per_unit")
        return {k: network[k] for k in keys if k in network}

    # ------------------------------------------------------------------
    # Scenario comparison
    # ------------------------------------------------------------------

    def scenario_comparison(
        self,
        context: "ExecutionContext",
        *,
        baseline_key: str = "optimization.solve",
        scenario_key: Optional[str] = None,
    ) -> List[ScenarioMetricDelta]:
        """
        BASELINE vs SCENARIO deltas, deterministic, never LLM-computed.

        `business_cost_delta`/`business_cost_delta_pct` are READ from the
        scenario's own `ScenarioResult` fields when that typed object is what
        populated `network_states` — the existing, already-tested computation
        in `netgravity/schemas/contracts.py::build_scenario_result` — never
        recomputed here.

        `demand_fill_rate`, `avg_utilization_pct`, `total_carbon_kg`,
        `pct_demand_in_sla` deltas are a NEW, generic, symmetric diff over two
        already-authoritative `NetworkStateResult`s — the same arithmetic
        `orchestrator/twin/service.py::_kpi_deltas` already uses for the
        Digital Twin's own comparison view, applied here directly to the two
        typed results this execution holds, with the same `NOT_COMPARABLE`
        refusal on a missing side.

        `rei_delta`/`risk_factor_delta` are NEW: genuinely absent from the
        Digital Twin's comparison (`_COMPARED_KPIS` operates on `TwinKPIs`
        fields only; REI/RF live in a separate `RiskContext` the twin never
        diffs). Computed here from the network-level summaries
        (`FacilityResilienceRegistry.max_performance_impact`-normalised `rei`
        is per-facility, so the NETWORK-level comparable figure is
        `max risk_factor`/highest REI — see field-level docstrings below) when
        BOTH sides have a resilience/risk assessment in this execution;
        `NOT_COMPARABLE` otherwise, never fabricated.
        """
        eid = context.execution_id
        scenario_state = (context.network_states.get(scenario_key) if scenario_key
                          else next((v for k, v in context.network_states.items()
                                    if k.startswith("scenario:")), None))
        baseline_state = context.network_states.get(baseline_key)

        deltas: List[ScenarioMetricDelta] = []

        # --- cost: reuse the existing, tested ScenarioResult fields ---------
        # `ctx.network_states["scenario:<id>"]` holds only `ScenarioResult.state`
        # (the plain `NetworkStateResult`) — the handler at
        # `orchestrator/registry.py::solve_scenario` stores `.state`
        # specifically, not the wrapping `ScenarioResult`. The deltas
        # (`business_cost_delta` etc.) exist only on the wrapper, which is why
        # they must be read from the FLATTENED transport projection
        # (`flatten_scenario_result`, `orchestrator/engines/deterministic.py`)
        # that the same handler already computed and recorded — never
        # recomputed here.
        scenario_flat = context.output_of("optimization.solve_scenario") or {}
        cost_delta = scenario_flat.get("business_cost_delta")
        cost_delta_pct = scenario_flat.get("business_cost_delta_pct")
        baseline_cost = (baseline_state.costs.business_network_cost if baseline_state else None)
        scenario_cost = (scenario_state.costs.business_network_cost if scenario_state else None)
        if cost_delta is not None:
            deltas.append(ScenarioMetricDelta(
                metric_id="business_network_cost",
                baseline_value=baseline_cost, comparison_value=scenario_cost,
                abs_delta=cost_delta, pct_delta=cost_delta_pct,
                direction=("UNCHANGED" if abs(cost_delta) < 1e-6 else
                          "INCREASED" if cost_delta > 0 else "DECREASED"),
            ))
        else:
            deltas.append(ScenarioMetricDelta(
                metric_id="business_network_cost", direction="NOT_COMPARABLE",
                reason="scenario result carries no business_cost_delta "
                      "(not produced via ScenarioResult, or no scenario present)",
            ))

        # --- generic NetworkStateResult-field deltas ------------------------
        def field_delta(metric_id: str, getter) -> ScenarioMetricDelta:
            if baseline_state is None or scenario_state is None:
                missing = "baseline" if baseline_state is None else ""
                missing += ("+" if missing and scenario_state is None else "") + \
                          ("scenario" if scenario_state is None else "")
                return ScenarioMetricDelta(
                    metric_id=metric_id, direction="NOT_COMPARABLE",
                    reason=f"no value on {missing} side",
                )
            left, right = getter(baseline_state), getter(scenario_state)
            if left is None or right is None:
                return ScenarioMetricDelta(
                    metric_id=metric_id, baseline_value=left, comparison_value=right,
                    direction="NOT_COMPARABLE", reason="metric absent on one side",
                )
            abs_d = round(float(right) - float(left), 6)
            pct_d = round(abs_d / abs(float(left)) * 100.0, 6) if abs(float(left)) > 1e-9 else None
            direction = "UNCHANGED" if abs(abs_d) < 1e-9 else ("INCREASED" if abs_d > 0 else "DECREASED")
            return ScenarioMetricDelta(
                metric_id=metric_id, baseline_value=float(left), comparison_value=float(right),
                abs_delta=abs_d, pct_delta=pct_d, direction=direction,
                reason=("baseline is zero, so no percentage change is defined" if pct_d is None else ""),
            )

        deltas.append(field_delta("demand_fill_rate", lambda s: s.demand.demand_fill_rate))
        deltas.append(field_delta("avg_utilization_pct", lambda s: s.avg_utilization_pct))
        deltas.append(field_delta("total_carbon_kg", lambda s: s.total_carbon_kg))
        deltas.append(field_delta(
            "pct_demand_in_sla",
            lambda s: s.service.pct_demand_in_sla if s.service is not None else None,
        ))

        # --- risk/resilience: genuinely new, only when both sides present --
        risk = context.risk_results
        if risk is not None and risk.max_risk_factor is not None:
            deltas.append(ScenarioMetricDelta(
                metric_id="risk_factor", direction="NOT_COMPARABLE",
                reason="only one risk assessment (not a paired baseline+scenario "
                      "pair) is present in this execution; a single-sided RF "
                      "cannot be diffed against itself",
            ))
        else:
            deltas.append(ScenarioMetricDelta(
                metric_id="risk_factor", direction="NOT_COMPARABLE",
                reason="no risk.compute_rf result is present in this execution",
            ))

        return deltas

    # ------------------------------------------------------------------
    # The evidence package
    # ------------------------------------------------------------------

    def evidence_package(self, context: "ExecutionContext") -> AuthoritativeEvidencePackage:
        """
        Every authoritative number for this execution, assembled in one place.

        A VIEW, exactly like `ExecutionContext.agent_result()` — nothing is
        stored here between calls, and calling this twice on an unchanged
        context returns an equal package.
        """
        network = self.network_kpis(context)
        facility = self.facility_kpis(context)
        resilience = self.resilience_kpis(context)
        facility_resilience = self.facility_resilience_kpis(context)
        risk = self.risk_kpis(context)
        facility_risk = self.facility_risk_kpis(context)
        forecast = self.forecast_metrics(context)
        sustainability = self.sustainability_kpis(context)

        # Merge facility-level REI/RF into facility_kpis so a caller has one
        # dict per facility rather than three to cross-reference.
        for fid, metrics in facility_resilience.items():
            facility.setdefault(fid, {}).update(metrics)
        for fid, metrics in facility_risk.items():
            facility.setdefault(fid, {}).update(metrics)

        all_named = {**network, **resilience, **risk, **forecast, **sustainability}
        for group in facility.values():
            all_named.update({f"facility.{k}": v for k, v in group.items()})
        all_results = list(all_named.values())

        triggered = self.evaluate_thresholds(all_results)
        unavailable = [
            UnavailableMetric(
                metric_id=r.metric_id, status=r.status,
                reason=r.metadata.get("reason", r.status.value),
                scope=r.scope, entity_id=r.entity_id,
            )
            for r in all_results if not r.is_valid
        ]

        return AuthoritativeEvidencePackage(
            network_kpis=network,
            facility_kpis=facility,
            lane_kpis={},
            forecast_metrics=forecast,
            resilience_metrics=resilience,
            risk_metrics=risk,
            sustainability_metrics=sustainability,
            scenario_comparison=[],
            triggered_thresholds=triggered,
            unavailable_evidence=unavailable,
            provenance=EvidenceProvenance(
                execution_id=context.execution_id,
                snapshot_id=context.baseline_snapshot_id,
                scenario_id=context.scenario_id,
                capability_statuses={
                    cap: status.value for cap, status in context.capability_status.items()
                },
            ),
        )


__all__ = ["KPIRegistry"]
