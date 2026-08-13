"""
NetGravity V1.2.2 — Production Hardening Regression Test Suite
================================================================
Regression tests for:
  1. Assignment-binary degeneracy fix (a_ij = 1 iff flow > 0)
  2. Network topology & phantom-supply elimination
  3. Explicit SLA modes (LAST_MILE vs END_TO_END)
  4. Single canonical inventory formulation reconciliation
  5. Existing manual validation reproduction
"""

from __future__ import annotations

import math
import pytest

from netgravity.costs.reconciliation import reconcile_costs
from netgravity.inventory.coefficient_engine import InventoryCoefficientEngine
from netgravity.optimization.milp import solve
from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    SLAMode,
    TransportMode,
)
from netgravity.schemas.results import SolverStatus
from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network


class TestAssignmentBinaryDegeneracyFix:

    def test_zero_inventory_coeff_zero_flow_no_spurious_assignment(self):
        """
        Regression Test A:
        When inventory_cost[i,j] = 0 (e.g., zero holding rate or zero variability),
        if flow(i,j) = 0, assignment a[i,j] MUST equal 0.
        No spurious zero-flow assignments may remain active.
        """
        p = FacilityRecord(id="P", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc_a = FacilityRecord(id="DC_A", name="DC A", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=1000, fixed_cost_per_year=12000) # $1k/mo
        dc_b = FacilityRecord(id="DC_B", name="DC B", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=1000, fixed_cost_per_year=24000) # $2k/mo
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1", unit_value=0.0, holding_rate=0.0) # Zero inventory cost
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=500, std_dev=0.0)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC_A", mode=TransportMode.ROAD, rate_per_unit=1.0),
            LaneRecord(origin_id="P", destination_id="DC_B", mode=TransportMode.ROAD, rate_per_unit=1.0),
            LaneRecord(origin_id="DC_A", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0),
            LaneRecord(origin_id="DC_B", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0),
        ]

        net = CanonicalNetwork(
            facilities=[p, dc_a, dc_b, mkt], products=[prod], demands=[demand], lanes=lanes,
            config=OptimizationConfig(enable_inventory=True),
        )
        res = solve(net)
        assert res.is_solved

        # DC_A is cheaper ($1k vs $2k), so DC_B will carry 0 flow.
        # DC_B -> MKT assignment must be False!
        dc_b_assignments = [ad for ad in res.assignment_decisions if ad.facility_id == "DC_B" and ad.market_id == "MKT"]
        assert len(dc_b_assignments) == 1
        assert dc_b_assignments[0].is_assigned is False, "Zero flow on DC_B -> MKT must result in a_ij = 0"

    def test_positive_inventory_coeff_existing_behaviour_preserved(self):
        """
        Regression Test B:
        Positive inventory coefficient behaves correctly and assigns active serving facility.
        """
        net = build_tiny_network()
        res = solve(net)
        assert res.is_solved
        t1_ass = [ad for ad in res.assignment_decisions if ad.facility_id == "DC_T1" and ad.is_assigned]
        assert len(t1_ass) > 0


