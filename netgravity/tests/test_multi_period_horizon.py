"""
The multi-period MILP: what a horizon model must do that a collapsed one cannot.

The previous phase stopped a crash and then collapsed every period into one,
reporting that it had. This tests the model that replaced the collapse — flow,
demand, capacity and stock indexed by period — and in particular the things a
single-period model gets WRONG rather than merely approximates:

  * per-period capacity must bind per period, not over the horizon;
  * a peak the upstream cannot make in its own month must be servable by
    building stock ahead of it;
  * a fixed cost stated per period must be charged in every period;
  * a one-time cost must stay one-time.

Each of those is a different plan, not a different rounding.
"""

from __future__ import annotations

import pytest

from netgravity.costs.reconciliation import reconcile_kpis_and_objective
from netgravity.optimization.milp import milp_solve
from netgravity.optimization.periods import resolve_horizon
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


def build(demands, *, policy="FULL_HORIZON", dc_capacity=9999.0,
          plant_capacity=9999.0, unit_value=10.0, storage=None,
          dc_fixed_cost_per_year=1200.0, enable_inventory=False):
    """PLANT -> DC -> MKT, with each capacity separately squeezable."""
    dc = FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                        status=FacilityStatus.EXISTING,
                        capacity_units_per_period=dc_capacity,
                        fixed_cost_per_year=dc_fixed_cost_per_year)
    if storage is not None:
        dc.storage_capacity_units = storage
    return CanonicalNetwork(
        network_id="HORIZON",
        facilities=[
            FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                           status=FacilityStatus.EXISTING,
                           capacity_units_per_period=plant_capacity,
                           is_mandatory=True, is_closable=False),
            dc,
            FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                           status=FacilityStatus.EXISTING, is_closable=False),
        ],
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0,
                                unit_value=unit_value)],
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
            enable_inventory=enable_inventory, enforce_sla=False,
            enable_carbon_cost=False, allow_shortage=True, verbose=False,
            multi_period_policy=policy),
    )


def rows(*quantities):
    return [DemandRecord(market_id="MKT", product_id="P1", period=i + 1, quantity=q)
            for i, q in enumerate(quantities)]


def served(result) -> float:
    return sum(f.flow_units for f in result.flow_decisions
               if f.destination_id == "MKT")


def served_in(result, period) -> float:
    return sum(f.flow_units for f in result.flow_decisions
               if f.destination_id == "MKT" and f.period == period)


# ===========================================================================

class TestTheHorizonIsActuallyModelled:

    def test_full_horizon_is_the_default(self):
        """
        A network stating twelve months is solved as twelve months.

        The default was a collapse policy, which meant the ordinary case —
        someone uploading a year of demand — got an averaged answer unless they
        knew to ask for something else.
        """
        assert OptimizationConfig().multi_period_policy == "FULL_HORIZON"
        result = milp_solve(build(rows(100.0, 120.0, 140.0), policy="FULL_HORIZON"), None)
        assert result.period_report["modelled_periods"] == 3
        assert result.period_report["collapsed"] is False

    def test_every_period_is_served_separately(self):
        result = milp_solve(build(rows(100.0, 120.0, 140.0)), None)
        assert served_in(result, 1) == pytest.approx(100.0)
        assert served_in(result, 2) == pytest.approx(120.0)
        assert served_in(result, 3) == pytest.approx(140.0)
        assert served(result) == pytest.approx(360.0)

    def test_flow_decisions_carry_their_real_period(self):
        """`FlowDecision.period` was hardcoded to 1 for every flow."""
        result = milp_solve(build(rows(10.0, 20.0, 30.0)), None)
        assert {f.period for f in result.flow_decisions} == {1, 2, 3}

    def test_a_single_period_network_is_untouched(self):
        """The ordinary case must not be perturbed by the horizon machinery."""
        result = milp_solve(build(rows(100.0)), None)
        assert served(result) == pytest.approx(100.0)
        assert result.period_report["modelled_periods"] == 1
        assert result.inventory_decisions == []
        assert {f.period for f in result.flow_decisions} == {1}


