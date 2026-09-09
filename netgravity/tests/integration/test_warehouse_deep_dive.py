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

    def test_there_is_exactly_one_export_control(self):
        """
        One button, one format. The PDF is the view as it looks with the
        filters that produced it named on it, which is the thing a reader
        circulates; a second format behind a menu was one more click in front
        of the only one anybody asked for.
        """
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        assert panel.count('id="btn-export-pdf"') == 1
        # The CSV path is gone, not hidden.
        assert "exportFacilityReport" not in panel
        assert "btn-export-csv" not in panel
        assert "btn-export-warehouse" not in panel


    def test_the_csv_export_is_gone_not_hidden(self):
        """
        One export, one format. The CSV writer, the delegated action that
        reached it and the row serialiser it called are all deleted rather
        than left unreachable — dead code that still passes review is how a
        second, drifting definition of "the current view" survives.
        """
        for asset in ("app.js", "actions.js", "warehouse.js"):
            js = _asset(asset)
            assert "exportFacilityReport" not in js, asset
            assert "warehouseHealthCsvLines" not in js, asset

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

    def test_the_kpi_route_lands_on_the_whole_network(self):
        """
        A reader arriving from the Overview arrives with a network-level
        question, so the screen opens on the network — never on whichever site
        a previous visit happened to leave selected.

        `renderKpiView()` clears the drill-down and draws the roll-up; the
        facility detail is rendered by the view controller when, and only
        when, a site is actually selected.
        """
        app_js = _asset("app.js")
        # The ROUTING branch, not the top-bar title branch a few hundred lines
        # above it that shares the same condition.
        block = app_js[app_js.index("state.activeTab = 'facility-dashboard';"):]
        block = block[:block.index("scrollPageToTop();")]
        assert "renderKpiView()" in block
        assert "renderWarehouseDashboard()" in block

        # And the landing state really is the roll-up, not a remembered site.
        view = _asset("kpi-view.js")
        entry = view[view.index("export function renderKpiView()"):]
        entry = entry[:entry.index("\n}")]
        assert "view.entityId = null" in entry

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

        # Defined in charts.js now, and imported by warehouse.js — one copy,
        # so a state cannot be drawn in two colours on one screen.
        js = _asset("charts.js")
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = js[js.index("export const BAND_COLOUR = {"):]
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


