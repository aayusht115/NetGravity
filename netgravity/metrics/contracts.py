"""
NetGravity — Deterministic Result Contract Builders
===================================================
Version: 1.4.0

Derives the frozen result contracts (`schemas/contracts.py`) from the engine's
native `OptimizationResult`.

Lives in `metrics/` because that is where NetGravity already derives structured
outputs from raw optimization results (see `metrics/kpis.py`). No optimization
or cost arithmetic happens here — cost components come from the existing
business-cost/reconciliation layer, so there is exactly one cost model.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from netgravity.costs.business_cost import (
    BusinessCostError,
    compute_business_network_cost,
)
from netgravity.optimization.modes import get_mode_policy
from netgravity.schemas.contracts import (
    CostBreakdown,
    DemandSummary,
    FacilitySummary,
    FlowSummary,
    ModelMetadata,
    NetworkStateResult,
    ScenarioResult,
)
from netgravity.schemas.network import CanonicalNetwork, NodeRole, OptimizationConfig
from netgravity.schemas.resilience import ResilienceCostBasis
from netgravity.schemas.results import OptimizationResult

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}


def _build_cost_breakdown(
    result:     OptimizationResult,
    network:    CanonicalNetwork,
    config:     OptimizationConfig,
    cost_basis: Optional[ResilienceCostBasis],
) -> CostBreakdown:
    """
    Assemble the cost contract, reusing the existing business-cost layer.

    Business network cost and the shortage-penalty split come from
    `costs/business_cost.py`, which in turn reuses `costs/reconciliation.py`.
    Nothing is recomputed here.
    """
    comp = result.objective_components or {}
    breakdown = CostBreakdown(
        facility_cost   = round(comp.get("facility_cost", 0.0), 4),
        opening_cost    = round(comp.get("opening_cost", 0.0), 4),
        closure_cost    = round(comp.get("closure_cost", 0.0), 4),
        transport_cost  = round(comp.get("transport_cost", 0.0), 4),
        handling_cost   = round(comp.get("handling_cost", 0.0), 4),
        inventory_cost  = round(comp.get("inventory_cost", 0.0), 4),
        carbon_cost     = round(comp.get("carbon_cost", 0.0), 4),
        shortage_penalty_cost = round(comp.get("shortage_cost", 0.0), 4),
        solver_objective      = round(result.solver.objective_value or 0.0, 4),
    )

    if not result.is_solved:
        breakdown.reconciliation_is_closed = False
        return breakdown

    try:
        business = compute_business_network_cost(
            result, network, config=config, cost_basis=cost_basis,
        )
    except BusinessCostError as exc:
        logger.warning("contracts.business_cost_unavailable run_id=%s error=%s", result.run_id, exc)
        breakdown.reconciliation_is_closed = False
        return breakdown

    breakdown.business_network_cost     = business.total
    breakdown.shortage_penalty_cost     = business.shortage_penalty_cost
    breakdown.included_components       = list(business.included_components)
    breakdown.excluded_components       = list(business.excluded_components.keys())
    breakdown.reconciliation_gap        = business.reconciliation_absolute_difference
    breakdown.reconciliation_is_closed  = business.reconciliation_is_reconciled
    return breakdown


def build_network_state_result(
    result:     OptimizationResult,
    network:    CanonicalNetwork,
    config:     Optional[OptimizationConfig] = None,
    cost_basis: Optional[ResilienceCostBasis] = None,
) -> NetworkStateResult:
    """
    Build the frozen NetworkStateResult contract from an OptimizationResult.

    Args:
        result:     The engine's native result.
        network:    The network that produced it (for snapshot identity and
                    facility metadata).
        config:     Config used for the run (defaults to network.config).
        cost_basis: Which components constitute business cost.

    Returns:
        NetworkStateResult — flat, self-describing, safe for downstream agents.
    """
    if config is None:
        config = network.config

    policy = get_mode_policy(config.optimization_mode)
    fac_map = {f.id: f for f in network.facilities}
    kpis = result.kpis

    costs = _build_cost_breakdown(result, network, config, cost_basis)

    # --- Facilities ---
    facilities: List[FacilitySummary] = []
    open_ids: List[str] = []
    closed_ids: List[str] = []
    for fd in result.facility_decisions:
        fac = fac_map.get(fd.facility_id)
        charged = 0.0
        if fac is not None and not fd.is_open:
            closure_active = bool(config.enable_closure_cost and policy.apply_closure_cost)
            if closure_active and fac.closure_cost_applies(is_open=False):
                charged = fac.closure_cost

        facilities.append(FacilitySummary(
            facility_id          = fd.facility_id,
            facility_name        = fd.facility_name,
            role                 = str(fd.role),
            is_open              = fd.is_open,
            throughput_units     = round(fd.throughput_units, 4),
            capacity_units       = round(fd.capacity_units, 4),
            utilization_pct      = round(fd.utilization_pct, 4),
            baseline_status      = fac.effective_baseline_status.value if fac else None,
            contract_status      = fac.contract_status.value if fac else "NONE",
            closure_cost_charged = round(charged, 4),
        ))
        (open_ids if fd.is_open else closed_ids).append(fd.facility_id)

    # --- Flows, aggregated across mode and product ---
    agg: Dict[tuple, Dict[str, float]] = defaultdict(
        lambda: {"flow": 0.0, "cost": 0.0, "carbon": 0.0, "distance": 0.0}
    )
    for fl in result.flow_decisions:
        if fl.flow_units <= 1e-6:
            continue
        entry = agg[(fl.origin_id, fl.destination_id)]
        entry["flow"]     += fl.flow_units
        entry["cost"]     += fl.transport_cost
        entry["carbon"]   += fl.carbon_kg
        entry["distance"] = fl.distance_km

    flows = [
        FlowSummary(
            origin_id      = o,
            destination_id = d,
            flow_units     = round(v["flow"], 4),
            transport_cost = round(v["cost"], 4),
            distance_km    = round(v["distance"], 4),
            carbon_kg      = round(v["carbon"], 6),
        )
        for (o, d), v in sorted(agg.items())
    ]

    demand = DemandSummary(
        total_demand     = round(kpis.total_demand, 4) if kpis else 0.0,
        served_demand    = round(kpis.total_served, 4) if kpis else 0.0,
        unserved_demand  = round(kpis.unmet_demand, 4) if kpis else 0.0,
        demand_fill_rate = round(kpis.demand_fill_rate, 6) if kpis else 0.0,
    )

    analytics = result.flow_analytics
    metadata = ModelMetadata(
        run_id           = result.run_id,
        model_version    = config.model_version,
        solver_name      = result.solver.solver_name,
        solver_status    = result.solver.status,
        optimality_label = result.solver.optimality_label or result.solver.get_optimality_label(),
        mip_gap          = result.solver.mip_gap,
        runtime_seconds  = result.solver.runtime_seconds,
        n_variables      = result.solver.n_variables,
        n_constraints    = result.solver.n_constraints,
        generated_at     = result.solver.timestamp or datetime.now().isoformat(),
        warnings         = list(result.solver.warnings),
    )

    return NetworkStateResult(
        network_id        = result.network_id,
        data_version      = result.data_version,
        optimization_mode = result.optimization_mode,
        mode_description  = policy.description,
        is_hypothetical   = result.is_hypothetical,
        result_type       = result.result_type,
        solver_status     = result.solver.status,
        is_feasible       = result.is_solved,
        costs             = costs,
        demand            = demand,
        service           = result.service_report,
        open_facilities   = sorted(open_ids),
        closed_facilities = sorted(closed_ids),
        facilities        = facilities,
        flows             = flows,
        avg_utilization_pct = round(kpis.avg_utilization_pct, 4) if kpis else 0.0,
        max_utilization_pct = round(kpis.max_utilization_pct, 4) if kpis else 0.0,
        overutilized_facilities  = list(analytics.overutilized_facilities) if analytics else [],
        underutilized_facilities = list(analytics.underutilized_facilities) if analytics else [],
        total_carbon_kg   = round(kpis.total_carbon_kg, 6) if kpis else 0.0,
        metadata          = metadata,
    )


def build_scenario_result(
    result:            OptimizationResult,
    network:           CanonicalNetwork,
    scenario_id:       str,
    scenario_name:     str = "",
    scenario_type:     str = "CUSTOM",
    baseline_state:    Optional[NetworkStateResult] = None,
    scenario_overrides: Optional[List[str]] = None,
    config:            Optional[OptimizationConfig] = None,
    cost_basis:        Optional[ResilienceCostBasis] = None,
) -> ScenarioResult:
    """
    Build the frozen ScenarioResult contract.

    `baseline_state` is used ONLY to compute deltas and to record the baseline's
    snapshot identity. It is never modified — observed baseline state stays
    single-sourced.

    Args:
        result:             Scenario optimization result.
        network:            The SCENARIO network (post-override).
        scenario_id/name/type: Scenario identity.
        baseline_state:     Optional baseline contract for delta computation.
        scenario_overrides: Human-readable list of what the scenario changed.
        config:             Config used for the run.
        cost_basis:         Which components constitute business cost.

    Returns:
        ScenarioResult, always flagged hypothetical.
    """
    state = build_network_state_result(result, network, config=config, cost_basis=cost_basis)

    overrides = list(scenario_overrides) if scenario_overrides else []
    manifest: Dict[str, Any] = dict(result.scenario_audit_metadata or {})

    sr = ScenarioResult(
        scenario_id        = scenario_id,
        scenario_name      = scenario_name or scenario_id,
        scenario_type      = scenario_type,
        is_hypothetical    = True,
        state              = state,
        scenario_overrides = overrides,
        change_manifest    = manifest,
    )

    if baseline_state is not None:
        sr.baseline_network_id    = baseline_state.network_id
        sr.baseline_data_version  = baseline_state.data_version
        base_cost = baseline_state.costs.business_network_cost
        sr.baseline_business_cost = base_cost

        if result.is_solved:
            delta = round(state.costs.business_network_cost - base_cost, 4)
            sr.business_cost_delta = delta
            sr.business_cost_delta_pct = (
                round(delta / base_cost * 100.0, 6) if base_cost > 0 else None
            )
            sr.served_demand_delta = round(
                state.demand.served_demand - baseline_state.demand.served_demand, 4
            )
            sr.carbon_delta_kg = round(
                state.total_carbon_kg - baseline_state.total_carbon_kg, 6
            )

    return sr
