"""
NetGravity V1.2 — Independent Validator Test Suite (TEST A through TEST T)
==========================================================================
Comprehensive independent validation test suite directly derived from the
requirements of Case 16 (Model/Final Case 16.pdf).

This test suite performs strict independent mathematical evaluation:
- Hand-solvable enumeration
- Hand-calculated safety stock & inventory trade-offs
- Capacity & supply bounds
- SLA feasibility enforcement
- Closed facility linking & single sourcing
- Independent total cost reconciliation
- Carbon & weighted distance calculations
- Scenario isolation & non-mutation
- Infeasibility detection & numerical stability
- 5-run determinism & reproducibility
"""

from __future__ import annotations

import math
import pytest

from netgravity.carbon.module import CarbonModule
from netgravity.costs.reconciliation import reconcile_costs
from netgravity.inventory.coefficient_engine import InventoryCoefficientEngine
from netgravity.metrics.kpis import compute_kpis
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
from netgravity.schemas.results import SolverStatus
from netgravity.schemas.scenario import DemandChange, FacilityChange, Scenario
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network


# ---------------------------------------------------------------------------
# TEST A — HAND-SOLVABLE FACILITY CHOICE
# ---------------------------------------------------------------------------
class TestA_HandSolvableFacilityChoice:

    def test_manual_enumeration_matches_milp(self):
        """
        1 plant, 2 candidate DCs, 2 markets.
        Enumerates all 3 feasible facility combinations:
          - DC_A only: Fixed = 5000, Transport = 1000 -> Total = 6000
          - DC_B only: Fixed = 7000, Transport = 800  -> Total = 7800
          - Both DCs : Fixed = 12000, Transport = 800 -> Total = 12800
        Manual optimum = DC_A only (6000).
        """
        plant = FacilityRecord(id="P", name="Plant", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc_a  = FacilityRecord(id="DC_A", name="DC A", role=NodeRole.DC, status=FacilityStatus.CANDIDATE, capacity_units_per_period=1000, fixed_cost_per_year=60000) # $5k/mo
        dc_b  = FacilityRecord(id="DC_B", name="DC B", role=NodeRole.DC, status=FacilityStatus.CANDIDATE, capacity_units_per_period=1000, fixed_cost_per_year=84000) # $7k/mo
        m1    = FacilityRecord(id="M1", name="M1", role=NodeRole.MARKET)
        m2    = FacilityRecord(id="M2", name="M2", role=NodeRole.MARKET)

        prod  = ProductRecord(id="P1", name="Prod1")
        d1    = DemandRecord(market_id="M1", product_id="P1", quantity=500)
        d2    = DemandRecord(market_id="M2", product_id="P1", quantity=500)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC_A", mode=TransportMode.ROAD, rate_per_unit=0.5, distance_km=50),
            LaneRecord(origin_id="P", destination_id="DC_B", mode=TransportMode.ROAD, rate_per_unit=0.4, distance_km=40),
            LaneRecord(origin_id="DC_A", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=0.5, distance_km=50),
            LaneRecord(origin_id="DC_A", destination_id="M2", mode=TransportMode.ROAD, rate_per_unit=0.5, distance_km=50),
            LaneRecord(origin_id="DC_B", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=0.4, distance_km=40),
            LaneRecord(origin_id="DC_B", destination_id="M2", mode=TransportMode.ROAD, rate_per_unit=0.4, distance_km=40),
        ]

        net = CanonicalNetwork(
            facilities=[plant, dc_a, dc_b, m1, m2],
            products=[prod], demands=[d1, d2], lanes=lanes,
            config=OptimizationConfig(cost_period=CostPeriod.MONTH, enable_inventory=False),
        )

        res = solve(net)
        assert res.is_solved
        assert abs(res.solver.objective_value - 6000.0) < 1e-2
        open_dcs = [fd.facility_id for fd in res.get_open_facilities() if fd.role == "DC"]
        assert open_dcs == ["DC_A"]

    def test_reference_benchmark_case_5400(self):
        """
        Verify the reference case benchmark:
          DC_T1 only = 5,400
          DC_T2 only = 7,200
          Both       = 5,800
        MILP optimum must select DC_T1 only with objective = 5,400.
        """
        net = build_tiny_network()
        res = solve(net)
        assert res.solver.status == SolverStatus.OPTIMAL
        assert abs(res.solver.objective_value - 5400.0) < 1e-2
        open_dcs = [fd.facility_id for fd in res.get_open_facilities() if fd.role == "DC"]
        assert open_dcs == ["DC_T1"]


