"""
Capacity limits — the limit that binds, reported as the limit that binds.

Four defects, each measured on a real upload before it was fixed:

  * a plant has two limits — what it can handle and what it can produce — and
    a capacity scenario moved only the first, so an expansion at a plant held
    at its production limit shipped exactly what it did before;
  * utilisation was divided by the rated capacity whatever bound the site, so
    that same plant reported its utilisation falling from 97.61% to 62.75%;
  * the capacity table's monthly availability never reached the optimiser;
  * expansion carried no one-time or recurring cost of its own.
"""
from __future__ import annotations

import pytest

from netgravity.orchestrator.engines.scenario_builder import (
    ScenarioBuilder,
    planned_capacity,
    usable_capacity,
)
from netgravity.orchestrator.exceptions import InvalidScenarioError
from netgravity.orchestrator.schemas.requests import (
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.validation.validators import ScenarioValidator
from netgravity.schemas.network import FacilityRecord, NodeRole


def _with(network, facility_id, **update):
    return network.model_copy(update={"facilities": [
        f.model_copy(update=update) if f.id == facility_id else f
        for f in network.facilities]})


def _solve(network, *, shortage=True):
    from netgravity.optimization.milp import solve as milp_solve
    from netgravity.schemas.network import OptimizationMode

    cfg = network.config.model_copy(update={
        "optimization_mode": OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION,
        "allow_shortage": shortage})
    return milp_solve(network, cfg, "limits")


def _period(network):
    return str(next(iter(network.demands)).period)


class TestTheLimitThatBindsIsOneDefinition:

    def test_availability_binds_its_month_up_to_the_rated_capacity(self):
        site = FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                              capacity_units_per_period=1000.0,
                              capacity_by_period={"1": 600.0, "2": 1400.0})
        assert site.period_limit(1) == (600.0, "AVAILABLE")
        # More available than rated is not more capacity.
        assert site.period_limit(2) == (1000.0, "HANDLING")
        assert site.period_limit(3) == (1000.0, "HANDLING")

    def test_availability_never_revives_a_site_that_was_zeroed(self):
        """Closures, disruptions and mode policies remove a site by zeroing its
        rated capacity. A monthly figure must not let it keep shipping."""
        site = FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                              capacity_units_per_period=0.0,
                              capacity_by_period={"1": 600.0})
        assert site.period_limit(1)[0] == 0.0

    def test_one_uploaded_figure_written_twice_is_one_limit(self):
        plant = FacilityRecord(id="P", name="P", role=NodeRole.PLANT,
                               capacity_units_per_period=90000.0,
                               production_capacity_units_per_period=90000.0)
        assert plant.production_limit() is None
        assert plant.period_limit(1) == (90000.0, "HANDLING")

    def test_a_separate_production_limit_binds_when_it_is_lower(self):
        plant = FacilityRecord(id="P", name="P", role=NodeRole.PLANT,
                               capacity_units_per_period=140000.0,
                               production_capacity_units_per_period=90000.0)
        assert plant.period_limit(1) == (90000.0, "PRODUCTION")

    def test_an_empty_monthly_table_leaves_a_networks_identity_unchanged(
            self, delhi_network):
        before = delhi_network.compute_data_version()
        again = _with(delhi_network, "DC_DELHI", capacity_by_period={})
        assert again.compute_data_version() == before
        stated = _with(delhi_network, "DC_DELHI", capacity_by_period={"1": 10.0})
        assert stated.compute_data_version() != before


class TestTheSolveReportsAgainstTheLimitThatBound:

    def test_a_month_with_less_available_binds_that_month(self, delhi_network):
        period = _period(delhi_network)
        net = _with(delhi_network, "DC_DELHI", capacity_by_period={period: 40.0})
        result = _solve(net)
        delhi = next(d for d in result.facility_decisions if d.facility_id == "DC_DELHI")
        assert delhi.throughput_units <= 40.0 + 1e-6
        assert delhi.capacity_limit == "AVAILABLE"
        assert delhi.capacity_units == pytest.approx(40.0 * delhi.n_periods)
        assert delhi.rated_capacity_units == pytest.approx(
            net.facilities[[f.id for f in net.facilities].index("DC_DELHI")]
            .capacity_units_per_period * delhi.n_periods)

    def test_a_plant_held_at_its_production_limit_reads_full_against_it(
            self, delhi_network):
        net = _with(delhi_network, "PLANT_N",
                    production_capacity_units_per_period=150.0)
        result = _solve(net)
        plant = next(d for d in result.facility_decisions if d.facility_id == "PLANT_N")
        assert plant.capacity_limit == "PRODUCTION"
        assert plant.throughput_units <= 150.0 * plant.n_periods + 1e-6
        assert plant.capacity_units == pytest.approx(150.0 * plant.n_periods)
        assert plant.production_capacity_units == pytest.approx(150.0 * plant.n_periods)
        # Utilisation against the limit that bound, not against 99,999 units
        # of handling capacity the plant can never use.
        assert plant.utilization_pct == pytest.approx(
            plant.throughput_units / plant.capacity_units * 100.0, abs=0.01)
        assert plant.rated_capacity_units > plant.capacity_units


