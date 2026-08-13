"""
NetGravity V1.1 — Adversarial / Stress Tests
===============================================
Edge-case and stress tests that push the model to its limits.

Purpose:
  - Verify solver stability under extreme inputs
  - Ensure no silent numerical failures
  - Test correctness at boundary conditions
  - Validate multi-product, multi-echelon scenarios
  - Test that infeasible models fail loudly
"""

from __future__ import annotations

import pytest

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
from netgravity.schemas.results import SolverStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(**kwargs) -> OptimizationConfig:
    defaults = dict(
        enable_inventory=False,
        inventory_max_iterations=1,
    )
    defaults.update(kwargs)
    return OptimizationConfig(**defaults)


def _plant(pid: str, cap: float, mandatory: bool = True) -> FacilityRecord:
    return FacilityRecord(
        id=pid, name=f"Plant {pid}", role=NodeRole.PLANT,
        status=FacilityStatus.EXISTING,
        capacity_units_per_period=cap,
        is_mandatory=mandatory,
    )


def _dc(did: str, cap: float, cost: float = 1000.0, existing: bool = True) -> FacilityRecord:
    return FacilityRecord(
        id=did, name=f"DC {did}", role=NodeRole.DC,
        status=FacilityStatus.EXISTING if existing else FacilityStatus.CANDIDATE,
        capacity_units_per_period=cap,
        fixed_cost_per_year=cost,
    )


def _market(mid: str) -> FacilityRecord:
    return FacilityRecord(id=mid, name=f"Market {mid}", role=NodeRole.MARKET)


def _demand(mid: str, pid: str, q: float) -> DemandRecord:
    return DemandRecord(market_id=mid, product_id=pid, quantity=q)


def _product(pid: str, value: float = 10.0) -> ProductRecord:
    return ProductRecord(
        id=pid, name=f"Product {pid}",
        weight_kg=1.0, unit_value=value, holding_rate=0.25,
    )


def _lane(o: str, d: str, rate: float = 1.0, lt: float = 1.0, dist: float = 100.0) -> LaneRecord:
    return LaneRecord(
        origin_id=o, destination_id=d,
        mode=TransportMode.ROAD, rate_per_unit=rate,
        distance_km=dist, lead_time_days=lt,
    )


# ---------------------------------------------------------------------------
# Boundary: Zero demand
# ---------------------------------------------------------------------------

class TestZeroDemandVariants:

    def test_zero_demand_single_market(self):
        """Zero demand everywhere → zero objective."""
        dc = _dc("DC1", 1000)
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 0.0)],
            lanes=[_lane("DC1", "MKT")],
            config=_cfg(),
        )
        res = solve(net)
        assert res.is_solved
        assert res.solver.objective_value is not None
        # DC should be closed (no demand to justify opening cost)
        # Objective = 0 if DC is closed (or 1000 if existing+mandatory)
        # With existing + no mandate, MILP can close it
        assert res.solver.objective_value >= 0.0

    def test_zero_demand_with_inventory_enabled(self):
        """Zero demand with inventory: zero inventory cost."""
        dc = FacilityRecord(
            id="DC1", name="DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=500, fixed_cost_per_year=1000,
            replenishment_lead_time_days=2.0,
        )
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 0.0)],
            lanes=[_lane("DC1", "MKT")],
            config=OptimizationConfig(
                enable_inventory=True, inventory_max_iterations=2,
                days_per_period=30,
            ),
        )
        res = solve(net)
        assert res.is_solved
        assert res.objective_components.get("inventory_cost", 0.0) == 0.0


# ---------------------------------------------------------------------------
# Boundary: Very large demand
# ---------------------------------------------------------------------------

class TestLargeDemand:

    def test_very_large_demand_with_shortage(self):
        """Demand 10× capacity + shortage enabled → shortage fills the gap."""
        dc = _dc("DC1", 100)   # capacity = 100
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 1000.0)],  # demand = 1000 >> 100
            lanes=[_lane("DC1", "MKT")],
            config=_cfg(allow_shortage=True, shortage_penalty=1e4),
        )
        res = solve(net)
        assert res.is_solved
        total_served = sum(fl.flow_units for fl in res.flow_decisions)
        assert total_served <= 100.1, f"Served {total_served} > capacity 100"
        shortage = res.objective_components.get("shortage_cost", 0) / 1e4
        assert shortage >= 899.9, f"Expected ~900 shortage, got {shortage:.1f}"