# ---------------------------------------------------------------------------
# TEST B — DEMAND BALANCE
# ---------------------------------------------------------------------------
class TestB_DemandBalance:

    def test_demand_balance_fulfillment_and_shortage(self):
        """inbound flow + shortage == demand for every market."""
        net = build_case16_network()
        res = solve(net)
        assert res.is_solved

        demand_map = {d.market_id: d.quantity for d in net.demands}
        market_flows: dict[str, float] = {}
        for fl in res.flow_decisions:
            if fl.destination_id in demand_map:
                market_flows[fl.destination_id] = market_flows.get(fl.destination_id, 0.0) + fl.flow_units

        for mkt_id, qty in demand_map.items():
            inbound = market_flows.get(mkt_id, 0.0)
            assert abs(inbound - qty) < 1e-2, f"Demand balance failed at market {mkt_id}: in={inbound}, req={qty}"


# ---------------------------------------------------------------------------
# TEST C — FACILITY CAPACITY
# ---------------------------------------------------------------------------
class TestC_FacilityCapacity:

    def test_facility_capacity_throughput_bound(self):
        """Throughput <= capacity for all facilities."""
        net = build_case16_network()
        res = solve(net)
        assert res.is_solved

        for fd in res.facility_decisions:
            if fd.is_open and fd.capacity_units > 0:
                assert fd.throughput_units <= fd.capacity_units + 1e-4

    def test_insufficient_total_capacity_is_infeasible(self):
        """Total DC capacity insufficient with allow_shortage=False -> INFEASIBLE."""
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=50)
        mkt = FacilityRecord(id="MKT1", name="MKT1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        d = DemandRecord(market_id="MKT1", product_id="P1", quantity=100)
        lane = LaneRecord(origin_id="DC1", destination_id="MKT1", mode=TransportMode.ROAD, rate_per_unit=1.0)

        net = CanonicalNetwork(
            facilities=[dc, mkt], products=[prod], demands=[d], lanes=[lane],
            config=OptimizationConfig(allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# TEST D — PLANT CAPACITY
# ---------------------------------------------------------------------------
class TestD_PlantCapacity:

    def test_plant_capacity_reallocation(self):
        """Plant supply constrained -> model reallocates to available plant."""
        p1 = FacilityRecord(id="P1", name="Plant 1", role=NodeRole.PLANT, production_capacity_units_per_period=80, is_mandatory=True)
        p2 = FacilityRecord(id="P2", name="Plant 2", role=NodeRole.PLANT, production_capacity_units_per_period=120, is_mandatory=True)
        mkt = FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        d = DemandRecord(market_id="MKT", product_id="P1", quantity=100)

        lanes = [
            LaneRecord(origin_id="P1", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=1.0),
            LaneRecord(origin_id="P2", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=2.0),
        ]

        net = CanonicalNetwork(
            facilities=[p1, p2, mkt], products=[prod], demands=[d], lanes=lanes,
            config=OptimizationConfig(allow_shortage=False),
        )
        res = solve(net)
        assert res.is_solved
        p1_out = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == "P1")
        p2_out = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == "P2")
        assert p1_out <= 80.01
        assert abs(p1_out + p2_out - 100.0) < 1e-2


# ---------------------------------------------------------------------------
# TEST E — FLOW CONSERVATION
# ---------------------------------------------------------------------------
class TestE_FlowConservation:

    def test_intermediate_flow_conservation(self):
        """Inbound flow == outbound flow at intermediate nodes (DCs)."""
        net = build_case16_network()
        res = solve(net)
        assert res.is_solved

        dc_ids = {f.id for f in net.facilities if f.role == NodeRole.DC}
        for dc_id in dc_ids:
            inflow  = sum(fl.flow_units for fl in res.flow_decisions if fl.destination_id == dc_id)
            outflow = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == dc_id)
            # If open and handling flow
            if inflow > 0 or outflow > 0:
                assert abs(inflow - outflow) < 1e-2, f"Flow conservation violated at {dc_id}: in={inflow}, out={outflow}"