class TestTheThreeTiers:
    """
    WHICH POPULATION THE SCREEN IS REPORTING ON.

    The KPI screen used to be one continuous scroll: the whole facility
    network, then every network chart, then every site in a table, and only
    then the one site the top bar had selected. Selecting Atlanta at the top
    left the reader scrolling past four network charts and a nine-row table to
    reach Atlanta's own numbers, and nothing on the way down said which of
    those figures were about Atlanta and which were about the network.

    Three tiers now:

      * Tier 0  the scorecard — the whole network, always, captioned as that;
      * Tier 1  the lens — storage sites, production sites, or the corridors;
      * Tier 2  region and status narrow it, and a facility drills into one
                site IN PLACE of the roll-up.
    """

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_the_three_tiers_are_on_the_screen_in_order(self):
        panel = self._panel()
        for element in ("kpi-domain-bar", "kpi-network-strip", "wh-summary-grid",
                        "kpi-filter-bar", "kpi-breadcrumb", "kpi-rollup",
                        "kpi-entity", "kpi-lanes"):
            assert element in panel, element
        # The LENS comes first, because it decides which scorecard is shown;
        # the scorecard then describes it, and the filters narrow it.
        assert panel.index("kpi-domain-bar") < panel.index("kpi-network-strip")
        assert panel.index("wh-summary-grid") < panel.index("kpi-filter-bar")

    def test_the_screen_lands_on_the_network_lens(self):
        """A reader arriving from the Overview arrives with a network-level
        question, and the four figures they just read are the four this tab
        shows."""
        panel = self._panel()
        bar = panel[panel.index('id="kpi-domain-bar"'):]
        bar = bar[:bar.index("</div>")]
        assert bar.index('data-domain="network"') < bar.index('data-domain="dc"')
        assert 'class="kpi-domain-tab active" data-domain="network"' in bar
        for js in ("kpi-view.js", "warehouse.js"):
            assert "domain: 'network'" in _asset(js), js

    def test_the_scorecard_describes_whatever_is_selected(self):
        """
        Tier 0 is NOT fixed.

        Held constant, it announced "Distribution facilities 8 of 9" above a
        screen showing three plants — a contradiction between the top of the
        screen and the rest of it, which is the failure this screen was rebuilt
        to remove. It now follows the scope, and re-counts on every filter.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderSummary()"):js.index("function renderAttention")]
        assert "visibleRows()" in block
        # Counted over the rows on screen, never read back off the report's
        # whole-network counters.
        for fixed in ("r.n_warehouses_open", "r.n_bottlenecks", "r.n_underused",
                      "r.avg_peak_utilization_pct"):
            assert fixed not in block, fixed
        # ...and it names the population it just counted.
        assert "Production sites" in block

    def test_the_network_lens_reuses_the_overviews_own_renderer(self):
        """
        One source, so the two screens cannot report different numbers for one
        network. A second copy of this arithmetic here is exactly how they
        would come to disagree.
        """
        view = _asset("kpi-view.js")
        assert "hooks.renderNetworkScorecard('kpi-network-strip-row')" in view
        app_js = _asset("app.js")
        assert "renderNetworkScorecard: (rowId) => renderHomeKpiStrip(rowId)" in app_js

    def test_the_caption_is_written_after_the_filter_is_applied(self):
        """
        Computing it first labelled the Plants tab "every facility", because it
        was still reading the lens the reader had just left.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("export function applyView()"):]
        block = block[:block.index("renderControls()")]
        assert block.index("setWarehouseFilter(") < block.index("kpi-scorecard-note")

    def test_every_section_below_the_lens_reads_the_same_population(self):
        """
        The half-filtered screen — a donut still drawn over nine sites beside a
        table showing three — is the failure that makes a reader stop trusting
        every other number on the page. Every section below the scorecard
        reads one filtered list.
        """
        js = _asset("warehouse.js")
        for renderer in ("function renderAttention()", "function renderMix()",
                         "function renderStock()", "function renderHealthTable()"):
            start = js.index(renderer)
            block = js[start:js.index("\n}", start)]
            assert "visibleRows()" in block, renderer
        # The charts and the headroom cut read it too.
        charts = js[js.index("function renderCharts()"):js.index("function renderMix()")]
        assert "visibleRows()" in charts
        head = js[js.index("function renderHeadroom()"):js.index("function renderStock()")]
        assert "visibleRows()" in head

    def test_no_chart_carries_a_corner_metric_pill(self):
        """
        The pills went. Each restated a number already on the chart, in its
        legend, in the scorecard or in the table below it — and the one beside
        the AI button made that button's placement look like an afterthought.

        A pill that disagreed with the chart under it was also the original
        defect here: "3 at or above 90%" above a chart drawing one of them.
        Removing them removes the class of bug, not just the instance.
        """
        panel = self._panel()
        for pill in ("wh-util-tag", "wh-spend-tag", "wh-mix-tag", "wh-headroom-tag",
                     "wh-stock-tag", "wh-health-count", "kpi-lane-cost-tag",
                     "kpi-lane-mode-tag", "kpi-lane-count", "dash-util-tag",
                     "dash-total-cost-tag", "dash-lane-count-tag"):
            assert pill not in panel, pill
        # And nothing is left writing to them.
        js = _asset("warehouse.js")
        assert "wh-util-tag" not in js
        assert "wh-spend-tag" not in js

    def test_the_cost_donut_is_built_from_the_rows_on_screen(self):
        """
        It used to read the backend's ranked cost drivers, which are capped at
        TOP_N — so on a network of more than ten facilities the donut drew ten
        slices while any total beside it summed all of them. A chart whose
        parts cannot add up to its own whole.

        Built from the visible rows instead, each carrying its own cost, so
        the slices ARE the population.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderCharts()"):js.index("function renderMix()")]
        # Not USED — the name still appears in the comment explaining why.
        assert "r.top_facilities_driving_cost" not in block
        assert "const drivers = shown" in block
        assert "total_facility_cost: Number(k.total_facility_cost)" in block
        assert "r.n_bottlenecks" not in block

    def test_the_donut_folds_its_tail_into_other_rather_than_dropping_it(self):
        """Beyond about eight the slices are too thin to read — but dropping
        them leaves a doughnut that does not sum to its own total."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseSpendChart("):]
        block = block[:block.index("\n}")]
        assert "MAX_SLICES" in block
        assert "Other (" in block

    def test_a_donut_writes_its_shares_on_the_slices(self):
        """A legend of names makes the reader match colours back and forth to
        find out how something is split."""
        charts = _asset("charts.js")
        assert "const doughnutSliceShare = {" in charts
        # OPT-IN: a registered plugin runs on every chart in the app, and this
        # one would have started writing percentages onto doughnuts nobody
        # asked to change.
        block = charts[charts.index("const doughnutSliceShare = {"):]
        block = block[:block.index("\n};")]
        assert "opts.enabled !== true" in block
        # Every doughnut on this screen, not two of them.
        assert charts.count("ngDoughnutShare: { enabled: true }") == 3

    def test_switching_lens_returns_to_that_lens_whole_population(self):
        """
        A selected site is never carried across: the same name on another lens
        is a different site or no site at all. Region and Status describe a
        facility and mean nothing to a corridor.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("function setDomain(domain)"):]
        block = block[:block.index("\n}")]
        for cleared in ("view.entityId = null", "view.region = 'all'",
                        "view.status = 'all'", "view.mode = 'all'"):
            assert cleared in block, cleared

    def test_narrowing_cannot_strand_the_reader_inside_a_removed_site(self):
        """
        A filter that removes the site being viewed drops back to the roll-up
        rather than showing a detail page for a row that is no longer in the
        population above it.

        Checked at APPLY now: the controls stage, so the moment a narrowing
        becomes real is the moment it is committed, not the moment a select
        changes.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("el('kpi-filters-apply')?.addEventListener"):]
        block = block[:block.index("\n  });")]
        assert "warehouseFacets().entities.some" in block
        assert "view.entityId = null" in block

    def test_the_drill_down_replaces_the_rollup_rather_than_sitting_below_it(self):
        """
        The whole point. Selecting a site swaps the roll-up for that site's
        detail in the same place; it does not leave the reader scrolling past
        four network charts and a nine-row table to reach it.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("export function applyView()"):]
        block = block[:block.index("\n}")]
        assert "rollup.style.display = (isLane || onEntity) ? 'none' : ''" in block
        assert "entity.style.display = onEntity ? '' : 'none'" in block
        assert "lanes.style.display = isLane ? '' : 'none'" in block

    def test_every_row_of_the_health_table_opens_that_site(self):
        js = _asset("warehouse.js")
        assert 'class="wh-health-row" data-facility-id=' in js
        view = _asset("kpi-view.js")
        assert "#table-wh-health tbody" in view
        assert "selectEntity(row.dataset.facilityId)" in view

    def test_the_corridor_lens_ranks_on_spend_and_says_when_it_cannot(self):
        """
        A list headed "highest-cost" ranked on a rate the volume could reverse
        is worse than no list. Where no corridor carries a solved flow there is
        no spend to rank, so the card ranks by the rate AND says so.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("function renderLaneView()"):]
        block = block[:block.index("\n}")]
        assert "laneSpend" in block
        assert "by rate — no solved transport cost" in block

    def test_an_absent_corridor_reading_is_not_exported_or_drawn_as_zero(self):
        """
        A corridor the solve routed nothing down is not one that costs zero.

        `figure()` rejects null and empty before converting, because
        `Number(null)` is 0 and a finite check therefore passed for a reading
        nobody made.
        """
        view = _asset("kpi-view.js")
        spend = view[view.index("function laneSpend(lane)"):]
        spend = spend[:spend.index("\n}")]
        assert "figure(lane.transportCost)" in spend
        # The browser no longer multiplies a rate by a volume to get money.
        assert "rate * flow" not in spend

    def test_the_screen_states_what_is_on_it(self):
        """A filtered screen that does not say it is filtered is a screen
        reporting a subset as the whole."""
        panel = self._panel()
        assert 'id="kpi-showing"' in panel
        view = _asset("kpi-view.js")
        block = view[view.index("function renderShowing()"):]
        block = block[:block.index("\nfunction renderBreadcrumb")]
        assert "Showing:" in block
        # The roll-up says how many of how many, not just how many.
        assert "of ${facets.domainCount} sites" in block

    def test_the_controls_offer_only_what_the_network_contains(self):
        """A filter leading only to an empty screen is a control that lies
        about what is there."""
        js = _asset("warehouse.js")
        block = js[js.index("export function warehouseFacets()"):]
        block = block[:block.index("\n}")]
        assert "new Set(inDomain.map" in block
        assert "inDomain.some((k) => k.health_band === band)" in block

    def test_the_two_lenses_together_show_every_facility(self):
        """
        A role the storage set does not name is a production or supply site and
        belongs to the Plants lens. Anything else would let a facility the
        solve reported fall off both tabs.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function visibleRows()"):]
        block = block[:block.index("\n}")]
        assert "view.domain === 'dc' && !storage" in block
        assert "view.domain === 'plant' && storage" in block

    def test_a_project_switch_drops_the_filters_with_the_report(self):
        """A region filter carried into a project that has no such region
        would open the new network on an empty screen."""
        js = _asset("warehouse.js")
        block = js[js.index("export function clearWarehouseState()"):]
        block = block[:block.index("\n}")]
        for reset in ("view.domain = 'dc'", "view.region = 'all'", "view.status = 'all'"):
            assert reset in block, reset

    def test_the_view_controller_computes_no_kpi(self):
        """
        Every facility figure on this screen is the backend's own record. The
        controller decides WHICH records are on screen and says so in words.
        """
        view = _asset("kpi-view.js")
        for banned in ("utilization_pct *", "/ periods", "avg_utilization_pct +"):
            assert banned not in view, banned


class TestTheCorrectionsAfterReview:
    """
    What the review of Phase 1 found on the live screen, held as tests.

    Each of these was a real reading a user took off the page and reported —
    not a hypothetical. They are grouped because they share one cause: a figure
    or a label that described something other than what the reader was
    looking at.
    """

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_the_network_service_level_is_off_the_facility_cards(self):
        """
        It showed the NETWORK's demand-served figure on a card headed by a
        facility's name, among six cards that are all about that one site.
        Captioning it "Network-wide" did not fix that: a reader scanning six
        cards about Atlanta reads the fifth as Atlanta's too.

        Service level is a property of demand, not of a building, and this
        model does not decompose it per site.
        """
        app_js = _asset("app.js")
        block = app_js[app_js.index("const metricsGrid = document.getElementById('dash-metrics-grid')"):]
        block = block[:block.index("// Connected Lanes calculation")]
        assert "Demand Served Within SLA" not in block
        assert "kpis?.sla?.value" not in block

    def test_a_solved_quantity_is_not_printed_to_three_decimals(self):
        """
        A solver returns continuous quantities, so throughput arrived as
        10982.667 and was printed as "10,982.667 units/month" — three decimals
        of a unit nobody can ship, on a screen a planner scans.
        """
        data_js = _asset("data.js")
        block = data_js[data_js.index("export function formatNumber(value)"):]
        block = block[:block.index("\n}")]
        assert "maximumFractionDigits: 0" in block

    def test_one_utilisation_is_written_to_one_precision(self):
        """A DC's stored percentage carried the solver's full precision and
        printed "34.32%" beside a plant's "82.7%": two precisions for one
        metric on one screen."""
        app_js = _asset("app.js")
        block = app_js[app_js.index("const isDC = isDCFacility(state.selectedFacility);"):]
        block = block[:block.index("const utilColor")]
        assert "toFixed(1)" in block
        # Both branches go through the same rounding, not just the plant one.
        assert "const utilRaw" in block

    def test_a_donut_puts_its_values_on_the_legend(self):
        """
        A doughnut answers "how is this split", and a legend of bare names
        makes the reader hover each slice in turn to find out.

        The SHARE is on the slice; the legend carries the name and the value.
        Printing the percentage in both made the legend the longest thing in
        the card.
        """
        charts = _asset("charts.js")
        mixed = charts[charts.index("export function renderWarehouseStatusMixChart("):]
        mixed = mixed[:mixed.index("\n}")]
        assert "generateLabels:" in mixed
        # The spend legend moved OUT of the canvas: a drawn legend cannot
        # ellipsise to the room it has, so it clipped the amount instead.
        spend = charts[charts.index("export function renderWarehouseSpendChart("):]
        spend = spend[:spend.index("\n}")]
        assert "renderHtmlLegend(" in spend
        assert "formatCurrency(sl.value)" in spend
        mix = charts[charts.index("export function renderWarehouseStatusMixChart("):]
        mix = mix[:mix.index("\n}")]
        assert "${label} — ${value}" in mix

    def test_a_card_states_an_absence_in_words(self):
        """
        A dash carrying its reason on hover is not enough on a card: most
        readers never hover, and an em dash where a number should be reads as
        "nothing" or, worse, as zero.
        """
        js = _asset("warehouse.js")
        assert "function absentLabel(" in js
        summary = js[js.index("function renderSummary()"):js.index("function renderAttention")]
        assert "absentLabel(" in summary

    def test_the_drill_down_lands_at_the_top_and_stays_there(self):
        """
        A smooth scroll animates while `renderFacilityDashboard()` is still
        drawing charts on a 60ms timer; each chart that lands changes the page
        height under the running animation, and the scroll finished wherever
        the shifting content left it — halfway down Atlanta's charts, with the
        breadcrumb off-screen.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("function scrollViewToTop()"):]
        block = block[:block.index("\n}")]
        # Instant, not animated, so a height change cannot strand it...
        assert "behavior" not in block
        assert "scrollTop = 0" in block
        # ...and again once the chart timers have run.
        assert "setTimeout(" in block


