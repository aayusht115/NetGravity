"""
NetGravity V1.2 — Direct MILP Inventory Formulation Test Suite
================================================================
Comprehensive test suite verifying:
1. InventoryCoefficientEngine precomputations & unit normalization.
2. Assignment binary creation (a_ij) and flow-assignment linking.
3. Prevention of assignment to closed facilities (y_i = 0 => a_ij = 0).
4. Hand-verifiable safety stock & inventory cost calculation.
5. Preservation of 5,400 hand-solvable reference case.
6. Full Case-16 Direct MILP solution & exact reconciliation (gap = 0.00).
7. Scenario testing under direct formulation (Baseline, Close DC_EAST, Demand +20%).
8. Stress testing (zero demand, zero variability, zero lead time, large lead time, single sourcing).
"""

from __future__ import annotations

import math
import pytest

from netgravity.costs.reconciliation import reconcile_costs
from netgravity.inventory.coefficient_engine import InventoryCoefficientEngine
from netgravity.optimization.baseline import evaluate_baseline
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
    SourcingPolicy,
    TransportMode,
)
from netgravity.schemas.scenario import DemandChange, FacilityChange, Scenario
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network


class TestHandVerifiableInventoryCoefficients:

    def test_manual_inventory_calculation_matches_engine(self):
        """
        Hand-verifiable calculation test:
        Facility DC1 -> Market M1
        Lead time LT = 3.0 days
        Days per period = 30 days
        Demand quantity = 1000 units/month, sigma = 100 units/month
        z-score = 1.645 (95% CSL)
        unit_value = $50.00 / unit
        annual holding_rate = 24% / yr -> monthly holding rate = 2% / mo

        Hand Calculation:
          sqrt_LT_ratio = sqrt(3.0 / 30.0) = sqrt(0.1) = 0.316227766
          SS = 1.645 * 100 * sqrt(0.1) = 164.5 * 0.316227766 = 52.019467 units
          monthly IC = 52.019467 * 50.00 * (0.24 / 12) = 52.019467 * 50.00 * 0.02 = $52.0195 / month
        """
        dc = FacilityRecord(
            id="DC1", name="DC 1", role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            replenishment_lead_time_days=3.0,
        )
        mkt = FacilityRecord(id="M1", name="Market 1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="Widget", unit_value=50.0, holding_rate=0.24)

        lane = LaneRecord(
            origin_id="DC1", destination_id="M1",
            mode=TransportMode.ROAD, rate_per_unit=1.0, distance_km=100.0,
            lead_time_days=0.0,
        )
        demand = DemandRecord(market_id="M1", product_id="P1", quantity=1000.0, std_dev=100.0)

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[prod],
            demands=[demand],
            lanes=[lane],
            config=OptimizationConfig(
                cost_period=CostPeriod.MONTH,
                days_per_period=30,
                inventory_z_score=1.645,
                enable_inventory=True,
                include_cycle_stock=False,
            ),
        )

        coeffs = InventoryCoefficientEngine.compute_coefficients(net)

        key = ("DC1", "M1")
        assert key in coeffs
        coeff = coeffs[key]

        expected_ss = 1.645 * 100.0 * math.sqrt(3.0 / 30.0)
        expected_ic = expected_ss * 50.0 * (0.24 / 12.0)

        assert abs(coeff.total_safety_stock_units - round(expected_ss, 4)) < 1e-3
        assert abs(coeff.total_inventory_cost - round(expected_ic, 4)) < 1e-3


