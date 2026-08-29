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

#: Verified data gap: `NetworkKPIs` (netgravity/metrics/kpis.py) computes these,
#: but the bridge from the engine's `OptimizationResult` to the orchestrator's
#: `NetworkStateResult` (netgravity/metrics/contracts.py:194-215) does not copy
#: them across, so nothing downstream of `ExecutionContext` can see them.
#: Reported honestly as INSUFFICIENT_EVIDENCE rather than silently omitted or
#: fabricated. See `docs/authoritative_kpi_architecture.md` §7 and
#: `validation/kpi_authoritative_layer/data_gap_inventory.json`.
_DROPPED_AT_CONTRACT_BRIDGE = {
    "weighted_avg_distance_km": "km",
    "inbound_avg_distance_km": "km",
    "outbound_avg_distance_km": "km",
    "carbon_per_unit": "kg/unit",
    "min_utilization_pct": "%",
}

_BRIDGE_GAP_REASON = (
    "NetworkKPIs.{field} is computed by netgravity/metrics/kpis.py::compute_kpis "
    "but is not carried across the OptimizationResult -> NetworkStateResult "
    "bridge (netgravity/metrics/contracts.py); ExecutionContext therefore never "
    "receives it. Not fabricated as zero. See data_gap_inventory.json."
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

        def missing(metric_id: str, unit: str = "", scope: MetricScope = MetricScope.NETWORK) -> KPIResult:
            return KPIResult.insufficient_evidence(
                metric_id, reason=_NO_NETWORK_STATE, scope=scope, unit=unit,
                source_capability="optimization.solve", execution_id=eid,
            )

        if state is None:
            ids = [
                "business_network_cost", "solver_objective", "shortage_penalty_cost",
                "total_demand", "served_demand", "unserved_demand", "demand_fill_rate",
                "n_facilities_open", "n_facilities_closed",
                "avg_utilization_pct", "max_utilization_pct",
                "total_carbon_kg", "pct_demand_in_sla",
            ]
            return {mid: missing(mid) for mid in ids}

        infeasible = not state.is_feasible or state.solver_status.value == "INFEASIBLE"

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
            return KPIResult(
                metric_id=metric_id, value=value, unit=unit, formula_id=formula_id,
                source_capability="optimization.solve",
                authoritative_owner="netgravity.optimization.milp",
                snapshot_id=context.baseline_snapshot_id, scenario_id=context.scenario_id,
                execution_id=eid, status=KPIStatus.VALID,
                input_evidence={"solver_status": state.solver_status.value},
            )

        costs, demand = state.costs, state.demand
        results: Dict[str, KPIResult] = {
            "business_network_cost": wrap("business_network_cost", costs.business_network_cost,
                                         "INR", "BUSINESS_NETWORK_COST"),
            "solver_objective": wrap("solver_objective", costs.solver_objective,
                                     "INR", "MILP_OBJECTIVE"),
            "shortage_penalty_cost": wrap("shortage_penalty_cost", costs.shortage_penalty_cost,
                                         "INR", "SHORTAGE_PENALTY"),
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

        # Verified data gap: computed by the engine, dropped at the contract
        # bridge. Reported honestly rather than silently absent.
        for field, unit in _DROPPED_AT_CONTRACT_BRIDGE.items():
            results[field] = KPIResult.insufficient_evidence(
                field, reason=_BRIDGE_GAP_REASON.format(field=field), unit=unit,
                source_capability="optimization.solve", execution_id=eid,
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
                "throughput_units": KPIResult(
                    metric_id="throughput_units", value=fac.throughput_units, unit="units",
                    scope=MetricScope.FACILITY, entity_id=fac.facility_id,
                    formula_id="THROUGHPUT", source_capability="optimization.solve",
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
            }
        return out

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
                    input_evidence={"performance_impact": row.performance_impact,
                                   "max_performance_impact": reg.max_performance_impact},
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

        Not a new computation: re-exposes `total_carbon_kg` already computed by
        `network_kpis`, under the grouping the phase brief asks for.
        `carbon_per_unit` is listed in `network_kpis` as a documented data gap
        (see `_DROPPED_AT_CONTRACT_BRIDGE`) and is reported the same way here.
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
