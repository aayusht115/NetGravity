"""
NetGravity V1.1 — Mathematical Correctness Tests
=================================================
Explicit tests for formulation correctness, not just behavioral tests.
Each test targets a specific mathematical property of the MILP.

Tests cover:
  1. Demand balance constraint (always written, C1 fix)
  2. Supply capacity constraint for plants (C10, unconditional)
  3. Forced-close constraint (y_i = 0, C5b)
  4. Opening cost in objective
  5. Minimum throughput enable/disable flag
  6. Inventory unit formula correctness
  7. Iterative inventory convergence
  8. Flow conservation at DCs (C4)
"""

from __future__ import annotations

import math

import pytest

from netgravity.inventory.module import NormalSafetyStockModule
from netgravity.optimization.milp import solve
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    TransportMode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_product(pid: str = "P1") -> ProductRecord:
    return ProductRecord(
        id="P1", name="Widget",
        weight_kg=1.0, volume_m3=0.001,
        unit_value=100.0, holding_rate=0.25,
    )


def _base_config(**kwargs) -> OptimizationConfig:
    defaults = dict(
        enable_inventory=False,
        inventory_max_iterations=1,
    )
    defaults.update(kwargs)
    return OptimizationConfig(**defaults)


# ---------------------------------------------------------------------------
# 1. Demand Balance — Always Written (C1 Fix)
# ---------------------------------------------------------------------------

class TestDemandBalanceAlwaysWritten:
    """
    BUG-001 Fix: Demand balance must be written even when no inbound arcs exist.

    Test that when allow_shortage=True and a market has no inbound arcs,
    the solver correctly returns unmet_demand = D_mk (all demand is shortage),
    not some arbitrary value. This validates that u_mk = D_mk is enforced.
    """

    def test_no_inbound_arcs_with_shortage_fully_unmet(self):
        """When market has no arcs and shortage enabled, ALL demand must be shortage."""
        plant = FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            is_mandatory=True,
        )
        # Market A has a lane from PLANT
        market_a = FacilityRecord(id="MKT_A", name="Market A", role=NodeRole.MARKET)
        # Market B has NO lanes from anywhere
        market_b = FacilityRecord(id="MKT_B", name="Market B", role=NodeRole.MARKET)

        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, market_a, market_b],
            products=[prod],
            demands=[
                DemandRecord(market_id="MKT_A", product_id="P1", quantity=100.0),
                DemandRecord(market_id="MKT_B", product_id="P1", quantity=50.0),  # no arcs
            ],
            lanes=[
                LaneRecord(
                    origin_id="PLANT", destination_id="MKT_A",
                    mode=TransportMode.ROAD, rate_per_unit=1.0,
                    distance_km=100, lead_time_days=1.0,
                ),
                # No lane to MKT_B intentionally
            ],
            config=_base_config(allow_shortage=True, shortage_penalty=1e6),
        )

        result = solve(network)
        assert result.is_solved, f"Expected solved, got {result.solver.status}"

        total_unmet = result.objective_components.get("shortage_cost", 0) / 1e6
        # MKT_B demand = 50 must all be shortage
        assert total_unmet >= 49.9, (
            f"Expected ~50 units shortage (MKT_B unserved), got {total_unmet:.2f}"
        )

    def test_no_inbound_arcs_without_shortage_is_infeasible(self):
        """When market has no arcs and shortage disabled, model must be infeasible."""
        plant = FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            is_mandatory=True,
        )
        market_b = FacilityRecord(id="MKT_B", name="Market B", role=NodeRole.MARKET)

        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, market_b],
            products=[prod],
            demands=[DemandRecord(market_id="MKT_B", product_id="P1", quantity=50.0)],
            lanes=[],   # no lanes at all
            config=_base_config(allow_shortage=False),
        )

        result = solve(network)
        from netgravity.schemas.results import SolverStatus
        assert result.solver.status == SolverStatus.INFEASIBLE, (
            f"Expected INFEASIBLE, got {result.solver.status}"
        )


# ---------------------------------------------------------------------------
# 2. Supply Capacity for Plants (C10 — Unconditional)
# ---------------------------------------------------------------------------