class TestTheCorridorLens:
    """
    The corridor lens is a different population from the two facility lenses,
    and everything on it has to say so.
    """

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_it_has_a_scorecard_of_its_own(self):
        """
        It had none. The facility grid was left visible carrying whatever the
        previous lens had rendered, so switching from Plants to Freight showed
        "Production sites 2 of 2" above a corridor table, with the caption
        blanked so nothing even labelled it.
        """
        assert 'id="kpi-lane-summary"' in self._panel()
        view = _asset("kpi-view.js")
        assert "function renderLaneSummary()" in view
        # Counted over the corridors on screen, not over every corridor.
        block = view[view.index("function renderLaneSummary()"):]
        block = block[:block.index("\nfunction renderLaneView")]
        assert "visibleLanes()" in block

    def test_exactly_one_scorecard_is_shown_at_a_time(self):
        """
        Each is tied to the lens it belongs to, rather than to "not the network
        one" — which is how the facility grid came to be left on the corridor
        lens.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("export function applyView()"):]
        block = block[:block.index("renderControls()")]
        assert "strip.style.display = isNetwork ? '' : 'none'" in block
        assert "cards.style.display = (!isNetwork && !isLane) ? '' : 'none'" in block
        assert "laneCards.style.display = isLane ? '' : 'none'" in block

    def test_a_corridor_reading_the_solve_did_not_make_is_not_zero(self):
        """A rate per unit with no volume behind it is not a spend of zero."""
        view = _asset("kpi-view.js")
        block = view[view.index("function renderLaneSummary()"):]
        block = block[:block.index("\nfunction renderLaneView")]
        assert "Not solved" in block

    def test_each_control_means_one_thing(self):
        """
        The Status slot used to be relabelled "Mode" on this lens — one select
        meaning two different things depending on a tab, which a reader has to
        check before they can trust it.
        """
        panel = self._panel()
        for control in ("kpi-filter-origin", "kpi-filter-dest", "kpi-filter-mode",
                        "kpi-filter-status", "kpi-filter-region", "kpi-filter-entity"):
            assert f'id="{control}"' in panel, control
        view = _asset("kpi-view.js")
        # No renaming of a control's label at runtime any more.
        assert "label.textContent = 'Mode'" not in view

    def test_each_lens_shows_only_the_dimensions_that_describe_it(self):
        """A corridor has no health band and a facility has no origin."""
        view = _asset("kpi-view.js")
        block = view[view.index("function renderControls() {"):]
        block = block[:block.index("/** What is on screen")]
        lane = block[block.index("if (isLane) {"):block.index("const facets = warehouseFacets();")]
        assert "show('kpi-filter-entity-wrap', false)" in lane
        assert "show('kpi-filter-status-wrap', false)" in lane
        assert "show('kpi-filter-origin-wrap', true)" in lane
        facility = block[block.index("const facets = warehouseFacets();"):]
        assert "show('kpi-filter-origin-wrap', false)" in facility
        assert "show('kpi-filter-mode-wrap', false)" in facility

    def test_origin_and_destination_are_dependent(self):
        """
        A control must not offer a value that leads to an empty screen.
        Picking an origin narrows the destinations to the ones that origin
        actually serves.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("function laneFacets()"):]
        block = block[:block.index("\n}")]
        assert "view.destination === 'all'" in block
        assert "view.origin === 'all'" in block

    def test_a_narrowing_clears_a_pair_that_can_no_longer_exist(self):
        """Choosing an origin can leave the destination naming a corridor the
        narrowed set no longer contains, so the panel re-offers only what is
        reachable before the reader commits."""
        view = _asset("kpi-view.js")
        start = view.index("el('kpi-filter-origin')?.addEventListener")
        block = view[start:view.index("\n  });", start)]
        assert "staged.destination = 'all'" in block
        assert "renderControls()" in block

    def test_there_is_no_single_lane_or_period_control(self):
        """
        Origin and destination together already narrow to one corridor and
        degrade gracefully on the way; a single-lane control would leave three
        panels saying less than the table row the reader came from.

        And a flow row carries no per-period breakdown, so a period control
        would show the same figures whichever period was chosen — the dead
        control this product refuses to ship.
        """
        panel = self._panel()
        assert 'id="kpi-filter-lane"' not in panel
        assert 'id="kpi-filter-lane-period"' not in panel
        view = _asset("kpi-view.js")
        assert "view.period" not in view


