"""
NetGravity — Test Suite for 4 Critical Defensibility Patches
============================================================
1. Sensitivity Engine Parameters (distance & service_target)
2. Go/No-Go Service-Level Degradation Check
3. Additive Carbon Cost Formulation & Reconciliation
4. CapEx Budget Constraint (C12)
"""

import pytest
from netgravity.schemas.network import OptimizationConfig, SourcingPolicy
from netgravity.optimization.milp import solve
from netgravity.costs.reconciliation import reconcile_costs
from netgravity.metrics.kpis import compare_scenarios
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.schemas.scenario import Scenario, FacilityChange as ScenFacilityChange
from netgravity.sensitivity.engine import SensitivityEngine, SENSITIVITY_PARAMETERS
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


class TestPatch1SensitivityParameters:

    def test_sensitivity_parameter_registry_has_8_keys(self):
        assert len(SENSITIVITY_PARAMETERS) == 8
        assert "distance" in SENSITIVITY_PARAMETERS
        assert "service_target" in SENSITIVITY_PARAMETERS

    def test_distance_and_service_target_one_way_sweep(self):
        net = build_case16_network()
        engine = SensitivityEngine()
        res_dist = engine.one_way_sweep(net, parameter="distance", values=[0.8, 1.0, 1.2])
        assert len(res_dist.points) == 3
        res_serv = engine.one_way_sweep(net, parameter="service_target", values=[0.8, 1.0, 1.2])
        assert len(res_serv.points) == 3

    def test_distance_and_service_target_tornado_analysis(self):
        net = build_case16_network()
        engine = SensitivityEngine()
        tornado_res = engine.tornado_analysis(net, parameters=["distance", "service_target", "transport_cost"], variation_pct=0.2)
        assert len(tornado_res) == 3
        evaluated_params = [t["parameter"] for t in tornado_res]
        assert "distance" in evaluated_params
        assert "service_target" in evaluated_params


class TestPatch2GoNoGoServiceCheck:

    def test_service_degradation_triggers_no_go(self):
        net = build_case16_network()
        net.config.allow_shortage = True
        base_res = solve(net)

        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="CLOSE_ALL_DCS",
            scenario_name="Close All DCs",
            facility_changes=[
                ScenFacilityChange(facility_id="DC_EAST", action="CLOSE"),
                ScenFacilityChange(facility_id="DC_WEST", action="CLOSE"),
                ScenFacilityChange(facility_id="DC_CENTRAL", action="CLOSE"),
                ScenFacilityChange(facility_id="DC_NORTH_NEW", action="CLOSE"),
                ScenFacilityChange(facility_id="DC_SOUTH_NEW", action="CLOSE"),
            ]
        )
        scen_res = engine.run(net, scen)
        comp = compare_scenarios(base_res, scen_res, "Close All DCs")

        assert comp.go_no_go is not None
        assert comp.go_no_go.service_delta_pct < 0
        assert comp.go_no_go.go_no_go in ("NO-GO", "MARGINAL")


class TestPatch3AdditiveCarbonFormulation:

    def test_additive_carbon_cost_reconciliation(self):
        net = build_case16_network()
        net.config.enable_carbon_cost = True
        net.config.carbon_price = 10.0
        net.config.carbon_weight = 5.0
        net.config.objective_mode = "WEIGHTED_COST_CARBON"

        res = solve(net)
        recon = reconcile_costs(res, net)

        assert res.is_solved
        assert recon.is_reconciled is True
        assert recon.absolute_difference <= 0.05


class TestPatch4CapExBudgetConstraint:

    def test_capex_budget_constraint_enforced(self):
        # This test isolates the CapEx budget constraint, so V1.4 closure
        # economics are switched off. With closure cost active the Case-16
        # optimum keeps all three existing DCs open and the unconstrained
        # premise below (DC_NORTH_NEW opening) no longer arises — that is
        # correct closure behaviour, covered by TestClosureCostEconomics, but it
        # would leave the CapEx constraint itself untested here.
        net = build_case16_network()
        net.config.enable_closure_cost = False
        engine = ScenarioEngine()
        scen_close_east = Scenario(
            scenario_id="CLOSE_EAST",
            scenario_name="Close DC East",
            facility_changes=[ScenFacilityChange(facility_id="DC_EAST", action="CLOSE")]
        )

        # Unconstrained: opens DC_NORTH_NEW ($200k CapEx)
        res_unconstrained = engine.run(net, scen_close_east)
        open_unconstrained = [f.facility_id for f in res_unconstrained.facility_decisions if f.is_open]
        assert "DC_NORTH_NEW" in open_unconstrained

        # Constrained budget = $150,000 (below $200k CapEx)
        net_budget = build_case16_network()
        net_budget.config.enable_closure_cost = False
        net_budget.config.budget_capex = 150000.0
        res_budget = engine.run(net_budget, scen_close_east)
        open_budget = [f.facility_id for f in res_budget.facility_decisions if f.is_open]

        assert "DC_NORTH_NEW" not in open_budget
        assert res_budget.solver.status == "OPTIMAL"
