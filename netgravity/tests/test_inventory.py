"""NetGravity — Inventory Module Tests"""

import math
import pytest

from netgravity.inventory.module import NormalSafetyStockModule, ZeroInventoryModule
from netgravity.schemas.network import DemandRecord, FacilityRecord, FacilityStatus, NodeRole, ProductRecord


def _make_facility(replen_lt=3.0):
    return FacilityRecord(
        id="DC_TEST", name="Test DC", role=NodeRole.DC,
        status=FacilityStatus.EXISTING, is_mandatory=False, is_closable=True,
        replenishment_lead_time_days=replen_lt, fixed_cost_per_year=0,
    )

def _make_demand(market_id, qty, std_dev):
    return DemandRecord(market_id=market_id, product_id="P001", quantity=qty, std_dev=std_dev)

def _make_product(unit_value=100.0, holding_rate=0.25):
    return ProductRecord(id="P001", name="Test", weight_kg=1.0,
                         unit_value=unit_value, holding_rate=holding_rate)


class TestNormalSafetyStock:

    def setup_method(self):
        self.module = NormalSafetyStockModule()

    def test_zero_variability_no_safety_stock(self):
        fac     = _make_facility()
        demands = [_make_demand("M1", 100, 0.0)]
        product_map = {"P001": _make_product()}
        result  = self.module.compute_cost(fac, demands, product_map, z_score=1.645)
        assert result.safety_stock_units == 0.0

    def test_positive_safety_stock_with_variability(self):
        fac     = _make_facility(replen_lt=4.0)
        demands = [_make_demand("M1", 200, 30.0)]
        product_map = {"P001": _make_product()}
        result  = self.module.compute_cost(fac, demands, product_map, z_score=1.645, days_per_period=30)
        # V1.1 formula: SS = z × σ_period × sqrt(LT_days / days_per_period)
        # SS = 1.645 × 30 × sqrt(4/30) = 1.645 × 30 × 0.3651 = 18.0202
        expected_ss = 1.645 * 30 * math.sqrt(4.0 / 30.0)
        assert abs(result.safety_stock_units - expected_ss) < 0.01, (
            f"V1.1 SS formula: expected {expected_ss:.4f}, got {result.safety_stock_units:.4f}.\n"
            f"V1.0 (incorrect) formula would give: {1.645 * 30 * math.sqrt(4):.4f}"
        )

    def test_cycle_stock_is_half_demand(self):
        fac     = _make_facility(replen_lt=0.0)
        demands = [_make_demand("M1", 400, 0.0)]
        product_map = {"P001": _make_product()}
        result  = self.module.compute_cost(fac, demands, product_map, z_score=1.645)
        # Cycle stock = μ/2 = 200
        assert abs(result.cycle_stock_units - 200.0) < 0.01

    def test_inventory_cost_formula(self):
        fac     = _make_facility(replen_lt=1.0)
        demands = [_make_demand("M1", 100, 10.0)]
        product_map = {"P001": _make_product(unit_value=80.0, holding_rate=0.25)}
        result_month = self.module.compute_cost(fac, demands, product_map, z_score=1.645, cost_period="MONTH")
        # Monthly IC = (total_inventory × 0.25 × 80) / 12
        expected_monthly_ic = (result_month.total_inventory * 0.25 * 80.0) / 12.0
        assert abs(result_month.inventory_cost - expected_monthly_ic) < 0.01

        result_year = self.module.compute_cost(fac, demands, product_map, z_score=1.645, cost_period="YEAR")
        # Annual IC = total_inventory × 0.25 × 80
        expected_annual_ic = result_year.total_inventory * 0.25 * 80.0
        assert abs(result_year.inventory_cost - expected_annual_ic) < 0.01

    def test_no_demands_returns_zero(self):
        fac    = _make_facility()
        result = self.module.compute_cost(fac, [], {})
        assert result.inventory_cost == 0.0
        assert result.safety_stock_units == 0.0

    def test_independence_assumption_aggregation(self):
        """With 2 markets, variance adds (independence assumption A-010)."""
        fac     = _make_facility(replen_lt=1.0)
        d1      = _make_demand("M1", 100, 20.0)
        d2      = _make_demand("M2", 100, 20.0)
        product_map = {"P001": _make_product()}
        result  = self.module.compute_cost(fac, [d1, d2], product_map, z_score=1.645, days_per_period=30)
        # σ_agg = sqrt(20²+20²) = sqrt(800) ≈ 28.28  (independence A-010)
        # V1.1 formula: SS = z × σ_agg × sqrt(LT / days_per_period)
        # SS = 1.645 × 28.284 × sqrt(1/30) = 1.645 × 28.284 × 0.1826 = 8.495
        expected_sigma = math.sqrt(20**2 + 20**2)
        expected_ss    = 1.645 * expected_sigma * math.sqrt(1.0 / 30.0)
        assert abs(result.safety_stock_units - expected_ss) < 0.01, (
            f"V1.1 SS (independence): expected {expected_ss:.4f}, got {result.safety_stock_units:.4f}"
        )


class TestZeroInventoryModule:

    def test_always_zero(self):
        module  = ZeroInventoryModule()
        fac     = _make_facility()
        demands = [_make_demand("M1", 100, 50.0)]
        product_map = {"P001": _make_product()}
        result  = module.compute_cost(fac, demands, product_map)
        assert result.inventory_cost == 0.0
        assert result.safety_stock_units == 0.0