class TestCapacityBindsPerPeriod:

    def test_a_per_period_capacity_is_not_a_horizon_budget(self):
        """
        150/period against 100+100+160 is short only in period 3.

        Applied to the horizon total instead, 450 of capacity against 360 of
        demand looks comfortable — and the plan would ship a whole quarter's
        volume in one month.
        """
        result = milp_solve(build(rows(100.0, 100.0, 160.0), dc_capacity=150.0), None)
        assert served_in(result, 1) == pytest.approx(100.0)
        assert served_in(result, 2) == pytest.approx(100.0)
        assert served_in(result, 3) == pytest.approx(150.0), \
            "period 3 must be capped at the per-period capacity"
        assert served(result) == pytest.approx(350.0)

    def test_utilization_is_reported_against_the_horizon_and_the_peak(self):
        result = milp_solve(build(rows(50.0, 50.0, 150.0), dc_capacity=150.0), None)
        dc = next(f for f in result.facility_decisions if f.facility_id == "DC")
        assert dc.n_periods == 3
        assert dc.capacity_units == pytest.approx(450.0), "horizon capacity"
        assert dc.utilization_pct == pytest.approx(250.0 / 450.0 * 100.0, abs=0.1)
        assert dc.peak_utilization_pct == pytest.approx(100.0, abs=0.1), \
            "the worst period is at capacity, and an average must not hide it"
        assert dc.throughput_by_period == {"1": 50.0, "2": 50.0, "3": 150.0}


