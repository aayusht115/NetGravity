"""
Orchestrator — Deterministic engine adapters.

Thin async adapters over NetGravity's existing, authoritative engines. They add
NO mathematics: every number comes from `optimization.milp`, `metrics.kpis`,
`metrics.contracts`, `resilience.rei` or `costs.business_cost` exactly as those
modules compute it.

The adapters exist to:
  * present engines behind the uniform Tool/capability interface,
  * flatten engine output into the plain dicts the control plane passes around,
  * run blocking solver work in a thread so independent DAG branches can
    genuinely overlap,
  * translate engine outcomes into the orchestrator's error taxonomy — most
    importantly, mapping solver infeasibility to a NON-retryable outcome.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from netgravity.costs.business_cost import BusinessCostError, compute_business_network_cost
from netgravity.metrics.contracts import build_network_state_result, build_scenario_result
from netgravity.optimization.milp import solve as milp_solve
from netgravity.resilience.rei import assess_network_resilience
from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig, OptimizationMode
from netgravity.schemas.resilience import DisruptionConfig
from netgravity.schemas.results import FacilityResilienceRegistry, SolverStatus

from netgravity.orchestrator.exceptions import (
    EngineFailureError,
    SolverInfeasibleError,
)

logger = logging.getLogger(__name__)


async def _in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    Run blocking engine work off the event loop.

    Lets independent DAG branches (for example KPI and REI after a solve, or
    several scenarios at once) overlap without introducing any distributed
    infrastructure.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ---------------------------------------------------------------------------
# Optimization
# ---------------------------------------------------------------------------

class OptimizationClient:
    """
    Adapter over the single authoritative MILP.

    Each solve is exposed twice, following the same split `REIClient` uses
    below: a `*_result` method returning the TYPED frozen contract, and a
    convenience method returning the flattened dict the control plane passes
    between steps.

    The typed form is the authoritative one. Flattening drops the per-facility
    and per-lane detail — utilisation, throughput, lane volumes — keeping only
    id lists, which is enough for governance and reasoning but not for anything
    that has to represent the network. Consumers needing that detail take the
    contract; rebuilding it from the projection is impossible, not merely
    wasteful.
    """

    async def solve_result(
        self,
        network: CanonicalNetwork,
        *,
        config: Optional[OptimizationConfig] = None,
        mode: Optional[OptimizationMode] = None,
        scenario_id: Optional[str] = None,
    ) -> Any:
        """
        Solve and return the TYPED `NetworkStateResult` contract.

        Raises:
            SolverInfeasibleError: the solver proved no feasible solution.
                Classified NON_RETRYABLE — infeasibility is a property of the
                model, so re-solving cannot change it.
            EngineFailureError: solver error or an unexpected engine exception.
        """
        cfg = config or network.config
        if mode is not None:
            cfg = cfg.model_copy(update={"optimization_mode": mode})

        try:
            result = await _in_thread(milp_solve, network, cfg, scenario_id)
        except Exception as exc:  # noqa: BLE001
            raise EngineFailureError(
                f"MILP engine raised {type(exc).__name__}: {exc}",
                context={"scenario_id": scenario_id}, cause=exc,
            ) from exc

        if result.solver.status == SolverStatus.INFEASIBLE:
            raise SolverInfeasibleError(
                "The MILP proved no feasible solution exists for this network "
                "configuration. This is a mathematical outcome, not a transient "
                "fault, so it is not retried.",
                context={
                    "scenario_id": scenario_id,
                    "solver_status": result.solver.status.value,
                    "warnings": list(result.solver.warnings),
                    "optimization_mode": result.optimization_mode,
                },
            )

        if not result.is_solved:
            raise EngineFailureError(
                f"MILP returned status {result.solver.status.value}.",
                context={"scenario_id": scenario_id,
                         "solver_status": result.solver.status.value},
            )

        return await _in_thread(build_network_state_result, result, network, cfg, None)

    async def solve(
        self,
        network: CanonicalNetwork,
        *,
        config: Optional[OptimizationConfig] = None,
        mode: Optional[OptimizationMode] = None,
        scenario_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Solve and flatten in one call, for callers that only need the dict."""
        state = await self.solve_result(
            network, config=config, mode=mode, scenario_id=scenario_id,
        )
        return flatten_network_state(state)

    async def solve_scenario_result(
        self,
        scenario_network: CanonicalNetwork,
        *,
        scenario_id: str,
        scenario_name: str,
        baseline_state: Any = None,
        overrides: Optional[List[str]] = None,
        config: Optional[OptimizationConfig] = None,
    ) -> Any:
        """
        Solve a scenario and return the TYPED `ScenarioResult` contract.

        The scenario network is a separate materialised object; observed state
        is untouched.
        """
        cfg = config or scenario_network.config
        cfg = cfg.model_copy(update={
            "optimization_mode": OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION
        })

        try:
            result = await _in_thread(milp_solve, scenario_network, cfg, scenario_id)
        except Exception as exc:  # noqa: BLE001
            raise EngineFailureError(
                f"MILP engine raised {type(exc).__name__}: {exc}",
                context={"scenario_id": scenario_id}, cause=exc,
            ) from exc

        if result.solver.status == SolverStatus.INFEASIBLE:
            raise SolverInfeasibleError(
                f"Scenario '{scenario_id}' has no feasible solution. The network cannot "
                f"absorb these changes under the current constraints.",
                context={"scenario_id": scenario_id,
                         "solver_status": result.solver.status.value},
            )

        if not result.is_solved:
            raise EngineFailureError(
                f"Scenario '{scenario_id}' returned solver status "
                f"{result.solver.status.value}.",
                context={"scenario_id": scenario_id},
            )

        return await _in_thread(
            build_scenario_result,
            result, scenario_network, scenario_id, scenario_name,
            "CUSTOM", baseline_state, list(overrides or []), cfg, None,
        )

    async def solve_scenario(
        self,
        scenario_network: CanonicalNetwork,
        *,
        scenario_id: str,
        scenario_name: str,
        baseline_state: Any = None,
        overrides: Optional[List[str]] = None,
        config: Optional[OptimizationConfig] = None,
    ) -> Dict[str, Any]:
        """Solve a scenario and flatten it, deltas included."""
        scenario_result = await self.solve_scenario_result(
            scenario_network,
            scenario_id=scenario_id,
            scenario_name=scenario_name,
            baseline_state=baseline_state,
            overrides=overrides,
            config=config,
        )
        return flatten_scenario_result(scenario_result)


