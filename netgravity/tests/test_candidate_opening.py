"""
Does the optimiser actually open a candidate warehouse when demand grows?

This exists because a live demo raised the opposite suspicion: a +50% demand
scenario came back with strain rather than a newly opened site, and it was not
clear whether the mechanism worked at all, whether the dataset had a candidate
in it, or whether something silently excluded candidates from the decision.

The answers, pinned here so they cannot quietly change:

  * At +50% the solver does NOT open a candidate, and that is correct — the
    existing DCs still hold the demand, and opening a site costs more than it
    saves. "It didn't open a warehouse" is an answer, not a failure.
  * At +75% existing capacity is genuinely short, and the solver opens one on
    its own. This is the behaviour the demo could not demonstrate.
  * Past the point where the REACHABLE network can serve the demand at all,
    the solve comes back INFEASIBLE with every KPI zeroed — no shortfall, no
    diagnosis, and in particular no statement that more capacity is needed.
    That is what a demo sees, and it is a reporting gap rather than a solver
    one: the same network with shortage allowed opens a candidate and reports
    exactly how much demand it could not serve.

The fixture is used rather than a client dataset because it is the only network
in the repository that genuinely carries CANDIDATE facilities with lanes.
"""

import pytest

from netgravity.optimization.milp import solve
from netgravity.schemas.network import CanonicalNetwork, FacilityStatus
from netgravity.schemas.results import SolverStatus
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


def grow(network: CanonicalNetwork, factor: float) -> CanonicalNetwork:
    """Scale every demand row, leaving the rest of the network untouched."""
    return network.model_copy(update={
        "demands": [d.model_copy(update={"quantity": d.quantity * factor})
                    for d in network.demands],
    })


def opened_candidates(network: CanonicalNetwork, result) -> list:
    decisions = {d.facility_id: d for d in result.facility_decisions}
    return sorted(
        f.id for f in network.facilities
        if f.status == FacilityStatus.CANDIDATE
        and decisions.get(f.id) is not None
        and decisions[f.id].is_open
    )


@pytest.fixture(scope="module")
def base() -> CanonicalNetwork:
    return build_case16_network()


class TestCandidateFacilitiesAreRealOptions:

    def test_baseline_leaves_candidates_shut(self, base):
        """Nothing is opened when nothing needs to be."""
        result = solve(base)
        assert result.solver.status == SolverStatus.OPTIMAL
        assert opened_candidates(base, result) == []
        assert result.kpis.unmet_demand == 0

    def test_fifty_percent_growth_still_fits_the_existing_network(self, base):
        """
        The demo case. No candidate opens, and that is the right answer.

        Asserted together with the utilisation it reaches, because the two facts
        belong to each other: the network absorbs the growth by running its
        existing sites to their limit, which is precisely the thing a planner
        needs told to them.
        """
        network = grow(base, 1.5)
        result = solve(network)
        assert result.solver.status == SolverStatus.OPTIMAL
        assert opened_candidates(network, result) == []
        assert result.kpis.unmet_demand == 0
        assert result.kpis.max_utilization_pct >= 99.0

    def test_growth_past_existing_capacity_opens_a_candidate(self, base):
        """
        The behaviour the whole feature depends on: the solver adds a site.

        At +75% the existing DCs total 12,500 units/period against 12,775 of
        demand, so the network cannot hold it without help, and the optimiser
        opens one of the two proposed sites of its own accord.
        """
        network = grow(base, 1.75)
        result = solve(network)
        assert result.solver.status == SolverStatus.OPTIMAL
        assert opened_candidates(network, result), (
            "The solver left every candidate shut on a network whose existing "
            "capacity cannot serve the demand. Either candidates are being "
            "excluded from the decision or they carry no usable lanes."
        )
        assert result.kpis.unmet_demand == 0

    def test_an_opened_candidate_carries_flow_and_is_reported(self, base):
        """An opened site is a working one, not just a flipped bit."""
        network = grow(base, 1.75)
        result = solve(network)
        decisions = {d.facility_id: d for d in result.facility_decisions}
        for fid in opened_candidates(network, result):
            decision = decisions[fid]
            assert decision.throughput_units > 0
            assert decision.utilization_pct > 0
            # The peak the facility panel reads comes from this same decision.
            assert decision.peak_utilization_pct is not None
            assert decision.peak_utilization_pct >= decision.utilization_pct


class TestUnservableDemandIsReportedRatherThanZeroed:
    """
    Demand can outgrow what the network can physically reach. What the product
    says when that happens is a product decision, and this records the two
    behaviours so a change to either is deliberate.
    """

    def test_unservable_demand_returns_infeasible_with_no_diagnosis(self, base):
        """
        Current behaviour, recorded rather than endorsed.

        `allow_shortage=False` asks the solver to serve everything, so when it
        cannot it reports INFEASIBLE — and every KPI comes back zero. A screen
        rendering this shows an empty network, not "you are short of capacity".
        """
        network = grow(base, 2.0)
        result = solve(network)
        assert result.solver.status == SolverStatus.INFEASIBLE
        assert result.kpis.total_cost == 0
        assert result.kpis.unmet_demand == 0  # not "no shortfall" — "not computed"

    def test_the_same_network_diagnoses_itself_when_shortage_is_allowed(self, base):
        """
        The identical demand, asked as "serve what you can" instead of "serve
        everything", opens a candidate AND states the shortfall in units.
        """
        network = grow(base, 2.0)
        network = network.model_copy(update={
            "config": network.config.model_copy(update={"allow_shortage": True})})
        result = solve(network)
        assert result.solver.status == SolverStatus.OPTIMAL
        assert opened_candidates(network, result)
        assert result.kpis.unmet_demand > 0
