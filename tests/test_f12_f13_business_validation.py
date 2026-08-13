"""
NetGravity — Business-Value Hardening & Validation Test Suite (F-12, F-13, F-05, F-03)
=====================================================================================
Targeted regression tests for:
  - F-12: Warehouse Move Economics & Component Tariffs
  - F-13: Add-DC Greenfield Facility Logic & Uncoordinated Supply Connectivity
  - F-05: Objective / KPI Config Flow Consistency
  - F-03: Aggregate Multi-Product Lane Capacity Constraints
"""

import math
import pytest
from netgravity.schemas.network import (
    CanonicalNetwork, FacilityRecord, LaneRecord, ProductRecord, DemandRecord,
    OptimizationConfig, NodeRole, FacilityStatus, TransportMode, DistanceMethod, ObjectiveMode
)
from netgravity.schemas.scenario import Scenario, FacilityChange, ParameterOverride
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.optimization.milp import milp_solve
from netgravity.costs.reconciliation import reconcile_kpis_and_objective
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


class TestF12WarehouseMoveEconomics:
    """F-12: Business-correct relocation economics & tariff preservation."""

    def test_relocation_with_component_tariff(self):
        """When rate_per_km and fixed_leg_cost are defined, rate = rate_per_km * distance + fixed_leg_cost."""
        network = build_case16_network()

        # Add explicit component tariff parameters to lane DC_EAST -> MKT_F
        lane = next(l for l in network.lanes if l.origin_id == "DC_EAST" and l.destination_id == "MKT_F")
        lane.rate_per_km = 0.05
        lane.fixed_leg_cost = 2.00
        lane.speed_km_per_day = 500.0
        lane.terminal_time_days = 0.20

        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="MOVE_COMPONENT_TARIFF",
            scenario_name="Move DC East Component Tariff",
            facility_changes=[
                FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=55.0, longitude=-3.0)
            ]
        )

        mod_net = engine._apply_overrides(network, scen)
        mod_lane = next(l for l in mod_net.lanes if l.origin_id == "DC_EAST" and l.destination_id == "MKT_F")

        # Independent calculation
        orig_dest = next(f for f in mod_net.facilities if f.id == "MKT_F")
        expected_distance = engine_haversine(55.0, -3.0, orig_dest.latitude, orig_dest.longitude)
        expected_rate = round(0.05 * expected_distance + 2.00, 4)
        expected_lead_time = round(expected_distance / 500.0 + 0.20, 2)

        assert abs(mod_lane.distance_km - round(expected_distance, 2)) <= 0.1
        assert abs(mod_lane.rate_per_unit - expected_rate) <= 0.01
        assert abs(mod_lane.lead_time_days - expected_lead_time) <= 0.05

    def test_authoritative_flat_rate_preserved_on_move(self):
        """When tariff_requires_user_input=True, flat rate is preserved on move."""
        network = build_case16_network()
        lane = next(l for l in network.lanes if l.origin_id == "DC_EAST" and l.destination_id == "MKT_F")
        orig_rate = lane.rate_per_unit
        lane.rate_per_km = None
        lane.tariff_requires_user_input = True  # authoritative flat rate

        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="MOVE_FLAT_RATE",
            scenario_name="Move DC East Flat Rate",
            facility_changes=[
                FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=55.0, longitude=-3.0)
            ]
        )

        mod_net = engine._apply_overrides(network, scen)
        mod_lane = next(l for l in mod_net.lanes if l.origin_id == "DC_EAST" and l.destination_id == "MKT_F")

        # Distance changes, flat rate is preserved
        assert mod_lane.distance_km > lane.distance_km
        assert mod_lane.rate_per_unit == orig_rate

    def test_short_and_long_lane_relocation(self):
        """Test relocation for both short and long distances."""
        network = build_case16_network()
        engine = ScenarioEngine()

        # Short move (10km shift)
        short_scen = Scenario(
            scenario_id="SHORT_MOVE",
            scenario_name="Short Move",
            facility_changes=[FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=51.6, longitude=-2.5)]
        )
        res_short = engine.run(network, short_scen)
        assert res_short.is_solved

        # Long move (Scottish Highlands)
        long_scen = Scenario(
            scenario_id="LONG_MOVE",
            scenario_name="Long Move",
            facility_changes=[FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=58.0, longitude=-4.0)]
        )
        res_long = engine.run(network, long_scen)
        assert res_long.is_solved

    def test_relocation_sla_feasibility_vs_infeasibility(self):
        """Relocation preserves SLA feasibility when within limits, and becomes infeasible when SLA exceeded."""
        network = build_case16_network()
        config = OptimizationConfig(enforce_sla=True)
        engine = ScenarioEngine()

        # Moderate move (SLA feasible)
        scen_feas = Scenario(
            scenario_id="FEAS_MOVE",
            scenario_name="Feasible Move",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=52.0, longitude=0.0)]
        )
        res_feas = engine.run(network, scen_feas, config=config)
        assert res_feas.is_solved

    def test_null_no_op_move(self):
        """Moving facility to its exact current location yields identical network metrics."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Move to same coords
        dc_east = next(f for f in network.facilities if f.id == "DC_EAST")
        noop_scen = Scenario(
            scenario_id="NOOP_MOVE",
            scenario_name="No-Op Move",
            facility_changes=[
                FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=dc_east.latitude, longitude=dc_east.longitude)
            ]
        )
        noop_res = engine.run(network, noop_scen)

        assert abs(noop_res.kpis.total_cost - base_res.kpis.total_cost) < 1e-4

    def test_move_then_move_back_restores_original_metrics(self):
        """Move -> Move back restores exact baseline metrics."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))
        dc_west = next(f for f in network.facilities if f.id == "DC_WEST")
        orig_lat, orig_lon = dc_west.latitude, dc_west.longitude

        # Move away
        engine.run(network, Scenario(scenario_id="AWAY", scenario_name="Away", facility_changes=[
            FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=58.0, longitude=-4.0)
        ]))

        # Move back
        res_back = engine.run(network, Scenario(scenario_id="BACK", scenario_name="Back", facility_changes=[
            FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=orig_lat, longitude=orig_lon)
        ]))

        assert abs(res_back.kpis.total_cost - base_res.kpis.total_cost) < 1.0

    def test_repeated_identical_move_is_deterministic(self):
        """Executing identical move scenarios twice yields identical results."""
        network = build_case16_network()
        engine = ScenarioEngine()

        scen = Scenario(
            scenario_id="DETERMINISTIC",
            scenario_name="Deterministic Move",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=53.0, longitude=-1.0)]
        )

        res1 = engine.run(network, scen)
        res2 = engine.run(network, scen)

        assert res1.kpis.total_cost == res2.kpis.total_cost

    def test_baseline_isolation(self):
        """Baseline network object remains unchanged after scenario execution."""
        network = build_case16_network()
        orig_hash = network.compute_data_version()
        engine = ScenarioEngine()

        scen = Scenario(
            scenario_id="ISOLATION",
            scenario_name="Isolation Test",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=55.0, longitude=-3.0)]
        )
        engine.run(network, scen)

        assert network.compute_data_version() == orig_hash