class TestStockCarriesBetweenPeriods:

    def test_a_peak_the_plant_cannot_make_in_month_is_built_ahead(self):
        """
        The reason a horizon model exists.

        The plant can make 120 a period; period 3 wants 160. Over the horizon
        the plant can make 360 and demand is 360, so the peak is servable — but
        ONLY by producing ahead and holding stock. A single-period model cannot
        express that at all: it either reports a 40-unit shortfall or averages
        the peak out of existence.
        """
        network = build(rows(100.0, 100.0, 160.0), plant_capacity=120.0)
        result = milp_solve(network, None)
        assert served(result) == pytest.approx(360.0), \
            "every unit is servable, and only carryover can do it"
        assert served_in(result, 3) == pytest.approx(160.0)

        held = {d.period: d.units for d in result.inventory_decisions}
        assert held.get(1) == pytest.approx(20.0)
        assert held.get(2) == pytest.approx(40.0)

    def test_stock_costs_money_when_the_product_states_a_value(self):
        network = build(rows(100.0, 100.0, 160.0), plant_capacity=120.0,
                        unit_value=10.0)
        result = milp_solve(network, None)
        # 20 units then 40, at 10.00 x 0.25 annual x (30/365) per period.
        per_unit = 10.0 * 0.25 * (30.0 / 365.0)
        assert result.objective_components["holding_cost"] == pytest.approx(
            60.0 * per_unit, rel=1e-3)

    def test_an_unpriced_product_is_reported_not_assumed(self):
        """
        A product with no unit value carries no holding cost — and the solve
        says so rather than substituting a plausible carrying rate.
        """
        network = build(rows(100.0, 100.0, 160.0), plant_capacity=120.0,
                        unit_value=0.0)
        result = milp_solve(network, None)
        assert result.objective_components["holding_cost"] == pytest.approx(0.0)
        assert any("no unit value" in w for w in result.solver.warnings), \
            "free storage is a property of the plan and must be stated"

    def test_a_stated_storage_capacity_binds(self):
        """
        With storage capped below what smoothing needs, the peak cannot be
        fully served — and that is the honest answer, not a reason to ignore
        the cap.
        """
        network = build(rows(100.0, 100.0, 160.0), plant_capacity=120.0,
                        storage=10.0)
        result = milp_solve(network, None)
        held = [d.units for d in result.inventory_decisions]
        assert all(u <= 10.0 + 1e-6 for u in held), held
        assert served(result) < 360.0, \
            "a real storage limit must reduce what the plan can smooth"

    def test_a_cross_dock_holds_no_stock_but_still_balances(self):
        """
        A cross-dock by definition does not store. Letting it would mean solving
        a seasonality problem with a building that cannot do it.

        It still gets a per-period balance constraint — inbound equals outbound
        in every period — so it moves volume without accumulating any.
        """
        from netgravity.schemas.network import (
            CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus,
            LaneRecord, NodeRole, OptimizationConfig, ProductRecord,
            TransportMode,
        )
        network = CanonicalNetwork(
            network_id="CROSS_DOCK",
            facilities=[
                FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=120.0,
                               is_mandatory=True, is_closable=False),
                FacilityRecord(id="XD", name="Cross dock", role=NodeRole.CROSS_DOCK,
                               status=FacilityStatus.EXISTING,
                               capacity_units_per_period=9999.0),
                FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                               status=FacilityStatus.EXISTING, is_closable=False),
            ],
            products=[ProductRecord(id="P1", name="P1", weight_kg=1.0,
                                    unit_value=10.0)],
            demands=rows(100.0, 100.0, 160.0),
            lanes=[
                LaneRecord(origin_id="PLANT", destination_id="XD",
                           mode=TransportMode.ROAD, rate_per_unit=1.0,
                           distance_km=10.0, lead_time_days=1.0),
                LaneRecord(origin_id="XD", destination_id="MKT",
                           mode=TransportMode.ROAD, rate_per_unit=2.0,
                           distance_km=10.0, lead_time_days=1.0),
            ],
            config=OptimizationConfig(
                enable_inventory=False, enforce_sla=False,
                enable_carbon_cost=False, allow_shortage=True, verbose=False,
                multi_period_policy="FULL_HORIZON"),
        )
        result = milp_solve(network, None)

        assert all(d.facility_id != "XD" for d in result.inventory_decisions),             "a cross-dock must have no stock variables at all"
        # The plant is capped at 120/period and period 3 wants 160. With no
        # storage anywhere, the peak cannot be smoothed and the shortfall is
        # reported rather than absorbed.
        assert served_in(result, 3) == pytest.approx(120.0)
        assert served(result) == pytest.approx(320.0)

        # And every period balances: what arrives leaves.
        for period in (1, 2, 3):
            inbound = sum(f.flow_units for f in result.flow_decisions
                          if f.destination_id == "XD" and f.period == period)
            outbound = sum(f.flow_units for f in result.flow_decisions
                           if f.origin_id == "XD" and f.period == period)
            assert inbound == pytest.approx(outbound), f"period {period}"

    def test_stock_is_never_held_at_a_facility_the_model_closes(self):
        """
        Without the y_i link, a closed DC could take delivery and sit on the
        goods for ever: the capacity constraint limits its OUTBOUND flow only.
        """
        network = build(rows(100.0, 100.0, 100.0), dc_fixed_cost_per_year=1e9)
        result = milp_solve(network, None)
        dc = next(f for f in result.facility_decisions if f.facility_id == "DC")
        if not dc.is_open:
            assert all(d.units == 0.0 for d in result.inventory_decisions
                       if d.facility_id == "DC")


class TestCostsAreChargedOverTheRightSpan:

    def test_a_per_period_fixed_cost_is_charged_every_period(self):
        """
        Charging it once would make a twelve-month plan look like it rents a
        warehouse for one month, and would tilt every siting decision towards
        more facilities than the money supports.
        """
        one = milp_solve(build(rows(100.0)), None)
        three = milp_solve(build(rows(100.0, 100.0, 100.0)), None)
        assert three.objective_components["facility_cost"] == pytest.approx(
            3.0 * one.objective_components["facility_cost"])

    def test_facility_decisions_carry_the_fixed_cost_they_were_charged(self):
        """
        `FacilityDecision.fixed_cost` was 0.0 on every facility of every result
        ever produced: the builder passed `fixed_cost_period=`, which is not a
        field, and pydantic's default `extra="ignore"` dropped it silently.
        """
        result = milp_solve(build(rows(100.0, 100.0, 100.0)), None)
        dc = next(f for f in result.facility_decisions if f.facility_id == "DC")
        assert dc.fixed_cost > 0.0
        assert dc.fixed_cost == pytest.approx(
            result.objective_components["facility_cost"])
        assert dc.total_facility_cost >= dc.fixed_cost
        assert dc.n_markets_served == 1
        assert dc.latitude is None and dc.longitude is None  # none stated here
        assert dc.status == "EXISTING"

    def test_a_misspelled_field_now_fails_loudly(self):
        """
        The defect above was silent for the life of the codebase. It cannot
        recur: the decision models refuse unknown keywords.
        """
        from netgravity.schemas.results import FacilityDecision
        with pytest.raises(Exception):
            FacilityDecision(facility_id="F", facility_name="F", role="DC",
                             is_open=True, fixed_cost_period=1.0)


