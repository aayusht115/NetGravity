"""
NetGravity — Scenario & Reconciliation Integrity Test Suite
============================================================
Regression tests verifying:
1. Warehouse Move recalculation & network economics propagation.
2. Add-DC scenario inclusion, optimizer selection, and baseline isolation.
3. Scenario overrides mutating canonical inputs.
4. Independent objective & KPI reconciliation matching solver outputs.
5. Scenario isolation and multi-scenario chains.
"""

from __future__ import annotations

import pytest
from netgravity.costs.reconciliation import reconcile_costs, reconcile_kpis_and_objective
from netgravity.optimization.milp import solve as milp_solve
from netgravity.scenarios.engine import ScenarioEngine, haversine_distance
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
from netgravity.schemas.scenario import (
    CostChange,
    DemandChange,
    FacilityChange,
    LaneChange,
    ParameterOverride,
    Scenario,
    ScenarioType,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network


class TestWarehouseMove:
    """Test 1: Warehouse Move recalculations."""

    def test_warehouse_move_recalculates_distances_and_economics(self):
        """Moving an active facility to a materially different location changes distances and network metrics."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Move DC_EAST (active in baseline) to Scottish Highlands (lat=58.0, lon=-4.0)
        move_scenario = Scenario(
            scenario_id="MOVE_DC_EAST",
            scenario_name="Move DC East to Highlands",
            facility_changes=[
                FacilityChange(
                    facility_id="DC_EAST",
                    action="MOVE",
                    latitude=58.0,
                    longitude=-4.0,
                )
            ]
        )

        scen_res = engine.run(network, move_scenario)

        # Baseline network remains completely unchanged
        dc_east_base = next(f for f in network.facilities if f.id == "DC_EAST")
        assert dc_east_base.latitude == 51.5
        assert dc_east_base.longitude == 0.1

        # Economics or metrics change under scenario
        assert scen_res.is_solved
        assert scen_res.kpis is not None
        assert (scen_res.kpis.total_cost != base_res.kpis.total_cost or
                scen_res.kpis.transport_cost != base_res.kpis.transport_cost or
                scen_res.kpis.weighted_avg_distance_km != base_res.kpis.weighted_avg_distance_km)

    def test_warehouse_move_back_restores_original_metrics(self):
        """Move facility -> Move back -> verify original metrics restored."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Move away
        move_scen = Scenario(
            scenario_id="MOVE_AWAY",
            scenario_name="Move Away",
            facility_changes=[
                FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=58.0, longitude=-4.0)
            ]
        )
        res_away = engine.run(network, move_scen)

        # Move back to original coordinates (lat=51.5, lon=-2.6)
        move_back_scen = Scenario(
            scenario_id="MOVE_BACK",
            scenario_name="Move Back",
            facility_changes=[
                FacilityChange(facility_id="DC_WEST", action="MOVE", latitude=51.5, longitude=-2.6)
            ]
        )
        res_back = engine.run(network, move_back_scen)

        assert abs(res_back.kpis.total_cost - base_res.kpis.total_cost) < 1.0
        assert abs(res_back.kpis.weighted_avg_distance_km - base_res.kpis.weighted_avg_distance_km) < 0.1

    def test_repeated_identical_move_is_deterministic(self):
        """Executing identical move scenarios twice yields identical results."""
        network = build_case16_network()
        engine = ScenarioEngine()

        scen = Scenario(
            scenario_id="DETERMINISTIC_MOVE",
            scenario_name="Move DC East",
            facility_changes=[
                FacilityChange(facility_id="DC_EAST", action="MOVE", latitude=53.0, longitude=-1.0)
            ]
        )

        res1 = engine.run(network, scen)
        res2 = engine.run(network, scen)

        assert res1.kpis.total_cost == res2.kpis.total_cost
        assert res1.kpis.weighted_avg_distance_km == res2.kpis.weighted_avg_distance_km