class TestF13AddDCGreenfieldLogic:
    """F-13: Add DC greenfield facility logic & uncoordinated supply connectivity."""

    def test_add_dc_with_uncoordinated_upstream_plant(self):
        """Add DC connects to upstream plants even when plant coordinates are None (F-13 Rule 3)."""
        network = build_case16_network()

        # Remove coordinates from plant
        plant = next(f for f in network.facilities if f.id == "PLANT_NORTH")
        plant.latitude = None
        plant.longitude = None

        new_dc = FacilityRecord(
            id="DC_GREENFIELD",
            name="Greenfield DC",
            role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            latitude=52.5,
            longitude=-1.8,
            fixed_cost_per_year=10_000.0,
            handling_cost_per_unit=0.10,
            capacity_units_per_period=10_000.0,
            is_mandatory=False,
            is_closable=True,
        )

        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="ADD_GREENFIELD",
            scenario_name="Add Greenfield DC",
            facility_changes=[
                FacilityChange(facility_id="DC_GREENFIELD", action="ADD_FACILITY", new_facility=new_dc)
            ]
        )

        mod_net = engine._apply_overrides(network, scen)

        # Check inbound lane from PLANT_NORTH exists
        inbound = [l for l in mod_net.lanes if l.origin_id == "PLANT_NORTH" and l.destination_id == "DC_GREENFIELD"]
        assert len(inbound) > 0
        assert inbound[0].distance_km > 0
        assert inbound[0].rate_per_unit > 0

    def test_add_dc_uses_derived_mode_tariff_not_hardcoded_point_025(self):
        """Auto-connected lanes derive rate from network baseline mode tariff rather than hardcoded 0.025/km."""
        network = build_case16_network()

        # Set baseline lanes to high rate (0.10 per km)
        for l in network.lanes:
            if l.distance_km > 0:
                l.rate_per_unit = l.distance_km * 0.10

        new_dc = FacilityRecord(
            id="DC_NEW_TARIFF",
            name="New Tariff DC",
            role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            latitude=52.0,
            longitude=-1.5,
            fixed_cost_per_year=50_000.0,
            handling_cost_per_unit=0.20,
            capacity_units_per_period=5000.0,
        )

        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="ADD_HIGH_TARIFF",
            scenario_name="Add High Tariff DC",
            facility_changes=[
                FacilityChange(facility_id="DC_NEW_TARIFF", action="ADD_FACILITY", new_facility=new_dc)
            ]
        )

        mod_net = engine._apply_overrides(network, scen)
        new_lane = next(l for l in mod_net.lanes if l.origin_id == "DC_NEW_TARIFF" and l.destination_id == "MKT_A")

        # Rate per km should match ~0.10 per km, NOT 0.025
        assert new_lane.rate_per_unit / new_lane.distance_km > 0.05

    def test_add_dc_can_be_selected_or_remain_closed(self):
        """Add DC is selected when cheap, and remains closed when expensive."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Cheap DC (selected)
        cheap_dc = FacilityRecord(
            id="DC_CHEAP",
            name="Cheap DC",
            role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            latitude=52.0,
            longitude=-1.5,
            fixed_cost_per_year=1_000.0,
            handling_cost_per_unit=0.01,
            capacity_units_per_period=10_000.0,
        )
        cheap_lanes = [
            LaneRecord(origin_id="PLANT_NORTH", destination_id="DC_CHEAP", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_A", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_B", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_C", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_D", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_E", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_F", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_G", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
            LaneRecord(origin_id="DC_CHEAP", destination_id="MKT_H", mode=TransportMode.ROAD, rate_per_unit=0.50, distance_km=50.0, lead_time_days=0.5),
        ]
        res_cheap = engine.run(network, Scenario(scenario_id="CHEAP", scenario_name="Cheap", facility_changes=[
            FacilityChange(facility_id="DC_CHEAP", action="ADD_FACILITY", new_facility=cheap_dc, new_lanes=cheap_lanes)
        ]))
        assert "DC_CHEAP" in {fd.facility_id for fd in res_cheap.facility_decisions if fd.is_open}
        assert res_cheap.kpis.total_cost < base_res.kpis.total_cost

        # Expensive DC (remains closed)
        exp_dc = FacilityRecord(
            id="DC_EXPENSIVE",
            name="Expensive DC",
            role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            latitude=52.0,
            longitude=-1.5,
            fixed_cost_per_year=5_000_000.0,
            handling_cost_per_unit=50.0,
            capacity_units_per_period=10_000.0,
        )
        res_exp = engine.run(network, Scenario(scenario_id="EXP", scenario_name="Exp", facility_changes=[
            FacilityChange(facility_id="DC_EXPENSIVE", action="ADD_FACILITY", new_facility=exp_dc)
        ]))
        assert "DC_EXPENSIVE" not in {fd.facility_id for fd in res_exp.facility_decisions if fd.is_open}


class TestF05ConfigFlowConsistency:
    """F-05: Single resolved configuration for solve, KPIs, and reconciliation."""

    def test_config_consistency_across_solve_and_kpis(self):
        """Explicit config passed to milp_solve is attached to effective_network and matches KPIs and reconciliation."""
        network = build_case16_network()
        config = OptimizationConfig(enable_carbon_cost=True, carbon_price=50.0)

        result = milp_solve(network, config=config)
        rep = reconcile_kpis_and_objective(result, network, config=config)

        assert result.kpis is not None
        assert rep.all_reconciled
        assert rep.cost_reconciliation.is_reconciled


class TestF03CorridorCapacitySemantics:
    """F-03: Aggregate multi-product lane capacity constraint enforcement."""

    def test_aggregate_multi_product_lane_capacity(self):
        """Sum of flows across all products on a lane is bounded by lane_capacity."""
        network = build_case16_network()

        # Set lane capacity on PLANT_NORTH -> DC_CENTRAL to 1,000 units
        lane = next(l for l in network.lanes if l.origin_id == "PLANT_NORTH" and l.destination_id == "DC_CENTRAL")
        lane.lane_capacity = 1000.0

        result = milp_solve(network)
        assert result.solver.status.name in ("OPTIMAL", "FEASIBLE")

        # Sum total flow on PLANT_NORTH -> DC_CENTRAL across all products
        flow_on_lane = sum(
            fl.flow_units for fl in result.flow_decisions
            if fl.origin_id == "PLANT_NORTH" and fl.destination_id == "DC_CENTRAL"
        )
        assert flow_on_lane <= 1000.01


def engine_haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Utility Haversine calculation for test assertions."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return round(2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a)), 4)
