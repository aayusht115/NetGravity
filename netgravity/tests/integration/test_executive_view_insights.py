"""
The Executive view's three insight tiles, and the recommendations behind them.

Four corrections, each pinned here:

  * A tile leads with a DECISION. "Hold this as the baseline every proposed
    change is measured against" is true and asks a leader to do nothing; it
    must not take one of three tiles while a finding with a real change waits.
  * The third tile is where the money goes. The total network cost is already
    the first figure in the KPI strip; the cost-structure finding names the
    largest cost line and how to cut it, with a scenario that prices it.
  * No "Open scenario planner" button on a tile — it opened an empty planner.
  * The detailed finding's planner button opens the recommended scenario
    filled in: the change, the site and a sized amount.

And the rename (Overview -> Executive view) and the Forecast page's demand box.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backend.api import insights as api

ROOT = Path(__file__).resolve().parents[3]


def _asset(*parts: str) -> str:
    return (ROOT / "app" / "frontend" / Path(*parts)).read_text(encoding="utf-8")


def _fn(js: str, signature: str) -> str:
    body = js[js.index(signature):]
    return body[:body.index("\n}\n")]


class _Pack:
    def __init__(self, payload):
        self.payload = payload


def _row(fid, util, *, role="DC", is_open=True, throughput=1000.0, capacity=1000.0):
    return {"facility_id": fid, "facility_name": f"{fid} site", "role": role,
            "is_open": is_open, "utilization_pct": util,
            "throughput_units": throughput, "capacity_units": capacity}


def _record(fid, *, role="DC", fixed=0.0, rate=0.0, capacity=1000.0):
    return SimpleNamespace(id=fid, role=role, fixed_cost_per_year=fixed,
                           handling_cost_per_unit=rate,
                           capacity_units_per_period=capacity)


def _pack(components, rows, flows=()):
    return _Pack({"network_state": {"cost_components": components},
                  "facilities": list(rows), "flows": list(flows)})


def _insight(theme, severity, headline="A finding"):
    return SimpleNamespace(theme=theme, severity=severity, headline=headline,
                           narrative="", recommended_action="",
                           metric_refs=[], comparison_refs=[], driver_refs=[])


class TestOnlyDecisionsLeadTheExecutiveView:

    def test_holding_the_baseline_is_not_a_decision(self):
        for key in (("Cost", "INFORMATION"), ("Service", "INFORMATION"),
                    ("Carbon", "INFORMATION"), ("Footprint", "INFORMATION")):
            assert not api.is_decision(api._ACTION_BY_THEME[key]), key
        assert not api.is_decision(api._ACTION_BY_SEVERITY["INFORMATION"])
        # The narrative layer's own wording of the same non-decision.
        assert not api.is_decision("Hold this run as the baseline for the next round.")

    def test_a_change_is_a_decision(self):
        assert api.is_decision(api._ACTION_BY_THEME[("Capacity", "RISK")])
        assert api.is_decision("anything", {"scenario": {"action": "CLOSE_FACILITY"}})
        assert not api.is_decision("", {})

    def test_every_serialised_insight_says_which_it_is(self):
        held = api._serialise_insight(_insight("Cost", "INFORMATION"), 0,
                                      scope="NETWORK", entity_id=None)
        acted = api._serialise_insight(_insight("Capacity", "RISK"), 1,
                                       scope="NETWORK", entity_id=None)
        assert held["actionable"] is False, held["recommended_action"]
        assert acted["actionable"] is True, acted["recommended_action"]

    def test_the_cached_briefing_is_invalidated(self):
        assert api._PAYLOAD_VERSION >= 11


class TestTheLargestCostLineNamesHowToCutIt:

    def test_transport_names_the_site_with_the_most_freight_spend(self):
        pack = _pack({"transport_cost": 900.0, "handling_cost": 100.0},
                     [_row("DC_A", 70), _row("DC_B", 70)],
                     [{"origin_id": "DC_A", "destination_id": "M1", "transport_cost": 700.0},
                      {"origin_id": "DC_B", "destination_id": "M2", "transport_cost": 200.0},
                      {"origin_id": "DC_B", "destination_id": "M3", "transport_cost": 100.0}])
        sentence, action = api._recommended_action(
            _insight("Cost structure", "INFORMATION"), "Cost structure",
            "INFORMATION", pack)
        assert "DC_A site" in sentence and sentence.startswith("Cut transport")
        scenario = action["scenario"]
        assert scenario["action"] == "CHANGE_TRANSPORT_COST"
        assert scenario["facility_id"] == "DC_A"
        assert scenario["amount"] < 0, "a test of cheaper freight, not dearer"
        assert action["cta"]

    def test_facility_cost_consolidates_the_least_used_site_that_carries_it(self):
        pack = _pack({"facility_cost": 900.0, "transport_cost": 100.0},
                     [_row("DC_A", 25), _row("DC_B", 80)])
        network = SimpleNamespace(facilities=[_record("DC_A", fixed=100_000),
                                              _record("DC_B", fixed=50_000)])
        sentence, action = api._cost_structure_action(pack, network)
        assert action["scenario"] == {"action": "CLOSE_FACILITY",
                                      "facility_id": "DC_A",
                                      "name": "Consolidate DC_A site"}
        assert "25%" in sentence

    def test_a_site_another_card_consolidates_is_not_recommended_twice(self):
        pack = _pack({"facility_cost": 900.0, "transport_cost": 100.0},
                     [_row("DC_A", 25), _row("DC_B", 80)])
        network = SimpleNamespace(facilities=[_record("DC_A", fixed=100_000),
                                              _record("DC_B", fixed=50_000)])
        sentence, action = api._cost_structure_action(
            pack, network, claimed={("CONSOLIDATE", "DC_A")})
        assert "DC_A" not in sentence
        assert action["scenario"] == {} and action["cta"] == ""
        # Still a decision: renegotiating is a change, not a hold.
        assert api.is_decision(sentence, action)

    def test_the_last_site_of_its_kind_is_never_consolidated(self):
        pack = _pack({"inventory_cost": 900.0, "transport_cost": 100.0},
                     [_row("DC_ONLY", 10), _row("PLANT_A", 10, role="PLANT"),
                      _row("PLANT_B", 90, role="PLANT")])
        sentence, action = api._cost_structure_action(pack, None)
        assert action["scenario"] == {}, sentence

    def test_handling_expands_a_cheap_handler_that_is_full(self):
        pack = _pack({"handling_cost": 900.0, "transport_cost": 100.0},
                     [_row("DC_A", 95), _row("DC_B", 50)])
        network = SimpleNamespace(facilities=[_record("DC_A", rate=1.0, capacity=1000),
                                              _record("DC_B", rate=3.0, capacity=1000)])
        insight = _insight("Cost structure", "INFORMATION")
        body = api._serialise_insight(insight, 0, scope="NETWORK", entity_id=None,
                                      pack=pack, claimed=set(), network=network)
        scenario = body["action"]["scenario"]
        assert scenario["action"] == "CHANGE_CAPACITY"
        assert scenario["facility_id"] == "DC_A"
        # 1,000 units at 95%, back to 85%: 118 units, to two figures.
        assert scenario["amount"] == 120
        assert body["actionable"] is True

    def test_handling_without_a_full_cheap_site_names_the_largest_spend(self):
        pack = _pack({"handling_cost": 900.0, "transport_cost": 100.0},
                     [_row("DC_A", 40, throughput=100), _row("DC_B", 50, throughput=900)])
        network = SimpleNamespace(facilities=[_record("DC_A", rate=1.0),
                                              _record("DC_B", rate=3.0)])
        sentence, action = api._cost_structure_action(pack, network)
        assert "DC_B site" in sentence and action["scenario"] == {}

    def test_inventory_pools_stock_at_a_stocking_point_not_a_plant(self):
        pack = _pack({"inventory_cost": 900.0, "transport_cost": 100.0},
                     [_row("PLANT_A", 5, role="PLANT"), _row("PLANT_B", 70, role="PLANT"),
                      _row("DC_A", 30), _row("DC_B", 75)])
        _, action = api._cost_structure_action(pack, None)
        assert action["scenario"]["facility_id"] == "DC_A"

    def test_one_cost_line_is_not_a_cost_structure(self):
        pack = _pack({"transport_cost": 900.0, "handling_cost": 0.0}, [_row("DC_A", 50)])
        assert api._cost_structure_action(pack, None) is None
        sentence, _ = api._recommended_action(
            _insight("Cost structure", "INFORMATION"), "Cost structure",
            "INFORMATION", pack)
        assert sentence == api._ACTION_BY_THEME[("Cost structure", "INFORMATION")]

    def test_no_lever_states_a_saving(self):
        rows = [_row("DC_A", 20), _row("DC_B", 95)]
        network = SimpleNamespace(facilities=[_record("DC_A", fixed=1e6, rate=1.0),
                                              _record("DC_B", fixed=1e6, rate=2.0)])
        for line in api._COST_LEVERS:
            pack = _pack({line: 900.0, "carbon_cost" if line != "carbon_cost"
                          else "transport_cost": 1.0}, rows)
            sentence, _ = api._cost_structure_action(pack, network)
            assert not re.search(r"[$€£]|\bsav(e|es|ing)\b\s+\d", sentence), sentence


class TestUnusedCandidatesNameTheSiteToReopen:

    def test_the_largest_closed_candidate_is_named_and_priced(self):
        pack = _pack({}, [_row("DC_A", 50),
                          _row("DC_SMALL", 0, is_open=False, capacity=100),
                          _row("DC_BIG", 0, is_open=False, capacity=900)])
        sentence, action = api._recommended_action(
            _insight("Footprint", "OPPORTUNITY"), "Footprint", "OPPORTUNITY", pack,
            claimed=set())
        assert "DC_BIG site" in sentence
        assert action["scenario"] == {"action": "OPEN_FACILITY", "open_mode": "EXISTING",
                                      "facility_id": "DC_BIG", "name": "Reopen DC_BIG site"}


class TestACapacityTestIsSized:

    def test_back_to_eighty_five_percent_in_two_figures(self):
        assert api._suggested_capacity_units(_record("P", capacity=150_000), 99) == 25_000

    def test_never_less_than_a_tenth(self):
        assert api._suggested_capacity_units(_record("P", capacity=8_000), 50) == 800

    def test_an_unstated_capacity_is_not_sized(self):
        assert api._suggested_capacity_units(_record("P", capacity=1e12), 99) is None
        assert api._suggested_capacity_units(None, 99) is None


class TestTheTilesLeadWithDecisions:

    def test_the_tiles_are_chosen_rather_than_cut_from_the_top(self):
        js = _asset("js", "app.js")
        tiles = _fn(js, "function renderHomeInsightTiles(")
        assert "executiveTileInsights(rankedAttentionInsights())" in tiles
        pick = _fn(js, "function executiveTileInsights(ranked)")
        assert "'Cost structure'" in pick
        assert "actionable !== false" in pick
        # The total-cost finding never takes a tile.
        assert "it.theme !== 'Cost'" in pick

    def test_no_tile_offers_the_empty_planner(self):
        js = _asset("js", "app.js")
        tile = _fn(js, "function insightTileHtml(")
        assert "cta.tab === 'scenarios' ? ''" in tile

    def test_the_record_carries_the_servers_call(self):
        data = _asset("js", "data.js")
        assert "apiInsight.actionable" in data
        assert "'Cost Reduction'" in data


class TestTheDetailedFindingOpensTheScenarioFilledIn:

    def test_both_planner_buttons_fill_the_builder(self):
        detail = _asset("js", "insight-detail.js")
        bind = _fn(detail, "function bindDeepDive(cta)")
        assert bind.count("openRecommendedScenario(insdFlow.record)") == 2, bind
        assert "scenarioCta(record, insightCta(" in detail

    def test_the_builder_is_given_the_site_and_the_amount(self):
        shared = _asset("js", "insight-presentation.js")
        opener = _fn(shared, "export function openRecommendedScenario(record)")
        assert "window.navigateToTab('scenarios')" in opener
        assert "openScenarioBuilderWith" in opener
        for option in ("facilityId: scn.facility_id", "openMode: scn.open_mode",
                       "amount: typeof scn.amount === 'number'"):
            assert option in opener, option
        # Nothing is submitted on the reader's behalf.
        for forbidden in ("simulate", "runScenarioCreation"):
            assert forbidden not in opener


class TestExecutiveViewAndForecast:

    def test_the_page_is_called_executive_view(self):
        html = _asset("index.html")
        assert "<span>Executive view</span>" in html
        assert '<h1 class="ov-title">Executive view</h1>' in html
        assert "<span>Overview</span>" not in html
        assert '<h1 class="ov-title">Overview</h1>' not in html

    def test_the_forecast_page_has_no_demand_box(self):
        html = _asset("index.html")
        assert 'id="fc-alert"' not in html
        js = _asset("js", "app.js")
        assert "All stated demand is served" not in js
        assert "renderOverviewAlert('fc-alert')" not in js