class TestReconciliationClosesOnAHorizon:

    @pytest.mark.parametrize("policy", ["FULL_HORIZON", "REPRESENTATIVE_MEAN",
                                        "PEAK", "SUM"])
    def test_the_objective_reconciles_under_every_policy(self, policy):
        """
        The independent cost evaluation must agree with the solver whatever the
        period policy — including against a network whose demand table was
        collapsed, where the naive comparison reported a shortage the solver
        never had.
        """
        network = build(rows(100.0, 100.0, 160.0), policy=policy,
                        plant_capacity=120.0)
        result = milp_solve(network, None)
        report = reconcile_kpis_and_objective(result, network)
        failing = {k: (v.reported_value, v.independent_value)
                   for k, v in report.metric_details.items() if not v.is_reconciled}
        assert report.all_reconciled, failing

    def test_kpi_total_cost_is_the_objective(self):
        """
        `total_cost` omitted opening, closure and holding costs, so a plan that
        opened a candidate reported a total that was not the number the solver
        minimised — and the reconciliation check could not see it, because it
        compared the same incomplete sum on both sides.
        """
        result = milp_solve(build(rows(100.0, 100.0, 160.0), plant_capacity=120.0), None)
        components = result.objective_components
        expected = sum(components[k] for k in (
            "facility_cost", "opening_cost", "closure_cost", "transport_cost",
            "handling_cost", "inventory_cost", "holding_cost", "shortage_cost",
            "carbon_cost"))
        assert result.kpis.total_cost == pytest.approx(expected, rel=1e-6)
        # KPI fields are rounded to 2dp and objective components to 4dp, as
        # every other pair in these two structures is.
        assert result.kpis.holding_cost == pytest.approx(
            components["holding_cost"], abs=0.01)


class TestNothingIsSilent:

    def test_the_report_states_that_costs_are_horizon_totals(self):
        result = milp_solve(build(rows(100.0, 120.0)), None)
        note = result.period_report["note"]
        assert "horizon totals" in note
        assert "charged once" in note

    def test_the_solver_warnings_carry_it_too(self):
        """A reader of the solver metadata alone must still be told."""
        result = milp_solve(build(rows(100.0, 120.0)), None)
        assert any("2 demand periods" in w for w in result.solver.warnings)

    def test_an_unknown_policy_models_the_horizon_rather_than_reducing_it(self):
        """
        An unrecognised name must not silently collapse a horizon by a rule
        nobody chose.
        """
        network = build(rows(100.0, 120.0), policy="NONSENSE")
        _, periods, report = resolve_horizon(network, "NONSENSE")
        assert len(periods) == 2
        assert report["collapsed"] is False
        assert "not a policy this model knows" in report["note"]


class TestTwoRowsForOnePeriodDoNotCollide:

    def test_the_same_market_product_and_period_twice_is_one_commitment(self):
        """
        Two rows sharing a market, product AND period produced two MILP
        constraints with one name — the same PulpError the period index was
        added to stop, one level down. They are aggregated.
        """
        demands = [
            DemandRecord(market_id="MKT", product_id="P1", period=1,
                         quantity=60.0, sla_days=1.0),
            DemandRecord(market_id="MKT", product_id="P1", period=1,
                         quantity=40.0, sla_days=3.0),
        ]
        result = milp_solve(build(demands), None)
        assert result.solver.status.value in {"OPTIMAL", "FEASIBLE"}
        assert served(result) == pytest.approx(100.0)