def flatten_scenario_result(scenario_result: Any) -> Dict[str, Any]:
    """Project a typed `ScenarioResult` into control-plane primitives."""
    flat = flatten_network_state(scenario_result.state)
    flat.update({
        "scenario_id": scenario_result.scenario_id,
        "scenario_name": scenario_result.scenario_name,
        "is_hypothetical": True,
        "business_cost_delta": scenario_result.business_cost_delta,
        "business_cost_delta_pct": scenario_result.business_cost_delta_pct,
        "served_demand_delta": scenario_result.served_demand_delta,
        "carbon_delta_kg": scenario_result.carbon_delta_kg,
        "baseline_business_cost": scenario_result.baseline_business_cost,
        "baseline_snapshot_id": scenario_result.baseline_network_id,
        "scenario_overrides": list(scenario_result.scenario_overrides),
    })
    return flat


def flatten_network_state(state: Any) -> Dict[str, Any]:
    """
    Flatten a `NetworkStateResult` into control-plane-friendly primitives.

    A PROJECTION, and a lossy one: per-facility utilisation and per-lane flow do
    not survive it. That is deliberate — governance, reasoning and the delta
    arithmetic all work from network-level figures, and carrying thousands of
    lane rows through every step of every plan would cost far more than it buys.
    Anything needing the detail consumes the typed contract instead.
    """
    costs = state.costs
    return {
        "network_id": state.network_id,
        "data_version": state.data_version,
        "optimization_mode": state.optimization_mode,
        "is_hypothetical": state.is_hypothetical,
        "solver_status": state.solver_status.value,
        "is_feasible": state.is_feasible,
        "solver_objective": costs.solver_objective,
        "business_network_cost": costs.business_network_cost,
        "cost_components": {
            "facility_cost": costs.facility_cost,
            "opening_cost": costs.opening_cost,
            "closure_cost": costs.closure_cost,
            "transport_cost": costs.transport_cost,
            "handling_cost": costs.handling_cost,
            "inventory_cost": costs.inventory_cost,
            "carbon_cost": costs.carbon_cost,
        },
        "shortage_penalty_cost": costs.shortage_penalty_cost,
        "reconciliation_is_closed": costs.reconciliation_is_closed,
        "total_demand": state.demand.total_demand,
        "served_demand": state.demand.served_demand,
        "unserved_demand": state.demand.unserved_demand,
        "demand_fill_rate": state.demand.demand_fill_rate,
        "unserved_demand_rate": (
            round(state.demand.unserved_demand / state.demand.total_demand, 6)
            if state.demand.total_demand else 0.0
        ),
        "open_facilities": list(state.open_facilities),
        "closed_facilities": list(state.closed_facilities),
        "avg_utilization_pct": state.avg_utilization_pct,
        "total_carbon_kg": state.total_carbon_kg,
        "pct_demand_in_sla": (state.service.pct_demand_in_sla if state.service else None),
        "sla_mode": (state.service.sla_mode if state.service else None),
        "service_unsupported_features": (
            list(state.service.unsupported_features) if state.service else []
        ),
        "run_id": state.metadata.run_id,
    }


