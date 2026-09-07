"""
The warehouse read: peak against average, ranked, sized, compared.

WHAT THESE PROTECT
------------------
The feature exists because a horizon average cannot answer the question a
multi-period model was built to answer. A DC at 50% for the year and 95% in one
month is comfortable by the average and out of room in March, and every screen
and every briefing in this product read the average. So the properties worth
pinning are the ones that go wrong quietly:

  * the peak and the average are DIFFERENT numbers and both survive to the
    reader (`TestPeakAgainstAverage`);
  * a growth rate is never invented — no default, no zero, no "assume 20%"
    (`TestTheGrowthRateIsAnInput`);
  * absent stock is absent and modelled zero is zero (`TestStockAbsentVsZero`);
  * nothing here re-derives a figure another engine owns
    (`TestItComputesNothingItDoesNotOwn`);
  * the briefing cannot contradict the card above it
    (`TestTheNarrativeReadsThePeak`).

The MILP is real throughout. Nothing here mocks a solve: the whole point is
what the solver's own per-period output means once it is read properly.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from netgravity.metrics.contracts import build_network_state_result
from netgravity.optimization.milp import milp_solve
from netgravity.orchestrator.metrics import warehouse_deep_dive as wdd
from netgravity.orchestrator.schemas.kpi import KPIStatus
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

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
JS = REPO_ROOT / "app" / "frontend" / "js"

#: A seasonal year. Period 3 is more than twice any other, which is what makes
#: the average and the peak different questions rather than different roundings.
SEASON = [400, 420, 950, 430, 410, 405]


def _network(*, periods=SEASON, dc_north_capacity=1000.0, plant_capacity=2000.0,
             inventory=False, with_candidate=True):
    """PLANT → {DC_N, DC_S, (DC_NEW)} → {MKT_N, MKT_S}, each part squeezable."""
    facilities = [
        FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING, region="North",
                       capacity_units_per_period=plant_capacity,
                       is_mandatory=True, is_closable=False,
                       fixed_cost_per_year=6000.0),
        FacilityRecord(id="DC_N", name="North DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, region="North",
                       capacity_units_per_period=dc_north_capacity,
                       storage_capacity_units=900.0,
                       handling_cost_per_unit=0.5, fixed_cost_per_year=2400.0,
                       is_closable=False),
        FacilityRecord(id="DC_S", name="South DC", role=NodeRole.WAREHOUSE,
                       status=FacilityStatus.EXISTING, region="South",
                       capacity_units_per_period=900.0,
                       storage_capacity_units=900.0,
                       handling_cost_per_unit=0.9, fixed_cost_per_year=3600.0,
                       is_closable=False),
        FacilityRecord(id="MKT_N", name="North Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, region="North",
                       is_closable=False),
        FacilityRecord(id="MKT_S", name="South Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, region="South",
                       is_closable=False),
    ]
    if with_candidate:
        facilities.insert(3, FacilityRecord(
            id="DC_NEW", name="Proposed East DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE, region="East",
            capacity_units_per_period=900.0, opening_cost=50_000.0,
            fixed_cost_per_year=1200.0))

    demands = []
    for index, quantity in enumerate(periods):
        demands.append(DemandRecord(market_id="MKT_N", product_id="P1",
                                    period=index + 1, quantity=quantity))
        demands.append(DemandRecord(market_id="MKT_S", product_id="P1",
                                    period=index + 1, quantity=quantity * 0.5))

    dcs = ["DC_N", "DC_S"] + (["DC_NEW"] if with_candidate else [])
    lanes = [LaneRecord(origin_id="PLANT", destination_id=d,
                        mode=TransportMode.ROAD, rate_per_unit=1.0,
                        distance_km=100.0, lead_time_days=1.0) for d in dcs]
    lanes += [LaneRecord(origin_id=o, destination_id=m, mode=TransportMode.ROAD,
                         rate_per_unit=r, distance_km=50.0, lead_time_days=1.0)
              for o, m, r in (("DC_N", "MKT_N", 2.0), ("DC_N", "MKT_S", 6.0),
                              ("DC_S", "MKT_S", 2.0), ("DC_S", "MKT_N", 6.0))]
    if with_candidate:
        lanes += [LaneRecord(origin_id="DC_NEW", destination_id=m,
                             mode=TransportMode.ROAD, rate_per_unit=3.0,
                             distance_km=50.0, lead_time_days=1.0)
                  for m in ("MKT_N", "MKT_S")]

    return CanonicalNetwork(
        network_id="WDD", facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", unit_value=10.0,
                                category="Ambient")],
        demands=demands, lanes=lanes,
        config=OptimizationConfig(enable_inventory=inventory, enforce_sla=False,
                                  enable_carbon_cost=False, allow_shortage=True,
                                  verbose=False,
                                  multi_period_policy="FULL_HORIZON"),
    )


def _solve(network):
    result = milp_solve(network, network.config)
    return result, build_network_state_result(result, network, network.config)


@pytest.fixture(scope="module")
def seasonal():
    _, state = _solve(_network())
    return state


@pytest.fixture(scope="module")
def report(seasonal):
    return wdd.build_warehouse_deep_dive(seasonal)


def _row(report_or_rows, facility_id):
    rows = getattr(report_or_rows, "health_kpis", report_or_rows)
    return next(r for r in rows if r.facility_id == facility_id)


# ---------------------------------------------------------------------------
# The finding the feature exists for
# ---------------------------------------------------------------------------

class TestPeakAgainstAverage:

    def test_the_two_readings_genuinely_differ(self, report):
        """
        If these were the same number this whole feature would be decoration.

        North DC carries the seasonal peak: comfortably below the threshold on
        average, at it in period 3.
        """
        north = _row(report, "DC_N")
        assert north.avg_utilization_pct < wdd.OVER_UTILISED_PCT
        assert north.peak_utilization_pct >= wdd.OVER_UTILISED_PCT
        # And by a wide margin, not a rounding.
        assert north.peak_utilization_pct > north.avg_utilization_pct * 1.5

    def test_the_worst_period_is_named(self, report):
        """"Tight sometimes" is not actionable; "tight in period 3" is."""
        assert _row(report, "DC_N").peak_period == "3"

    def test_a_site_that_shipped_nothing_has_no_worst_period(self, report):
        """
        The per-period series exists for a closed site too, all zeros, and
        `max` returns the first of them — which would print a specific month
        beside a volume of nothing.
        """
        candidate = _row(report, "DC_NEW")
        assert candidate.is_open is False
        assert candidate.peak_throughput_units == 0.0
        assert candidate.peak_period is None

    def test_the_band_says_which_kind_of_absence_a_closed_site_is(self, report):
        """
        Not "healthy at 0%". A site the plan does not use has no utilisation to
        be healthy about, and calling it healthy is the exact failure
        `test_warehouse_planning_ui.py` records against the facility panel.
        """
        assert _row(report, "DC_NEW").health_band == "NOT_OPERATING"

    def test_the_tight_site_is_banded_and_counted(self, report):
        north = _row(report, "DC_N")
        assert north.health_band == "TIGHT"
        assert north.is_bottleneck is True
        assert north.bottleneck_periods_count == 1
        assert north.periods_observed == len(SEASON)
        assert report.n_bottlenecks == 1

    def test_a_single_period_solve_reports_one_period_not_none(self):
        """
        `utilization_by_period` is empty by design on a one-period solve — the
        series would restate the average. Read as ONE period, so a bottleneck
        count of 1 means that period was tight, rather than as no periods,
        which would report every single-period network as never tight.
        """
        _, state = _solve(_network(periods=[950], dc_north_capacity=1000.0))
        rows = wdd.compute_warehouse_health(state)
        north = _row(rows, "DC_N")
        assert north.periods_observed == 1
        assert north.peak_utilization_pct == pytest.approx(north.avg_utilization_pct)
        assert north.bottleneck_periods_count == 1


# ---------------------------------------------------------------------------
# It computes nothing another engine owns
# ---------------------------------------------------------------------------

class TestItComputesNothingItDoesNotOwn:
    """
    §5: no second KPI engine. Every figure below is the solver's own, read
    across a boundary — if any of these drifts, two screens are showing
    different numbers for one thing.
    """

    def test_utilisation_is_the_solvers_own_figure(self, seasonal, report):
        for facility in seasonal.facilities:
            row = _row(report, facility.facility_id)
            assert row.avg_utilization_pct == pytest.approx(facility.utilization_pct, abs=0.01)
            assert row.peak_utilization_pct == pytest.approx(
                facility.peak_utilization_pct or facility.utilization_pct, abs=0.01)

    def test_facility_cost_is_the_engines_own_attribution(self, seasonal, report):
        for facility in seasonal.facilities:
            row = _row(report, facility.facility_id)
            assert row.total_facility_cost == pytest.approx(facility.total_facility_cost)

    def test_the_engines_total_is_used_rather_than_re_added(self, seasonal):
        """
        `total_facility_cost` includes CLOSURE cost, which the parts carried
        here do not. Re-summing the parts would quietly drop it, so the total
        must come from the engine.
        """
        for decision_source in (seasonal.facilities,):
            for facility in decision_source:
                parts = (facility.fixed_cost + facility.handling_cost
                         + facility.holding_cost + facility.opening_cost)
                # Equal on this network (nothing closes) — the point is that the
                # report reads the engine's field, which is asserted above.
                assert facility.total_facility_cost >= parts - 1e-6

    def test_the_threshold_is_the_configured_one(self):
        """
        One line, drawn once. A table calling a site tight that the briefing
        beside it calls healthy is the failure this shares a constant to avoid.
        """
        from netgravity.config.defaults import UTILIZATION_THRESHOLDS
        assert wdd.OVER_UTILISED_PCT == UTILIZATION_THRESHOLDS["over_threshold"] * 100.0
        assert wdd.UNDER_UTILISED_PCT == UTILIZATION_THRESHOLDS["under_threshold"] * 100.0

    def test_markets_are_not_in_the_footprint(self, report):
        """
        A market carries an unbounded nominal capacity (1e12), so one that
        reached a ranking would sit at 0.0% at the bottom of every table.
        """
        assert not [r for r in report.health_kpis if r.role in wdd.MARKET_ROLES]

    def test_a_dc_and_a_warehouse_are_the_same_thing(self, report):
        """
        The upload may say either word. Both are storage sites, both are
        counted, and a network of DCs does not report that it has no
        warehouses.
        """
        assert {"DC", "WAREHOUSE"} <= wdd.STORAGE_ROLES
        assert _row(report, "DC_N").role == "DC"
        assert _row(report, "DC_S").role == "WAREHOUSE"
        # DC_N, DC_S and the proposed DC_NEW: all three counted as warehouses.
        assert report.n_warehouses == 3
        assert report.n_warehouses_open == 2


# ---------------------------------------------------------------------------
# Absent stock and measured zero
# ---------------------------------------------------------------------------

class TestStockAbsentVsZero:

    def test_a_model_that_carries_no_stock_reports_absence(self, report):
        """
        Inventory disabled: the model never asks. An average of zero here would
        state that these warehouses run empty, which is a reading nobody took.
        """
        north = _row(report, "DC_N")
        assert north.avg_inventory_units is None
        assert north.peak_inventory_units is None
        assert north.inventory_status.status == KPIStatus.INSUFFICIENT_EVIDENCE
        assert "no next period" in north.inventory_status.reason

    def test_a_model_that_does_carry_stock_reports_the_horizon_mean(self):
        """
        Squeeze the plant below the peak and the only way to serve period 3 is
        to build stock ahead of it. The average is over EVERY modelled period,
        not over the periods that held something — otherwise "285 units in the
        two months it pre-built" is reported as 285 on average, three times the
        true level.
        """
        result, state = _solve(_network(plant_capacity=800.0, inventory=True))
        assert result.inventory_decisions, "the fixture must force stock to be carried"
        rows = wdd.compute_warehouse_health(state)
        north = _row(rows, "DC_N")

        held = {}
        for decision in result.inventory_decisions:
            if decision.facility_id == "DC_N":
                held[decision.period] = held.get(decision.period, 0.0) + decision.units
        assert north.avg_inventory_units == pytest.approx(
            sum(held.values()) / len(SEASON), abs=0.01)
        assert north.peak_inventory_units == pytest.approx(max(held.values()), abs=0.01)

    def test_a_site_holding_none_in_that_model_reports_none_held(self):
        """
        The other half of the same distinction. Once the solve models stock, a
        site that held none held none — that is a decision the model made, not
        a gap in the evidence, and it is reported as 0.0 with a VALID status.
        """
        _, state = _solve(_network(plant_capacity=800.0, inventory=True))
        rows = wdd.compute_warehouse_health(state)
        plant = _row(rows, "PLANT")
        assert plant.avg_inventory_units == 0.0
        assert plant.inventory_status.status == KPIStatus.VALID


# ---------------------------------------------------------------------------
# The growth rate
# ---------------------------------------------------------------------------

class TestTheGrowthRateIsAnInput:

    def test_no_rate_means_no_sizing_and_a_reason(self, report):
        """
        The single most important property here. A hardcoded default — the
        reference implementation used +20% — puts a capacity gap in UNITS on a
        screen, indistinguishable from a measured one, resting on an assumption
        nobody made.
        """
        assert report.future_requirements == []
        assert report.future_status.status == KPIStatus.INSUFFICIENT_EVIDENCE
        assert "growth rate" in report.future_status.reason
        assert report.growth_assumption is None

    def test_an_empty_assumption_is_not_a_rate(self, report, seasonal):
        rows, status = wdd.compute_future_requirements(
            report.health_kpis, seasonal, wdd.GrowthAssumption())
        assert rows == []
        assert status.status == KPIStatus.INSUFFICIENT_EVIDENCE

    def test_a_stated_rate_sizes_the_footprint(self, seasonal):
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(network_pct=25.0))
        north = next(r for r in sized.future_requirements if r.facility_id == "DC_N")
        base = _row(wdd.compute_warehouse_health(seasonal), "DC_N")

        assert north.growth_pct == 25.0
        assert north.projected_peak_throughput == pytest.approx(
            base.peak_throughput_units * 1.25, abs=0.01)
        assert north.required_capacity == pytest.approx(
            north.projected_peak_throughput / 0.85, abs=0.01)
        assert north.capacity_gap_units == pytest.approx(
            max(0.0, north.required_capacity - north.current_capacity_per_period),
            abs=0.01)
        assert north.expansion_needed is True

    def test_the_assumption_travels_with_the_figures(self, seasonal):
        """A sized gap whose assumption is not on screen is an
        authoritative-looking number with an invisible input."""
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(
                network_pct=25.0, source="FORECAST_ENGINE",
                description="Measured by the forecasting engine."))
        assert sized.growth_assumption is not None
        assert sized.growth_assumption.source == "FORECAST_ENGINE"
        assert sized.growth_assumption.description

    def test_a_regional_rate_follows_the_markets_a_site_actually_serves(self, seasonal):
        """
        Growth is stated about DEMAND, and a warehouse's demand is the markets
        it ships to — which the solve has already decided. Using the site's own
        region instead is the same answer only while every site serves its own
        doorstep.
        """
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(
                by_region={"North": 40.0, "South": 2.0}))
        north = next(r for r in sized.future_requirements if r.facility_id == "DC_N")
        south = next(r for r in sized.future_requirements if r.facility_id == "DC_S")
        assert north.growth_basis == "served markets"
        assert north.growth_pct == pytest.approx(40.0)
        assert south.growth_basis == "served markets"
        assert south.growth_pct == pytest.approx(2.0)

    def test_a_site_serving_no_market_falls_back_to_its_own_region(self, seasonal):
        """The plant ships to DCs, not to markets, so there are no served
        markets to weight — and its own region is the honest next answer."""
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(by_region={"North": 40.0}))
        plant = next(r for r in sized.future_requirements if r.facility_id == "PLANT")
        assert plant.growth_basis == "region North"

    def test_a_site_no_stated_rate_reaches_is_left_out_not_sized_at_zero(self, seasonal):
        """
        Sizing an unreached site at 0% growth would read as a considered
        forecast of flat demand. It is omitted, and the omission is counted.
        """
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(by_region={"Nowhere": 10.0}))
        assert sized.future_requirements == []
        assert "no stated growth rate" in sized.future_status.reason

    def test_a_closed_site_is_not_sized_from_a_throughput_of_zero(self, seasonal):
        """It would report that it needs no capacity, which is an artefact of
        it being shut rather than a finding about it."""
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(network_pct=25.0))
        assert not [r for r in sized.future_requirements if r.facility_id == "DC_NEW"]

    def test_a_plant_is_not_told_to_open_a_distribution_centre(self, seasonal):
        """
        A plant that runs out of PRODUCTION capacity is not relieved by opening
        a warehouse, and saying so sends a planner to build the wrong thing.
        """
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(network_pct=200.0))
        plant = next(r for r in sized.future_requirements if r.facility_id == "PLANT")
        assert plant.capacity_gap_units > 0
        assert plant.recommended_action == "EXPAND_CAPACITY"
        assert "no proposed distribution centre can absorb production capacity" in \
            plant.recommended_action_reason

    def test_a_warehouse_is_pointed_at_the_site_the_client_proposed(self, seasonal):
        sized = wdd.build_warehouse_deep_dive(
            seasonal, growth=wdd.GrowthAssumption(network_pct=25.0))
        north = next(r for r in sized.future_requirements if r.facility_id == "DC_N")
        assert north.recommended_action == "OPEN_CANDIDATE_DC"
        # The sizing points at the proposed site AND says it has not been
        # solved for — naming the next test is a recommendation; naming its
        # result would be an invention.
        assert "Nothing here has re-solved the network" in north.recommended_action_reason

    def test_with_no_proposed_site_the_only_lever_is_expansion(self):
        _, state = _solve(_network(with_candidate=False))
        sized = wdd.build_warehouse_deep_dive(
            state, growth=wdd.GrowthAssumption(network_pct=25.0))
        north = next(r for r in sized.future_requirements if r.facility_id == "DC_N")
        assert north.recommended_action == "EXPAND_CAPACITY"

    def test_a_target_utilisation_of_zero_is_refused_rather_than_dividing(self, report, seasonal):
        rows, status = wdd.compute_future_requirements(
            report.health_kpis, seasonal,
            wdd.GrowthAssumption(network_pct=10.0, target_utilization_pct=0.0))
        assert rows == []
        assert status.status == KPIStatus.NOT_COMPUTABLE


# ---------------------------------------------------------------------------
# Before and after
# ---------------------------------------------------------------------------

class TestBeforeAndAfter:

    def test_one_plan_cannot_be_compared_with_itself(self, report):
        rows, status = wdd.compare_facilities(None, report.health_kpis)
        assert rows == []
        assert status.status == KPIStatus.INSUFFICIENT_EVIDENCE
        assert "two solved plans" in status.reason

    def test_two_plans_subtract_site_by_site(self, report, seasonal):
        _, tighter = _solve(_network(dc_north_capacity=600.0))
        after = wdd.compute_warehouse_health(tighter)
        rows, status = wdd.compare_facilities(report.health_kpis, after)

        assert status.status == KPIStatus.VALID
        assert {r.facility_id for r in rows} == {
            r.facility_id for r in report.health_kpis}
        for row in rows:
            assert row.facility_cost_savings == pytest.approx(
                row.baseline_cost - row.optimized_cost, abs=0.01)
            assert row.throughput_delta == pytest.approx(
                row.optimized_throughput - row.baseline_throughput, abs=0.01)

    def test_a_site_present_in_only_one_plan_still_appears(self, report):
        """A site the optimiser opened must not be silently absent from the
        'after' column."""
        trimmed = [r for r in report.health_kpis if r.facility_id != "DC_S"]
        rows, _ = wdd.compare_facilities(trimmed, report.health_kpis)
        south = next(r for r in rows if r.facility_id == "DC_S")
        assert south.baseline_status == "ABSENT"

    def test_the_savings_caveat_is_on_the_report_not_left_to_the_screen(self, report, seasonal):
        """
        Facility savings do NOT sum to the network saving — they exclude the
        transport cost of moving the volume elsewhere. A reader adding the
        column up reaches a number nobody computed, so the caveat ships with
        the figures.
        """
        after = wdd.compute_warehouse_health(seasonal)
        compared = wdd.build_warehouse_deep_dive(
            seasonal, baseline_health=after, baseline_business_cost=1_000.0)
        assert "do not sum to the network saving" in compared.savings_basis
        assert compared.baseline_business_cost == 1_000.0

    def test_the_delta_points_the_right_way(self, seasonal):
        """
        `business_cost` is what THIS plan costs; `baseline_business_cost` is
        what the plan it is compared against costs; the delta is the second
        minus the first, so POSITIVE means this plan is cheaper. Getting the
        order wrong reports every saving as a cost increase, and the previous
        field name (`optimized_business_cost`, holding the as-is cost on the
        as-is report) invited exactly that.
        """
        health = wdd.compute_warehouse_health(seasonal)
        dearer_baseline = wdd.build_warehouse_deep_dive(
            seasonal, baseline_health=health,
            baseline_business_cost=(seasonal.costs.business_network_cost + 500.0))
        assert dearer_baseline.business_cost_delta == pytest.approx(500.0, abs=0.01)

        cheaper_baseline = wdd.build_warehouse_deep_dive(
            seasonal, baseline_health=health,
            baseline_business_cost=(seasonal.costs.business_network_cost - 500.0))
        assert cheaper_baseline.business_cost_delta == pytest.approx(-500.0, abs=0.01)

    def test_the_comparison_says_which_optimisation_it_is(self):
        """
        Measured on the demo network: baseline 150,627.70, optimised
        150,627.70, every row OPEN -> OPEN. Not a bug —
        `CURRENT_FOOTPRINT_OPTIMIZATION` pins the existing footprint open and
        excludes candidates by policy, so it re-solves ROUTING and cannot close
        a site.

        Unlabelled, a table of unchanged rows reads as "nothing about this
        network can be improved", which is a conclusion about the footprint
        that this comparison did not test. So the basis ships WITH the figures,
        wherever they are read.

        The card that displayed them has since been removed from the KPI
        screen, so this now tests only what the endpoint serves. That is where
        the caveat has to live anyway: it travels with the data, not with one
        panel that happened to render it.
        """
        from app.backend.api.kpis import _COMPARISON_BASIS
        assert "footprint held fixed" in _COMPARISON_BASIS
        assert "no site's status can change here" in _COMPARISON_BASIS
        assert "Scenario Planner" in _COMPARISON_BASIS

    def test_the_modes_this_rests_on_are_what_they_are_said_to_be(self):
        """
        The sentence above is a claim about `optimization/modes.py`. If that
        policy changes — if CURRENT_FOOTPRINT_OPTIMIZATION ever releases the
        footprint — the screen's caveat becomes false, and this fails rather
        than letting it quietly mislead.
        """
        from netgravity.optimization.modes import get_mode_policy
        from netgravity.schemas.network import OptimizationMode
        asis = get_mode_policy(OptimizationMode.ACTUAL_AS_IS_EVALUATION)
        routed = get_mode_policy(OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION)
        assert asis.is_hypothetical is False
        assert routed.is_hypothetical is True
        assert asis.pin_existing_open is True
        assert routed.pin_existing_open is True

    def test_corridors_are_compared_too(self):
        """
        The brief asks to rank facility AND corridor deltas, and on a
        footprint-fixed comparison the corridor half is the ONLY one that can
        differ: no site opens, closes, or changes what it costs to run. Without
        it the section reports "nothing changed" about a solve whose whole job
        was to change the routing.
        """
        rows, status = wdd.compare_corridors(
            [{"origin_id": "A", "destination_id": "B",
              "flow_units": 100.0, "transport_cost": 500.0},
             {"origin_id": "A", "destination_id": "C",
              "flow_units": 50.0, "transport_cost": 400.0}],
            [{"origin_id": "A", "destination_id": "B",
              "flow_units": 150.0, "transport_cost": 600.0},
             {"origin_id": "A", "destination_id": "D",
              "flow_units": 20.0, "transport_cost": 60.0}])
        assert status.status == KPIStatus.VALID
        by_lane = {(r.origin_id, r.destination_id): r for r in rows}

        # A lane the optimiser stopped using is a saving, and it must appear
        # even though it is absent from the "after" plan.
        assert by_lane[("A", "C")].change == "No longer used"
        assert by_lane[("A", "C")].transport_cost_savings == 400.0
        # ...and one it started using, even though it is absent from "before".
        assert by_lane[("A", "D")].change == "Newly used"
        assert by_lane[("A", "D")].transport_cost_savings == -60.0
        assert by_lane[("A", "B")].units_delta == 50.0

    def test_a_lane_nobody_used_is_not_a_row(self):
        """Zero before and zero after is not a change; it is noise in a table
        the reader is scanning for changes."""
        rows, _ = wdd.compare_corridors(
            [{"origin_id": "A", "destination_id": "B",
              "flow_units": 0.0, "transport_cost": 0.0}],
            [{"origin_id": "A", "destination_id": "B",
              "flow_units": 0.0, "transport_cost": 0.0}])
        assert rows == []

    def test_one_plan_cannot_be_compared_lane_by_lane_either(self):
        rows, status = wdd.compare_corridors(None, [{"origin_id": "A"}])
        assert rows == []
        assert status.status == KPIStatus.INSUFFICIENT_EVIDENCE

    def test_the_status_words_tell_proposed_from_closed(self, report):
        """
        A site the client never built and a site the optimiser shut both arrive
        with `is_open` False, and calling both CLOSED is the mislabel
        `test_warehouse_planning_ui.py` was written about.
        """
        rows, _ = wdd.compare_facilities(report.health_kpis, report.health_kpis)
        candidate = next(r for r in rows if r.facility_id == "DC_NEW")
        assert candidate.baseline_status == "PROPOSED"
        assert candidate.change == "Not used in either plan"


# ---------------------------------------------------------------------------
# Rankings and roll-ups
# ---------------------------------------------------------------------------

class TestRankingsAreOrderingAndNothingElse:

    def test_capacity_constraints_rank_by_peak_and_only_open_sites(self, report):
        peaks = [r.peak_utilization_pct for r in report.top_capacity_constraints]
        assert peaks == sorted(peaks, reverse=True)
        assert all(r.is_open for r in report.top_capacity_constraints)
        assert report.top_capacity_constraints[0].facility_id == "DC_N"

    def test_the_warehouse_ranking_excludes_the_plant(self, report):
        ids = {r.facility_id for r in report.top_warehouses_by_utilization}
        assert "PLANT" not in ids
        assert "DC_N" in ids

    def test_cost_drivers_rank_by_spend_and_carry_their_share(self, report):
        costs = [d.total_facility_cost for d in report.top_facilities_driving_cost]
        assert costs == sorted(costs, reverse=True)
        total = sum(r.total_facility_cost for r in report.health_kpis)
        for driver in report.top_facilities_driving_cost:
            assert driver.share_of_facility_spend == pytest.approx(
                driver.total_facility_cost / total, abs=1e-6)
        # A site that cost nothing is not a cost driver.
        assert all(d.total_facility_cost > 0 for d in report.top_facilities_driving_cost)

    def test_every_ranking_is_bounded(self, report):
        for ranking in (report.top_capacity_constraints,
                        report.top_warehouses_by_utilization,
                        report.top_facilities_driving_cost,
                        report.top_savings_opportunities):
            assert len(ranking) <= wdd.TOP_N

    def test_the_average_peak_is_named_for_what_it_is(self, report):
        """A mean of maxima, over OPEN sites. Not the network's peak."""
        peaks = [r.peak_utilization_pct for r in report.health_kpis if r.is_open]
        assert report.avg_peak_utilization_pct == pytest.approx(
            sum(peaks) / len(peaks), abs=0.01)

    def test_a_network_with_no_footprint_says_so(self):
        assert wdd.build_warehouse_deep_dive(None).status.status == \
            KPIStatus.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# The contract carries what the report needs