class TestAddDCScenario:
    """Test 2: ADD_FACILITY scenario functionality."""

    def test_add_dc_appears_in_scenario_network_and_can_be_selected(self):
        """A new candidate DC added via ADD_FACILITY can be selected when economically feasible."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Create cheap strategic super-DC
        new_dc = FacilityRecord(
            id="DC_SUPER",
            name="Super Distribution Centre",
            role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            latitude=52.0,
            longitude=-1.5,
            fixed_cost_per_year=10_000,   # extremely low fixed cost
            handling_cost_per_unit=0.10,
            capacity_units_per_period=10_000,
            is_mandatory=False,
            is_closable=True,
            opening_cost=0.0,
        )

        add_scen = Scenario(
            scenario_id="ADD_SUPER_DC",
            scenario_name="Add Super DC",
            facility_changes=[
                FacilityChange(
                    facility_id="DC_SUPER",
                    action="ADD_FACILITY",
                    new_facility=new_dc,
                )
            ]
        )

        res = engine.run(network, add_scen)

        # Baseline network is unchanged
        assert not any(f.id == "DC_SUPER" for f in network.facilities)

        # New DC is selected by optimizer
        open_fac_ids = {fd.facility_id for fd in res.facility_decisions if fd.is_open}
        assert "DC_SUPER" in open_fac_ids
        assert res.kpis.total_cost < base_res.kpis.total_cost

    def test_invalid_new_dc_data_fails_clearly(self):
        """Adding a facility without new_facility payload or unknown facility ID fails fast."""
        network = build_case16_network()
        engine = ScenarioEngine()

        scen = Scenario(
            scenario_id="INVALID_ADD",
            scenario_name="Invalid Add",
            facility_changes=[
                FacilityChange(facility_id="DC_NONEXISTENT", action="CLOSE")
            ]
        )

        with pytest.raises(ValueError, match="not found in canonical network"):
            engine.run(network, scen)


class TestScenarioOverrides:
    """Test 3: Parameter override mutations on canonical inputs."""

    def test_demand_override(self):
        """Demand override mutates scenario copy and updates solution/KPIs."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))
        base_demand_mkt_a = next(d for d in network.demands if d.market_id == "MKT_A" and d.product_id == "P001").quantity

        scen = Scenario(
            scenario_id="DEMAND_OVERRIDE",
            scenario_name="Scale Demand",
            parameter_overrides=[
                ParameterOverride(path="demands.MKT_A.P001.quantity", operation="MULTIPLY", value=2.0)
            ]
        )

        res = engine.run(network, scen)

        # Original baseline network demand is unchanged
        assert next(d for d in network.demands if d.market_id == "MKT_A" and d.product_id == "P001").quantity == base_demand_mkt_a
        assert res.kpis.total_demand > base_res.kpis.total_demand

    def test_facility_capacity_override(self):
        """Capacity override mutates scenario facility capacity."""
        network = build_case16_network()
        engine = ScenarioEngine()

        scen = Scenario(
            scenario_id="CAP_OVERRIDE",
            scenario_name="Capacity Override",
            parameter_overrides=[
                ParameterOverride(path="facilities.DC_CENTRAL.capacity", operation="SET", value=100.0)
            ]
        )

        res = engine.run(network, scen)
        fac_dec = next(fd for fd in res.facility_decisions if fd.facility_id == "DC_CENTRAL")
        assert fac_dec.capacity_units == 100.0

    def test_transport_rate_override(self):
        """Transport rate override changes transport cost."""
        network = build_case16_network()
        engine = ScenarioEngine()

        base_res = engine.run(network, Scenario(scenario_id="BASE", scenario_name="Base"))

        # Override active lane (DC_EAST -> MKT_F)
        scen = Scenario(
            scenario_id="RATE_OVERRIDE",
            scenario_name="Rate Increase",
            parameter_overrides=[
                ParameterOverride(path="lanes.DC_EAST.MKT_F.ROAD.rate_per_unit", operation="MULTIPLY", value=5.0)
            ]
        )

        res = engine.run(network, scen)
        assert res.kpis.transport_cost != base_res.kpis.transport_cost or res.kpis.total_cost != base_res.kpis.total_cost


class TestObjectiveAndKPIReconciliation:
    """Test 4: 100% Independent objective and KPI reconciliation."""

    def test_case16_baseline_full_reconciliation(self):
        """Verify baseline Case16 model achieves 100% independent reconciliation."""
        network = build_case16_network()
        result = milp_solve(network)

        rep = reconcile_kpis_and_objective(result, network)
        if not rep.all_reconciled:
            failed_metrics = {k: v for k, v in rep.metric_details.items() if not v.is_reconciled}
            pytest.fail(f"Baseline reconciliation failed on metrics: {failed_metrics}")

        assert rep.all_reconciled
        assert rep.cost_reconciliation.is_reconciled

    def test_carbon_objective_reconciliation(self):
        """When carbon cost is enabled in objective, objective reconciliation includes carbon cost."""
        config = OptimizationConfig(enable_carbon_cost=True, carbon_price=50.0)
        network = build_case16_network(config=config)
        result = milp_solve(network, config=config)

        rep = reconcile_kpis_and_objective(result, network, config=config)
        if not rep.all_reconciled:
            failed_metrics = {k: v for k, v in rep.metric_details.items() if not v.is_reconciled}
            pytest.fail(f"Carbon objective reconciliation failed on metrics: {failed_metrics}")

        assert rep.all_reconciled
        assert rep.cost_reconciliation.is_reconciled


class TestScenarioIsolation:
    """Test 5: Baseline network isolation."""

    def test_baseline_isolation_after_multiple_scenarios(self):
        """Running multiple scenarios does not mutate the original baseline network or results."""
        network = build_case16_network()
        engine = ScenarioEngine()

        orig_hash = network.compute_data_version()
        base_res1 = engine.run(network, Scenario(scenario_id="BASE1", scenario_name="Base 1"))

        scen1 = Scenario(
            scenario_id="SCEN_1",
            scenario_name="Scen 1",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")]
        )
        scen2 = Scenario(
            scenario_id="SCEN_2",
            scenario_name="Scen 2",
            demand_changes=[DemandChange(quantity_multiplier=1.5)]
        )

        engine.run(network, scen1)
        engine.run(network, scen2)

        base_res2 = engine.run(network, Scenario(scenario_id="BASE2", scenario_name="Base 2"))

        assert network.compute_data_version() == orig_hash
        assert base_res1.kpis.total_cost == base_res2.kpis.total_cost
