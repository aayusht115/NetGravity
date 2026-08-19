"""
NetGravity — KPI Calculator
==============================
Derives all structured KPIs from an OptimizationResult.

Every KPI has a mathematically defined formula (see docs/mathematical_model.md §8).
No heuristic scores. No arbitrary formulas.

Outputs:
  - NetworkKPIs       (cost breakdown, utilization, service, carbon, distance)
  - FlowAnalytics     (top corridors, hotspots, over/under-utilized nodes)
  - GoNoGoEvidence    (structured evidence for decision support)
  - ScenarioComparison (baseline vs scenario deltas)
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from netgravity.config.defaults import ANALYTICS_DEFAULTS, UTILIZATION_THRESHOLDS, GO_NO_GO_DEFAULTS
from netgravity.schemas.network import CanonicalNetwork, NodeRole
from netgravity.schemas.results import (
    CorridorInfo,
    FacilityDecision,
    FlowAnalytics,
    FlowDecision,
    GoNoGoEvidence,
    NetworkKPIs,
    OptimizationResult,
    ScenarioComparison,
    ScenarioDelta,
    SolverStatus,
)


# ---------------------------------------------------------------------------
# Primary KPI computation
# ---------------------------------------------------------------------------

def compute_kpis(
    result:  OptimizationResult,
    network: CanonicalNetwork,
) -> NetworkKPIs:
    """
    Derive all NetworkKPIs from an OptimizationResult.

    Args:
        result:  The optimization result
        network: The canonical network (for demand totals, facility metadata)

    Returns:
        NetworkKPIs — fully populated
    """
    if not result.is_solved:
        # Return zeroed KPIs for infeasible/error results
        return NetworkKPIs(
            total_cost=0, facility_cost=0, transport_cost=0,
            handling_cost=0, inventory_cost=0, shortage_cost=0,
            total_demand=0, total_served=0, unmet_demand=0,
            demand_fill_rate=0, n_facilities_open=0, n_facilities_closed=0,
            avg_distance_km=0, max_distance_km=0, min_distance_km=0,
            pct_demand_in_sla=0, avg_utilization_pct=0,
            max_utilization_pct=0, min_utilization_pct=0,
            overutilized_count=0, underutilized_count=0,
            total_carbon_kg=0, carbon_per_unit=0,
        )

    obj_comp = result.objective_components

    # --- Cost components ---
    facility_cost  = obj_comp.get("facility_cost",  0.0)
    transport_cost = obj_comp.get("transport_cost", 0.0)
    handling_cost  = obj_comp.get("handling_cost",  0.0)
    inventory_cost = obj_comp.get("inventory_cost", 0.0)
    shortage_cost  = obj_comp.get("shortage_cost",  0.0)
    carbon_cost    = obj_comp.get("carbon_cost",    0.0)

    cfg = network.config
    if carbon_cost == 0.0 and cfg:
        tot_c = sum(fl.carbon_kg for fl in result.flow_decisions)
        # Additive, matching the actual solver objective terms (see optimization/milp.py)
        if cfg.enable_carbon_cost:
            carbon_cost += tot_c * cfg.carbon_price
        if (hasattr(cfg, "objective_mode") and
                (cfg.objective_mode.value if hasattr(cfg.objective_mode, "value") else str(cfg.objective_mode)) == "WEIGHTED_COST_CARBON"):
            carbon_cost += tot_c * cfg.carbon_weight

    total_cost     = (
        facility_cost + transport_cost + handling_cost +
        inventory_cost + shortage_cost + carbon_cost
    )

    # --- Demand ---
    total_demand = sum(d.quantity for d in network.demands)
    total_served = sum(fd.flow_units for fd in result.flow_decisions
                      if _is_market_dest(fd, network))
    unmet_demand = max(0.0, total_demand - total_served)
    fill_rate    = total_served / total_demand if total_demand > 0 else 1.0

    # --- Facilities ---
    open_fds  = [fd for fd in result.facility_decisions if fd.is_open]
    close_fds = [fd for fd in result.facility_decisions if not fd.is_open]

    # --- Distance Metrics ---
    market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
    plant_roles  = {NodeRole.PLANT, NodeRole.SUPPLIER}

    all_positive_flows = [fl for fl in result.flow_decisions if fl.flow_units > 1e-6]
    total_network_flow = sum(fl.flow_units for fl in all_positive_flows)

    if total_network_flow > 0:
        weighted_avg_dist = sum(fl.flow_units * fl.distance_km for fl in all_positive_flows) / total_network_flow
    else:
        weighted_avg_dist = 0.0

    # Inbound distance (Plant/Supplier -> DC/Warehouse/Depot)
    fac_map = {f.id: f for f in network.facilities}
    inbound_flows = [
        fl for fl in all_positive_flows
        if fac_map.get(fl.origin_id) and fac_map[fl.origin_id].role in plant_roles
        and fac_map.get(fl.destination_id) and fac_map[fl.destination_id].role not in market_roles
    ]
    inbound_flow_sum = sum(fl.flow_units for fl in inbound_flows)
    inbound_avg_dist = (
        sum(fl.flow_units * fl.distance_km for fl in inbound_flows) / inbound_flow_sum
        if inbound_flow_sum > 0 else 0.0
    )

    # Outbound distance (DC/Warehouse/Depot -> Market/Customer)
    outbound_flows = [
        fl for fl in all_positive_flows
        if fac_map.get(fl.destination_id) and fac_map[fl.destination_id].role in market_roles
    ]
    outbound_flow_sum = sum(fl.flow_units for fl in outbound_flows)
    outbound_avg_dist = (
        sum(fl.flow_units * fl.distance_km for fl in outbound_flows) / outbound_flow_sum
        if outbound_flow_sum > 0 else 0.0
    )

    distances = [fl.distance_km for fl in outbound_flows] if outbound_flows else [fl.distance_km for fl in all_positive_flows]
    max_dist  = max(distances) if distances else 0.0
    min_dist  = min(distances) if distances else 0.0
    avg_dist  = outbound_avg_dist if outbound_flow_sum > 0 else weighted_avg_dist

    # --- Utilization ---
    util_vals = [fd.utilization_pct for fd in open_fds if fd.capacity_units > 0]
    avg_util  = sum(util_vals) / len(util_vals) if util_vals else 0.0
    max_util  = max(util_vals) if util_vals else 0.0
    min_util  = min(util_vals) if util_vals else 0.0
    over_threshold  = UTILIZATION_THRESHOLDS["over_threshold"]  * 100
    under_threshold = UTILIZATION_THRESHOLDS["under_threshold"] * 100
    overutil_count  = sum(1 for u in util_vals if u >= over_threshold)
    underutil_count = sum(1 for u in util_vals if u <= under_threshold)

    # --- Service: % demand in SLA ---
    demand_map = {(d.market_id, d.product_id): d for d in network.demands}
    served_in_sla = 0.0
    for fl in outbound_flows:
        key_d = (fl.destination_id, fl.product_id)
        d_rec = demand_map.get(key_d)
        if d_rec is None or d_rec.sla_days is None or fl.lead_time_days <= d_rec.sla_days:
            served_in_sla += fl.flow_units

    pct_in_sla = (served_in_sla / total_demand * 100) if total_demand > 0 else 100.0

    # --- Carbon ---
    total_carbon = sum(fl.carbon_kg for fl in result.flow_decisions)
    carbon_per_unit = total_carbon / total_served if total_served > 0 else 0.0

    return NetworkKPIs(
        total_cost               = round(total_cost, 2),
        facility_cost            = round(facility_cost, 2),
        transport_cost           = round(transport_cost, 2),
        handling_cost            = round(handling_cost, 2),
        inventory_cost           = round(inventory_cost, 2),
        shortage_cost            = round(shortage_cost, 2),
        total_demand             = round(total_demand, 2),
        total_served             = round(total_served, 2),
        unmet_demand             = round(unmet_demand, 2),
        demand_fill_rate         = round(fill_rate, 6),
        n_facilities_open        = len(open_fds),
        n_facilities_closed      = len(close_fds),
        avg_distance_km          = round(avg_dist, 2),
        weighted_avg_distance_km = round(weighted_avg_dist, 2),
        inbound_avg_distance_km  = round(inbound_avg_dist, 2),
        outbound_avg_distance_km = round(outbound_avg_dist, 2),
        max_distance_km          = round(max_dist, 2),
        min_distance_km          = round(min_dist, 2),
        pct_demand_in_sla        = round(pct_in_sla, 2),
        avg_utilization_pct      = round(avg_util, 2),
        max_utilization_pct      = round(max_util, 2),
        min_utilization_pct      = round(min_util, 2),
        overutilized_count       = overutil_count,
        underutilized_count      = underutil_count,
        total_carbon_kg          = round(total_carbon, 4),
        carbon_per_unit          = round(carbon_per_unit, 6),
    )


# ---------------------------------------------------------------------------
# Flow analytics
# ---------------------------------------------------------------------------

def compute_flow_analytics(
    result:  OptimizationResult,
    network: CanonicalNetwork,
    top_n:   int = 10,
) -> FlowAnalytics:
    """
    Compute flow-pattern analytics for dashboard hotspot detection.

    Args:
        result:  Optimization result
        network: Canonical network
        top_n:   Number of top corridors to return

    Returns:
        FlowAnalytics
    """
    if not result.is_solved or not result.flow_decisions:
        return FlowAnalytics()

    market_ids = {f.id for f in network.facilities if f.role == NodeRole.MARKET}
    over_thresh  = UTILIZATION_THRESHOLDS["over_threshold"]  * 100
    under_thresh = UTILIZATION_THRESHOLDS["under_threshold"] * 100

    # Aggregate by corridor (origin, destination, mode)
    corridor_map: Dict[Tuple[str, str, str], Dict] = defaultdict(
        lambda: {"total_flow": 0.0, "total_cost": 0.0, "distance_km": 0.0, "carbon_kg": 0.0}
    )
    for fl in result.flow_decisions:
        key = (fl.origin_id, fl.destination_id, fl.mode)
        corridor_map[key]["total_flow"]  += fl.flow_units
        corridor_map[key]["total_cost"]  += fl.transport_cost
        corridor_map[key]["distance_km"]  = fl.distance_km
        corridor_map[key]["carbon_kg"]   += fl.carbon_kg

    corridors = [
        CorridorInfo(
            origin_id      = k[0],
            destination_id = k[1],
            mode           = k[2],
            total_flow     = round(v["total_flow"], 2),
            total_cost     = round(v["total_cost"], 2),
            distance_km    = round(v["distance_km"], 2),
            carbon_kg      = round(v["carbon_kg"], 4),
        )
        for k, v in corridor_map.items()
        if v["total_flow"] > ANALYTICS_DEFAULTS["min_flow_display"]
    ]

    by_volume  = sorted(corridors, key=lambda c: c.total_flow, reverse=True)[:top_n]
    by_cost    = sorted(corridors, key=lambda c: c.total_cost, reverse=True)[:top_n]
    by_carbon  = sorted(corridors, key=lambda c: c.carbon_kg,  reverse=True)[:top_n]

    # Longest last-mile flows
    last_mile = [fl for fl in result.flow_decisions if fl.destination_id in market_ids]
    longest   = sorted(last_mile, key=lambda fl: fl.distance_km, reverse=True)[:top_n]

    # Utilization alerts
    overutil  = [fd.facility_id for fd in result.facility_decisions
                 if fd.is_open and fd.utilization_pct >= over_thresh]
    underutil = [fd.facility_id for fd in result.facility_decisions
                 if fd.is_open and fd.capacity_units > 0 and fd.utilization_pct <= under_thresh]

    return FlowAnalytics(
        top_corridors_by_volume  = by_volume,
        top_corridors_by_cost    = by_cost,
        longest_distance_flows   = longest,
        overutilized_facilities  = overutil,
        underutilized_facilities = underutil,
        cost_hotspots            = by_cost[:5],
        high_carbon_corridors    = by_carbon[:5],
    )


# ---------------------------------------------------------------------------
# Scenario comparison
# ---------------------------------------------------------------------------

def compare_scenarios(
    baseline:  OptimizationResult,
    scenario:  OptimizationResult,
    scenario_name: str = "Scenario",
) -> ScenarioComparison:
    """
    Compute absolute and percentage KPI deltas between baseline and scenario.

    Args:
        baseline:      Baseline optimization result
        scenario:      Scenario optimization result
        scenario_name: Human-readable scenario name

    Returns:
        ScenarioComparison with delta list and go/no-go evidence
    """
    def _delta(baseline_val: float, scen_val: float, metric: str) -> ScenarioDelta:
        abs_d = scen_val - baseline_val
        pct_d = (abs_d / baseline_val * 100) if abs_d != 0 and baseline_val != 0 else 0.0
        return ScenarioDelta(
            metric    = metric,
            baseline  = round(baseline_val, 4),
            scenario  = round(scen_val, 4),
            abs_delta = round(abs_d, 4),
            pct_delta = round(pct_d, 2),
        )

    base_comp = baseline.objective_components
    scen_comp = scenario.objective_components

    deltas = [
        _delta(base_comp.get("facility_cost",  0), scen_comp.get("facility_cost",  0), "facility_cost"),
        _delta(base_comp.get("transport_cost", 0), scen_comp.get("transport_cost", 0), "transport_cost"),
        _delta(base_comp.get("handling_cost",  0), scen_comp.get("handling_cost",  0), "handling_cost"),
        _delta(base_comp.get("inventory_cost", 0), scen_comp.get("inventory_cost", 0), "inventory_cost"),
        _delta(base_comp.get("shortage_cost",  0), scen_comp.get("shortage_cost",  0), "shortage_cost"),
        _delta(base_comp.get("carbon_kg",      0), scen_comp.get("carbon_kg",      0), "carbon_kg"),
        _delta(
            sum(base_comp.get(k, 0) for k in ("facility_cost", "transport_cost", "handling_cost", "inventory_cost", "shortage_cost")),
            sum(scen_comp.get(k, 0) for k in ("facility_cost", "transport_cost", "handling_cost", "inventory_cost", "shortage_cost")),
            "total_cost",
        ),
        _delta(
            len(baseline.get_open_facilities()),
            len(scenario.get_open_facilities()),
            "n_facilities_open",
        ),
    ]

    # Facility changes: which opened/closed
    base_open = {fd.facility_id for fd in baseline.facility_decisions if fd.is_open}
    scen_open = {fd.facility_id for fd in scenario.facility_decisions if fd.is_open}
    newly_opened = list(scen_open - base_open)
    newly_closed = list(base_open - scen_open)

    facility_changes = [
        {"facility_id": fid, "change": "OPENED"} for fid in newly_opened
    ] + [
        {"facility_id": fid, "change": "CLOSED"} for fid in newly_closed
    ]

    # Go/No-Go evidence
    total_base_cost = sum(base_comp.get(k, 0) for k in
                         ("facility_cost", "transport_cost", "handling_cost", "inventory_cost"))
    total_scen_cost = sum(scen_comp.get(k, 0) for k in
                         ("facility_cost", "transport_cost", "handling_cost", "inventory_cost"))
    annual_savings  = total_base_cost - total_scen_cost

    # Fraction change in demand served (same definition used by resilience/engine.py),
    # so a scenario that degrades service can actually fail the Go/No-Go check.
    base_kpis = baseline.kpis
    scen_kpis = scenario.kpis
    if base_kpis and scen_kpis and base_kpis.demand_fill_rate > 0:
        service_delta = scen_kpis.demand_fill_rate - base_kpis.demand_fill_rate
    else:
        service_delta = 0.0

    gng = _build_go_no_go(
        scenario_id      = scenario.scenario_id or "unknown",
        baseline_id      = baseline.scenario_id or "BASELINE",
        annual_savings   = annual_savings,
        is_feasible      = scenario.is_solved,
        carbon_delta     = scen_comp.get("carbon_kg", 0) - base_comp.get("carbon_kg", 0),
        service_delta    = service_delta,
    )

    return ScenarioComparison(
        baseline_id      = baseline.scenario_id or "BASELINE",
        scenario_id      = scenario.scenario_id or "unknown",
        scenario_name    = scenario_name,
        kpi_deltas       = deltas,
        facility_changes = facility_changes,
        go_no_go         = gng,
    )


# ---------------------------------------------------------------------------
# Go/No-Go evidence builder
# ---------------------------------------------------------------------------

def _build_go_no_go(
    scenario_id:    str,
    baseline_id:    str,
    annual_savings: float,
    is_feasible:    bool,
    carbon_delta:   float = 0.0,
    service_delta:  float = 0.0,
    thresholds:     Optional[Dict] = None,
) -> GoNoGoEvidence:
    """
    Build structured go/no-go evidence from measured metrics.

    Decision rules (all configurable via thresholds):
        GO:    savings >= threshold AND service_delta >= -0.02 AND feasible
        NO-GO: infeasible OR service below threshold
        MARGINAL: borderline
    """
    if thresholds is None:
        thresholds = GO_NO_GO_DEFAULTS

    savings_ok = annual_savings >= thresholds.get("savings_threshold", 0)
    service_ok = service_delta >= thresholds.get("service_delta_threshold", -0.02)

    if not is_feasible:
        gng, rationale = "NO-GO", "Network is infeasible under this scenario."
    elif not service_ok:
        gng, rationale = "NO-GO", f"Service level drops by {service_delta:.1%}, below threshold."
    elif savings_ok:
        gng, rationale = "GO", f"Annual savings of {annual_savings:,.0f} with acceptable service."
    else:
        gng, rationale = "MARGINAL", f"Savings ({annual_savings:,.0f}) below threshold or insufficient evidence."

    return GoNoGoEvidence(
        scenario_id         = scenario_id,
        baseline_id         = baseline_id,
        annual_savings      = round(annual_savings, 2),
        is_feasible         = is_feasible,
        carbon_delta_kg     = round(carbon_delta, 4),
        service_delta_pct   = round(service_delta * 100, 2),
        go_no_go            = gng,
        go_no_go_rationale  = rationale,
    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _is_market_dest(flow: FlowDecision, network: CanonicalNetwork) -> bool:
    """True if flow's destination is a MARKET node."""
    for f in network.facilities:
        if f.id == flow.destination_id and f.role == NodeRole.MARKET:
            return True
    return False
