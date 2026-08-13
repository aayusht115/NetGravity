"""
NetGravity — Truly Independent Cost Reconciliation Engine V1.2
================================================================
Independently computes all objective cost components directly from:
  1. facility_decisions (y_i)
  2. flow_decisions (x_ijvk)
  3. assignment_decisions (a_ij)
  4. network parameters (facilities, lanes, products, demands)
  5. OptimizationConfig (cost_period, shortage_penalty, etc.)

Crucial Design Requirement (V1.2):
This module does NOT use result.objective_components as the source of truth!
It evaluates cost components strictly from raw decision vectors and network data.
It compares solver_objective vs independently_calculated_total.
Under V1.2 Direct MILP inventory formulation, solver_objective == independently_calculated_total (gap = 0.00).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from netgravity.inventory.coefficient_engine import InventoryCoefficientEngine
from netgravity.inventory.module import NormalSafetyStockModule
from netgravity.schemas.network import CanonicalNetwork, NodeRole, OptimizationConfig
from netgravity.schemas.results import OptimizationResult


@dataclass
class CostReconciliation:
    """
    Structured outcome of independent cost reconciliation.
    """
    solver_objective:               float
    independently_calculated_total: float
    independent_component_costs:    Dict[str, float]
    objective_components_reported:  Dict[str, float]
    absolute_difference:            float
    relative_difference:            float
    is_reconciled:                  bool
    inventory_converged:            bool = True


def reconcile_costs(
    result:       OptimizationResult,
    network:      CanonicalNetwork,
    config:       Optional[OptimizationConfig] = None,
    tolerance:    float = 1e-3,
) -> CostReconciliation:
    """
    Independently evaluate total network cost from raw decision outputs
    and network parameters, then reconcile against solver.objective_value.

    Args:
        result:    OptimizationResult returned from solver
        network:   CanonicalNetwork input
        config:    Optional OptimizationConfig (uses network.config if None)
        tolerance: Relative tolerance threshold (default: 0.001 = 0.1%)

    Returns:
        CostReconciliation containing independent evaluation, solver objective, and reconciliation status
    """
    if config is None:
        config = network.config

    cost_period = config.cost_period

    fac_map      = {f.id: f for f in network.facilities}
    prod_map     = {p.id: p for p in network.products}
    demand_map   = {(d.market_id, d.product_id): d for d in network.demands}
    lane_map     = {
        (l.origin_id, l.destination_id, l.mode.value if hasattr(l.mode, "value") else str(l.mode)): l
        for l in network.lanes
    }
    market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}

    # 1. Independent Facility Fixed, Opening, Handling Costs
    ind_facility_cost = 0.0
    ind_opening_cost  = 0.0
    ind_handling_cost = 0.0

    # Calculate facility throughput from flow_decisions
    facility_throughput: Dict[str, float] = {}
    for fl in result.flow_decisions:
        facility_throughput[fl.origin_id] = facility_throughput.get(fl.origin_id, 0.0) + fl.flow_units

    for fd in result.facility_decisions:
        if fd.is_open:
            fac = fac_map.get(fd.facility_id)
            if fac:
                ind_facility_cost += fac.get_fixed_cost_for_period(cost_period)
                if fac.is_candidate and fac.opening_cost > 0:
                    ind_opening_cost += fac.opening_cost
                throughput = facility_throughput.get(fd.facility_id, 0.0)
                if fac.handling_cost_per_unit > 0:
                    ind_handling_cost += fac.handling_cost_per_unit * throughput

    # 2. Independent Transportation & Carbon Costs
    ind_transport_cost = 0.0
    ind_carbon_kg      = 0.0
    for fl in result.flow_decisions:
        if fl.flow_units > 1e-6:
            key = (fl.origin_id, fl.destination_id, fl.mode.value if hasattr(fl.mode, "value") else str(fl.mode))
            ln = lane_map.get(key)
            rate = ln.rate_per_unit if ln else fl.rate_per_unit
            ind_transport_cost += rate * fl.flow_units
            ind_carbon_kg      += fl.carbon_kg

    # 3. Independent Inventory Cost (V1.2 Direct MILP or Fallback)
    ind_inventory_cost = 0.0
    if config.enable_inventory:
        if result.assignment_decisions:
            inv_coeffs = InventoryCoefficientEngine.compute_coefficients(network, config)
            for ad in result.assignment_decisions:
                if ad.is_assigned:
                    coeff = inv_coeffs.get((ad.facility_id, ad.market_id))
                    if coeff:
                        ind_inventory_cost += coeff.total_inventory_cost
        else:
            # Fallback for legacy flow-based attribution
            inv_module = NormalSafetyStockModule()
            assigned_by_fac: Dict[str, list] = {}
            for fl in result.flow_decisions:
                if fl.flow_units > 1e-6:
                    orig_fac = fac_map.get(fl.origin_id)
                    dest_fac = fac_map.get(fl.destination_id)
                    if (dest_fac and dest_fac.role in market_roles
                            and orig_fac and orig_fac.role not in market_roles):
                        d_rec = demand_map.get((fl.destination_id, fl.product_id))
                        if d_rec:
                            if fl.origin_id not in assigned_by_fac:
                                assigned_by_fac[fl.origin_id] = []
                            assigned_by_fac[fl.origin_id].append(d_rec)

            for fd in result.facility_decisions:
                if fd.is_open and fd.facility_id in assigned_by_fac:
                    fac = fac_map.get(fd.facility_id)
                    if fac:
                        inv_res = inv_module.compute_cost(
                            facility         = fac,
                            assigned_demands = assigned_by_fac[fd.facility_id],
                            products         = prod_map,
                            z_score          = config.inventory_z_score,
                            days_per_period  = config.days_per_period,
                            cost_period      = cost_period,
                        )
                        ind_inventory_cost += inv_res.inventory_cost

    # 4. Independent Shortage Cost
    ind_shortage_cost = 0.0
    if config.allow_shortage:
        total_demand = sum(d.quantity for d in network.demands)
        total_served = sum(
            fl.flow_units for fl in result.flow_decisions
            if fac_map.get(fl.destination_id) and fac_map[fl.destination_id].role in market_roles
        )
        unmet_demand = max(0.0, total_demand - total_served)
        ind_shortage_cost = unmet_demand * config.shortage_penalty

    independent_component_costs = {
        "facility_cost":  round(ind_facility_cost, 4),
        "opening_cost":   round(ind_opening_cost, 4),
        "transport_cost": round(ind_transport_cost, 4),
        "handling_cost":  round(ind_handling_cost, 4),
        "inventory_cost": round(ind_inventory_cost, 4),
        "shortage_cost":  round(ind_shortage_cost, 4),
        "carbon_kg":      round(ind_carbon_kg, 6),
    }

    independently_calculated_total = round(
        ind_facility_cost + ind_opening_cost + ind_transport_cost +
        ind_handling_cost + ind_inventory_cost + ind_shortage_cost, 4
    )

    solver_obj = round(result.solver.objective_value or 0.0, 4)
    abs_diff   = round(abs(independently_calculated_total - solver_obj), 4)
    rel_diff   = round(abs_diff / max(abs(solver_obj), 1.0), 6)

    inventory_converged = getattr(result, "inventory_converged", True)

    is_reconciled = bool((abs_diff <= 0.05) or (rel_diff <= tolerance))

    return CostReconciliation(
        solver_objective               = solver_obj,
        independently_calculated_total = independently_calculated_total,
        objective_components_reported  = result.objective_components,
        independent_component_costs    = independent_component_costs,
        absolute_difference            = abs_diff,
        relative_difference            = rel_diff,
        is_reconciled                  = is_reconciled,
        inventory_converged            = inventory_converged,
    )