class TestPlantSupplyCapacity:
    """
    C10 Fix: Plant supply capacity constraint is unconditional (no y_i multiplier).
    The plant can produce at most production_capacity_units_per_period.
    """

    def test_plant_production_cap_is_binding(self):
        """Total flow from plant must not exceed production_capacity."""
        plant = FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING,
            is_mandatory=True,
            capacity_units_per_period=1000,
            production_capacity_units_per_period=80,   # tight supply cap
        )
        dc = FacilityRecord(
            id="DC1", name="DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=1000,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC1",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=200, lead_time_days=2.0),
                LaneRecord(origin_id="DC1", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=0.5,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(allow_shortage=True, shortage_penalty=1e6),
        )

        result = solve(network)
        assert result.is_solved

        # Total flow from PLANT cannot exceed production_capacity=80
        plant_outbound = sum(
            fl.flow_units for fl in result.flow_decisions
            if fl.origin_id == "PLANT"
        )
        assert plant_outbound <= 80.01, (
            f"Plant outbound {plant_outbound:.2f} > production capacity 80"
        )

        # Market only gets ≤ 80 units (20 are shortage)
        shortage_cost = result.objective_components.get("shortage_cost", 0)
        assert shortage_cost >= 1e6 * 19.9, (
            f"Expected shortage ≥ 20 units (penalty ≥ {20e6:.0f}), got {shortage_cost:.0f}"
        )


# ---------------------------------------------------------------------------
# 3. Forced-Close Constraint (C5b)
# ---------------------------------------------------------------------------

class TestForcedCloseConstraint:
    """
    C5b Fix: is_forced_closed forces y_i = 0 via explicit constraint.
    Previously implemented as capacity=0 hack.
    """

    def test_forced_closed_facility_stays_closed(self):
        """A facility marked is_forced_closed must have y_i = 0."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        dc1 = FacilityRecord(
            id="DC1", name="DC1", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            fixed_cost_per_year=500,
            is_forced_closed=True,    # <- explicitly forced closed
        )
        dc2 = FacilityRecord(
            id="DC2", name="DC2", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=1000,
            fixed_cost_per_year=1000,
            is_forced_closed=False,
            is_mandatory=True,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc1, dc2, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="PLANT", destination_id="DC2", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC1", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
                LaneRecord(origin_id="DC2", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        dc1_decision = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC1"), None
        )
        assert dc1_decision is not None
        assert not dc1_decision.is_open, (
            "DC1 is forced-closed but optimizer opened it"
        )

        # DC1 should have zero throughput
        dc1_flow = sum(
            fl.flow_units for fl in result.flow_decisions if fl.origin_id == "DC1"
        )
        assert dc1_flow < 1e-6, f"DC1 (forced-closed) has nonzero flow: {dc1_flow}"

    def test_forced_closed_facility_capacity_irrelevant(self):
        """A forced-closed facility with high capacity must still be closed."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        dc1 = FacilityRecord(
            id="DC1", name="DC1 (forced closed)", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=9999,   # high capacity — irrelevant
            fixed_cost_per_year=0,            # cheapest option — optimizer would prefer it
            is_forced_closed=True,
        )
        dc2 = FacilityRecord(
            id="DC2", name="DC2", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=500,
            fixed_cost_per_year=1000,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc1, dc2, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="PLANT", destination_id="DC2", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC1", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=0.1,  # cheapest
                           distance_km=50, lead_time_days=1.0),
                LaneRecord(origin_id="DC2", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=2.0,  # expensive
                           distance_km=200, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        dc1_decision = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC1"), None
        )
        assert dc1_decision is not None, "DC1 should appear in facility decisions"
        assert not dc1_decision.is_open, "DC1 must be closed despite being cheapest"


# ---------------------------------------------------------------------------
# 4. Opening Cost in Objective
# ---------------------------------------------------------------------------