class TestTheBriefingStatesAFinding:

    def test_the_prompt_forbids_a_roster_as_the_conclusion(self):
        """
        "Name the real things" is satisfied, literally and uselessly, by
        listing every site in the payload. A live call on a network where every
        site read the same returned six facility names as the conclusion AND as
        the paragraph under it.
        """
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
               / "reasoning_agent.py").read_text(encoding="utf-8")
        block = src[src.index('"RULES: no figures'):]
        block = block[:block.index('"Reply with ONLY this JSON')]
        assert "State a finding, not a roster" in block
        # A headline is capped at 140 characters, so a sentence that inlines
        # every name is truncated into nonsense.
        assert "name at most" in block
        assert "no facility" in block

    def test_the_rule_is_short_enough_to_be_followed(self):
        """
        The instruction block has a hard budget, and it is not stylistic: past
        roughly 900 characters this model spends its whole output allowance
        deliberating and returns nothing parseable — the exact intermittent
        failure that makes explanations fall back to templates.

        The first version of this rule was 390 characters of prose and pushed
        the block to 1186. It says the same thing in a quarter of the space.
        """
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
               / "reasoning_agent.py").read_text(encoding="utf-8")
        block = src[src.index('"RULES: no figures'):]
        block = block[:block.index('"Reply with ONLY this JSON')]
        # The rendered instruction, not the source with its quotes and breaks.
        rendered = "".join(line.strip().strip('"') for line in block.splitlines()
                           if line.strip().startswith('"'))
        assert len(rendered) < 800, len(rendered)


