"""
NetGravity V1.2 — Inventory Coefficient Engine
==============================================
Deterministic precomputation of facility-market safety stock and inventory cost
coefficients before MILP construction.

Formulation (V1.2 Direct MILP):
───────────────────────────────
For each feasible facility/DC i, market/customer j, and product p:

    SS[i,j,p] = z[j,p] * sigma[j,p] * sqrt( LT[i,j] / DaysPerPlanningPeriod )

    IC[i,j,p] = SS[i,j,p] * unit_value[j,p] * holding_rate_period[j,p]

Total coefficient per facility-market pair (i,j):

    IC[i,j] = sum_p IC[i,j,p]

Units & Period Normalization:
─────────────────────────────
- holding_rate is normalized to config.cost_period (e.g. annual_rate / 12 for MONTH).
- DaysPerPlanningPeriod converts lead time in days to the planning period unit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    NodeRole,
    OptimizationConfig,
)


@dataclass
class FacilityMarketInventoryCoeff:
    """Deterministic precomputed inventory coefficients for pair (facility_id, market_id)."""
    facility_id: str
    market_id: str
    total_safety_stock_units: float
    total_inventory_cost: float
    product_breakdown: Dict[str, Tuple[float, float]]  # prod_id -> (SS_units, IC_cost)
    total_cycle_stock_units: float = 0.0
    total_cycle_stock_cost: float = 0.0
    unit_inv_cost_by_product: Dict[str, float] = None  # prod_id -> unit_cost_per_flow


class InventoryCoefficientEngine:
    """
    Engine for precomputing facility-market safety stock and inventory cost coefficients
    for direct linear integration into the MILP objective.
    """

    @staticmethod
    def compute_coefficients(
        network: CanonicalNetwork,
        config: Optional[OptimizationConfig] = None,
    ) -> Dict[Tuple[str, str], FacilityMarketInventoryCoeff]:
        """
        Compute fixed inventory cost coefficients IC[i,j] for all facility-market pairs.

        Args:
            network: Input CanonicalNetwork
            config: Optional OptimizationConfig (uses network.config if None)

        Returns:
            Dict mapping (facility_id, market_id) -> FacilityMarketInventoryCoeff
        """
        if config is None:
            config = network.config

        if not config.enable_inventory:
            return {}

        cost_period = config.cost_period
        days_per_period = float(config.days_per_period)
        z_score = config.inventory_z_score
        include_cycle = getattr(config, "include_cycle_stock", True)

        market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
        non_market_facs = [f for f in network.facilities if f.role not in market_roles]
        markets = [f for f in network.facilities if f.role in market_roles]

        prod_map = {p.id: p for p in network.products}
        demand_map = {(d.market_id, d.product_id): d for d in network.demands}

        # Build lane lookup for lead times: (origin_id, dest_id) -> min_lead_time
        lane_lead_times: Dict[Tuple[str, str], float] = {}
        for l in network.lanes:
            key = (l.origin_id, l.destination_id)
            if key not in lane_lead_times or l.lead_time_days < lane_lead_times[key]:
                lane_lead_times[key] = l.lead_time_days

        coeffs: Dict[Tuple[str, str], FacilityMarketInventoryCoeff] = {}

        for fac in non_market_facs:
            # Facility replenishment lead time baseline
            fac_lt = fac.replenishment_lead_time_days or 0.0

            for mkt in markets:
                lane_lt = lane_lead_times.get((fac.id, mkt.id), 0.0)
                total_lt = fac_lt + lane_lt

                if total_lt <= 0.0 or days_per_period <= 0.0:
                    lt_factor = 0.0
                    lt_ratio = 0.0
                else:
                    lt_factor = math.sqrt(total_lt / days_per_period)
                    lt_ratio = total_lt / days_per_period

                total_ss = 0.0
                total_cycle_units = 0.0
                total_cycle_cost = 0.0
                total_ic = 0.0
                prod_breakdown: Dict[str, Tuple[float, float]] = {}
                unit_inv_cost_by_product: Dict[str, float] = {}

                for prod in network.products:
                    d_rec = demand_map.get((mkt.id, prod.id))
                    if not d_rec or d_rec.quantity <= 0.0:
                        unit_inv_cost_by_product[prod.id] = 0.0
                        continue

                    d_qty = d_rec.quantity
                    sigma = d_rec.std_dev or 0.0
                    holding_rate_period = prod.get_holding_rate_for_period(cost_period)

                    # Safety stock
                    if sigma > 0.0 and lt_factor > 0.0:
                        ss_units = z_score * sigma * lt_factor
                        ss_cost = ss_units * prod.unit_value * holding_rate_period
                    else:
                        ss_units = 0.0
                        ss_cost = 0.0

                    # Cycle stock (Q/2 on replenishment cycle)
                    if include_cycle and lt_ratio > 0.0:
                        cyc_units = 0.5 * d_qty * lt_ratio
                        cyc_cost = cyc_units * prod.unit_value * holding_rate_period
                    else:
                        cyc_units = 0.0
                        cyc_cost = 0.0

                    ic_cost = ss_cost + cyc_cost
                    unit_inv_cost = ic_cost / d_qty if d_qty > 0 else 0.0

                    total_ss += ss_units
                    total_cycle_units += cyc_units
                    total_cycle_cost += cyc_cost
                    total_ic += ic_cost
                    prod_breakdown[prod.id] = (round(ss_units + cyc_units, 4), round(ic_cost, 4))
                    unit_inv_cost_by_product[prod.id] = unit_inv_cost

                coeffs[(fac.id, mkt.id)] = FacilityMarketInventoryCoeff(
                    facility_id=fac.id,
                    market_id=mkt.id,
                    total_safety_stock_units=round(total_ss, 4),
                    total_inventory_cost=round(total_ic, 4),
                    total_cycle_stock_units=round(total_cycle_units, 4),
                    total_cycle_stock_cost=round(total_cycle_cost, 4),
                    product_breakdown=prod_breakdown,
                    unit_inv_cost_by_product=unit_inv_cost_by_product,
                )

        return coeffs
