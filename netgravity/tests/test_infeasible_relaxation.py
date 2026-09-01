"""
An infeasible network still has to answer.
==========================================

A client's own network frequently cannot serve all of its demand within its own
service levels. The strict model proves that and stops, and the KPI layer then
correctly reports every metric as INFEASIBLE with no value — which is true, and
useless: the planner still needs to know which facilities carry what, what the
served volume costs, and exactly how much demand is stranded.

`OptimizationConfig.relax_to_shortage_when_infeasible` lets the engine re-solve
once with unmet demand permitted and priced, and return THAT plan marked as a
relaxation. These tests pin the three things that make it safe:

  * it is off by default, so a caller that asked for a fully-served plan still
    gets the infeasible answer;
  * the relaxed result is unmistakable — `solve_relaxation` is set and
    `unserved_demand` is non-zero;
  * the shortage penalty, which is a solver device rather than a price anyone
    pays, stays out of `business_network_cost`.
"""

from __future__ import annotations

import asyncio

import pytest

from netgravity.orchestrator.engines.deterministic import OptimizationClient
from netgravity.orchestrator.exceptions import SolverInfeasibleError
from netgravity.orchestrator.metrics.registry import KPIRegistry
from netgravity.orchestrator.schemas.kpi import KPIStatus
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    OptimizationMode,
    ProductRecord,
)


class _Ctx:
    """The two attributes `KPIRegistry` reads, and nothing else."""

    execution_id = "exec-relaxed"
    baseline_snapshot_id = "snap-relaxed"
    scenario_id = None

    def __init__(self, state=None):
        self.network_states = {"optimization.solve": state} if state else {}
        self.unavailable_evidence = {}
        self.escalations = []


def _unservable_network(**config_kw) -> CanonicalNetwork:
    """
    One market, one DC, one plant. The market's only SLA-eligible lane carries
    far less than it needs, so no plan can serve it in full — the shape the
    client workbook has, in miniature.
    """
    facilities = [
        FacilityRecord(id="P1", name="Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING,
                       latitude=19.0, longitude=72.9,
                       capacity_units_per_period=10000,
                       production_capacity_units_per_period=10000),
        FacilityRecord(id="D1", name="DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       latitude=28.7, longitude=77.1,
                       capacity_units_per_period=10000,
                       fixed_cost_per_year=1_200_000),
        FacilityRecord(id="M1", name="Market", role=NodeRole.MARKET,
                       latitude=28.7, longitude=77.1),
    ]
    lanes = [
        LaneRecord(origin_id="P1", destination_id="D1", rate_per_unit=10.0,
                   distance_km=1400, lead_time_days=2.0, lane_capacity=10000),
        # The only lane into the market, and it is far too small.
        LaneRecord(origin_id="D1", destination_id="M1", rate_per_unit=4.0,
                   distance_km=20, lead_time_days=0.5, lane_capacity=1000),
    ]
    demands = [DemandRecord(market_id="M1", product_id="PA", quantity=5000,
                            sla_days=2.0)]
    return CanonicalNetwork(
        network_id="unservable",
        facilities=facilities,
        products=[ProductRecord(id="PA", name="Product A", unit_value=100.0)],
        demands=demands,
        lanes=lanes,
        config=OptimizationConfig(
            optimization_mode=OptimizationMode.ACTUAL_AS_IS_EVALUATION,
            **config_kw,
        ),
    )


def _solve(network):
    return asyncio.run(OptimizationClient().solve_result(network))


class TestRelaxationIsOptIn:

    def test_default_still_raises_infeasible(self):
        """Nothing changes for a caller that did not ask for the relaxation."""
        network = _unservable_network()
        assert network.config.relax_to_shortage_when_infeasible is False
        with pytest.raises(SolverInfeasibleError):
            _solve(network)

    def test_a_servable_network_is_unaffected(self):
        """The relaxation is dead code on a network that solves as posed."""
        network = _unservable_network(relax_to_shortage_when_infeasible=True)
        network.lanes[1].lane_capacity = 10000
        state = _solve(network)
        assert state.solve_relaxation is None
        assert state.demand.unserved_demand == pytest.approx(0.0)
        assert state.demand.demand_fill_rate == pytest.approx(1.0)