class TestOneFigureMeansOneThing:
    """
    The corrections that changed what a number SAYS, rather than how it looks.
    """

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_there_is_one_facility_cost_and_every_screen_reads_it(self):
        """
        The detail card read `kpis.totalCost`, which the per-facility endpoint
        never produces — it returns utilisation, throughput, capacity and
        is_open, and no cost at all. So the card was blank on every real
        network while the table beside it showed a figure.

        Not two definitions disagreeing: one real metric, and one card reading
        a field nobody fills. Both now read the warehouse report's
        `total_facility_cost`.
        """
        app_js = _asset("app.js")
        imports = app_js[app_js.index("import { renderWarehouseDashboard"):]
        imports = imports[:imports.index("';") + 2]
        assert "warehouseRow" in imports
        assert "from './warehouse.js'" in imports
        block = app_js[app_js.index("const whRow = warehouseRow(state.selectedFacility)"):]
        block = block[:block.index("// 6 Executive Metric Cards")]
        assert "total_facility_cost" in block
        # The old, never-populated field is gone from the card.
        grid = app_js[app_js.index("const metricsGrid = document.getElementById('dash-metrics-grid')"):]
        grid = grid[:grid.index("// Connected Lanes calculation")]
        assert "kpis?.totalCost" not in grid
        # And it names what the figure covers, so it cannot be read as
        # everything the site costs.
        assert "transport excluded" in grid

    def test_a_reported_zero_is_not_a_missing_stock_reading(self):
        """
        The backend keeps them apart: no inventory decisions comes back as
        INSUFFICIENT_EVIDENCE with a reason saying it is "not a reading of zero
        stock", while 0.0 is a measured level. The caption flattened that — it
        said "12 of 12 sites hold stock" when what it had counted was sites
        that REPORTED, including any holding nothing.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function renderStock()"):js.index("function renderHealthTable()")]
        assert "Number(k.peak_inventory_units) > 0" in block
        assert "hold stock" in block
        assert "report none" in block

    def test_a_corridor_with_no_solved_flow_is_named_not_counted_as_zero(self):
        view = _asset("kpi-view.js")
        block = view[view.index("function renderLaneSummary()"):]
        block = block[:block.index("\nfunction renderLaneView")]
        # Spend counts corridors carrying a solved transport cost; volume
        # counts those carrying a solved volume. Each says what it left out.
        assert "report none" in block
        assert "report no volume" in block


class TestTheExport:

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_the_export_is_wired(self):
        """
        It was not. A block replacement in this module removed the listener
        along with the menu it belonged to, so the control rendered and did
        nothing — which is worse than a missing button, because the reader
        keeps pressing it.
        """
        panel = self._panel()
        assert 'id="btn-export-pdf"' in panel
        view = _asset("kpi-view.js")
        assert "el('btn-export-pdf')?.addEventListener" in view
        assert "exportKpiViewAsPdf()" in view

    def test_the_pdf_is_the_page_itself(self):
        """
        The requirement is "exactly the visuals displayed". The thing that IS
        exactly those visuals is the page — a client-side renderer would
        re-draw every chart and could drift from what the reader is looking at,
        which is the class of bug this screen has been spent removing.
        """
        view = _asset("kpi-view.js")
        assert "export function exportKpiViewAsPdf()" in view
        block = view[view.index("export function exportKpiViewAsPdf()"):]
        block = block[:block.index("\n}")]
        assert "window.print()" in block
        # An open explanation floats OVER a chart, so printing with it open
        # would hide the visual it describes.
        assert "closeKpiExplainPanels()" in block

    def test_the_printed_copy_states_what_produced_it(self):
        """A PDF that does not name the project, the horizon and the filters
        behind its numbers cannot be checked later."""
        panel = self._panel()
        assert 'id="kpi-print-head"' in panel
        assert "Netgravity" in panel
        view = _asset("kpi-view.js")
        block = view[view.index("function renderPrintHeader()"):]
        block = block[:block.index("\n}")]
        for field in ("Project", "View", "Horizon", "Showing", "Exported"):
            assert f"'{field}'" in block, field

    def test_the_application_chrome_does_not_print(self):
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index("@media print {"):]
        for chrome in (".sidebar", ".app-global-topbar", ".kpi-export",
                       ".kpi-explain-btn", ".facility-toolbar"):
            assert chrome in block, chrome
        # Only the KPI screen prints, whatever else is mounted.
        assert ".tab-panel#tab-facility-dashboard { display: block !important; }" in block

    def test_the_page_is_constrained_to_the_paper(self):
        """The app lays out against a viewport wider than A4 landscape, so
        without this the right-hand card of every row was sliced down its
        edge."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index("@media print {"):]
        assert "max-width: 100% !important" in block
        assert "@page { size: A4 landscape" in block


class TestTheCorridorTableIsUsable:

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_a_search_finds_rows_without_changing_the_cards(self):
        """
        FIND, not filter. The selects above scope the whole lens; this narrows
        only the table's rows, which is what a reader wants when they know the
        corridor they are after and do not want to change what the cards above
        them are reporting.
        """
        assert 'id="kpi-lane-search"' in self._panel()
        view = _asset("kpi-view.js")
        block = view[view.index("function renderLaneView() {"):]
        block = block[:block.index("// ─── The controls")]
        # The cards read `lanes`; only the table reads the searched subset.
        assert "const found = term" in block
        assert "found.map((l)" in block

    def test_typing_does_not_take_the_cursor_out_of_the_box(self):
        """Re-running the whole view would rebuild the controls, and the input
        being typed into with them."""
        view = _asset("kpi-view.js")
        start = view.index("el('kpi-lane-search')?.addEventListener")
        block = view[start:view.index("\n  });", start)]
        assert "renderLaneView()" in block
        assert "applyView()" not in block

    def test_it_searches_the_things_a_reader_knows(self):
        view = _asset("kpi-view.js")
        block = view[view.index("const found = term"):]
        block = block[:block.index(";")]
        # Origin and destination arrive together in the corridor's name.
        assert "laneName(l)" in block
        assert "l.mode" in block

    def test_a_search_that_matches_nothing_says_so(self):
        """Distinct from "your filters match nothing" — the reader mistyped a
        name, they did not narrow the lens."""
        view = _asset("kpi-view.js")
        assert "No corridor matches that search." in view
        assert "No corridor matches the current filters." in view

    def test_clearing_the_lens_clears_the_search_with_it(self):
        view = _asset("kpi-view.js")
        block = view[view.index("el('kpi-filter-reset')?.addEventListener"):]
        block = block[:block.index("\n  });")]
        assert "view.laneSearch = ''" in block

    def test_the_column_headers_stay_put(self):
        """A corridor table runs to fifty rows, and a reader scrolled into the
        middle of it was reading unlabelled numbers."""
        assert "table-wrap-sticky" in self._panel()
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".table-wrap-sticky thead th {"):]
        block = block[:block.index("}")]
        assert "position: sticky" in block

    def test_the_search_and_the_scroll_cap_do_not_reach_the_pdf(self):
        """A control nobody can use, and a table cut off at 460 pixels."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        tail = css[css.rindex("@media print {"):]
        assert ".kpi-table-search { display: none !important; }" in tail
        assert "max-height: none" in tail


class TestAbsenceIsSaidInWords:
    """
    An em dash where a number should be reads as "nothing" or, worse, as zero,
    and a reader has no way to tell which.
    """

    def test_the_facility_cards_name_what_is_missing(self):
        app_js = _asset("app.js")
        block = app_js[app_js.index("const absent = (reason = 'Not reported')"):]
        block = block[:block.index("// Connected Lanes calculation")]
        assert "wh-absent-label" in block
        # No bare em dash left standing in for a figure on these cards.
        assert "? '—' :" not in block
        assert "<strong>—</strong>" not in block

    def test_the_corridor_table_names_what_is_missing(self):
        view = _asset("kpi-view.js")
        block = view[view.index("const ABSENT ="):]
        block = block[:block.index("\n")]
        assert "Not reported" in block
        assert "&mdash;" not in block

    def test_the_dense_health_table_may_still_use_a_dash(self):
        """
        Eleven numeric columns. A column of "Not reported" would be unreadable,
        and the heading above each cell already names what is absent — so this
        is the one place a dash still earns its keep, and it carries its reason
        on hover.
        """
        js = _asset("warehouse.js")
        block = js[js.index("function absent(reason) {"):]
        block = block[:block.index("\n}")]
        assert "&mdash;" in block
        assert "title=" in block


class TestAMissingFlowIsNotAZeroFlow:
    """
    `Number(null)` is 0, not NaN.

    So `Number.isFinite(Number(lane.flow))` was TRUE for a corridor the solve
    never routed anything down, and every missing volume silently became a zero
    volume: the spend card counted those corridors as "calculated" and
    multiplied a real rate by a flow nobody reported to get zero, and the
    volume card averaged them in. A corridor with no reported flow does not
    cost nothing — it is not known to cost anything.
    """

    def test_absence_is_read_as_absence_not_as_zero(self):
        view = _asset("kpi-view.js")
        block = view[view.index("function figure(value) {"):]
        block = block[:block.index("\n}")]
        # null and undefined are rejected BEFORE the numeric conversion.
        assert "value === null || value === undefined" in block

    def test_spend_is_the_solvers_own_transport_cost(self):
        """
        IT WAS A MULTIPLICATION IN THE BROWSER, and of two different bases.

        `rate × lane.flow` used the PER-PERIOD volume, so the corridor-spend
        card reported a per-period amount on a screen whose every other cost
        is a horizon total — about a twelfth of the transport spend, leaving a
        gap under the network cost that no line on the page accounted for.

        `transport_cost` is the engine's own charge for that corridor across
        the horizon. Reading it reconciles the screen and keeps §9: the
        frontend calculates no authoritative KPI.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("function laneSpend(lane) {"):]
        block = block[:block.index("\n}")]
        assert "figure(lane.transportCost)" in block
        # No arithmetic on money here, and not the per-period volume either.
        assert "rate * flow" not in block
        assert "lane.flow" not in block

    def test_the_lane_cards_count_only_what_was_reported(self):
        view = _asset("kpi-view.js")
        block = view[view.index("function renderLaneSummary()"):]
        block = block[:block.index("\nfunction renderLaneView")]
        # Volume and mode share read absence the same way spend does.
        assert "figure(l.flow)" in block
        assert "Number(l.flow)" not in block
        # ...and the cards say how many were left out.
        assert "report none" in block
        assert "report no volume" in block

    def test_the_preview_exercises_the_absent_path(self):
        """
        A mock where every corridor has a flow only ever tests the happy path,
        which is how this shipped: the bug was invisible until a corridor with
        no reported volume was on screen.
        """
        preview = (REPO_ROOT / "app" / "frontend" / "js"
                   / "standalone-preview.js").read_text(encoding="utf-8")
        block = preview[preview.index("  lanes: ["):]
        block = block[:block.index("\n  ],")]
        assert "flow: null" in block
        assert "cost: null" in block