class TestPhantomSupplyElimination:

    def test_dc_without_inbound_lane_cannot_provide_phantom_supply(self):
        """
        Regression Test C:
        A DC with outbound lanes but NO inbound lane must NOT act as an unconstrained supply source.
        Outbound flow must equal 0.
        """
        p = FacilityRecord(id="P", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc_legit = FacilityRecord(id="DC_LEGIT", name="DC Legit", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=1000)
        dc_phantom = FacilityRecord(id="DC_PHANTOM", name="DC Phantom", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=1000)
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=500)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC_LEGIT", mode=TransportMode.ROAD, rate_per_unit=1.0),
            LaneRecord(origin_id="DC_LEGIT", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0),
            # DC_PHANTOM has NO inbound lane from P, only outbound to MKT
            LaneRecord(origin_id="DC_PHANTOM", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=0.1), # Very cheap rate
        ]

        net = CanonicalNetwork(facilities=[p, dc_legit, dc_phantom, mkt], products=[prod], demands=[demand], lanes=lanes)
        res = solve(net)
        assert res.is_solved

        phantom_flows = [fl for fl in res.flow_decisions if fl.origin_id == "DC_PHANTOM"]
        assert len(phantom_flows) == 0, "DC_PHANTOM without inbound supply must carry zero outbound flow"

    def test_dc_without_inbound_lane_serving_unreachable_demand_is_infeasible(self):
        """
        Regression Test D:
        If a DC has no inbound lane and is the only DC connected to a market (allow_shortage=False),
        the solver MUST return INFEASIBLE.
        """
        dc_phantom = FacilityRecord(id="DC_PHANTOM", name="DC Phantom", role=NodeRole.DC, status=FacilityStatus.EXISTING)
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=500)

        lane = LaneRecord(origin_id="DC_PHANTOM", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0)

        net = CanonicalNetwork(
            facilities=[dc_phantom, mkt], products=[prod], demands=[demand], lanes=[lane],
            config=OptimizationConfig(allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE


class TestSLAModes:

    def test_last_mile_sla_mode(self):
        """
        Regression Test E:
        LAST_MILE SLA mode filters lane based on DC -> Market lead time only.
        """
        p = FacilityRecord(id="P", name="P", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc = FacilityRecord(id="DC", name="DC", role=NodeRole.DC, status=FacilityStatus.EXISTING)
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=100, sla_days=3.0)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=5.0), # Inbound LT = 5
            LaneRecord(origin_id="DC", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=2.0), # Outbound LT = 2 <= 3 SLA
        ]

        net = CanonicalNetwork(
            facilities=[p, dc, mkt], products=[prod], demands=[demand], lanes=lanes,
            config=OptimizationConfig(enforce_sla=True, sla_mode=SLAMode.LAST_MILE, allow_shortage=False),
        )
        res = solve(net)
        assert res.is_solved, "LAST_MILE mode accepts DC->MKT lead_time=2 <= SLA=3"

    def test_end_to_end_sla_mode_rejects_violating_route(self):
        """
        Regression Test F:
        END_TO_END SLA mode calculates cumulative Plant -> DC -> Market lead time (5 + 2 = 7 > 3 SLA)
        and rejects the route as INFEASIBLE.
        """
        p = FacilityRecord(id="P", name="P", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc = FacilityRecord(id="DC", name="DC", role=NodeRole.DC, status=FacilityStatus.EXISTING)
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=100, sla_days=3.0)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=5.0), # Inbound LT = 5
            LaneRecord(origin_id="DC", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=2.0), # Outbound LT = 2
        ]

        net = CanonicalNetwork(
            facilities=[p, dc, mkt], products=[prod], demands=[demand], lanes=lanes,
            config=OptimizationConfig(enforce_sla=True, sla_mode=SLAMode.END_TO_END, allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE, "END_TO_END mode rejects cumulative LT=7 > SLA=3"


class TestCanonicalInventoryAndAssignments:

    def test_inventory_canonicalization_exact_reconciliation(self):
        """
        Regression Test G:
        MILP optimizer inventory cost exactly matches independent canonical inventory calculation.
        Reconciliation gap must equal 0.00.
        """
        net = build_case16_network()
        res = solve(net)
        recon = reconcile_costs(res, net)
        assert res.is_solved
        assert recon.is_reconciled is True
        assert res.objective_reconciliation_gap == 0.0

    def test_multi_product_assignment_reflects_positive_flow(self):
        """
        Regression Test H:
        For multi-product demands, assignment reflects actual positive flow across products.
        """
        net = build_case16_network()
        res = solve(net)
        assert res.is_solved
        for ad in res.assignment_decisions:
            flow_units = sum(
                fl.flow_units for fl in res.flow_decisions
                if fl.origin_id == ad.facility_id and fl.destination_id == ad.market_id
            )
            if flow_units > 1e-5:
                assert ad.is_assigned is True, f"Positive flow ({flow_units}) must have is_assigned=True"
            else:
                assert ad.is_assigned is False, f"Zero flow must have is_assigned=False"

    def test_zero_demand_no_spurious_assignments(self):
        """
        Regression Test I:
        Zero demand market yields zero assignments.
        """
        p = FacilityRecord(id="P", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc = FacilityRecord(id="DC", name="DC", role=NodeRole.DC, status=FacilityStatus.EXISTING)
        mkt = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=0.0)

        lane1 = LaneRecord(origin_id="P", destination_id="DC", mode=TransportMode.ROAD, rate_per_unit=1.0)
        lane2 = LaneRecord(origin_id="DC", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0)

        net = CanonicalNetwork(facilities=[p, dc, mkt], products=[prod], demands=[demand], lanes=[lane1, lane2])
        res = solve(net)
        assert res.is_solved
        assignments = [ad for ad in res.assignment_decisions if ad.is_assigned]
        assert len(assignments) == 0

    def test_existing_manual_validation_cases_reproduced(self):
        """
        Regression Test J:
        5,400 benchmark reference case produces exact optimal objective of $5,400.00.
        """
        net = build_tiny_network()
        res = solve(net)
        assert res.solver.status == SolverStatus.OPTIMAL
        assert abs(res.solver.objective_value - 5400.0) < 1e-2