# ---------------------------------------------------------------------------

class TestTheContractCarriesIt:

    def test_per_facility_cost_crosses_the_bridge(self, seasonal):
        north = next(f for f in seasonal.facilities if f.facility_id == "DC_N")
        assert north.total_facility_cost > 0
        assert north.handling_cost > 0
        assert north.fixed_cost > 0

    def test_inventory_cost_is_deliberately_not_carried(self, seasonal):
        """
        `FacilityDecision.inventory_cost` is declared and never written —
        inventory cost is attributed to facility→market PAIRS. Carrying it
        would put a hard 0.00 on every warehouse's cost card, which reads as
        "this site carries no inventory cost" rather than "the model does not
        attribute it per site".
        """
        assert not hasattr(seasonal.facilities[0], "inventory_cost")

    def test_a_markets_region_crosses_the_bridge(self, seasonal):
        """
        Markets get no facility decision, so they are absent from `facilities`
        and their region has no other carrier — without which a rate stated by
        region cannot be matched to the sites serving it.
        """
        assert seasonal.market_regions == {"MKT_N": "North", "MKT_S": "South"}
        assert not [f for f in seasonal.facilities if f.facility_id.startswith("MKT")]

    def test_the_analysis_document_version_was_bumped(self):
        """
        The cache key is (snapshot, data_version, variant), and `data_version`
        describes the NETWORK. Adding a block to the document changes neither,
        so every previously analysed network would be served a document with no
        warehouse block in it — forever.
        """
        from app.backend.services import analysis_store
        assert analysis_store._ANALYSIS_VERSION >= 5

    def test_a_stored_document_of_the_current_shape_rehydrates(self, seasonal):
        """
        Round-trip the model the endpoint rehydrates.

        `WarehouseDeepDiveReport` is `extra="forbid"`, so a stored document
        carrying a field the model no longer has does not degrade — it raises,
        and the endpoint returns 500 for every project already analysed. That
        happened, live, when a field was renamed without bumping the version.
        """
        from netgravity.orchestrator.metrics.warehouse_deep_dive import (
            WarehouseDeepDiveReport)
        document = wdd.build_warehouse_deep_dive(seasonal).model_dump(mode="json")
        assert WarehouseDeepDiveReport(**document).health_kpis


