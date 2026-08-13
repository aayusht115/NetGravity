"""
NetGravity — Inventory Module (DEPRECATED LEGACY MODULE)
===========================================================
DEPRECATION NOTICE:
This module contains legacy V1.0 iteration-based inventory calculations.
NetGravity V1.2 canonical optimization and cost reconciliation MUST use
InventoryCoefficientEngine in netgravity/inventory/coefficient_engine.py.

V1.1 FORMULA (unit-corrected):
    Safety Stock: SS_i = z * sigma_daily * sqrt(LT_i)

    where:
        sigma_{daily,i} = sigma_{period,i} / sqrt(days_per_period)
        LT_i = facility replenishment lead time in DAYS

    Simplification:
        SS_i = z * sigma_{period,i} * sqrt(LT_i / days_per_period)

    Inventory Cost:
        IC_i = (SS_i + CS_i) * r_h * p_bar

        r_h = annual holding cost rate (from ProductRecord.holding_rate)
        p_bar = weighted average unit value across assigned products

V1.1 ASSUMPTIONS:
  - Normal demand distribution (A-001)
  - Demand independence across markets/products (A-010)
  - Replenishment lead time is deterministic (A-011)

Grounded in: Chopra & Meindl, SCM 5th Ed., Chapter 12.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

from netgravity.schemas.network import DemandRecord, FacilityRecord, ProductRecord


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------

@dataclass
class InventoryCostResult:
    """Result of inventory cost calculation for one facility."""
    facility_id:          str
    cycle_stock_units:    float   # CS_i = μ_agg / 2 [units/period]
    safety_stock_units:   float   # SS_i = z × σ_daily × √LT_days [units/period]
    total_inventory:      float   # cycle_stock + safety_stock [units/period]
    inventory_cost:       float   # total_inventory × holding_rate × unit_value [currency/period]
    assumption_distribution: str = "NORMAL"
    days_per_period_used: int    = 30   # documented for auditability


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class InventoryModule(ABC):
    """
    Abstract interface for inventory cost computation.
    Must be replaceable without changing the optimizer.
    """

    @abstractmethod
    def compute_cost(
        self,
        facility:         FacilityRecord,
        assigned_demands: List[DemandRecord],
        products:         Dict[str, ProductRecord],
        z_score:          float = 1.645,
        days_per_period:  int   = 30,
        cost_period:      str   = "MONTH",
    ) -> InventoryCostResult:
        """
        Compute total inventory carrying cost for a facility,
        given the set of demands assigned to it.

        Args:
            facility:         The facility holding inventory
            assigned_demands: Demand records served from this facility
            products:         Product registry (for weight/value)
            z_score:          CSL-based z-score (default: 1.645 → 95% CSL)
            days_per_period:  Days in one planning period (default: 30)
                              Used to convert periodic σ to daily σ.
                              Must match the time unit of DemandRecord.quantity.

        Returns:
            InventoryCostResult
        """
        ...


# ---------------------------------------------------------------------------
# V1.1 — Unit-Corrected Normal Safety Stock Module
# ---------------------------------------------------------------------------

class NormalSafetyStockModule(InventoryModule):
    """
    V1.1 Inventory cost using unit-corrected Normal-distribution safety stock.

    KEY CORRECTION FROM V1.0:
    V1.0 used: SS = z × σ_monthly × √(LT_days)
    V1.1 uses: SS = z × σ_monthly × √(LT_days / days_per_period)

    This converts the periodic std_dev to a daily std_dev before applying
    the √LT scaling, ensuring dimensional consistency.

    Safety Stock:
        σ_{period,i} = √(Σ_{m∈M_i, k∈K} σ_{mk}²)  [independence assumption, A-010]
        σ_{daily,i}  = σ_{period,i} / √(days_per_period)
        SS_i         = z_α × σ_{daily,i} × √(LT_i)
                     = z_α × σ_{period,i} × √(LT_i / days_per_period)

    Cycle Stock:
        CS_i = μ_{agg,i} / 2  [order once per period, hold half on average]

    Inventory Cost:
        IC_i = (SS_i + CS_i) × r_h × p̄

    Assumptions: A-001 (Normal), A-007 (z-score), A-010 (independence),
                 A-011 (deterministic LT), A-012 (cycle stock approximation)
    """

    def compute_cost(
        self,
        facility:         FacilityRecord,
        assigned_demands: List[DemandRecord],
        products:         Dict[str, ProductRecord],
        z_score:          float = 1.645,
        days_per_period:  int   = 30,
        cost_period:      str   = "MONTH",
    ) -> InventoryCostResult:

        if not assigned_demands:
            return InventoryCostResult(
                facility_id          = facility.id,
                cycle_stock_units    = 0.0,
                safety_stock_units   = 0.0,
                total_inventory      = 0.0,
                inventory_cost       = 0.0,
                days_per_period_used = days_per_period,
            )

        # ── Aggregate mean demand at this facility ─────────────────────────
        # Unit: units/period
        mu_agg = sum(d.quantity for d in assigned_demands)

        # ── Aggregate demand variance (independence assumption A-010) ──────
        # Unit: (units/period)²
        var_agg   = sum(d.std_dev ** 2 for d in assigned_demands)
        # σ_period: units/period
        sigma_period = math.sqrt(var_agg) if var_agg > 0 else 0.0

        # ── Replenishment lead time ─────────────────────────────────────────
        # Unit: days
        lt_days = max(facility.replenishment_lead_time_days, 0.0)

        # ── Safety stock (V1.1 unit-corrected formula) ─────────────────────
        # Convert: LT_days / days_per_period = fraction of planning period
        # SS = z × σ_period × √(LT_days / days_per_period)
        # Unit: units/period (same as σ_period × dimensionless multiplier)
        if sigma_period > 0 and lt_days > 0 and days_per_period > 0:
            safety_stock = z_score * sigma_period * math.sqrt(lt_days / days_per_period)
        else:
            safety_stock = 0.0

        # ── Cycle stock approximation ───────────────────────────────────────
        # CS = μ_agg / 2 (order quantity = one period's demand, hold half)
        # Unit: units/period
        cycle_stock = mu_agg / 2.0

        total_inventory = safety_stock + cycle_stock

        # ── Inventory cost ─────────────────────────────────────────────────
        # IC = (total_inventory × r_h × p̄) × period_factor
        # where r_h is annual holding rate, p̄ is average unit value
        # holding_rate r_h is per YEAR. If cost_period = MONTH, period_factor = 1/12.
        period_str = cost_period.value if hasattr(cost_period, "value") else str(cost_period)
        if period_str == "MONTH":
            period_factor = 1.0 / 12.0
        elif period_str == "YEAR":
            period_factor = 1.0
        elif period_str == "DAY":
            period_factor = 1.0 / 365.0
        elif period_str == "QUARTER":
            period_factor = 1.0 / 4.0
        else:
            period_factor = 1.0 / 12.0

        if products:
            product_ids   = {d.product_id for d in assigned_demands}
            values        = [products[pid].unit_value   for pid in product_ids if pid in products]
            holding_rates = [products[pid].holding_rate for pid in product_ids if pid in products]
            avg_unit_value   = sum(values)        / len(values)        if values        else 0.0
            avg_holding_rate = sum(holding_rates) / len(holding_rates) if holding_rates else 0.25
        else:
            avg_unit_value   = 0.0
            avg_holding_rate = 0.25

        inventory_cost = total_inventory * avg_holding_rate * avg_unit_value * period_factor

        return InventoryCostResult(
            facility_id          = facility.id,
            cycle_stock_units    = round(cycle_stock, 4),
            safety_stock_units   = round(safety_stock, 4),
            total_inventory      = round(total_inventory, 4),
            inventory_cost       = round(inventory_cost, 4),
            days_per_period_used = days_per_period,
        )


# ---------------------------------------------------------------------------
# Zero inventory module (when inventory is disabled)
# ---------------------------------------------------------------------------

class ZeroInventoryModule(InventoryModule):
    """
    Returns zero inventory cost for all facilities.
    Used when config.enable_inventory = False.
    """
    def compute_cost(
        self,
        facility:         FacilityRecord,
        assigned_demands: List[DemandRecord],
        products:         Dict[str, ProductRecord],
        z_score:          float = 1.645,
        days_per_period:  int   = 30,
        cost_period:      str   = "MONTH",
    ) -> InventoryCostResult:
        return InventoryCostResult(
            facility_id          = facility.id,
            cycle_stock_units    = 0.0,
            safety_stock_units   = 0.0,
            total_inventory      = 0.0,
            inventory_cost       = 0.0,
            days_per_period_used = days_per_period,
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_inventory_module(enabled: bool = True) -> InventoryModule:
    """Return the appropriate inventory module."""
    if enabled:
        return NormalSafetyStockModule()
    return ZeroInventoryModule()