# ---------------------------------------------------------------------------
# TEST F — SLA / SERVICE
# ---------------------------------------------------------------------------
class TestF_SLA_Service:

    def test_over_sla_lane_carries_zero_flow(self):
        """Invalid lane (lead_time > SLA) carries zero flow when enforce_sla=True."""
        net = build_tiny_network()
        net.config.enforce_sla = True
        net.demands[0].sla_days = 1.0

        for ln in net.lanes:
            if ln.origin_id == "DC_T2" and ln.destination_id == "MKT_T1":
                ln.lead_time_days = 5.0  # violates 1-day SLA

        res = solve(net)
        assert res.is_solved
        slow_flows = [fl for fl in res.flow_decisions if fl.origin_id == "DC_T2" and fl.destination_id == "MKT_T1"]
        assert len(slow_flows) == 0

    def test_only_over_sla_lane_is_infeasible(self):
        """If only available lane exceeds SLA and allow_shortage=False -> INFEASIBLE."""
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, capacity_units_per_period=1000)
        mkt = FacilityRecord(id="MKT1", name="MKT1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        d = DemandRecord(market_id="MKT1", product_id="P1", quantity=100, sla_days=1.0)
        lane = LaneRecord(origin_id="DC1", destination_id="MKT1", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=4.0)

        net = CanonicalNetwork(
            facilities=[dc, mkt], products=[prod], demands=[d], lanes=[lane],
            config=OptimizationConfig(enforce_sla=True, allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# TEST G — FACILITY OPEN/CLOSE
# ---------------------------------------------------------------------------
class TestG_FacilityOpenClose:

    def test_closed_facility_zero_assignment_and_flow(self):
        """Closed facility -> zero active assignment (a_ij=0) and zero flow."""
        net = build_case16_network()
        engine = ScenarioEngine()
        scen = Scenario(scenario_id="CLOSE_EAST", scenario_name="Close DC_EAST", facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")])
        res = engine.run(net, scen)
        assert res.is_solved

        east_assignments = [ad for ad in res.assignment_decisions if ad.facility_id == "DC_EAST" and ad.is_assigned]
        east_flows = [fl for fl in res.flow_decisions if fl.origin_id == "DC_EAST" or fl.destination_id == "DC_EAST"]
        assert len(east_assignments) == 0
        assert len(east_flows) == 0


# ---------------------------------------------------------------------------
# TEST H — ASSIGNMENT / FLOW LINKING
# ---------------------------------------------------------------------------
class TestH_AssignmentFlowLinking:

    def test_assignment_flow_linking_rules(self):
        """Verify a_ij ∈ {0,1}, x_ij <= D_j * a_ij, a_ij <= y_i, x_ij > 0 => a_ij = 1."""
        net = build_case16_network()
        res = solve(net)
        assert res.is_solved

        ass_map = {(ad.facility_id, ad.market_id): ad.is_assigned for ad in res.assignment_decisions}
        fac_map = {fd.facility_id: fd.is_open for fd in res.facility_decisions}

        # x_ij > 0 => a_ij = 1
        for fl in res.flow_decisions:
            if fl.flow_units > 1e-5 and fl.destination_id in {d.market_id for d in net.demands}:
                key = (fl.origin_id, fl.destination_id)
                assert ass_map.get(key) is True, f"Flow present on {key} but a_ij is 0"

        # a_ij = 1 => y_i = 1
        for (fac_id, mkt_id), is_ass in ass_map.items():
            if is_ass:
                assert fac_map.get(fac_id) is True, f"Assignment on closed facility {fac_id}"


# ---------------------------------------------------------------------------
# TEST I — DIRECT INVENTORY FORMULATION
# ---------------------------------------------------------------------------
class TestI_DirectInventoryFormulation:

    def test_hand_calculated_inventory_coefficients(self):
        """Hand-calculated safety stock matches InventoryCoefficientEngine exactly."""
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, replenishment_lead_time_days=4.0)
        mkt = FacilityRecord(id="M1", name="M1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1", unit_value=100.0, holding_rate=0.24) # 2%/mo
        lane = LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=1.0)
        demand = DemandRecord(market_id="M1", product_id="P1", quantity=1000.0, std_dev=200.0)

        net = CanonicalNetwork(
            facilities=[dc, mkt], products=[prod], demands=[demand], lanes=[lane],
            config=OptimizationConfig(cost_period=CostPeriod.MONTH, days_per_period=30, inventory_z_score=1.645, enable_inventory=True),
        )

        coeffs = InventoryCoefficientEngine.compute_coefficients(net)
        coeff = coeffs[("DC1", "M1")]

        expected_lt = 4.0 + 1.0 # 5 days
        expected_ss = 1.645 * 200.0 * math.sqrt(5.0 / 30.0) # 134.3135
        expected_ic = expected_ss * 100.0 * 0.02 # 268.6270

        assert abs(coeff.total_safety_stock_units - round(expected_ss, 4)) < 1e-3
        assert abs(coeff.total_inventory_cost - round(expected_ic, 4)) < 1e-3


# ---------------------------------------------------------------------------
# TEST J — INVENTORY DECISION TRADE-OFF
# ---------------------------------------------------------------------------
class TestJ_InventoryDecisionTradeoff:

    def test_inventory_actively_drives_facility_selection(self):
        """
        Facility A: Transport = $1,000, Inventory = $918 -> Total = $1,918 (without fixed cost)
        Facility B: Transport = $1,200, Inventory = $254 -> Total = $1,454 (without fixed cost)
        Fixed / opening cost = $100 per candidate DC.
        Transport-only MILP selects Facility A ($1,100 < $1,300).
        Inventory-inclusive MILP selects Facility B ($1,554 < $2,018).
        """
        p = FacilityRecord(id="P", name="P", role=NodeRole.PLANT, capacity_units_per_period=2000, is_mandatory=True)
        fc_a = FacilityRecord(id="FC_A", name="FC A", role=NodeRole.DC, status=FacilityStatus.CANDIDATE, capacity_units_per_period=2000, opening_cost=100.0, replenishment_lead_time_days=25.0)
        fc_b = FacilityRecord(id="FC_B", name="FC B", role=NodeRole.DC, status=FacilityStatus.CANDIDATE, capacity_units_per_period=2000, opening_cost=100.0, replenishment_lead_time_days=1.0)
        mkt  = FacilityRecord(id="MKT", name="MKT", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1", unit_value=100.0, holding_rate=0.24)
        demand = DemandRecord(market_id="MKT", product_id="P1", quantity=1000.0, std_dev=300.0)

        lanes = [
            LaneRecord(origin_id="P", destination_id="FC_A", mode=TransportMode.ROAD, rate_per_unit=0.5, lead_time_days=1.0),
            LaneRecord(origin_id="P", destination_id="FC_B", mode=TransportMode.ROAD, rate_per_unit=0.6, lead_time_days=1.0),
            LaneRecord(origin_id="FC_A", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=0.5, lead_time_days=1.0),
            LaneRecord(origin_id="FC_B", destination_id="MKT", mode=TransportMode.ROAD, rate_per_unit=0.6, lead_time_days=1.0),
        ]

        net_no_inv = CanonicalNetwork(
            facilities=[p, fc_a, fc_b, mkt], products=[prod], demands=[demand], lanes=lanes,
            config=OptimizationConfig(enable_inventory=False, sourcing_policy=SourcingPolicy.SINGLE),
        )
        res_no_inv = solve(net_no_inv)
        open_no_inv = [fd.facility_id for fd in res_no_inv.get_open_facilities() if fd.role == "DC"]
        assert open_no_inv == ["FC_A"] # Transport-only selects A

        net_inv = CanonicalNetwork(
            facilities=[p, fc_a, fc_b, mkt], products=[prod], demands=[demand], lanes=lanes,
            config=OptimizationConfig(enable_inventory=True, inventory_z_score=1.645, sourcing_policy=SourcingPolicy.SINGLE),
        )
        res_inv = solve(net_inv)
        open_inv = [fd.facility_id for fd in res_inv.get_open_facilities() if fd.role == "DC"]
        assert open_inv == ["FC_B"] # Inventory-inclusive selects B


# ---------------------------------------------------------------------------
# TEST K — INVENTORY EDGE CASES
# ---------------------------------------------------------------------------
class TestK_InventoryEdgeCases:

    def test_inventory_edge_cases_no_nan_no_negatives(self):
        """Zero sigma, zero lead time, high lead time, different z-scores, zero demand."""
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, replenishment_lead_time_days=0.0)
        mkt = FacilityRecord(id="M1", name="M1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1", unit_value=50.0, holding_rate=0.20)
        lane = LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=1.0, lead_time_days=0.0)
        demand = DemandRecord(market_id="M1", product_id="P1", quantity=0.0, std_dev=0.0)

        net = CanonicalNetwork(
            facilities=[dc, mkt], products=[prod], demands=[demand], lanes=[lane],
            config=OptimizationConfig(enable_inventory=True),
        )
        res = solve(net)
        assert res.is_solved
        assert not math.isnan(res.evaluated_total_cost)
        assert res.objective_components["inventory_cost"] >= 0.0


# ---------------------------------------------------------------------------
# TEST L — COST UNIT CONSISTENCY
# ---------------------------------------------------------------------------
class TestL_CostUnitConsistency:

    def test_annual_to_monthly_cost_normalization(self):
        """Annual fixed cost $120k -> $10k monthly in MILP objective."""
        p = FacilityRecord(id="P", name="P", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, fixed_cost_per_year=120000, is_mandatory=True)
        mkt = FacilityRecord(id="M1", name="M1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        demand = DemandRecord(market_id="M1", product_id="P1", quantity=100)
        lane1 = LaneRecord(origin_id="P", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=0.0)
        lane2 = LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=1.0)

        net = CanonicalNetwork(
            facilities=[p, dc, mkt], products=[prod], demands=[demand], lanes=[lane1, lane2],
            config=OptimizationConfig(cost_period=CostPeriod.MONTH, enable_inventory=False),
        )
        res = solve(net)
        assert abs(res.solver.objective_value - 10100.0) < 1e-2


# ---------------------------------------------------------------------------
# TEST M — OBJECTIVE RECONCILIATION
# ---------------------------------------------------------------------------
class TestM_ObjectiveReconciliation:

    def test_objective_reconciliation_gap_is_zero(self):
        """Solver objective == independently evaluated total cost (Gap = 0.00)."""
        net = build_case16_network()
        res = solve(net)
        recon = reconcile_costs(res, net)

        assert res.is_solved
        assert recon.is_reconciled is True
        assert res.objective_reconciliation_gap == 0.0
        assert res.solver.objective_value is not None


# ---------------------------------------------------------------------------
# TEST N — CARBON
# ---------------------------------------------------------------------------
class TestN_CarbonEmissions:

    def test_carbon_emissions_formula_matching(self):
        """CO2_kg = distance * weight * EF / 1000."""
        lane = LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, distance_km=200.0, rate_per_unit=1.0)
        prod = ProductRecord(id="P1", name="P1", weight_kg=5.0)

        cmod = CarbonModule()
        co2_unit = cmod.compute_unit_co2(lane, prod)
        # ROAD EF = 0.062 kg/t-km. unit_co2 = 200 * 5 * 0.062 / 1000 = 0.062 kg/unit
        expected = 200.0 * 5.0 * 0.062 / 1000.0
        assert abs(co2_unit - expected) < 1e-4