class TestTheFilterPanel:
    """
    Three loose selects read as a form, and each one re-solved the screen the
    moment it changed — so narrowing by region and then by status redrew
    everything twice, and the reader watched the page move under a decision
    they had not finished making.
    """

    def _panel(self) -> str:
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        return panel[:panel.index("</section>")]

    def test_the_controls_live_in_one_elevated_panel(self):
        panel = self._panel()
        for part in ("kpi-filters-trigger", "kpi-filters-panel",
                     "kpi-filters-apply", "kpi-filter-reset"):
            assert part in panel, part
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-filters-panel {"):]
        block = block[:block.index("}")]
        assert "box-shadow: var(--shadow-lg)" in block

    def test_nothing_moves_until_apply(self):
        """The whole point: set three things, see the consequence once."""
        view = _asset("kpi-view.js")
        for control in ("kpi-filter-entity", "kpi-filter-region", "kpi-filter-status"):
            start = view.index(f"el('{control}')?.addEventListener")
            block = view[start:view.index("\n  });", start)]
            assert "staged." in block, control
            # A staging control must not redraw the screen behind the panel.
            assert "applyView()" not in block, control
        commit = view[view.index("el('kpi-filters-apply')?.addEventListener"):]
        commit = commit[:commit.index("\n  });")]
        assert "applyView()" in commit

    def test_the_panel_opens_on_what_is_actually_on_screen(self):
        """An abandoned edit must not leak into the next one."""
        view = _asset("kpi-view.js")
        block = view[view.index("const openPanel = (open) =>"):]
        block = block[:block.index("\n  };")]
        assert "stageFromView()" in block

    def test_clicking_away_abandons_rather_than_half_applies(self):
        view = _asset("kpi-view.js")
        assert "if (!el('kpi-filters')?.contains(e.target)) openPanel(false);" in view

    def test_the_closed_control_says_what_is_active(self):
        """A filtered screen has to say it is filtered with the panel shut."""
        view = _asset("kpi-view.js")
        assert "function renderFilterTrigger()" in view
        block = view[view.index("function renderFilterTrigger()"):]
        block = block[:block.index("\n}")]
        assert "activeFilterCount()" in block
        # It names the selections, not just a count.
        assert "endpointName(view.origin)" in block
        assert "view.region" in block

    def test_reset_clears_both_the_edit_and_the_view(self):
        """There is no half-reset state to be surprised by."""
        view = _asset("kpi-view.js")
        block = view[view.index("el('kpi-filter-reset')?.addEventListener"):]
        block = block[:block.index("\n  });")]
        assert "stageFromView()" in block
        assert "view.region = 'all'" in block
        assert "applyView()" in block

    def test_the_panel_does_not_print(self):
        """A control nobody can click, on paper. The header carries the
        selections instead."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        rule = ".kpi-filters-panel, .kpi-filters-trigger { display: none !important; }"
        assert rule in css
        # There is more than one print block in this sheet, so the check is
        # that the rule sits inside one — not inside the last one.
        assert css.index("@media print {") < css.index(rule)


class TestTheAiButtonInvitesWithoutNagging:

    def test_the_reflection_repeats_but_does_not_loop(self):
        """
        A single sweep on arrival is missed by anyone reading the scorecard at
        the time; a control that animates continuously is what makes a
        dashboard tiring.
        """
        js = _asset("kpi-explain.js")
        assert "SHIMMER_INTERVAL_MS" in js
        block = js[js.index("export function startKpiExplainShimmer()"):]
        block = block[:block.index("\n}")]
        assert "setInterval" in block
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        anim = css[css.index(".kpi-explain-btn.is-shimmering::after {"):]
        anim = anim[:anim.index("}")]
        assert "infinite" not in anim

    def test_it_rests_while_the_tab_is_hidden(self):
        js = _asset("kpi-explain.js")
        assert "document.hidden" in js

    def test_it_stops_for_good_once_used(self):
        js = _asset("kpi-explain.js")
        block = js[js.index("function stopShimmer() {"):]
        block = block[:block.index("\n}")]
        assert "clearInterval" in block
        assert "shimmerDone = true" in block

    def test_the_button_is_readable_against_the_card(self):
        """It was primary-on-primary-light with a transparent border, which at
        12px did not register as a control at all."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-explain-btn {"):]
        block = block[:block.index("}")]
        assert "border: 1px solid var(--purple-200)" in block
        assert "font-weight: 700" in block