# ---------------------------------------------------------------------------
# The narrative
# ---------------------------------------------------------------------------

class TestTheNarrativeReadsThePeak:

    @staticmethod
    def _agent():
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        return ReasoningAgent

    @staticmethod
    def _warehouse_payload(**overrides):
        payload = {
            "periods_modelled": 6,
            "n_bottlenecks": 1,
            "n_underused": 0,
            "avg_peak_utilization_pct": 73.0,
            "tightest": [{
                "facility_id": "DC_N", "name": "North DC",
                "avg_utilization_pct": 50.25, "peak_utilization_pct": 95.0,
                "peak_period": "3", "bottleneck_periods_count": 1,
                "periods_observed": 6, "health_band": "TIGHT",
            }],
            "cost_drivers": [{
                "facility_id": "DC_S", "name": "South DC",
                "total_facility_cost": 3156.75, "share_of_facility_spend": 0.356,
            }],
        }
        payload.update(overrides)
        return payload

    def test_it_states_the_peak_the_average_hid(self):
        insights = self._agent()._warehouse_insights(
            self._warehouse_payload(), lambda field, limit=1: [field])
        headline = insights[0].headline
        assert "95.0%" in headline and "50.2%" in headline
        assert "period 3" in insights[0].narrative

    def test_it_states_how_often_not_only_how_high(self):
        insights = self._agent()._warehouse_insights(
            self._warehouse_payload(), lambda field, limit=1: [field])
        assert any("1 of 6 periods" in i.headline for i in insights)

    def test_the_spend_card_needs_real_concentration(self):
        """
        A briefing holds six insights. Three warehouse cards pushed the
        footprint and carbon findings off the end of every network's briefing,
        including ones where the largest site held an ordinary share — which is
        division, not a finding.
        """
        agent = self._agent()
        modest = self._warehouse_payload()
        modest["cost_drivers"][0]["share_of_facility_spend"] = 0.26
        assert not [i for i in agent._warehouse_insights(
            modest, lambda f, limit=1: [f]) if i.theme == "Cost"]

        concentrated = self._warehouse_payload()
        concentrated["cost_drivers"][0]["share_of_facility_spend"] = 0.55
        assert [i for i in agent._warehouse_insights(
            concentrated, lambda f, limit=1: [f]) if i.theme == "Cost"]

    def test_it_says_nothing_when_the_two_readings_agree(self):
        """
        On a single-period solve the peak IS the average, and
        `_utilization_insights` already reports it. Two cards making one point
        in different words is worse than one.
        """
        insights = self._agent()._warehouse_insights(
            self._warehouse_payload(periods_modelled=1),
            lambda field, limit=1: [field])
        assert not [i for i in insights if i.theme == "Capacity"]

    def test_an_empty_block_produces_no_insight(self):
        assert self._agent()._warehouse_insights({}, lambda f, limit=1: []) == []

    def test_the_recommendation_cannot_contradict_the_card(self):
        """
        Measured, before this existed: the card said "North DC is at 95.0% in
        its busiest period" and the recommendation under it said "no site is at
        its capacity threshold". Both from the same briefing.
        """
        recommendation = self._agent()._recommendation(
            infeasible=False,
            state={"unserved_demand": 0.0},
            payload={"facilities": [{"facility_id": "DC_N", "is_open": True,
                                     "utilization_pct": 50.25}],
                     "warehouse": self._warehouse_payload()},
            negatives=[], insights=[])
        assert "peak" in recommendation.lower()
        assert "North DC" in recommendation
        assert "no site" not in recommendation.lower()

    def test_it_fits_the_field_it_is_written_into(self):
        """
        `ExecutiveBriefing.recommendation` caps at 350 characters and going over
        does not truncate — it fails validation, fails the whole capability and
        returns a briefing with NO recommendation. A long site name must not be
        able to cause that.
        """
        payload = self._warehouse_payload()
        payload["tightest"][0]["name"] = "A" * 200
        payload["tightest"][0]["bottleneck_periods_count"] = 11
        payload["tightest"][0]["periods_observed"] = 12
        recommendation = self._agent()._recommendation(
            infeasible=False, state={"unserved_demand": 0.0},
            payload={"facilities": [], "warehouse": payload},
            negatives=[], insights=[])
        assert len(recommendation) <= 350

    def test_the_figures_survive_numeric_grounding(self):
        """
        Without these keys every number in a warehouse insight is stripped as
        unsupported — the failure that left a forecast recommendation reading
        "run a demand scenario at [UNSUPPORTED FIGURE REMOVED]".
        """
        from netgravity.orchestrator.validation.numeric_grounding import _FACT_SPEC
        for key in ("peak_utilization_pct", "bottleneck_periods_count",
                    "periods_observed", "n_bottlenecks",
                    "total_facility_cost", "share_of_facility_spend"):
            assert key in _FACT_SPEC, key

    def test_the_stated_capacity_stays_out_of_the_fact_space(self):
        """
        Grounding matches on KIND, not on metric name, so every citable number
        widens the space an invented one can match against. A fixture plant
        with a stated capacity of 99,999 was once enough to make a hallucinated
        cost of "99,999.00" verify as grounded. Outputs earn their place;
        inputs do not.
        """
        from netgravity.orchestrator.registry import _warehouse_evidence
        import inspect
        source = inspect.getsource(_warehouse_evidence)
        assert "rated_capacity_per_period" not in source
        assert "headroom_units_peak" not in source


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------