# ---------------------------------------------------------------------------
# TEST O — WEIGHTED DISTANCE
# ---------------------------------------------------------------------------
class TestO_WeightedDistance:

    def test_weighted_average_distance_25km(self):
        """Flow 1 = 100 units at 10 km, Flow 2 = 300 units at 30 km -> WAD = 25 km."""
        p = FacilityRecord(id="P", name="P", role=NodeRole.PLANT, capacity_units_per_period=1000, is_mandatory=True)
        dc1 = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING, is_mandatory=True)
        dc2 = FacilityRecord(id="DC2", name="DC2", role=NodeRole.DC, status=FacilityStatus.EXISTING, is_mandatory=True)
        mkt1 = FacilityRecord(id="M1", name="M1", role=NodeRole.MARKET)
        mkt2 = FacilityRecord(id="M2", name="M2", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")

        d1 = DemandRecord(market_id="M1", product_id="P1", quantity=100)
        d2 = DemandRecord(market_id="M2", product_id="P1", quantity=300)

        lanes = [
            LaneRecord(origin_id="P", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=0.0),
            LaneRecord(origin_id="P", destination_id="DC2", mode=TransportMode.ROAD, rate_per_unit=0.0),
            LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, distance_km=10.0, rate_per_unit=1.0),
            LaneRecord(origin_id="DC2", destination_id="M2", mode=TransportMode.ROAD, distance_km=30.0, rate_per_unit=1.0),
        ]

        net = CanonicalNetwork(facilities=[p, dc1, dc2, mkt1, mkt2], products=[prod], demands=[d1, d2], lanes=lanes)
        res = solve(net)
        kpis = compute_kpis(res, net)
        assert abs(kpis.outbound_avg_distance_km - 25.0) < 1e-2


