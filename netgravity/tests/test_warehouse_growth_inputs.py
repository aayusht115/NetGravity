"""
Warehouse feedback, backend half: what the client supplies must reach the model.

Two things a client states that the product used to drop on the floor:

  * what it costs to open a site they have proposed — parsed nowhere, so every
    candidate was offered to the optimiser as free to build;
  * which region and which product category their growth applies to — there was
    only ever one network-wide demand multiplier, which loads every warehouse in
    the country with growth happening in one of its regions.

These tests are about the data reaching the solver correctly, not about what the
solver then decides. `test_candidate_opening.py` covers the decision.
"""

import pytest

from netgravity.orchestrator.engines.scenario_builder import ScenarioBuilder
from netgravity.orchestrator.exceptions import InvalidScenarioError
from netgravity.orchestrator.schemas.requests import (
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.validation.validators import ScenarioValidator
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    ProductRecord,
    TransportMode,
)


def _network() -> CanonicalNetwork:
    """Two regions, two categories, one demand row per combination."""
    facilities = [
        FacilityRecord(id="PLT", name="Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING, latitude=20.0, longitude=75.0,
                       capacity_units_per_period=100_000,
                       production_capacity_units_per_period=100_000, region="West"),
        FacilityRecord(id="DC1", name="DC One", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, latitude=28.6, longitude=77.2,
                       capacity_units_per_period=10_000, region="North"),
        FacilityRecord(id="MKT_N", name="North Market", role=NodeRole.MARKET,
                       latitude=28.7, longitude=77.1, region="North"),
        FacilityRecord(id="MKT_S", name="South Market", role=NodeRole.MARKET,
                       latitude=12.9, longitude=77.6, region="South"),
    ]
    products = [
        ProductRecord(id="P_AMB", name="Ambient line", category="Ambient"),
        ProductRecord(id="P_CHI", name="Chilled line", category="Chilled"),
    ]
    demands = [
        DemandRecord(market_id="MKT_N", product_id="P_AMB", quantity=100.0),
        DemandRecord(market_id="MKT_N", product_id="P_CHI", quantity=200.0),
        DemandRecord(market_id="MKT_S", product_id="P_AMB", quantity=400.0),
        DemandRecord(market_id="MKT_S", product_id="P_CHI", quantity=800.0),
    ]
    lanes = [
        LaneRecord(origin_id="PLT", destination_id="DC1", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100, lead_time_days=1.0),
        LaneRecord(origin_id="DC1", destination_id="MKT_N", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=10, lead_time_days=1.0),
        LaneRecord(origin_id="DC1", destination_id="MKT_S", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=1800, lead_time_days=3.0),
    ]
    return CanonicalNetwork(network_id="SCOPE_TEST", facilities=facilities,
                            products=products, demands=demands, lanes=lanes)


def quantities(network):
    return {(d.market_id, d.product_id): d.quantity for d in network.demands}


class TestGrowthCanBeScoped:

    def setup_method(self):
        self.builder = ScenarioBuilder()
        self.network = _network()

    def _run(self, **kw):
        spec = ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND, **kw)
        return self.builder.build(self.network, spec)

    def test_unscoped_growth_is_unchanged_behaviour(self):
        """No region, no category: every row moves, exactly as before."""
        out, _ = self._run(demand_multiplier=1.5)
        assert quantities(out) == {
            ("MKT_N", "P_AMB"): 150.0, ("MKT_N", "P_CHI"): 300.0,
            ("MKT_S", "P_AMB"): 600.0, ("MKT_S", "P_CHI"): 1200.0,
        }

    def test_region_scope_leaves_other_regions_alone(self):
        out, overrides = self._run(demand_multiplier=1.5, demand_region="North")
        q = quantities(out)
        assert q[("MKT_N", "P_AMB")] == 150.0
        assert q[("MKT_N", "P_CHI")] == 300.0
        # Untouched, not scaled by 1.0 — the South is not growing.
        assert q[("MKT_S", "P_AMB")] == 400.0
        assert q[("MKT_S", "P_CHI")] == 800.0
        assert "region=North" in overrides[0]
        assert "(2 rows)" in overrides[0]

    def test_category_scope_crosses_regions(self):
        out, _ = self._run(demand_multiplier=2.0, demand_product_category="Chilled")
        q = quantities(out)
        assert q[("MKT_N", "P_CHI")] == 400.0
        assert q[("MKT_S", "P_CHI")] == 1600.0
        assert q[("MKT_N", "P_AMB")] == 100.0
        assert q[("MKT_S", "P_AMB")] == 400.0

    def test_both_scopes_narrow_to_their_overlap(self):
        out, overrides = self._run(demand_multiplier=3.0, demand_region="South",
                                   demand_product_category="Chilled")
        q = quantities(out)
        assert q[("MKT_S", "P_CHI")] == 2400.0
        assert q[("MKT_S", "P_AMB")] == 400.0
        assert q[("MKT_N", "P_CHI")] == 200.0
        assert "(1 rows)" in overrides[0]

    def test_scope_matching_is_case_insensitive(self):
        """A client writes "north"; their sheet says "North"."""
        out, _ = self._run(demand_multiplier=1.5, demand_region="north")
        assert quantities(out)[("MKT_N", "P_AMB")] == 150.0

    def test_the_parent_network_is_never_mutated(self):
        before = quantities(self.network)
        self._run(demand_multiplier=4.0, demand_region="North")
        assert quantities(self.network) == before


class TestAScopeThatMatchesNothingIsRefused:
    """
    Scaling zero rows produces a scenario identical to the base case, and a
    comparison card reading "no change in cost" is a confident answer to a
    question nobody asked. A misspelt region must fail loudly.
    """

    def setup_method(self):
        self.validator = ScenarioValidator()
        self.network = _network()

    def _validate(self, **kw):
        spec = ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                  demand_multiplier=1.2, **kw)
        return self.validator.validate(spec, self.network)

    def test_unknown_region_is_rejected_and_lists_the_real_ones(self):
        with pytest.raises(InvalidScenarioError) as exc:
            self._validate(demand_region="Northeast")
        assert "Northeast" in str(exc.value)
        assert "North" in str(exc.value)

    def test_unknown_category_is_rejected(self):
        with pytest.raises(InvalidScenarioError) as exc:
            self._validate(demand_product_category="Frozen")
        assert "Frozen" in str(exc.value)

    def test_known_scopes_pass(self):
        self._validate(demand_region="South")
        self._validate(demand_product_category="Ambient")
        self._validate()