class TestRelaxedResultIsMarked:

    @pytest.fixture(scope="class")
    def state(self):
        return _solve(_unservable_network(relax_to_shortage_when_infeasible=True))

    def test_the_result_says_it_is_a_relaxation(self, state):
        assert state.solve_relaxation is not None
        assert state.solve_relaxation["solve_relaxation"] == "SHORTAGE_PERMITTED"
        assert state.solve_relaxation["strict_solve_status"] == "INFEASIBLE"

    def test_the_stranded_demand_is_reported(self, state):
        # Demand 5,000 against a 1,000-unit lane: 4,000 cannot be served.
        assert state.demand.unserved_demand == pytest.approx(4000.0)
        assert state.demand.served_demand == pytest.approx(1000.0)
        assert state.demand.demand_fill_rate == pytest.approx(0.2)

    def test_the_notional_penalty_is_not_a_business_cost(self, state):
        """A million rupees a unit is how the solver ranks shortages, not a price."""
        assert state.costs.shortage_penalty_cost > 0
        assert state.costs.business_network_cost < state.costs.shortage_penalty_cost
        assert state.costs.solver_objective > state.costs.business_network_cost

    def test_the_result_is_feasible_for_the_model_it_solved(self, state):
        assert state.is_feasible
        assert state.solver_status.value != "INFEASIBLE"


class TestKPIsFromARelaxedSolve:

    @pytest.fixture(scope="class")
    def kpis(self):
        state = _solve(_unservable_network(relax_to_shortage_when_infeasible=True))
        return KPIRegistry().network_kpis(_Ctx(state))

    def test_every_metric_carries_a_value(self, kpis):
        missing = [k for k, v in kpis.items()
                   if v.status is not KPIStatus.VALID or v.value is None]
        assert missing == []

    def test_the_cost_components_are_exposed(self, kpis):
        for metric in ("facility_cost", "transport_cost", "handling_cost",
                       "inventory_cost", "carbon_cost"):
            assert metric in kpis, metric

    def test_the_components_reconcile_with_the_total(self, kpis):
        parts = sum(kpis[m].value for m in (
            "facility_cost", "opening_cost", "closure_cost", "transport_cost",
            "handling_cost", "inventory_cost", "carbon_cost"))
        assert parts == pytest.approx(kpis["business_network_cost"].value, rel=1e-6)

    def test_every_metric_says_it_came_from_a_relaxation(self, kpis):
        for metric_id, result in kpis.items():
            assert result.metadata.get("solve_relaxation") == "SHORTAGE_PERMITTED", metric_id
            assert result.metadata.get("unserved_demand") == pytest.approx(4000.0)

    def test_the_shortage_penalty_is_flagged_notional(self, kpis):
        assert "notional" in kpis["shortage_penalty_cost"].metadata
        assert "notional" not in kpis["business_network_cost"].metadata


class TestFlowKPIs:

    def test_solved_lane_volumes_are_exposed(self):
        state = _solve(_unservable_network(relax_to_shortage_when_infeasible=True))
        flows = KPIRegistry().flow_kpis(_Ctx(state))
        assert flows, "the solve moved units but reported no flows"
        outbound = next(f for f in flows
                        if f["origin_id"] == "D1" and f["destination_id"] == "M1")
        assert outbound["flow_units"] == pytest.approx(1000.0)
        assert outbound["transport_cost"] == pytest.approx(1000.0 * 4.0)

    def test_no_state_yields_no_flows(self):
        assert KPIRegistry().flow_kpis(_Ctx()) == []