def _asset(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


class TestTheScreen:

    def test_the_metrics_live_on_the_kpi_dashboard(self):
        """
        Not a screen of their own. They are KPIs of the same solve the KPI
        screen already reports, and a second sidebar entry made the reader
        choose between two pages answering one question.
        """
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        for element in ("wh-summary-grid", "wh-attention", "chart-wh-utilisation",
                        "table-wh-health"):
            assert element in panel, element
        assert 'id="tab-warehouse"' not in html
        assert 'data-tab="warehouse"' not in html

    def test_it_is_one_screen_and_not_two_bands(self):
        """
        The panel used to carry a network band above a per-site band, each with
        its own heading, its own rule and its own export button. That division
        read as a division between two kinds of building — which is exactly
        what a DC and a warehouse are not.

        One flow now: the network, then down into the selected site, with
        nothing between them announcing a second report.
        """
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        for divider in ("kpi-band-head", "kpi-band-title",
                        "Selected facility", "Facility network"):
            assert divider not in panel, divider
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".kpi-band-head" not in css
        assert ".kpi-band-title" not in css

    def test_the_per_site_half_survived_intact(self):
        """One flow, not one half. Everything the selected-facility view had is
        still on the screen, and still says which facility it is about."""
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        for kept in ("dash-metrics-grid", "table-dash-lanes", "dash-facility-name",
                     "dash-facility-type", "dash-facility-dot"):
            assert kept in panel, kept

    def test_there_is_exactly_one_export_button(self):
        """
        Two buttons made the reader work out which half each file covered
        before they could trust either. One button, one file, both halves.
        """
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        assert panel.count("<button") == panel.count('id="btn-export-kpi"') == 1
        assert "btn-export-warehouse" not in panel

    def test_the_one_export_carries_both_halves(self):
        """The network table is appended to the facility report, so a reader
        who exports the KPI dashboard gets every metric that was on it."""
        app_js = _asset("app.js")
        block = app_js[app_js.index("export function exportFacilityReport()"):]
        block = block[:block.index("\n}")]
        assert "warehouseHealthCsvLines()" in block
        # ...and the file is no longer named after one site, which it no
        # longer only describes.
        assert "NetGravity_KPI_Dashboard_" in app_js

    def test_the_two_removed_cards_are_off_the_screen(self):
        """
        Growth sizing and the re-optimisation comparison, removed on request.

        The ENGINE still computes both and `/api/kpis/warehouse` still serves
        them — the classes above this one still test them — so this asserts
        they are off the panel, not that the capability was deleted.
        """
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        for gone in ("Future Capacity Requirement", "What Re-optimising",
                     "wh-growth-pct", "wh-growth-target", "wh-future-body",
                     "wh-optimized-body", "btn-wh-size", "btn-wh-optimize",
                     "btn-wh-forecast-growth"):
            assert gone not in panel, gone
        js = _asset("warehouse.js")
        for gone in ("renderFuture", "renderOptimized", "readGrowth",
                     "onRunComparison", "renderWarehouseGapChart"):
            assert gone not in js, gone
        # The renderer for the deleted chart went with the card rather than
        # staying behind as a function nothing calls.
        assert "renderWarehouseGapChart" not in _asset("charts.js")

    def test_the_kpi_route_renders_both_bands(self):
        app_js = _asset("app.js")
        # The ROUTING branch, not the top-bar title branch a few hundred lines
        # above it that shares the same condition.
        block = app_js[app_js.index("state.activeTab = 'facility-dashboard';"):]
        block = block[:block.index("scrollPageToTop();")]
        assert "renderFacilityDashboard()" in block
        assert "renderWarehouseDashboard()" in block

    def test_a_new_network_drops_the_cached_report(self):
        """
        Re-uploading into the SAME project keeps the project id and changes the
        data, so a report cached against the id alone would survive its own
        network.
        """
        app_js = _asset("app.js")
        block = app_js[app_js.index("window.addEventListener('networkDataLoaded'"):]
        block = block[:block.index("\n});")]
        assert "clearWarehouseState()" in block

    def test_absent_stock_renders_as_absent_not_as_zero(self):
        js = _asset("warehouse.js")
        block = js[js.index("function renderHealthTable()"):]
        block = block[:block.index("\n// ─")]
        assert "avg_inventory_units === null" in block
        assert "absent(stockReason)" in block

    def test_dc_and_warehouse_read_as_one_thing(self):
        """
        One site, one name, and the name the rest of the product already uses.
        The map legend, the 3D twin and the scenario toolbox all say
        "Distribution Centre"; a compound like "Warehouse / DC" shows the
        reader the seam between two spellings of one thing.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function roleLabel(role)"):]
        block = block[:block.index("\nfunction multiPeriod")]
        assert block.count("'Distribution Centre'") == 2
        assert "Warehouse" not in block

    def test_the_screen_names_no_second_kind_of_building(self):
        """
        The KPI dashboard is the network's standard facility view. Nothing a
        reader can SEE on it may call a site a warehouse, because that invites
        the question of how a warehouse differs from a DC — which it does not.

        Attributes and ids are exempt and stay as they are: `wh-summary-grid`
        and `/api/kpis/warehouse` are not shown to anyone, and renaming a
        stored document's keys to change a caption would invalidate every
        cached analysis for no reader's benefit.
        """
        import re

        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        visible = re.sub(r"<!--.*?-->", " ", panel, flags=re.S)   # source comments
        visible = re.sub(r"<[^>]+>", " ", visible)                # ids and attributes
        assert "arehouse" not in visible, visible

    def test_the_count_card_counts_a_named_population(self):
        """
        "Warehouses 2 of 3" named one population in the title and compared it
        against another in the line below. The title now names what is counted.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderSummary()"):]
        block = block[:block.index("function renderAttention")]
        assert "Distribution facilities" in block
        assert "Warehouses" not in block

    def test_the_export_keeps_absence_out_of_the_spreadsheet(self):
        """An absent stock reading exports empty, not 0 — or a spreadsheet
        averages a zero nobody measured."""
        js = _asset("warehouse.js")
        block = js[js.index("export function warehouseHealthCsvLines()"):]
        block = block[:block.index("\nexport function clearWarehouseState")]
        assert "v === null || v === undefined ? ''" in block

    def test_an_unread_network_exports_a_line_saying_so(self):
        """A report whose network section is silently missing reads as a
        network with no sites in it."""
        js = _asset("warehouse.js")
        block = js[js.index("export function warehouseHealthCsvLines()"):]
        block = block[:block.index("\nexport function clearWarehouseState")]
        assert "No facility network analysis has been read" in block

# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

DEMO_PROJECT = "pr-demo-case16"


@pytest.fixture()
def client():
    from app.backend.app import app as flask_app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def auth(client):
    email = f"warehouse-{uuid.uuid4().hex}@example.com"
    response = client.post("/api/auth/signup",
                           json={"email": email, "password": "warehouse-test-pw-1"})
    assert response.status_code == 201, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


class TestTheEndpoint:
    """
    Against the bound demo project, so these exercise the real chain: solve →
    contract → registry → stored analysis → route.
    """

    def test_it_serves_the_footprint(self, client, auth):
        response = client.get(
            f"/api/kpis/warehouse?project_id={DEMO_PROJECT}", headers=auth)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["warehouse"]["health_kpis"]
        for row in body["warehouse"]["health_kpis"]:
            # Every row is a real facility with a band, not a placeholder.
            assert row["facility_id"] and row["health_band"]

    def test_it_refuses_to_answer_without_a_project(self, client, auth):
        response = client.get("/api/kpis/warehouse", headers=auth)
        assert response.status_code == 400

    def test_it_requires_authentication(self, client):
        response = client.get(f"/api/kpis/warehouse?project_id={DEMO_PROJECT}")
        assert response.status_code == 401

    def test_no_growth_rate_means_a_stated_reason(self, client, auth):
        response = client.get(
            f"/api/kpis/warehouse?project_id={DEMO_PROJECT}", headers=auth)
        warehouse = response.get_json()["warehouse"]
        assert warehouse["future_requirements"] == []
        assert warehouse["future_status"]["status"] == "INSUFFICIENT_EVIDENCE"
        assert warehouse["future_status"]["reason"]
        assert warehouse["growth_assumption"] is None

    def test_a_stated_rate_sizes_it_and_records_who_stated_it(self, client, auth):
        response = client.get(
            f"/api/kpis/warehouse?project_id={DEMO_PROJECT}"
            f"&growth_pct=20&growth_source=FORECAST_ENGINE", headers=auth)
        assert response.status_code == 200
        warehouse = response.get_json()["warehouse"]
        assert warehouse["growth_assumption"]["network_pct"] == 20.0
        assert warehouse["growth_assumption"]["source"] == "FORECAST_ENGINE"
        # A rate lifted from the forecast must not be shown as though the user
        # had typed it.
        assert "forecasting engine" in warehouse["growth_assumption"]["description"]

    def test_a_bad_rate_is_refused_at_the_boundary(self, client, auth):
        for query in ("growth_pct=banana", "growth_pct=5000",
                      "region_growth=:12", "growth_source=MADE_UP&growth_pct=5"):
            response = client.get(
                f"/api/kpis/warehouse?project_id={DEMO_PROJECT}&{query}", headers=auth)
            assert response.status_code == 400, query
            assert response.get_json()["error"]["message"]

    def test_the_before_and_after_is_opt_in_and_says_why(self, client, auth):
        """
        Not run with the baseline: it is a second MILP solve of the whole
        network. An empty comparison must not read as "optimising would change
        nothing".
        """
        response = client.get(
            f"/api/kpis/warehouse?project_id={DEMO_PROJECT}", headers=auth)
        body = response.get_json()
        assert body["optimized"] is None
        assert body["optimized_status"]["status"] == "NOT_REQUESTED"
        assert "not a statement that" in body["optimized_status"]["reason"]

    def test_the_envelope_says_when_and_in_what_currency(self, client, auth):
        response = client.get(
            f"/api/kpis/warehouse?project_id={DEMO_PROJECT}", headers=auth)
        body = response.get_json()
        for field in ("project_id", "snapshot_id", "computed_at", "horizon"):
            assert field in body

# ---------------------------------------------------------------------------
# The trap this feature fell into
# ---------------------------------------------------------------------------

class TestNoBacktickInsideATemplateLiteral:
    """
    A backtick inside an HTML comment ends the template literal it sits in.

    What happened: an explanatory comment inside `renderFuture()`'s markup was
    written as

        <!-- `perPeriodLabel()` already reads "units/month" ... -->

    The first backtick closed the template. Everything after it parsed as code,
    `warehouse.js` failed to parse, and because `app.js` imports it statically
    the ENTIRE module graph failed with `Unexpected identifier
    'perPeriodLabel'`. The visible symptom was three screens away from the
    cause: the landing page lost its world map, because `initLandingPage()`
    lives in the module that never ran.

    The whole suite passed while the application would not boot. Every
    frontend test in this repo reads the sources as TEXT — none of them parses
    the JavaScript, and there is no JS engine in the test environment to do it
    with. So this checks the one construct that caused it rather than pretending
    to be a parser.

    HTML comments appear in these files only inside template literals; a
    backtick in one is never anything but this mistake.
    """

    def test_no_frontend_module_hides_a_backtick_in_an_html_comment(self):
        import re

        offenders = []
        for path in sorted((REPO_ROOT / "app" / "frontend" / "js").rglob("*.js")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in re.finditer(r"<!--.*?-->", text, re.S):
                if "`" in match.group(0):
                    line = text[:match.start()].count("\n") + 1
                    offenders.append(
                        f"{path.name}:{line} {match.group(0)[:70]!r}")
        assert not offenders, (
            "A backtick inside an HTML comment ends the template literal it is "
            "written in, and the module stops parsing there:\n  "
            + "\n  ".join(offenders))

    def test_the_module_that_broke_still_carries_the_warning(self):
        """
        The comment that caused it sat inside `renderFuture()`, which has since
        been removed from the screen. The warning outlived the function: it is
        in the module header now, where the next person writing markup in a
        template literal will read it.
        """
        js = _asset("warehouse.js")
        header = js[:js.index("import ")]
        assert "backtick" in header.lower()
        assert "template literal" in header

# ---------------------------------------------------------------------------
# The same network, three more ways
# ---------------------------------------------------------------------------


class TestTheAddedCuts:
    """
    Three charts drawn from readings the table already carries.

    A table answers "what is THIS site" and a chart answers "what is this
    NETWORK", and no row can do the second. Each of these earns its space by
    answering a question the two charts above it cannot:

      * the MIX says how much of the network is in trouble before any name is
        read;
      * HEADROOM says where the next volume can go, which per-cent utilisation
        ranks the wrong way round — 95% of 200 units is ten spare and 80% of
        40,000 is eight thousand;
      * STOCK says whether a site holds for a season or holds a constant
        buffer, which is the gap between its two bars.
    """

    def test_the_three_cuts_are_on_the_kpi_dashboard(self):
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        for canvas in ("chart-wh-mix", "chart-wh-headroom", "chart-wh-stock"):
            assert canvas in panel, canvas
        # Above the per-site detail, in one flow rather than a band of
        # their own.
        assert panel.index("chart-wh-mix") < panel.index("dash-metrics-grid")

    def test_every_renderer_exists_and_is_wired(self):
        charts = _asset("charts.js")
        warehouse = _asset("warehouse.js")
        for name in ("renderWarehouseStatusMixChart", "renderWarehouseHeadroomChart",
                     "renderWarehouseStockChart"):
            assert f"export function {name}" in charts, name
            assert name in warehouse, name

    def test_the_mix_uses_the_colours_the_tags_already_use(self):
        """One state is one colour in the tag, the attention row, the table and
        the chart. Two colour codes for one vocabulary is worse than no
        chart."""
        import re

        js = _asset("warehouse.js")
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = js[js.index("const BAND_COLOUR = {"):]
        block = block[:block.index("};")]
        # The semantic tokens, resolved to their hex so a canvas can use them —
        # `var(--red)` means nothing to Chart.js, so the two can drift and this
        # is what notices.
        for token, hex_value in (("--red", "#dc2626"), ("--amber", "#d97706"),
                                 ("--blue", "#2563eb"), ("--green", "#16a34a")):
            assert hex_value in block, hex_value
            assert re.search(rf"{re.escape(token)}:\s+{hex_value}", css), token
        # Every band the screen can show has one, or a slice draws undefined.
        for band in ("CRITICAL", "TIGHT", "UNDERUSED", "HEALTHY", "NOT_OPERATING"):
            assert band in block, band

    def test_the_mix_counts_the_sites_this_plan_does_not_open(self):
        """A network where four of eleven sites are not running is a finding. A
        mix that dropped them would draw a smaller, healthier network than the
        one the client has."""
        js = _asset("warehouse.js")
        block = js[js.index("function renderMix()"):js.index("function renderHeadroom()")]
        assert "is_open" not in block

    def test_headroom_is_drawn_for_open_sites_only(self):
        """The opposite call, for the opposite reason: a site this plan does
        not open has no headroom the plan can use, and drawing its whole
        capacity as spare room points a planner at a building nobody runs."""
        js = _asset("warehouse.js")
        block = js[js.index("function renderHeadroom()"):js.index("function renderStock()")]
        assert "k.is_open" in block

    def test_headroom_shows_an_overrun_rather_than_clipping_it(self):
        """The model can exceed a stated capacity. A bar clipped at 100% would
        hide the single worst thing this chart could have to show."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseHeadroomChart"):]
        block = block[:block.index("\n/**")]
        assert "Over stated capacity" in block
        assert "Math.max(0, v - cap[i])" in block
        # ...and it is only in the legend when something is actually over.
        assert "over.some((v) => v > 0)" in block

    def test_headroom_is_drawn_from_the_two_reported_figures(self):
        """The split is a DRAWING of the difference between two authoritative
        numbers, not a KPI this file invents: the tooltip names both, so the
        pale segment can be checked against them."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseHeadroomChart"):]
        block = block[:block.index("\n/**")]
        assert "Rated capacity" in block
        assert "Busiest period carries" in block

    def test_absent_stock_replaces_the_chart_with_its_reason(self):
        """
        An empty chart frame with axes on it reads as a measurement of zero.
        A model that writes no inventory decisions has no stock to draw, which
        is not the statement that these sites hold nothing.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderStock()"):js.index("function renderHealthTable()")]
        # Both readings, so a site reporting one and not the other is not
        # drawn against a zero for the half it never reported.
        assert "avg_inventory_units !== null" in block
        assert "peak_inventory_units !== null" in block
        assert "wrap.style.display = 'none'" in block
        assert "inventory_status?.reason" in block

    def test_the_headroom_legend_knows_whether_there_is_a_busiest_period(self):
        """
        The same care the utilisation chart beside it already takes. On a
        single-period solve there is no busiest period, and a legend naming one
        implies a seasonal reading the data does not carry.
        """
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseHeadroomChart"):]
        block = block[:block.index("\n/**")]
        assert "multiPeriod ? 'Used in the busiest period' : 'Used'" in block
        js = _asset("warehouse.js")
        assert ("renderWarehouseHeadroomChart('chart-wh-headroom', open, multiPeriod())"
                in js)

    def test_the_stock_note_states_the_network_fact_before_the_site_reason(self):
        """
        The engine's reason is written about ONE site. Printed alone on a card
        about every site, "no inventory decisions for this site" reads as a
        statement about some unnamed one — so the network-level fact leads and
        the engine's words are attributed rather than paraphrased.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderStock()"):js.index("function renderHealthTable()")]
        assert "No site in this plan reports a stock level" in block
        # "one reason for all of them" is counted, not assumed.
        assert "new Set(rows" in block

    def test_the_stock_card_carries_a_place_for_that_reason(self):
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        assert 'id="wh-stock-wrap"' in panel
        assert 'id="wh-stock-absent"' in panel