class TestWhichLimitAChangeMoves:

    PLANT = FacilityRecord(id="P", name="P", role=NodeRole.PLANT,
                           capacity_units_per_period=90000.0,
                           production_capacity_units_per_period=90000.0,
                           fixed_cost_per_year=73_200_000.0)

    def test_an_ordinary_expansion_moves_both_when_they_were_one_figure(self):
        assert planned_capacity(self.PLANT, delta_units=50000.0) == (140000.0, 140000.0)

    def test_handling_only_leaves_production_binding(self):
        handling, production = planned_capacity(
            self.PLANT, delta_units=50000.0, limit="HANDLING")
        assert (handling, production) == (140000.0, 90000.0)
        assert usable_capacity(self.PLANT, handling, production) == 90000.0

    def test_production_only_moves_production(self):
        assert planned_capacity(self.PLANT, delta_units=10000.0,
                                limit="PRODUCTION") == (90000.0, 100000.0)

    def test_a_limit_that_does_not_bind_is_not_charged_for(self, delhi_network):
        net = _with(delhi_network, "PLANT_N",
                    production_capacity_units_per_period=99_999.0,
                    fixed_cost_per_year=100_000.0)
        built, overrides = ScenarioBuilder().build(net, ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["PLANT_N"],
            capacity_delta_units=50_000.0, capacity_limit="HANDLING"))
        plant = next(f for f in built.facilities if f.id == "PLANT_N")
        assert plant.capacity_units_per_period == pytest.approx(149_999.0)
        assert plant.production_capacity_units_per_period == pytest.approx(99_999.0)
        assert plant.fixed_cost_per_year == pytest.approx(100_000.0)
        assert "production capacity 99,999 units/period still limits it" in overrides[0]
        assert "(handling limit)" in overrides[0]

    def test_a_stated_recurring_cost_is_charged_as_stated(self, delhi_network):
        net = _with(delhi_network, "DC_DELHI", fixed_cost_per_year=120_000.0)
        built, overrides = ScenarioBuilder().build(net, ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=2_000.0, expansion_fixed_cost_per_year=30_000.0))
        delhi = next(f for f in built.facilities if f.id == "DC_DELHI")
        assert delhi.fixed_cost_per_year == pytest.approx(150_000.0)
        assert "per year, as stated" in overrides[0]

    def test_monthly_availability_moves_with_the_expansion(self, delhi_network):
        period = _period(delhi_network)
        net = _with(delhi_network, "DC_DELHI", capacity_by_period={period: 4_000.0})
        current = next(f for f in net.facilities if f.id == "DC_DELHI").capacity_units_per_period
        built, overrides = ScenarioBuilder().build(net, ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=current))
        delhi = next(f for f in built.facilities if f.id == "DC_DELHI")
        assert delhi.capacity_by_period[period] == pytest.approx(8_000.0)
        assert "monthly available capacity scaled in proportion" in overrides[0]

    def test_production_is_refused_at_a_site_that_does_not_produce(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=100.0, capacity_limit="PRODUCTION")
        with pytest.raises(InvalidScenarioError, match="not a plant"):
            ScenarioValidator().validate(spec, delhi_network)

    def test_a_negative_expansion_cost_is_refused(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=100.0, expansion_one_time_cost=-1.0)
        with pytest.raises(InvalidScenarioError, match="expansion_one_time_cost"):
            ScenarioValidator().validate(spec, delhi_network)

    def test_an_unknown_limit_is_refused_by_the_spec(self):
        with pytest.raises(Exception, match="capacity_limit"):
            ScenarioIntentSpec(action=ScenarioActionType.CHANGE_CAPACITY,
                               facility_ids=["X"], capacity_delta_units=1.0,
                               capacity_limit="WAREHOUSE")