# ---------------------------------------------------------------------------
# KPI
# ---------------------------------------------------------------------------

class KPIClient:
    """
    KPI projection.

    KPIs are computed inside the MILP result already; this adapter selects and
    presents them rather than recomputing, so there is exactly one definition of
    every metric.
    """

    async def summarise(self, optimization_output: Dict[str, Any]) -> Dict[str, Any]:
        src = optimization_output or {}
        return {
            "business_network_cost": src.get("business_network_cost"),
            "cost_components": src.get("cost_components", {}),
            "total_demand": src.get("total_demand"),
            "served_demand": src.get("served_demand"),
            "unserved_demand": src.get("unserved_demand"),
            "demand_fill_rate": src.get("demand_fill_rate"),
            "pct_demand_in_sla": src.get("pct_demand_in_sla"),
            "avg_utilization_pct": src.get("avg_utilization_pct"),
            "total_carbon_kg": src.get("total_carbon_kg"),
            "n_open_facilities": len(src.get("open_facilities", []) or []),
        }


# ---------------------------------------------------------------------------
# Resilience / REI
# ---------------------------------------------------------------------------

class REIClient:
    """
    Adapter over the deterministic REI service.

    Routes through `REIService` rather than calling the batch directly, so the
    orchestrator inherits caching, idempotency and invalidation without knowing
    anything about them. The orchestrator decides WHEN REI is needed; the
    service decides whether that requires 1 + N solves or a stored batch.
    """

    def __init__(self, service: Optional["REIService"] = None) -> None:
        from netgravity.resilience.service import REIService
        self.service = service or REIService()

    async def assess_registry(
        self,
        network: CanonicalNetwork,
        *,
        config: Optional[OptimizationConfig] = None,
        disruption_config: Optional[DisruptionConfig] = None,
        snapshot_id: Optional[str] = None,
    ) -> "FacilityResilienceRegistry":
        """
        Build (or reuse) the facility resilience registry, TYPED.

        This is the authoritative form. `assess()` flattens it for transport;
        anything that needs per-node calculation status, failure reasons or
        snapshot provenance must consume the registry itself rather than
        reconstructing one from the flattened projection.

        Raises:
            EngineFailureError: the assessment could not run (for example an
                infeasible baseline, which leaves nothing to compare against).
        """
        cfg = config or network.config
        dcfg = disruption_config or DisruptionConfig()

        try:
            return await _in_thread(
                self.service.get_or_compute, network, cfg, dcfg, snapshot_id=snapshot_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise EngineFailureError(
                f"REI assessment failed: {type(exc).__name__}: {exc}",
                context={"network_id": network.network_id}, cause=exc,
            ) from exc

    async def assess(
        self,
        network: CanonicalNetwork,
        *,
        config: Optional[OptimizationConfig] = None,
        disruption_config: Optional[DisruptionConfig] = None,
        snapshot_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assess and flatten in one call, for callers that only need the dict."""
        registry = await self.assess_registry(
            network, config=config, disruption_config=disruption_config,
            snapshot_id=snapshot_id,
        )
        return flatten_rei_registry(registry)


def flatten_rei_registry(registry: "FacilityResilienceRegistry") -> Dict[str, Any]:
    """
    Project a typed REI registry into control-plane primitives.

    A PROJECTION, deliberately one-way. Every field a consumer might need for
    provenance or failure reporting is carried through — including
    `calculation_status` and `failure_reason`, whose absence would make a node
    that FAILED indistinguishable from one that simply has no exposure.
    """
    rows: List[Dict[str, Any]] = [
        {
            "facility_id": r.facility_id,
            "facility_name": r.facility_name,
            "facility_role": r.facility_role,
            "rank": r.rank,
            "rei": r.rei,
            "performance_impact": r.performance_impact,
            "economic_impact": r.economic_impact,
            "cost_impact_pct": r.cost_impact_pct,
            "unserved_demand": r.unserved_demand,
            "unserved_demand_rate": r.unserved_demand_rate,
            "service_loss": r.service_loss,
            "rerouted_volume": r.rerouted_volume,
            "is_feasible": r.is_feasible,
            "solver_status": r.solver_status.value,
            "calculation_status": r.calculation_status.value,
            "failure_reason": r.failure_reason,
            "risk_classification": r.risk_classification.value,
            "diagnostics": list(r.diagnostics),
        }
        for r in registry.results
    ]
    top = registry.results[0] if registry.results else None

    return {
        "network_id": registry.network_id,
        "data_version": registry.data_version,
        "batch_id": registry.batch_id,
        "batch_status": registry.batch_status.value,
        "network_snapshot_id": registry.network_snapshot_id,
        # Set only when a cached batch was re-stamped for an equivalent snapshot;
        # None on a freshly computed batch.
        "computed_for_snapshot_id": registry.computed_for_snapshot_id,
        "material_fingerprint": registry.material_fingerprint,
        "model_version": registry.model_version,
        "served_from_cache": registry.served_from_cache,
        "n_milp_solves": registry.n_milp_solves,
        "baseline_business_cost": registry.baseline_business_cost,
        "baseline_solver_status": registry.baseline_solver_status.value,
        "rei_status": registry.rei_status.value,
        "max_performance_impact": registry.max_performance_impact,
        "n_facilities_assessed": registry.n_facilities_assessed,
        "n_successful": registry.n_successful,
        "n_failed": registry.n_failed,
        "n_infeasible": registry.n_infeasible,
        "highest_exposure_facility": top.facility_id if top else None,
        "max_rei": top.rei if top else None,
        "facilities": rows,
        # facility_id -> REI, the exact shape the RF calculator consumes.
        "rei_by_facility": {r.facility_id: r.rei for r in registry.results},
        "disruption_summary": registry.disruption_summary,
        "warnings": list(registry.warnings),
    }


# ---------------------------------------------------------------------------
# Forecasting
# ---------------------------------------------------------------------------

def flatten_forecast_result(result: Any) -> Dict[str, Any]:
    """
    Project a typed `ForecastResult` into control-plane primitives.

    A PROJECTION, one-way, exactly like `flatten_rei_registry`. Every series is
    carried through INCLUDING the failed ones — a series omitted because it
    could not be forecast would be indistinguishable from one nobody asked
    about, and the point of the status is that those are different.

    No quantity appears anywhere for a failed series. `points` is empty and
    `status` says why.
    """
    series_rows: List[Dict[str, Any]] = [
        {
            "market_id": s.market_id,
            "product_id": s.product_id,
            "status": s.status.value,
            "reason": s.reason,
            "engine": s.engine,
            "pattern": s.pattern.value if s.pattern else None,
            "n_history_periods": s.n_history_periods,
            # Empty for anything that did not succeed.
            "points": [
                {
                    "period": p.period, "mean": p.mean, "std_dev": p.std_dev,
                    "p10": p.p10, "p50": p.p50, "p90": p.p90,
                    "baseline_mean": p.baseline_mean,
                }
                for p in s.points
            ],
            # None when unmeasured — never a default that reads as accuracy.
            "accuracy": (
                {
                    "mae": s.accuracy.mae, "rmse": s.accuracy.rmse,
                    "wape": s.accuracy.wape, "mase": s.accuracy.mase,
                    "n_folds": s.accuracy.n_folds,
                    "beat_naive": s.accuracy.beat_naive,
                }
                if s.accuracy else None
            ),
            "signal_adjustments": [
                {
                    "signal_id": a.signal_id, "rule_id": a.rule_id,
                    "effect": a.effect.value,
                    "mean_multiplier": a.mean_multiplier,
                    "std_multiplier": a.std_multiplier,
                    "is_assumption": a.is_assumption,
                }
                for a in s.signal_adjustments
            ],
            # Why this forecast is what it is when a regime change moved it.
            # Projected so a control-plane consumer can answer "the forecast
            # changed because a structural break was detected" without
            # reaching back into the typed result. None when detection did
            # not run; present-and-not-detected when it ran and found nothing,
            # which is a different and equally reportable fact.
            "structural_break": (
                {
                    "detected": s.structural_break.detected,
                    "status": s.structural_break.status.value,
                    "change_period": s.structural_break.change_period,
                    "pre_break_level": s.structural_break.pre_break_level,
                    "post_break_level": s.structural_break.post_break_level,
                    "magnitude": s.structural_break.magnitude,
                    "sup_f": s.structural_break.sup_f,
                    "threshold": s.structural_break.threshold,
                    "detection_method": s.structural_break.detection_method,
                    "reason": s.structural_break.reason,
                }
                if s.structural_break is not None else None
            ),
            "regime": (
                {
                    "strategy": s.regime.strategy.value,
                    "basis": s.regime.basis.value,
                    "window_start": s.regime.window_start,
                    "n_periods_used": s.regime.n_periods_used,
                    "full_history_mae": s.regime.full_history_mae,
                    "recent_regime_mae": s.regime.recent_regime_mae,
                    "n_folds": s.regime.n_folds,
                    "reason": s.regime.reason,
                }
                if s.regime is not None else None
            ),
            "warnings": list(s.warnings),
        }
        for s in result.series
    ]

    return {
        "status": result.status.value,
        "target": result.provenance.target.value,
        "horizon": result.provenance.horizon,
        "frequency": result.provenance.frequency.value,
        "snapshot_id": result.provenance.snapshot_id,
        "data_version": result.provenance.data_version,
        "model_version": result.provenance.model_version,
        "engines_used": list(result.provenance.engines_used),
        "selection_mode": result.provenance.selection_mode.value,
        "signal_ids": list(result.provenance.signal_ids),
        #: Non-empty only when a structural break changed which history a
        #: forecast was built from — the one-line answer for a consumer that
        #: does not want to walk every series.
        "adapted_series": list(result.provenance.adapted_series),
        "generated_at": result.provenance.generated_at,
        "n_series": len(result.series),
        "n_successful": len(result.successful),
        "n_failed": len(result.failed),
        "status_counts": result.status_counts(),
        "series": series_rows,
        "warnings": list(result.warnings),
        "errors": list(result.errors),
    }


# ---------------------------------------------------------------------------
# Business cost
# ---------------------------------------------------------------------------

class BusinessCostClient:
    """Adapter over the reconciled business-cost layer."""

    async def compute(
        self,
        result: Any,
        network: CanonicalNetwork,
        *,
        config: Optional[OptimizationConfig] = None,
    ) -> Dict[str, Any]:
        try:
            bc = await _in_thread(compute_business_network_cost, result, network, config, None)
        except BusinessCostError as exc:
            raise EngineFailureError(
                f"Business cost could not be computed: {exc}",
                context={"network_id": network.network_id}, cause=exc,
            ) from exc

        return {
            "business_network_cost": bc.total,
            "components": dict(bc.components),
            "excluded_components": dict(bc.excluded_components),
            "solver_objective": bc.solver_objective,
            "shortage_penalty_cost": bc.shortage_penalty_cost,
            "reconciliation_is_closed": bc.reconciliation_is_reconciled,
            "notes": list(bc.notes),
        }