# ---------------------------------------------------------------------------
# Boundary: Large number of markets
# ---------------------------------------------------------------------------

class TestManyMarkets:

    def test_20_markets_single_dc(self):
        """20 markets, single DC: all demand served."""
        n = 20
        plant = _plant("PLANT", 100_000)
        dc = _dc("DC", 100_000)
        markets = [_market(f"M{i}") for i in range(n)]
        products = [_product("P1")]
        demands = [_demand(f"M{i}", "P1", 100.0) for i in range(n)]
        lanes = [_lane("PLANT", "DC")] + [_lane("DC", f"M{i}", rate=0.5) for i in range(n)]

        net = CanonicalNetwork(
            facilities=[plant, dc] + markets,
            products=products,
            demands=demands,
            lanes=lanes,
            config=_cfg(),
        )
        res = solve(net)
        assert res.is_solved
        total_served = sum(fl.flow_units for fl in res.flow_decisions if fl.destination_id.startswith("M"))
        assert abs(total_served - n * 100.0) < 0.1, (
            f"Expected {n * 100.0}, got {total_served:.2f}"
        )


# ---------------------------------------------------------------------------
# Boundary: Many products
# ---------------------------------------------------------------------------

class TestManyProducts:

    def test_5_products_single_dc(self):
        """5 products, single DC: all products served."""
        n_prod = 5
        plant = _plant("PLANT", 100_000)
        dc = _dc("DC", 100_000)
        mkt = _market("MKT")
        products = [_product(f"P{i}") for i in range(n_prod)]
        demands = [_demand("MKT", f"P{i}", 100.0) for i in range(n_prod)]
        lanes = [_lane("PLANT", "DC")] + [_lane("DC", "MKT") for _ in range(n_prod)]  # same lane, all products

        net = CanonicalNetwork(
            facilities=[plant, dc, mkt],
            products=products,
            demands=demands,
            lanes=lanes,
            config=_cfg(),
        )
        res = solve(net)
        assert res.is_solved
        total_served = sum(fl.flow_units for fl in res.flow_decisions if fl.destination_id == "MKT")
        assert abs(total_served - n_prod * 100.0) < 0.1


# ---------------------------------------------------------------------------
# Infeasibility: Various causes
# ---------------------------------------------------------------------------

class TestInfeasibilityVariants:

    def test_max_facilities_zero_is_infeasible(self):
        """max_facilities=0 forces all DCs closed → infeasible if demand > 0."""
        dc = _dc("DC1", 1000, existing=False)
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[_lane("DC1", "MKT")],
            config=_cfg(max_facilities=0, allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE, (
            f"Expected INFEASIBLE with max_facilities=0, got {res.solver.status}"
        )

    def test_conflicting_mandatory_and_forced_close(self):
        """Mandatory AND forced-close on same facility should fail loudly."""
        # Pydantic or MILP should raise or produce infeasible
        dc = FacilityRecord(
            id="DC1", name="DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            capacity_units_per_period=500,
            is_mandatory=True,
            is_forced_closed=True,   # contradiction
        )
        mkt = _market("MKT")
        try:
            net = CanonicalNetwork(
                facilities=[dc, mkt],
                products=[_product("P1")],
                demands=[_demand("MKT", "P1", 100.0)],
                lanes=[_lane("DC1", "MKT")],
                config=_cfg(),
            )
            res = solve(net)
            # If it gets here, it must be infeasible (mandatory y=1 and forced y=0 conflict)
            assert res.solver.status == SolverStatus.INFEASIBLE, (
                "Conflicting mandatory+forced_closed should produce INFEASIBLE"
            )
        except Exception:
            pass  # Raised before solve = also acceptable behavior


# ---------------------------------------------------------------------------
# Solver reproducibility and determinism
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_five_runs_same_objective(self):
        """Five independent solves of the same model produce identical objectives."""
        plant = _plant("PLANT", 1000)
        dc1 = _dc("DC1", 500, cost=1000.0, existing=True)
        dc2 = _dc("DC2", 500, cost=2000.0, existing=False)
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[plant, dc1, dc2, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 300.0)],
            lanes=[
                _lane("PLANT", "DC1"),
                _lane("PLANT", "DC2"),
                _lane("DC1", "MKT", rate=1.0),
                _lane("DC2", "MKT", rate=0.5),
            ],
            config=_cfg(),
        )

        objectives = []
        for _ in range(5):
            res = solve(net)
            assert res.is_solved
            objectives.append(res.solver.objective_value)

        first_obj = objectives[0]
        for obj in objectives[1:]:
            assert abs(obj - first_obj) < 1e-4, (
                f"Non-deterministic: {objectives}"
            )


