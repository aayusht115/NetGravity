"""
NetGravity V1.2.1 — SLA Feasibility Regression Test Suite
===========================================================
Regression tests verifying that over-SLA facility-market lanes are strictly excluded
from the feasible arc set when enforce_sla=True, and that no service-violating solutions
are returned as OPTIMAL.
"""

from __future__ import annotations

import pytest

from netgravity.costs.reconciliation import reconcile_costs
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
from netgravity.schemas.scenario import DemandChange, FacilityChange, Scenario
from netgravity.schemas.results import SolverStatus
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network


class TestSLAFeasibilityHardening:

    def test_over_sla_lane_is_excluded(self):
        """
        Regression Test 1:
        Over-SLA lane MUST be excluded from candidate arcs and flow decisions when enforce_sla=True.
        """
        net = build_tiny_network()
        net.config.enforce_sla = True
        # Set market MKT_T1 demand SLA to 1 day
        net.demands[0].sla_days = 1.0

        # Set lane DC_T2 -> MKT_T1 lead_time_days to 3 days (violates SLA of 1 day)
        for ln in net.lanes:
            if ln.origin_id == "DC_T2" and ln.destination_id == "MKT_T1":
                ln.lead_time_days = 3.0

        res = solve(net)
        assert res.is_solved

        # Verify no flow uses DC_T2 -> MKT_T1
        slow_flows = [
            fl for fl in res.flow_decisions
            if fl.origin_id == "DC_T2" and fl.destination_id == "MKT_T1"
        ]
        assert len(slow_flows) == 0, "Over-SLA lane DC_T2->MKT_T1 must be excluded and have zero flow"

    def test_under_sla_lane_remains_available(self):
        """
        Regression Test 2:
        Under-SLA lane (lead_time_days <= sla_days) remains available and can carry flow.
        """
        net = build_tiny_network()
        net.config.enforce_sla = True
        # Set market MKT_T1 SLA to 3 days
        net.demands[0].sla_days = 3.0

        # Set lane DC_T1 -> MKT_T1 lead_time_days to 2 days (satisfies SLA)
        for ln in net.lanes:
            if ln.origin_id == "DC_T1" and ln.destination_id == "MKT_T1":
                ln.lead_time_days = 2.0

        res = solve(net)
        assert res.is_solved

        # Flow should be present from DC_T1 -> MKT_T1
        valid_flows = [
            fl for fl in res.flow_decisions
            if fl.origin_id == "DC_T1" and fl.destination_id == "MKT_T1"
        ]
        assert len(valid_flows) > 0, "Under-SLA lane DC_T1->MKT_T1 must remain available for flow"
        assert all(fl.lead_time_days <= 3.0 for fl in valid_flows)

    def test_close_east_scenario_has_no_sla_violation(self):
        """
        Regression Test 3:
        Close-East scenario under Case-16 with enforce_sla=True has zero SLA violations.
        """
        net = build_case16_network()
        net.config.enforce_sla = True
        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="CLOSE_EAST",
            scenario_name="Close DC_EAST",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")],
        )
        res = engine.run(net, scen)
        assert res.is_solved

        market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
        dest_facs = {f.id: f for f in net.facilities}
        demand_sla = {d.market_id: d.sla_days for d in net.demands if d.sla_days is not None}

        # Check all flow decisions to market nodes satisfy SLA
        for fl in res.flow_decisions:
            dest_node = dest_facs.get(fl.destination_id)
            if dest_node and dest_node.role in market_roles:
                sla = demand_sla.get(fl.destination_id)
                if sla is not None:
                    assert fl.lead_time_days <= sla, (
                        f"SLA violation detected on flow {fl.origin_id}->{fl.destination_id}: "
                        f"lead_time={fl.lead_time_days} > sla={sla}"
                    )

    def test_demand_surge_20pct_scenario_has_no_sla_violation(self):
        """
        Regression Test 4:
        Demand +20% scenario under Case-16 with enforce_sla=True has zero SLA violations.
        """
        net = build_case16_network()
        net.config.enforce_sla = True
        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="DEMAND_SURGE_20PCT",
            scenario_name="Demand Surge +20%",
            demand_changes=[DemandChange(market_id="*", product_id="*", factor=1.20)],
        )
        res = engine.run(net, scen)
        assert res.is_solved

        market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
        dest_facs = {f.id: f for f in net.facilities}
        demand_sla = {d.market_id: d.sla_days for d in net.demands if d.sla_days is not None}

        for fl in res.flow_decisions:
            dest_node = dest_facs.get(fl.destination_id)
            if dest_node and dest_node.role in market_roles:
                sla = demand_sla.get(fl.destination_id)
                if sla is not None:
                    assert fl.lead_time_days <= sla, (
                        f"SLA violation detected on flow {fl.origin_id}->{fl.destination_id}: "
                        f"lead_time={fl.lead_time_days} > sla={sla}"
                    )

    def test_no_sla_feasible_network_reports_infeasible(self):
        """
        Regression Test 5:
        If no SLA-feasible network exists (e.g. all inbound lanes exceed market.sla_days)
        and allow_shortage=False, the solver MUST report INFEASIBLE (not OPTIMAL).
        """
        dc = FacilityRecord(
            id="DC1", name="DC 1", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
        )
        mkt = FacilityRecord(id="MKT1", name="Market 1", role=NodeRole.MARKET)
        prod = ProductRecord(id="P1", name="Widget")
        demand = DemandRecord(market_id="MKT1", product_id="P1", quantity=100.0, sla_days=1.0) # SLA = 1 day

        lane = LaneRecord(
            origin_id="DC1", destination_id="MKT1", mode=TransportMode.ROAD,
            rate_per_unit=1.0, distance_km=500.0, lead_time_days=5.0, # Lead time = 5 days > 1 day SLA
        )

        net = CanonicalNetwork(
            facilities=[dc, mkt],
            products=[prod],
            demands=[demand],
            lanes=[lane],
            config=OptimizationConfig(enforce_sla=True, allow_shortage=False),
        )

        res = solve(net)
        assert res.solver.status == SolverStatus.INFEASIBLE, (
            f"Expected INFEASIBLE when all lanes exceed SLA, got {res.solver.status}"
        )
