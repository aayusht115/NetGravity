"""
Multi-period demand, and what a single-period model is allowed to do with it.

The MILP has no period index on its flow variables. Before this, a demand table
stating the same market and product in two periods produced two constraints
with the same name and PuLP refused to build the model:

    pulp.constants.PulpError: overlapping constraint names: demand_MKT_P1

Twelve months of demand is the shape most planning data arrives in, so that was
an unexplained solver crash on an ordinary workbook.
"""

from __future__ import annotations

import pytest

from netgravity.optimization.milp import milp_solve
from netgravity.optimization.periods import (
    collapse_to_representative_period,
    summarise_periods,
)
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


def build(demands, policy="REPRESENTATIVE_MEAN", dc_capacity=150.0):
    """PLANT -> DC -> MKT, with the DC sized so the policy's effect is visible."""
    return CanonicalNetwork(
        network_id="MULTI_PERIOD",
        facilities=[
            FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                           status=FacilityStatus.EXISTING,
                           capacity_units_per_period=9999,
                           is_mandatory=True, is_closable=False),
            FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                           status=FacilityStatus.EXISTING,
                           capacity_units_per_period=dc_capacity,
                           fixed_cost_per_year=1200.0),
            FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                           status=FacilityStatus.EXISTING, is_closable=False),
        ],
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=10.0)],
        demands=demands,
        lanes=[
            LaneRecord(origin_id="PLANT", destination_id="DC",
                       mode=TransportMode.ROAD, rate_per_unit=1.0,
                       distance_km=10.0, lead_time_days=1.0),
            LaneRecord(origin_id="DC", destination_id="MKT",
                       mode=TransportMode.ROAD, rate_per_unit=2.0,
                       distance_km=10.0, lead_time_days=1.0),
        ],
        config=OptimizationConfig(
            enable_inventory=False, enforce_sla=False, enable_carbon_cost=False,
            allow_shortage=True, verbose=False, multi_period_policy=policy),
    )


def rows(*quantities):
    return [DemandRecord(market_id="MKT", product_id="P1", period=i + 1, quantity=q)
            for i, q in enumerate(quantities)]


def served(result) -> float:
    return sum(f.flow_units for f in result.flow_decisions
               if f.destination_id == "MKT")


# ===========================================================================

class TestSinglePeriodIsUntouched:

    def test_a_one_period_network_is_returned_unchanged(self):
        network = build(rows(100.0))
        collapsed, report = collapse_to_representative_period(network)
        assert collapsed is network, "the ordinary case must pay nothing"
        assert report["collapsed"] is False
        assert report["n_periods"] == 1

    def test_it_still_solves_exactly_as_before(self):
        result = milp_solve(build(rows(100.0)), None)
        assert served(result) == pytest.approx(100.0)
        assert result.period_report["collapsed"] is False


class TestMultiPeriodNoLongerCrashes:

    def test_three_periods_build_and_solve(self):
        """This raised PulpError before the fix."""
        result = milp_solve(build(rows(100.0, 100.0, 160.0)), None)
        assert result.solver.status is not None
        assert served(result) > 0

    def test_two_rows_for_the_same_market_and_product_do_not_collide(self):
        result = milp_solve(build(rows(50.0, 50.0)), None)
        assert served(result) == pytest.approx(50.0), \
            "the mean of two identical periods is that period"