class TestOpeningCostInObjective:
    """
    Opening cost for candidate facilities must appear in objective.
    A candidate with high opening cost should not be opened unnecessarily.
    """

    def test_high_opening_cost_deters_candidate(self):
        """High opening cost makes the candidate less attractive than existing."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        existing = FacilityRecord(
            id="DC_EXIST", name="Existing DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=500,
            fixed_cost_per_year=1000,
            opening_cost=0.0,        # existing: no opening cost
        )
        candidate = FacilityRecord(
            id="DC_CAND", name="Candidate DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=500,
            fixed_cost_per_year=500,          # cheaper annual cost
            opening_cost=100_000.0,           # but enormous opening cost
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, existing, candidate, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC_EXIST", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="PLANT", destination_id="DC_CAND", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC_EXIST", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
                LaneRecord(origin_id="DC_CAND", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        cand_dec = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC_CAND"), None
        )
        # Candidate has opening_cost=100,000 vs annual savings of 500 — should not open
        assert cand_dec is None or not cand_dec.is_open, (
            "Candidate with opening_cost=100,000 should not open when existing has "
            "only 500 more annual cost"
        )

    def test_low_opening_cost_candidate_opens(self):
        """Candidate with low opening cost and better annual cost should open."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        existing = FacilityRecord(
            id="DC_EXIST", name="Existing DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=500,
            fixed_cost_per_year=5000,  # expensive existing
        )
        candidate = FacilityRecord(
            id="DC_CAND", name="Candidate DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=500,
            fixed_cost_per_year=100,   # very cheap annual
            opening_cost=10.0,         # negligible opening cost
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, existing, candidate, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC_EXIST", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="PLANT", destination_id="DC_CAND", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC_EXIST", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
                LaneRecord(origin_id="DC_CAND", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        cand_dec = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC_CAND"), None
        )
        # With annual savings of 4900 vs opening cost 10, candidate should open
        if cand_dec is not None:
            assert cand_dec.is_open, (
                "Candidate with fixed_cost=100, opening_cost=10 should open "
                "when existing has fixed_cost=5000"
            )


# ---------------------------------------------------------------------------
# 5. Minimum Throughput Enable/Disable Flag
# ---------------------------------------------------------------------------

class TestMinimumThroughputFlag:
    """
    minimum_throughput_enabled = False must disable all C3 constraints.
    """

    def test_min_throughput_enabled_enforces_constraint(self):
        """With minimum_throughput_enabled=True, min_throughput is enforced."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        dc = FacilityRecord(
            id="DC", name="DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=1000,
            fixed_cost_per_year=100,
            min_throughput_per_period=200,   # must process at least 200
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=50.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(minimum_throughput_enabled=True),
        )

        result = solve(network)
        # With demand=50 but min_throughput=200, the DC cannot be open economically
        # unless supply can be routed. Since demand=50 < min=200, DC should be closed.
        # This makes the model infeasible (only one DC, market demand=50 < min=200)
        from netgravity.schemas.results import SolverStatus
        dc_decision = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC"), None
        )
        if result.is_solved and dc_decision and dc_decision.is_open:
            # If DC is open, its throughput must be >= min_throughput
            assert dc_decision.throughput_units >= 199.9, (
                f"DC open with throughput {dc_decision.throughput_units:.2f} < "
                f"min_throughput 200"
            )

    def test_min_throughput_disabled_allows_low_throughput(self):
        """With minimum_throughput_enabled=False, min_throughput is ignored."""
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        dc = FacilityRecord(
            id="DC", name="DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=1000,
            fixed_cost_per_year=100,
            min_throughput_per_period=500,   # very high min — but disabled
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=50.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(origin_id="DC", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(minimum_throughput_enabled=False),
        )

        result = solve(network)
        assert result.is_solved, (
            "With min_throughput disabled, model should be feasible even with "
            f"demand=50 < min_throughput=500. Status: {result.solver.status}"
        )


# ---------------------------------------------------------------------------
# 6. Inventory Unit Formula Correctness (V1.1 correction)
# ---------------------------------------------------------------------------

class TestInventoryUnitFormula:
    """
    Verify the corrected safety stock formula:
    SS = z × σ_period × sqrt(LT_days / days_per_period)

    V1.0 used: SS = z × σ_period × sqrt(LT_days)
    V1.1 uses: SS = z × σ_period × sqrt(LT_days / days_per_period)
    """

    def test_safety_stock_unit_correct(self):
        """Verify SS formula with days_per_period=30, LT=3 days."""
        from netgravity.schemas.network import FacilityRecord, DemandRecord, ProductRecord

        module = NormalSafetyStockModule()
        fac = FacilityRecord(
            id="F1", name="Facility", role=NodeRole.DC,
            replenishment_lead_time_days=3.0,
        )
        demands = [
            DemandRecord(market_id="M1", product_id="P1", quantity=100.0, std_dev=20.0)
        ]
        products = {"P1": ProductRecord(
            id="P1", name="Widget", weight_kg=1.0, unit_value=100.0, holding_rate=0.25
        )}

        result = module.compute_cost(
            facility=fac, assigned_demands=demands, products=products,
            z_score=1.645, days_per_period=30,
        )

        # Expected: SS = 1.645 × 20 × sqrt(3/30) = 1.645 × 20 × 0.3162 = 10.41
        expected_ss = 1.645 * 20.0 * math.sqrt(3.0 / 30.0)
        assert abs(result.safety_stock_units - expected_ss) < 0.01, (
            f"Safety stock {result.safety_stock_units:.4f} ≠ expected {expected_ss:.4f}"
        )

    def test_safety_stock_old_formula_would_differ(self):
        """Confirm the old (wrong) formula gives a different (larger) value."""
        module = NormalSafetyStockModule()
        fac = FacilityRecord(
            id="F1", name="Facility", role=NodeRole.DC,
            replenishment_lead_time_days=3.0,
        )
        demands = [
            DemandRecord(market_id="M1", product_id="P1", quantity=100.0, std_dev=20.0)
        ]
        products = {"P1": ProductRecord(
            id="P1", name="Widget", weight_kg=1.0, unit_value=100.0, holding_rate=0.25
        )}

        result = module.compute_cost(
            facility=fac, assigned_demands=demands, products=products,
            z_score=1.645, days_per_period=30,
        )

        old_formula_ss = 1.645 * 20.0 * math.sqrt(3.0)   # old incorrect formula
        new_formula_ss = result.safety_stock_units

        # Old formula should be ~5.5x larger
        assert old_formula_ss > new_formula_ss * 4.0, (
            f"Old formula SS ({old_formula_ss:.2f}) should be much larger than "
            f"new formula SS ({new_formula_ss:.2f})"
        )

    def test_zero_lead_time_zero_safety_stock(self):
        """Zero lead time → zero safety stock."""
        module = NormalSafetyStockModule()
        fac = FacilityRecord(
            id="F1", name="Facility", role=NodeRole.DC,
            replenishment_lead_time_days=0.0,
        )
        demands = [
            DemandRecord(market_id="M1", product_id="P1", quantity=100.0, std_dev=20.0)
        ]
        products = {"P1": ProductRecord(
            id="P1", name="Widget", weight_kg=1.0, unit_value=100.0, holding_rate=0.25
        )}

        result = module.compute_cost(
            facility=fac, assigned_demands=demands, products=products,
            z_score=1.645, days_per_period=30,
        )
        assert result.safety_stock_units == 0.0, (
            f"Zero LT should give zero SS, got {result.safety_stock_units}"
        )

    def test_days_per_period_recorded_in_result(self):
        """days_per_period used should be recorded in InventoryCostResult."""
        module = NormalSafetyStockModule()
        fac = FacilityRecord(
            id="F1", name="Facility", role=NodeRole.DC,
            replenishment_lead_time_days=5.0,
        )
        demands = [DemandRecord(market_id="M1", product_id="P1", quantity=100.0)]
        products = {}

        result = module.compute_cost(
            facility=fac, assigned_demands=demands, products=products,
            z_score=1.645, days_per_period=28,
        )
        assert result.days_per_period_used == 28


# ---------------------------------------------------------------------------
# 7. Iterative Inventory Solve Convergence
# ---------------------------------------------------------------------------

class TestIterativeInventorySolve:
    """
    Test that iterative inventory solve:
    - Completes without error
    - Produces a solved result
    - Converges within max_iterations
    - Records inventory_iterations in result
    """

    def _make_two_dc_network(
        self, iterations: int = 5, tol: float = 0.001
    ) -> CanonicalNetwork:
        """Helper: two-DC network to test iterative solve."""
        dc1 = FacilityRecord(
            id="DC1", name="DC1", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
            fixed_cost_per_year=500,
            handling_cost_per_unit=0.0,
            replenishment_lead_time_days=2.0,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = ProductRecord(
            id="P1", name="Widget", weight_kg=1.0,
            unit_value=50.0, holding_rate=0.25,
        )
        plant = FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        return CanonicalNetwork(
            facilities=[plant, dc1, market],
            products=[prod],
            demands=[
                DemandRecord(
                    market_id="MKT", product_id="P1",
                    quantity=100.0, std_dev=15.0,
                )
            ],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=1.0),
                LaneRecord(
                    origin_id="DC1", destination_id="MKT",
                    mode=TransportMode.ROAD, rate_per_unit=1.0,
                    distance_km=100, lead_time_days=1.0,
                )
            ],
            config=OptimizationConfig(
                enable_inventory=True,
                inventory_max_iterations=iterations,
                inventory_convergence_tolerance=tol,
                days_per_period=30,
            ),
        )

    def test_iterative_solve_completes(self):
        """Iterative solve completes and returns a solved result."""
        network = self._make_two_dc_network(iterations=3)
        result = solve(network)
        assert result.is_solved, f"Iterative solve failed: {result.solver.status}"

    def test_iterative_solve_records_iterations(self):
        """inventory_iterations must be >= 1 after iterative solve."""
        network = self._make_two_dc_network(iterations=3)
        result = solve(network)
        assert result.inventory_iterations >= 1, (
            f"Expected inventory_iterations >= 1, got {result.inventory_iterations}"
        )

    def test_iterative_solve_inventory_cost_in_objective_components(self):
        """After iterative solve, inventory_cost must appear in objective_components."""
        network = self._make_two_dc_network(iterations=3)
        result = solve(network)
        if result.is_solved:
            # Inventory cost should be in objective_components
            assert "inventory_cost" in result.objective_components, (
                "inventory_cost not in objective_components after iterative solve"
            )

    def test_single_iteration_is_not_iterative(self):
        """max_iterations=1 should run exactly one iteration (post-solve only)."""
        network = self._make_two_dc_network(iterations=1)
        result = solve(network)
        assert result.is_solved
        assert result.inventory_iterations == 1

    def test_convergence_message_in_warnings(self):
        """Under V1.2 Direct Formulation, single-pass solve completes with DIRECT_MILP method and 1 iteration."""
        network = self._make_two_dc_network(iterations=10, tol=0.001)
        result = solve(network)
        assert result.is_solved
        assert result.inventory_method == "DIRECT_MILP"
        assert result.inventory_iterations == 1


# ---------------------------------------------------------------------------
# 8. Flow Conservation at DCs (C4)
# ---------------------------------------------------------------------------

class TestFlowConservationFormulation:
    """
    Test that flow conservation at DC/Warehouse nodes is mathematically correct.
    Σ inbound = Σ outbound for each through-node × product.
    """

    def test_flow_conservation_holds_at_dc(self):
        """For each DC, total inbound = total outbound (per product)."""
        plant = FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=500,
        )
        dc = FacilityRecord(
            id="DC", name="DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=500,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, dc, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DC",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=200, lead_time_days=2.0),
                LaneRecord(origin_id="DC", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=0.5,
                           distance_km=100, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        # Check flow conservation at DC
        dc_inbound  = sum(fl.flow_units for fl in result.flow_decisions if fl.destination_id == "DC" and fl.product_id == "P1")
        dc_outbound = sum(fl.flow_units for fl in result.flow_decisions if fl.origin_id == "DC" and fl.product_id == "P1")

        assert abs(dc_inbound - dc_outbound) < 0.01, (
            f"Flow conservation violated at DC: inbound={dc_inbound:.4f}, outbound={dc_outbound:.4f}"
        )

    def test_depot_role_in_flow_conservation(self):
        """DEPOT role nodes should also have flow conservation enforced."""
        plant = FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=500,
        )
        depot = FacilityRecord(
            id="DEPOT", name="Depot", role=NodeRole.DEPOT,  # new in V1.1
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=500,
        )
        market = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = _make_product()

        network = CanonicalNetwork(
            facilities=[plant, depot, market],
            products=[prod],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=50.0)],
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="DEPOT",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=100, lead_time_days=1.0),
                LaneRecord(origin_id="DEPOT", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=0.5,
                           distance_km=50, lead_time_days=1.0),
            ],
            config=_base_config(),
        )

        result = solve(network)
        assert result.is_solved

        depot_inbound  = sum(fl.flow_units for fl in result.flow_decisions if fl.destination_id == "DEPOT")
        depot_outbound = sum(fl.flow_units for fl in result.flow_decisions if fl.origin_id == "DEPOT")
        assert abs(depot_inbound - depot_outbound) < 0.01, (
            f"Flow conservation violated at DEPOT: inbound={depot_inbound:.4f}, outbound={depot_outbound:.4f}"
        )