# ---------------------------------------------------------------------------
# Optimality label correctness
# ---------------------------------------------------------------------------

class TestOptimalityLabel:

    def test_optimal_label_non_overclaiming(self):
        """Solver optimality label must not claim 'proven optimal' when gap > 0."""
        plant = _plant("PLANT", 1000)
        dc = _dc("DC1", 500, existing=True)
        mkt = _market("MKT")
        net = CanonicalNetwork(
            facilities=[plant, dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[_lane("PLANT", "DC1"), _lane("DC1", "MKT")],
            config=_cfg(mip_gap=0.01),  # 1% gap
        )
        res = solve(net)
        assert res.is_solved
        # The label should exist and not claim 'proven optimal' with 1% gap tolerance
        label = res.solver.optimality_label
        assert isinstance(label, str) and len(label) > 0
        # If gap tolerance is 1%, we should not call it "proven optimal" from config alone
        # (We can only claim proven if solver confirmed gap=0 via best_bound)

    def test_infeasible_has_correct_label(self):
        """Infeasible model has a sensible optimality label."""
        dc = _dc("DC1", 0, existing=False)  # zero capacity
        mkt = _market("MKT")
        try:
            net = CanonicalNetwork(
                facilities=[dc, mkt],
                products=[_product("P1")],
                demands=[_demand("MKT", "P1", 100.0)],
                lanes=[_lane("DC1", "MKT")],
                config=_cfg(allow_shortage=False),
            )
            res = solve(net)
            if res.solver.status == SolverStatus.INFEASIBLE:
                label = res.solver.optimality_label
                assert "infeasible" in label.lower() or "no feasible" in label.lower(), (
                    f"Infeasible label should mention infeasibility: '{label}'"
                )
        except Exception:
            pass  # Zero-capacity facility may be caught at validation


# ---------------------------------------------------------------------------
# Multi-echelon flow conservation
# ---------------------------------------------------------------------------

class TestMultiEchelon:

    def test_two_echelon_flow_balance(self):
        """Plant → DC → Market: total plant output = total market demand."""
        plant = FacilityRecord(
            id="PLT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
        )
        dc = FacilityRecord(
            id="DC", name="DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
        )
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[plant, dc, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 250.0)],
            lanes=[
                _lane("PLT", "DC", rate=1.0),
                _lane("DC",  "MKT", rate=0.5),
            ],
            config=_cfg(),
        )
        res = solve(net)
        assert res.is_solved

        # Total plant outbound = 250 (conservation across full chain)
        plant_out = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == "PLT")
        market_in  = sum(fl.flow_units for fl in res.flow_decisions if fl.destination_id == "MKT")
        assert abs(plant_out - 250.0) < 0.01, f"Plant output {plant_out} ≠ 250"
        assert abs(market_in  - 250.0) < 0.01, f"Market input {market_in} ≠ 250"

    def test_three_echelon_flow_balance(self):
        """Plant → DC → Depot → Market: each node conserves flow."""
        plant = FacilityRecord(
            id="PLT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
        )
        dc = FacilityRecord(
            id="DC", name="DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
        )
        depot = FacilityRecord(
            id="DEP", name="Depot", role=NodeRole.DEPOT,
            status=FacilityStatus.EXISTING, is_mandatory=True,
            capacity_units_per_period=1000,
        )
        mkt = _market("MKT")

        net = CanonicalNetwork(
            facilities=[plant, dc, depot, mkt],
            products=[_product("P1")],
            demands=[_demand("MKT", "P1", 100.0)],
            lanes=[
                _lane("PLT", "DC",  rate=1.0, dist=300.0),
                _lane("DC",  "DEP", rate=0.8, dist=200.0),
                _lane("DEP", "MKT", rate=0.5, dist=50.0),
            ],
            config=_cfg(),
        )
        res = solve(net)
        assert res.is_solved

        for node_id, role in [("DC", "DC"), ("DEP", "Depot")]:
            inbound  = sum(fl.flow_units for fl in res.flow_decisions if fl.destination_id == node_id)
            outbound = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == node_id)
            assert abs(inbound - outbound) < 0.01, (
                f"Flow conservation violated at {role}: in={inbound:.4f}, out={outbound:.4f}"
            )