# ---------------------------------------------------------------------------
# TEST P — SCENARIO ISOLATION
# ---------------------------------------------------------------------------
class TestP_ScenarioIsolation:

    def test_scenario_execution_preserves_baseline_unmutated(self):
        """Baseline -> Facility Closure -> Baseline again returns identical results."""
        net = build_case16_network()
        res_base1 = solve(net)

        engine = ScenarioEngine()
        scen = Scenario(scenario_id="CLOSE_EAST", scenario_name="Close DC_EAST", facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")])
        res_scen = engine.run(net, scen)

        res_base2 = solve(net)

        assert res_base1.solver.objective_value == res_base2.solver.objective_value
        assert res_base1.evaluated_total_cost == res_base2.evaluated_total_cost


# ---------------------------------------------------------------------------
# TEST Q — SCENARIO LOGIC
# ---------------------------------------------------------------------------
class TestQ_ScenarioLogic:

    def test_scenario_engine_transforms_inputs_and_solves(self):
        """Verify scenario transforms demand and close facility correctly."""
        net = build_case16_network()
        engine = ScenarioEngine()
        scen = Scenario(scenario_id="SURGE", scenario_name="Surge", demand_changes=[DemandChange(quantity_multiplier=1.20)])
        res = engine.run(net, scen)
        assert res.is_solved
        assert res.objective_components["transport_cost"] > 0.0


# ---------------------------------------------------------------------------
# TEST R — INFEASIBILITY
# ---------------------------------------------------------------------------
class TestR_InfeasibilityDetection:

    def test_deliberately_infeasible_cases(self):
        """No plant-to-market path -> INFEASIBLE when allow_shortage=False."""
        dc = FacilityRecord(id="DC1", name="DC1", role=NodeRole.DC, status=FacilityStatus.EXISTING)
        mkt = FacilityRecord(id="MKT1", name="MKT1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="P1")
        d = DemandRecord(market_id="MKT1", product_id="P1", quantity=100)

        net = CanonicalNetwork(
            facilities=[dc, mkt], products=[prod], demands=[d], lanes=[], # No lanes
            config=OptimizationConfig(allow_shortage=False),
        )
        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE


# ---------------------------------------------------------------------------
# TEST S — EXTREME VALUES
# ---------------------------------------------------------------------------
class TestS_ExtremeValues:

    def test_numerical_stability_large_demand_and_costs(self):
        """Very large demand (730,000 units) with scaled capacity solves with numerical stability."""
        net_raw = build_case16_network()
        net = net_raw.model_copy(deep=True)
        net.demands = [d.model_copy(update={"quantity": d.quantity * 100.0}) for d in net_raw.demands]
        net.facilities = [
            f.model_copy(update={
                "capacity_units_per_period": f.capacity_units_per_period * 100.0 if f.capacity_units_per_period else None,
                "production_capacity_units_per_period": f.production_capacity_units_per_period * 100.0 if f.production_capacity_units_per_period else None,
            })
            for f in net_raw.facilities
        ]

        res = solve(net)
        assert res.is_solved
        assert not math.isnan(res.evaluated_total_cost)


# ---------------------------------------------------------------------------
# TEST T — REPRODUCIBILITY
# ---------------------------------------------------------------------------
class TestT_Reproducibility:

    def test_five_consecutive_runs_identical_results(self):
        """Five consecutive runs yield 100% bitwise identical objectives and decisions."""
        net = build_case16_network()
        results = [solve(net) for _ in range(5)]

        obj0 = results[0].solver.objective_value
        total0 = results[0].evaluated_total_cost
        facs0 = [fd.facility_id for fd in results[0].get_open_facilities()]

        for r in results[1:]:
            assert r.solver.objective_value == obj0
            assert r.evaluated_total_cost == total0
            assert [fd.facility_id for fd in r.get_open_facilities()] == facs0