class TestTheScorecardIsNotMistakenForTheSite:

    def test_a_drill_down_names_the_scorecard_as_the_lens_summary(self):
        """
        It does not narrow to one facility, so above a screen headed
        "Bengaluru — one facility" it read "4 of 5 open" and "49.0%", which a
        reader takes for Bengaluru's own.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("const note = el('kpi-scorecard-note');"):]
        block = block[:block.index("\n  }")]
        assert "network summary" in block
        assert "not this facility's own figures" in block
        assert "Distribution-centre" in block


class TestTheScreenSpeaksOneColourLanguage:
    """
    TWO vocabularies, and they must not be confused with each other:

      STATUS   red, amber, green, blue, grey — each names a health band and
               nothing else;
      IDENTITY purples and teals, for telling one FACILITY from another, with
               no meaning beyond "not the same site as the last one".

    They were mixed. The spend doughnut ran through the status hues to colour
    facilities, so a site could be drawn in the exact red the doughnut beside
    it used for "over capacity"; and the utilisation chart drew everything
    under the threshold in purple, so an under-used site was blue in the table,
    blue in the doughnut and purple in the bar chart above them.
    """

    def _palettes(self):
        import re
        charts = _asset("charts.js")
        band = charts[charts.index("export const BAND_COLOUR = {"):]
        band = band[:band.index("};")]
        ident = charts[charts.index("export const IDENTITY_PALETTE = ["):]
        ident = ident[:ident.index("];")]
        hexes = lambda t: {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", t)}
        return hexes(band), hexes(ident)

    def test_status_and_identity_share_no_colour(self):
        """A reader who has learned that red means trouble cannot be shown a
        red that means "the third facility"."""
        status, identity = self._palettes()
        assert status, "no status palette found"
        assert identity, "no identity palette found"
        assert not (status & identity), sorted(status & identity)

    def test_the_status_colours_are_defined_once(self):
        """A second copy is how one state comes to be drawn in two colours."""
        charts = _asset("charts.js")
        warehouse = _asset("warehouse.js")
        assert "export const BAND_COLOUR" in charts
        # warehouse.js imports them rather than restating them.
        assert "BAND_COLOUR } from './charts.js'" in warehouse
        assert "const BAND_COLOUR = {" not in warehouse

    def test_the_utilisation_bars_are_coloured_by_state(self):
        """Not by threshold arithmetic of their own — the row already carries
        the band every other part of this screen reads."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseUtilisationChart("):]
        block = block[:block.index("\n}")]
        assert "BAND_COLOUR[r.health_band]" in block
        # The old hard-coded ladder is gone.
        assert "v >= thresholdPct ? '#d97706'" not in block

    def test_the_spend_doughnut_uses_identity_colours(self):
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseSpendChart("):]
        block = block[:block.index("\n}")]
        assert "const palette = IDENTITY_PALETTE;" in block

    def test_a_multi_coloured_series_gets_a_neutral_legend_swatch(self):
        """
        Chart.js takes the first bar's colour for the legend dot, so a red dot
        appeared beside "Peak period" — saying the series is red, when red
        means "over capacity" here and most of the bars are not.
        """
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseUtilisationChart("):]
        block = block[:block.index("\n}")]
        assert "generateLabels:" in block
        assert "#5a5a72" in block


class TestTheFacilityCostBreakdownIsReal:
    """
    This chart used to be invented. It read:

        const transportCost = isDC ? 580000 : 720000;
        const holdingCost   = 140000;
        const surchargeCost = 45000;
        const fixedCost     = (facility.fixedCost || 100) * 100000 / 12;

    — five hard-coded figures and two magic multipliers, drawn as a doughnut
    labelled with the site's name, in the same currency as every solved number
    beside it. A reader had no way to tell it apart. It also decided the site's
    role from the spelling of its id, which this product settled long ago.
    """

    def test_no_cost_is_conjured_from_a_constant(self):
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityCostBreakdownChart("):]
        block = block[:block.index("\nexport function renderFacilityLaneFlowsChart")]
        for invented in ("580000", "720000", "140000", "45000", "* 30", "/ 12"):
            assert invented not in block, invented
        assert "startsWith('DC_')" not in block

    def test_it_reads_the_components_the_engine_reports(self):
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityCostBreakdownChart("):]
        block = block[:block.index("\nexport function renderFacilityLaneFlowsChart")]
        for field in ("fixed_cost", "opening_cost", "handling_cost", "holding_cost"):
            assert field in block, field
        # Which are the same four that sum to the card above it.
        app_js = _asset("app.js")
        assert "renderFacilityCostBreakdownChart('chart-dash-costs', whRow)" in app_js

    def test_transport_is_not_implied(self):
        """The model does not attribute it to a site, and the old subtitle
        named it first."""
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        assert "Transport, Handling, Storage, Holding & Accessorials" not in panel
        assert "Facility Cost Breakdown" in panel

    def test_a_site_with_no_reported_cost_draws_nothing(self):
        """A ring of zeroes reads as "this site costs nothing"."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityCostBreakdownChart("):]
        block = block[:block.index("\nexport function renderFacilityLaneFlowsChart")]
        assert "parts.length === 0" in block
        assert "reported no facility cost" in block

    def test_its_slices_are_identity_colours(self):
        """A cost component is not a health band."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityCostBreakdownChart("):]
        block = block[:block.index("\nexport function renderFacilityLaneFlowsChart")]
        assert "IDENTITY_PALETTE" in block
        for status_hue in ("#dc2626", "#16a34a", "#2563eb", "#f59e0b"):
            assert status_hue not in block, status_hue


class TestTheCorridorDetailStatesAbsence:

    def test_a_flow_with_no_reading_carries_no_unit(self):
        """"— units/month" reads as a measurement of nothing per month."""
        app_js = _asset("app.js")
        block = app_js[app_js.index("const tableBody = document.querySelector('#table-dash-lanes tbody')"):]
        block = block[:block.index("\n  }")]
        assert "absent('Not reported')" in block
        assert "${formatNumber(l.flow)} ${perPeriodLabel()}</td>" not in block

    def test_the_flow_total_counts_only_what_was_reported(self):
        """`l.flow || 0` folded an absent reading into a total presented as
        the site's whole throughput."""
        app_js = _asset("app.js")
        assert "sum + (l.flow || 0)" not in app_js
        assert "const reportedFlows = connectedLanes" in app_js


class TestTheFilterPanelCanActuallyHide:

    def test_an_author_display_does_not_defeat_the_hidden_attribute(self):
        """
        `.kpi-filters-panel { display: flex }` BEATS the browser's own
        `[hidden] { display: none }`, so the panel was permanently visible
        however the attribute was set — through Apply, through clicking away,
        through a reload.
        """
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        assert "[hidden] { display: none !important; }" in css

    def test_the_controls_sit_side_by_side(self):
        """A column of six was taller than the charts it was filtering, and
        the Apply button fell below the fold."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-filters-panel {"):]
        block = block[:block.index("}")]
        assert "flex-wrap: wrap" in block
        assert "flex-direction: column" not in block
        # An absolutely positioned wrapping flex container shrink-wraps to its
        # widest item, not the sum — which stacked them anyway.
        assert "width: max-content" in block


class TestTheOverlayLeavesTheChartReadable:

    def test_it_is_a_column_in_the_corner_not_a_full_width_band(self):
        """It spanned the card and sat over the plot area, so a reader had the
        explanation or the picture but never both — and the sentence is about
        the picture."""
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-ai-overlay {"):]
        block = block[:block.index("}")]
        assert "right: 14px" in block
        # A SHARE of the card, not a fixed width: 320px is a corner on a wide
        # card and most of a narrow one, and on the facility detail it covered
        # the plot it was describing.
        assert "width: clamp(220px, 44%, 300px)" in block
        assert "left: 16px; right: 16px" not in block


class TestTheLegendGivesBackWhatItTruncated:

    def test_hovering_a_legend_row_reveals_the_full_name(self):
        """
        A canvas `onHover` that set the canvas title did not work: the browser
        decides a tooltip from the title at the moment hover BEGINS, so a title
        written during mousemove is never shown. A DOM row carries its own
        title and needs nothing re-implemented.
        """
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderHtmlLegend("):]
        block = block[:block.index("\n}")]
        assert 'title="${' in block
        assert "ng-legend-name" in block
        # The value is reserved and never truncates; the name gives way.
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        name = css[css.index(".ng-legend-name {"):]
        name = name[:name.index("}")]
        assert "text-overflow: ellipsis" in name
        value = css[css.index(".ng-legend-value {"):]
        value = value[:value.index("}")]
        assert "flex: 0 0 auto" in value

    def test_no_two_slices_share_a_colour(self):
        """
        A ninth slice wrapped the palette and came back the same purple as the
        first, so the largest facility and "everything else" looked like one
        thing. The remainder is not an identity and takes a neutral instead.
        """
        charts = _asset("charts.js")
        assert "export const OTHER_SLICE_COLOUR" in charts
        block = charts[charts.index("export function renderWarehouseSpendChart("):]
        block = block[:block.index("\n}")]
        assert "sl.isOther" in block and "OTHER_SLICE_COLOUR" in block
        # Eight identity colours for at most eight real slices.
        pal = charts[charts.index("export const IDENTITY_PALETTE = ["):]
        pal = pal[:pal.index("];")]
        import re
        hexes = re.findall(r"#[0-9a-fA-F]{6}", pal)
        assert len(hexes) == len(set(h.lower() for h in hexes)) == 8


class TestTheThroughputChartPlotsTheSolve:
    """
    The chart said one thing and the explanation beside it said another: a card
    stating an average of about 4,004 units, over a chart whose axis started at
    8,800. The AI was reading the solve; the picture was fiction.

    It read:

        const months     = ['Sep 25', 'Oct 25', … 'Nov 26 (F)'];   // fixed
        const historical = [baseTput * 0.88, baseTput * 0.90, …];  // a ramp
        const isAtRisk   = facility.id === 'DC_DELHI' || … ;       // two ids
        const growth     = isAtRisk ? 1.05 : 1.015;                // a guess

    — fifteen hard-coded month labels whatever horizon was solved, a "12-month
    history" that was one number times a fixed curve, and a "3-month
    projection" compounding a rate nothing had measured.
    """

    def test_no_series_is_conjured_from_a_base_figure(self):
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityThroughputChart("):]
        block = block[:block.index("\n/**")]
        for invented in ("* 0.88", "* 0.90", "growthMultiplier", "1.015",
                         "'Sep 25'", "(F)"):
            assert invented not in block, invented
        assert "DC_DELHI" not in block

    def test_it_plots_the_period_series_the_engine_reports(self):
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityThroughputChart("):]
        block = block[:block.index("\n/**")]
        assert "row.throughput_by_period" in block
        assert "rated_capacity_per_period" in block
        app_js = _asset("app.js")
        # The SAME record the explanation and the table read.
        assert "renderFacilityThroughputChart('chart-dash-throughput', whRow)" in app_js

    def test_the_series_actually_reaches_the_frontend(self):
        """
        Only figures DERIVED from it used to survive onto the contract — the
        peak, its period, how many were tight — so the screen had nothing real
        to plot even though the solve had computed it.
        """
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "metrics"
               / "warehouse_deep_dive.py").read_text(encoding="utf-8")
        model = src[src.index("class WarehouseHealthKPI(BaseModel):"):]
        model = model[:model.index("model_config")]
        assert "throughput_by_period: Dict[str, float]" in model
        assert "utilization_by_period: Dict[str, float]" in model
        # ...and is populated from the dicts the computation already had.
        assert "throughput_by_period=by_period," in src
        assert "utilization_by_period=util_by_period," in src

    def test_one_period_draws_nothing_rather_than_a_line(self):
        """One period is not a horizon, and two points invented between them
        is the defect this replaced."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityThroughputChart("):]
        block = block[:block.index("\n/**")]
        assert "periods.length < 2" in block
        assert "single period" in block

    def test_the_card_no_longer_promises_a_projection(self):
        """The solve does not produce one."""
        import re
        html = (REPO_ROOT / "app" / "frontend" / "index.html").read_text(encoding="utf-8")
        panel = html[html.index('id="tab-facility-dashboard"'):]
        panel = panel[:panel.index("</section>")]
        # What a READER sees: the source comment explaining this fix names the
        # old promise, and asserting over the raw markup catches that instead.
        visible = re.sub(r"<!--.*?-->", " ", panel, flags=re.S)
        assert "3-Month Projection" not in visible
        assert "Solved throughput each period" in visible

    def test_the_preview_series_agrees_with_its_own_summary(self):
        """
        A sample whose maximum is not the period it names as the peak teaches
        a tester that the screen is wrong when it is right. The first draft of
        this data did exactly that: a rescaled non-peak month came out at
        14,886 against a stated peak of 14,700.
        """
        import json
        import re
        preview = (REPO_ROOT / "app" / "frontend" / "js"
                   / "standalone-preview.js").read_text(encoding="utf-8")
        blocks = re.findall(r"throughput_by_period: (\{[^}]*\})", preview)
        assert blocks, "no preview series found to check"
        checked = 0
        for series_json in blocks:
            series = json.loads(series_json)
            # The row states its own peak and average a few lines below.
            tail = preview[preview.index(series_json) + len(series_json):]
            tail = tail[:600]
            peak = re.search(r"peak_throughput_units: (\d+)", tail)
            avg = re.search(r"avg_throughput_units: (\d+)", preview[:preview.index(series_json)][-400:])
            period = re.search(r"peak_period: '([^']+)'", tail)
            if not (peak and avg and period):
                continue
            assert max(series.values()) == float(peak.group(1)), series_json[:60]
            assert series[period.group(1)] == float(peak.group(1)), period.group(1)
            assert round(sum(series.values()) / len(series)) == int(avg.group(1))
            checked += 1
        assert checked >= 4, f"only {checked} series were checkable"