class TestPolicies:

    def test_representative_mean_is_the_default_and_averages(self):
        result = milp_solve(build(rows(100.0, 100.0, 160.0)), None)
        assert result.period_report["policy"] == "REPRESENTATIVE_MEAN"
        assert served(result) == pytest.approx(120.0)

    def test_peak_sizes_for_the_worst_period(self):
        result = milp_solve(build(rows(100.0, 100.0, 160.0), policy="PEAK"), None)
        # 160 wanted against 150 of capacity: the peak breach is visible, which
        # is the whole point of asking for PEAK.
        assert served(result) == pytest.approx(150.0)
        assert result.period_report["peak_total"] == pytest.approx(160.0)

    def test_sum_adds_the_periods_together(self):
        network = build(rows(100.0, 100.0, 160.0), policy="SUM", dc_capacity=9999.0)
        result = milp_solve(network, None)
        assert served(result) == pytest.approx(360.0)

    def test_an_unknown_policy_falls_back_to_the_default_rather_than_failing(self):
        _, report = collapse_to_representative_period(
            build(rows(10.0, 20.0)), policy="NONSENSE")
        assert report["policy"] == "REPRESENTATIVE_MEAN"


class TestNothingIsSilent:

    def test_the_result_states_what_it_covers(self):
        result = milp_solve(build(rows(100.0, 100.0, 160.0)), None)
        report = result.period_report
        assert report["collapsed"] is True
        assert report["n_periods"] == 3
        assert report["total_by_period"] == {"1": 100.0, "2": 100.0, "3": 160.0}
        assert report["peak_period"] == 3
        assert "3 demand periods" in report["note"]
        assert "does not size for" in report["note"]

    def test_the_solver_warnings_carry_it_too(self):
        result = milp_solve(build(rows(100.0, 160.0)), None)
        assert any("demand periods" in w for w in result.solver.warnings), \
            "a reader of the solver metadata alone must still be told"

    def test_the_peak_is_reported_even_under_the_mean_policy(self):
        """Averaging must not hide the month the network cannot carry."""
        result = milp_solve(build(rows(50.0, 50.0, 500.0)), None)
        assert result.period_report["mean_total"] == pytest.approx(200.0)
        assert result.period_report["peak_total"] == pytest.approx(500.0)


class TestGroupingIsFaithful:

    def test_rows_with_different_service_levels_are_not_merged(self):
        """
        Two rows for the same market and product under different SLAs are
        different commitments. Averaging across them would invent a service
        level the client never stated.
        """
        demands = [
            DemandRecord(market_id="MKT", product_id="P1", period=1,
                         quantity=100.0, sla_days=1.0),
            DemandRecord(market_id="MKT", product_id="P1", period=2,
                         quantity=100.0, sla_days=3.0),
        ]
        collapsed, report = collapse_to_representative_period(build(demands))
        assert report["collapsed"] is True
        assert len(collapsed.demands) == 2
        assert {d.sla_days for d in collapsed.demands} == {1.0, 3.0}

    def test_variability_travels_with_the_quantity(self):
        demands = [
            DemandRecord(market_id="MKT", product_id="P1", period=1,
                         quantity=100.0, std_dev=10.0),
            DemandRecord(market_id="MKT", product_id="P1", period=2,
                         quantity=200.0, std_dev=30.0),
        ]
        collapsed, _ = collapse_to_representative_period(build(demands))
        assert collapsed.demands[0].quantity == pytest.approx(150.0)
        assert collapsed.demands[0].std_dev == pytest.approx(20.0)

    def test_peak_takes_the_peak_periods_own_variability(self):
        demands = [
            DemandRecord(market_id="MKT", product_id="P1", period=1,
                         quantity=100.0, std_dev=10.0),
            DemandRecord(market_id="MKT", product_id="P1", period=2,
                         quantity=200.0, std_dev=30.0),
        ]
        collapsed, _ = collapse_to_representative_period(build(demands), policy="PEAK")
        assert collapsed.demands[0].quantity == pytest.approx(200.0)
        assert collapsed.demands[0].std_dev == pytest.approx(30.0)

    def test_summarise_reports_every_period(self):
        summary = summarise_periods(build(rows(10.0, 20.0, 30.0)))
        assert summary["n_periods"] == 3
        assert summary["mean_total"] == pytest.approx(20.0)
        assert summary["peak_total"] == pytest.approx(30.0)
        assert summary["peak_period"] == 3