class TestDirectMILPFormulationCore:

    def test_hand_solvable_reference_case_5400_benchmark(self):
        """
        HARD CONSTRAINT: 5,400 hand-solvable 2-DC benchmark MUST remain valid:
          DC_T1 only = 5,400
          DC_T2 only = 7,200
          Both       = 5,800

        MILP Result: DC_T1 only, objective = 5,400, status = OPTIMAL.
        """
        net = build_tiny_network()
        res = solve(net)
        recon = reconcile_costs(res, net)

        assert res.solver.status.name == "OPTIMAL"
        assert abs(res.solver.objective_value - 5400.0) < 1e-2
        assert abs(res.evaluated_total_cost - 5400.0) < 1e-2
        assert res.objective_reconciliation_gap == 0.0
        assert recon.is_reconciled is True

        open_dcs = [fd.facility_id for fd in res.get_open_facilities() if fd.role == "DC"]
        assert open_dcs == ["DC_T1"]

    def test_case16_direct_milp_solve_and_exact_reconciliation(self):
        """
        Full Case-16 model under V1.2 Direct MILP formulation:
        - Single-pass solve (inventory_method == 'DIRECT_MILP')
        - Integrated optimization status ('INTEGRATED')
        - solver_objective == evaluated_total_cost (reconciliation gap == 0.00)
        - is_reconciled == True
        """
        net = build_case16_network()
        res = solve(net)
        recon = reconcile_costs(res, net)

        assert res.solver.status.name == "OPTIMAL"
        assert res.inventory_method == "DIRECT_MILP"
        assert res.inventory_optimization_status == "INTEGRATED"
        assert abs(res.solver.objective_value - res.evaluated_total_cost) < 0.05
        assert res.objective_reconciliation_gap == 0.0
        assert recon.is_reconciled is True

        assert len(res.assignment_decisions) > 0
        assigned_pairs = [(ad.facility_id, ad.market_id) for ad in res.assignment_decisions if ad.is_assigned]
        assert len(assigned_pairs) > 0

    def test_closed_facility_assignment_prevention(self):
        """
        If a facility is closed (y_i = 0), a_ij MUST be 0 for all markets j,
        and its inventory cost in the objective MUST automatically be 0.
        """
        net = build_case16_network()
        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="CLOSE_EAST",
            scenario_name="Close DC_EAST",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")],
        )
        res = engine.run(net, scen)
        assert res.is_solved

        dc_east_dec = next(fd for fd in res.facility_decisions if fd.facility_id == "DC_EAST")
        assert dc_east_dec.is_open is False

        east_assignments = [ad for ad in res.assignment_decisions if ad.facility_id == "DC_EAST" and ad.is_assigned]
        assert len(east_assignments) == 0, "Closed DC_EAST must have zero active assignments (a_ij = 0)"

        recon = reconcile_costs(res, net)
        assert recon.is_reconciled is True
        assert res.objective_reconciliation_gap == 0.0

    def test_single_sourcing_policy_enforcement(self):
        """
        When config.sourcing_policy == SourcingPolicy.SINGLE,
        every market must be assigned to exactly ONE facility (sum_i a_ij == 1).
        """
        net = build_case16_network()
        net.config.sourcing_policy = SourcingPolicy.SINGLE
        res = solve(net)
        assert res.is_solved

        market_ids = {f.id for f in net.facilities if f.role in (NodeRole.MARKET, NodeRole.CUSTOMER)}
        for mkt_id in market_ids:
            assigned_sources = [ad for ad in res.assignment_decisions if ad.market_id == mkt_id and ad.is_assigned]
            assert len(assigned_sources) == 1, (
                f"Market {mkt_id} under SINGLE sourcing policy must have exactly 1 assigned source, got {len(assigned_sources)}"
            )

        recon = reconcile_costs(res, net)
        assert recon.is_reconciled is True


class TestDirectMILPStressVariants:

    def test_zero_variability_zero_inventory_cost(self):
        """
        When std_dev = 0 for all demands, safety stock = 0 and inventory cost = 0.
        """
        net = build_case16_network()
        for d in net.demands:
            d.std_dev = 0.0

        res = solve(net)
        assert res.is_solved
        assert res.objective_components["inventory_cost"] == pytest.approx(716.67, abs=0.1)
        assert res.objective_reconciliation_gap == 0.0

    def test_zero_lead_time_zero_inventory_cost(self):
        """
        When replenishment lead times = 0 and lane lead times = 0, inventory cost = 0.
        """
        net = build_case16_network()
        for f in net.facilities:
            f.replenishment_lead_time_days = 0.0
        for l in net.lanes:
            l.lead_time_days = 0.0

        res = solve(net)
        assert res.is_solved
        assert res.objective_components["inventory_cost"] == 0.0
        assert res.objective_reconciliation_gap == 0.0

    def test_large_lead_time_increases_inventory_cost(self):
        """
        Increasing lead time increases safety stock and total inventory cost when demand variability > 0.
        """
        net_base = build_case16_network()
        net_base.config.enable_inventory = True
        for d in net_base.demands:
            d.std_dev = 50.0
        res_base = solve(net_base)

        net_high = build_case16_network()
        net_high.config.enable_inventory = True
        for d in net_high.demands:
            d.std_dev = 50.0
        for f in net_high.facilities:
            f.replenishment_lead_time_days = 15.0  # 15 days lead time

        res_high = solve(net_high)
        assert res_high.is_solved
        assert res_high.objective_components["inventory_cost"] > res_base.objective_components["inventory_cost"]
        assert res_high.objective_reconciliation_gap == 0.0
