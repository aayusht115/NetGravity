"""
NetGravity — Scenario Engine Tests
====================================
Tests for scenario application and comparison.
"""

import pytest

from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network
from netgravity.optimization.milp import solve
from netgravity.optimization.baseline import evaluate_baseline
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.metrics.kpis import compute_kpis, compare_scenarios
from netgravity.schemas.scenario import (
    DemandChange, FacilityChange, CostChange, LaneChange, Scenario, ScenarioType
)
from netgravity.schemas.results import SolverStatus


engine = ScenarioEngine()


class TestCloseFacilityScenario:
    """Test: close a facility and verify routing adapts."""

    def test_close_dc_central(self):
        network = build_case16_network()
        scenario = Scenario(
            scenario_id   = "CLOSE_DC_CENTRAL",
            scenario_name = "Close Central DC",
            scenario_type = ScenarioType.CLOSE_FACILITY,
            facility_changes = [
                FacilityChange(facility_id="DC_CENTRAL", action="CLOSE")
            ],
            config_overrides = {"allow_shortage": True},
        )
        result = engine.run(network, scenario)
        assert result.scenario_id == "CLOSE_DC_CENTRAL"
        # DC_CENTRAL should have zero throughput
        dc_fd = next((fd for fd in result.facility_decisions if fd.facility_id == "DC_CENTRAL"), None)
        if dc_fd:
            assert dc_fd.throughput_units < 0.01, "Closed DC_CENTRAL has non-zero throughput"

    def test_close_dc_cost_increases_or_decreases(self):
        """Closing a DC changes cost — direction depends on network config."""
        network  = build_case16_network()
        baseline = evaluate_baseline(network)
        baseline_kpis = compute_kpis(baseline, network)

        scenario = Scenario(
            scenario_id   = "CLOSE_DC_EAST",
            scenario_name = "Close Eastern DC",
            scenario_type = ScenarioType.CLOSE_FACILITY,
            facility_changes = [FacilityChange(facility_id="DC_EAST", action="CLOSE")],
            config_overrides = {"allow_shortage": True},
        )
        result = engine.run(network, scenario)
        # Just verify it runs and returns a cost
        assert result.solver.objective_value is not None or not result.is_solved


class TestDemandChangeScenario:
    """Test: demand change modifies flows proportionally."""

    def test_demand_surge_20pct(self):
        network = build_case16_network()
        scenario = Scenario(
            scenario_id   = "DEMAND_SURGE",
            scenario_name = "Demand +20%",
            scenario_type = ScenarioType.CHANGE_DEMAND,
            demand_changes = [DemandChange(quantity_multiplier=1.2)],
        )
        result = engine.run(network, scenario)
        assert result.is_solved
        # Total served should be ~20% more than baseline demand
        kpis = compute_kpis(result, network)
        base_demand = sum(d.quantity for d in network.demands)
        expected_demand = base_demand * 1.2
        assert kpis.total_served >= expected_demand * 0.99, (
            f"Demand surge not served: {kpis.total_served:.0f} < {expected_demand:.0f}"
        )

    def test_demand_drop_50pct(self):
        network = build_case16_network()
        scenario = Scenario(
            scenario_id   = "DEMAND_DROP",
            scenario_name = "Demand -50%",
            scenario_type = ScenarioType.CHANGE_DEMAND,
            demand_changes = [DemandChange(quantity_multiplier=0.5)],
        )
        result = engine.run(network, scenario)
        assert result.is_solved


class TestCostChangeScenario:
    """Test: transport cost changes affect objective."""

    def test_transport_cost_increase_raises_objective(self):
        network  = build_case16_network()
        baseline = solve(network)
        base_obj = baseline.solver.objective_value or 0

        scenario = Scenario(
            scenario_id   = "COST_SHOCK",
            scenario_name = "Transport Cost +30%",
            scenario_type = ScenarioType.CHANGE_TRANSPORT_COST,
            cost_changes  = [CostChange(rate_multiplier=1.3)],
        )
        result   = engine.run(network, scenario)
        new_obj  = result.solver.objective_value or 0
        # Higher transport cost should increase (or equal) objective
        assert new_obj >= base_obj * 0.99, (
            f"Cost shock did not raise objective: {base_obj:.0f} → {new_obj:.0f}"
        )


class TestCapacityChangeScenario:
    """Test: capacity changes are reflected in MILP."""

    def test_capacity_reduction_may_cause_shortage(self):
        network = build_case16_network()
        scenario = Scenario(
            scenario_id   = "CAP_REDUCE",
            scenario_name = "Capacity -50%",
            scenario_type = ScenarioType.CHANGE_CAPACITY,
            facility_changes = [
                FacilityChange(facility_id=fid, action="SET_CAPACITY", capacity_multiplier=0.5)
                for fid in ("DC_CENTRAL", "DC_EAST", "DC_WEST")
            ],
            config_overrides = {"allow_shortage": True},
        )
        result = engine.run(network, scenario)
        assert result is not None   # should not crash

    def test_capacity_expansion(self):
        network = build_case16_network()
        scenario = Scenario(
            scenario_id   = "CAP_EXPAND",
            scenario_name = "DC_CENTRAL Capacity +50%",
            scenario_type = ScenarioType.EXPAND,
            facility_changes = [
                FacilityChange(facility_id="DC_CENTRAL", action="SET_CAPACITY", capacity_multiplier=1.5)
            ],
        )
        result = engine.run(network, scenario)
        assert result.is_solved


class TestScenarioComparison:
    """Test: scenario comparison produces correct deltas."""

    def test_scenario_comparison_returns_deltas(self):
        network  = build_case16_network()
        baseline = solve(network)
        baseline.scenario_id = "BASELINE"
        baseline.kpis = compute_kpis(baseline, network)

        scenario_obj = Scenario(
            scenario_id   = "COST_CHANGE",
            scenario_name = "Transport +15%",
            cost_changes  = [CostChange(rate_multiplier=1.15)],
        )
        scenario_result = engine.run(network, scenario_obj)
        comparison = compare_scenarios(baseline, scenario_result, "Transport +15%")

        assert comparison.baseline_id == "BASELINE"
        assert len(comparison.kpi_deltas) > 0

    def test_library_run(self):
        network = build_case16_network()
        scenarios = [
            Scenario(scenario_id="S1", scenario_name="Demand+10%",
                     demand_changes=[DemandChange(quantity_multiplier=1.1)]),
            Scenario(scenario_id="S2", scenario_name="Demand+20%",
                     demand_changes=[DemandChange(quantity_multiplier=1.2)]),
        ]
        results = engine.run_library(network, scenarios)
        assert "S1" in results and "S2" in results
        assert results["S1"].is_solved
        assert results["S2"].is_solved

    def test_base_network_not_mutated(self):
        """After running a scenario, the base network is unchanged."""
        network = build_case16_network()
        original_demand = sum(d.quantity for d in network.demands)

        scenario = Scenario(
            scenario_id   = "MUT_TEST",
            scenario_name = "Mutation Test",
            demand_changes = [DemandChange(quantity_multiplier=2.0)],
        )
        engine.run(network, scenario)

        # Base network demand unchanged
        new_demand = sum(d.quantity for d in network.demands)
        assert abs(original_demand - new_demand) < 0.01, (
            f"Base network mutated! Before: {original_demand}, After: {new_demand}"
        )
