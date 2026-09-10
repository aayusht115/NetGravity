"""
What the application tells a leader to DO.

THE STANDARD THIS HOLDS TO. A recommendation is a decision: a change, a place,
and evidence. It is not a destination. "Open the KPI page to see which sites
are over the threshold, then test a scenario that relieves them" was the
recommended action on a capacity finding, and it fails on every count — it
names no site, no change and no magnitude, it is identical on every network
ever uploaded, and it asks the reader to go and do the analysis themselves.

Each test below is one property of a real recommendation, held so it cannot
quietly become a navigation instruction again.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from netgravity.orchestrator.reasoning.strategic_actions import (
    ACTION_KEYS,
    IDLE_PCT,
    LOADED_PCT,
    SATURATED_PCT,
    build_actions,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

#: A recommendation that matches any of these has told the reader where to
#: look rather than what to do.
NAVIGATIONAL = re.compile(
    r"\bopen the (kpi|digital twin|forecast|scenario)\b"
    r"|\bgo to\b"
    r"|\breview the proposed\b"
    r"|\bview the (screen|page|detail)\b",
    re.I,
)


def site(fid, name, util, *, role="DC", capacity=5000.0, region=None,
         is_open=True):
    return {"facility_id": fid, "facility_name": name, "role": role,
            "is_open": is_open, "utilization_pct": util,
            "capacity_units": capacity, "region": region}


class TestTheLadderRecommendsTheCheapestThingThatWorks:
    """
    Order is the whole argument. Capacity that exists beats capacity that has
    to be built; expanding a site that already has land, labour and a licence
    beats a greenfield. A ladder that offers them in any other order is
    recommending the expensive option first.
    """

    def test_closed_capacity_is_offered_before_anything_is_built(self):
        actions = build_actions([
            site("A", "Pune DC", 97.0, region="West"),
            site("B", "Delhi DC", 0.0, region="North", is_open=False),
        ], unserved_demand=0)
        assert actions[0].key == "REOPEN_FACILITY"
        assert "Delhi DC" in actions[0].label

    def test_expanding_a_site_is_offered_before_building_beside_it(self):
        actions = build_actions([
            site("A", "Pune DC", 97.0, region="West"),
        ], unserved_demand=0)
        keys = [a.key for a in actions]
        assert keys.index("ADD_CAPACITY") < keys.index("OPEN_NEW_FACILITY")

    def test_a_site_at_ninety_percent_gets_a_new_facility_offered_too(self):
        """
        The case the ladder was rewritten for. A site at 90% has two answers
        and a leader is owed both: expand it, or build alongside it. Offering
        only the cheap one to a site that cannot physically take another bay
        has offered nothing — and the building option is the one with the
        lead time, so it is the one that has to be decided early.
        """
        actions = build_actions([site("A", "Pune DC", 90.4, region="West")])
        keys = [a.key for a in actions]
        assert "ADD_CAPACITY" in keys
        assert "OPEN_NEW_FACILITY" in keys

    def test_an_idle_site_is_a_consolidation_finding_not_a_capacity_one(self):
        actions = build_actions([
            site("A", "Pune DC", 61.0, region="West"),
            site("B", "Kochi DC", 8.0, region="South"),
        ], unserved_demand=0)
        assert [a.key for a in actions] == ["CONSOLIDATE"]
        assert "Kochi DC" in actions[0].label


class TestEveryRecommendationNamesAChangeAndAPlace:

    @pytest.mark.parametrize("rows,kw", [
        ([site("A", "Pune DC", 99.9, region="West")], {}),
        ([site("A", "Pune DC", 97.0), site("B", "Delhi DC", 0.0, is_open=False)],
         {"unserved_demand": 5000.0}),
        ([site("P", "Hosur Plant", 96.0, role="PLANT", region="South")], {}),
        ([site("A", "Pune DC", 12.0, region="West")], {"unserved_demand": 0}),
    ])
    def test_no_recommendation_is_a_place_to_look(self, rows, kw):
        for action in build_actions(rows, **kw):
            blob = f"{action.label} {action.reason}"
            assert not NAVIGATIONAL.search(blob), blob

    def test_the_label_is_an_imperative_naming_an_intervention(self):
        actions = build_actions([site("A", "Pune DC", 97.0, region="West")])
        verbs = ("Expand", "Establish", "Bring", "Add", "Test", "Re-run",
                 "Obtain", "No network change")
        for action in actions:
            assert action.label.startswith(verbs), action.label

    def test_a_plant_is_relieved_by_a_plant_and_a_dc_by_a_dc(self):
        """
        "Establish a new facility" means nothing to the person who has to
        sponsor it. Production capacity and distribution capacity are
        different decisions, different money and different lead times.
        """
        plant = build_actions([site("P", "Hosur Plant", 99.5, role="PLANT",
                                    region="South")])
        dc = build_actions([site("D", "Pune DC", 99.5, role="DC",
                                 region="West")])
        assert any("new plant" in a.label for a in plant), \
            [a.label for a in plant]
        assert any("new distribution centre" in a.label for a in dc), \
            [a.label for a in dc]


class TestEveryRecommendationEndsInATestNotACommitment:
    """
    A recommendation nobody can price is an opinion. A button that APPLIED one
    would be a structural change made from a dashboard, which is what the
    governance layer exists to prevent. Every intervention therefore carries
    the scenario that would prove it.
    """

    def test_an_intervention_carries_the_scenario_that_prices_it(self):
        for action in build_actions([site("A", "Pune DC", 97.0, region="West")]):
            assert action.scenario, action.key
            assert action.scenario.get("action")
            assert action.to_dict()["cta"]

    def test_the_scenario_names_a_form_the_builder_actually_has(self):
        """
        The builder offers CHANGE_CAPACITY, OPEN_FACILITY, CLOSE_FACILITY,
        CHANGE_DEMAND, CHANGE_TRANSPORT_COST and CHANGE_SLA as type cards. A
        greenfield is OPEN_FACILITY with mode NEW — it is NOT a card of its
        own, and emitting `ADD_FACILITY` selected no card at all and left the
        builder on whatever was open, so pressing a recommendation to build
        opened a form for something else.
        """
        cards = {"CHANGE_CAPACITY", "OPEN_FACILITY", "CLOSE_FACILITY",
                 "CHANGE_DEMAND", "CHANGE_TRANSPORT_COST", "CHANGE_SLA"}
        rows = [site("A", "Pune DC", 99.5, region="West"),
                site("B", "Delhi DC", 0.0, region="North", is_open=False),
                site("C", "Kochi DC", 5.0, region="South")]
        seen = set()
        for kwargs in ({}, {"unserved_demand": 9000.0},
                       {"demand_change_is_unscoped": True}):
            for action in build_actions(rows, limit=9, **kwargs):
                if action.scenario:
                    assert action.scenario["action"] in cards, action.scenario
                    seen.add(action.scenario["action"])
        assert seen

    def test_a_statement_has_no_button(self):
        """NO_ACTION and REQUEST_DATA are findings, not forms."""
        healthy = build_actions([site("A", "Pune DC", 55.0)], unserved_demand=0)
        assert healthy[0].key == "NO_ACTION"
        assert healthy[0].scenario == {}
        assert healthy[0].to_dict()["cta"] == ""


class TestNothingIsClaimedThatWasNotSolved:

    def test_no_recommendation_states_a_saving(self):
        """
        Nothing has been solved at the point a recommendation is made — the
        scenario it hands over is what produces a saving. A figure quoted here
        would be exactly what the numeric grounding layer exists to catch.
        """
        money = re.compile(r"(save|saving|payback|roi|reduce cost by)", re.I)
        rows = [site("A", "Pune DC", 99.5, region="West"),
                site("B", "Delhi DC", 0.0, is_open=False),
                site("C", "Kochi DC", 4.0)]
        for action in build_actions(rows, limit=9, unserved_demand=1000.0):
            assert not money.search(action.reason), action.reason

    def test_an_absent_utilisation_is_said_not_assumed(self):
        actions = build_actions([
            {"facility_id": "A", "facility_name": "Pune DC", "is_open": True,
             "capacity_units": 5000.0},
        ])
        blob = " ".join(a.reason for a in actions)
        assert "0%" not in blob

    def test_nothing_to_do_is_a_finding_with_its_reason(self):
        actions = build_actions([site("A", "Pune DC", 55.0)], unserved_demand=0)
        assert len(actions) == 1
        assert actions[0].key == "NO_ACTION"
        assert "serves all of its demand" in actions[0].reason

    def test_no_rows_says_so_rather_than_reporting_a_healthy_network(self):
        """
        Two different findings: "we looked and it is fine" and "we could not
        look". Reporting the first when the second is true tells a reader a
        network is healthy on the strength of no evidence at all.
        """
        actions = build_actions([])
        assert actions[0].key == "NO_ACTION"
        assert "No solved facility rows" in actions[0].reason


class TestARoundedPercentageNeverOverstatesTheFinding:

    def test_a_site_at_ninety_nine_point_six_does_not_print_as_full(self):
        """
        Full and nearly-full are different findings, and the decimal is the
        only thing separating "this is the constraint" from "this has
        stopped".
        """
        actions = build_actions([site("A", "Pune DC", 99.6, region="West")])
        blob = " ".join(a.reason for a in actions)
        assert "99.6%" in blob
        assert "100%" not in blob

    def test_ordinary_utilisations_are_whole_percentages(self):
        actions = build_actions([site("A", "Pune DC", 92.37, region="West")])
        blob = " ".join(a.reason for a in actions)
        assert "92%" in blob
        assert "92.37" not in blob


class TestTheThresholdsAreTheOnesTheRestOfTheProductUses:

    def test_the_bands_do_not_drift_from_the_configured_policy(self):
        from netgravity.config.defaults import UTILIZATION_THRESHOLDS
        assert LOADED_PCT == UTILIZATION_THRESHOLDS["over_threshold"] * 100
        assert SATURATED_PCT > LOADED_PCT
        assert IDLE_PCT > 0


class TestTheInsightsFeedUsesTheLadder:

    def _source(self) -> str:
        return (REPO_ROOT / "app" / "backend" / "api"
                / "insights.py").read_text(encoding="utf-8")

    def test_no_theme_default_sends_the_reader_to_a_screen(self):
        import app.backend.api.insights as insights
        sentences = (list(insights._ACTION_BY_THEME.values())
                     + list(insights._ACTION_BY_SEVERITY.values()))
        assert sentences
        for sentence in sentences:
            assert not NAVIGATIONAL.search(sentence), sentence

    def test_a_capacity_finding_names_the_site_the_ladder_found(self):
        import app.backend.api.insights as insights

        class Pack:
            payload = {
                "facilities": [
                    {"facility_id": "DC_W", "facility_name": "Pune DC",
                     "role": "DC", "is_open": True, "utilization_pct": 96.0,
                     "capacity_units": 5000, "region": "West"},
                ],
                "network_state": {"unserved_demand": 0.0},
            }

        class Insight:
            recommended_action = ""

        sentence, action = insights._recommended_action(
            Insight(), "Utilisation", "RISK", Pack())
        assert "Pune DC" in sentence
        assert action["scenario"]["facility_id"] == "DC_W"
        # The verb names THIS change — see TestEveryCtaNamesTheChangeItIsAbout.
        assert action["cta"] == "Test the capacity increase"

    def test_the_agent_s_own_line_still_wins(self):
        """It saw the evidence. Nothing here overrides a written finding."""
        import app.backend.api.insights as insights

        class Insight:
            recommended_action = "Split the Western volume across two sites."

        sentence, action = insights._recommended_action(
            Insight(), "Utilisation", "RISK", None)
        assert sentence == "Split the Western volume across two sites."
        assert action == {}

    def test_a_network_with_no_rows_falls_back_rather_than_inventing(self):
        import app.backend.api.insights as insights

        class Insight:
            recommended_action = ""

        sentence, action = insights._recommended_action(
            Insight(), "Utilisation", "RISK", None)
        assert action == {}
        assert not NAVIGATIONAL.search(sentence)


class TestTheScenarioPlannerSpeaksTheSameVocabulary:

    def _source(self) -> str:
        return (REPO_ROOT / "app" / "backend" / "api"
                / "scenarios.py").read_text(encoding="utf-8")

    def test_it_imports_the_keys_rather_than_restating_them(self):
        """
        A second tuple of the same strings is a vocabulary that drifts: a key
        added to the ladder and not here produces a card the scenario screen
        cannot map to a form.
        """
        import app.backend.api.scenarios as scenarios
        assert scenarios._ACTION_KEYS is ACTION_KEYS

    def test_every_intervention_carries_the_same_test_contract(self):
        import app.backend.api.scenarios as scenarios
        for key in ACTION_KEYS:
            prefill = scenarios._scenario_for(
                key, {"facility_id": "F1", "name": "Pune DC", "region": "West"})
            if key in {"NO_ACTION", "REQUEST_DATA", "CONSOLIDATE"}:
                continue
            assert prefill.get("action"), key

    def test_reopening_is_offered_when_a_site_is_at_its_ceiling(self):
        """
        The gate read "unserved > 0", so reopening was only ever offered on a
        plan that stranded demand. On a demand+80% run the solve filled the
        Southern DC to 100%, left a Northern DC closed and served everything —
        so the only recommendation was to BUILD capacity at a full site while
        paid-for capacity sat switched off.
        """
        import app.backend.api.scenarios as scenarios
        record = {
            "capacity_response": {
                "at_ceiling": [{"id": "DC_S", "name": "Southern DC",
                                "util_pct": 100.0, "capacity": 4000}],
                "idle": [{"id": "DC_N", "name": "Northern DC",
                          "capacity": 4500, "region": "North"}],
                "regions_without_room": [],
                "working_harder": [],
            },
            "scenario_kpis": {"unserved_demand": {"value": 0.0}},
            "request": {},
        }
        keys = [a["key"] for a in scenarios._recommended_actions(record)]
        assert keys[0] == "REOPEN_FACILITY", keys
        assert "ADD_CAPACITY" in keys


class TestTheModelPhrasesTheRecommendationItDoesNotChooseIt:
    """
    WHERE THE MODEL IS ALLOWED TO ACT, and where it is not.

    Which intervention a network needs is a function of the solved per-site
    load. It is decided by `build_actions`, deterministically, down a fixed
    ladder. The model's job is to say the chosen one in a sentence a leader
    would act on.

    Left to write the field freely against a real network, it produced:

        "Reassess transport and facility cost drivers and identify options to
         rebalance capacity away from Pune DC."

    — which names no change anyone can action. Constrained, on the same
    payload, it produced "Bring Delhi DC back into the network", which is the
    ladder's own first rung.

    THE SECOND CONSTRAINT IS BUDGET, and it is why this is ONE line rather
    than a shortlist. Measured against the live gateway: a three-option block
    asking the model to choose returned output_tokens=1984 and zero characters
    of visible text — the whole allowance spent deliberating, and the reasoning
    layer silently degraded to its template. The same payload without the block
    returned 1,688 tokens and real prose. Choosing is a decision task, and this
    model bills its thinking to the same budget it writes with.
    """

    def _payload(self, **overrides):
        payload = {
            "network_state": {"unserved_demand": 0.0, "currency": "INR"},
            "facilities": [
                {"facility_id": "DC_W", "facility_name": "Pune DC", "role": "DC",
                 "is_open": True, "utilization_pct": 96.4,
                 "capacity_units": 52000, "region": "West"},
                {"facility_id": "DC_N", "facility_name": "Delhi DC", "role": "DC",
                 "is_open": False, "utilization_pct": 0.0,
                 "capacity_units": 40000, "region": "North"},
            ],
        }
        payload.update(overrides)
        return payload

    def test_the_block_names_the_ladders_own_first_rung(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        block = ReasoningAgent._intervention_options(self._payload())
        expected = build_actions(self._payload()["facilities"],
                                 unserved_demand=0.0, limit=1)[0]
        assert expected.label in block
        assert "RECOMMEND EXACTLY THIS" in block

    def test_it_offers_no_choice(self):
        """
        A shortlist is what cost the entire output budget. One instruction is
        a rewriting task; three options is a decision.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        block = ReasoningAgent._intervention_options(self._payload())
        assert block.count("RECOMMEND") == 1
        # Short enough not to move the model's deliberation budget.
        assert len(block) < 160, len(block)

    def test_a_healthy_network_is_told_to_recommend_nothing(self):
        """
        Told to phrase an intervention on a network that needs none, the model
        would manufacture one.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        block = ReasoningAgent._intervention_options(self._payload(facilities=[
            {"facility_id": "A", "facility_name": "Pune DC", "role": "DC",
             "is_open": True, "utilization_pct": 55.0, "capacity_units": 5000},
        ]))
        assert "NO NETWORK CHANGE" in block

    def test_no_rows_means_no_constraint_rather_than_a_wrong_one(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        assert ReasoningAgent._intervention_options({}) == ""
        assert ReasoningAgent._intervention_options({"facilities": []}) == ""

    def test_a_chart_card_is_never_given_one(self):
        """A chart describes; it does not prescribe."""
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        source = __import__("inspect").getsource(ReasoningAgent._llm)
        assert 'if not payload.get("kpi_chart"):' in source

    def test_the_model_gets_the_instruction_in_its_prompt(self):
        """
        End to end against a STUB gateway — the shared token is 100 requests a
        day for the whole product, so the wiring is proven without spending one.
        """
        from netgravity.orchestrator.agents.llm_gateway import LLMResponse
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        seen = {}

        class Gateway:
            available = True

            def generate(self, prompt, purpose=""):
                seen["prompt"] = prompt
                return LLMResponse(output=(
                    '{"summary":"Pune DC is close to its ceiling while Delhi '
                    'DC is closed. This means capacity already paid for is '
                    'sitting idle.",'
                    '"recommendation":"Bring Delhi DC back into the network",'
                    '"confidence":"HIGH",'
                    '"key_drivers":["Pune near ceiling","Delhi closed"],'
                    '"risks":["No headroom in the west"]}'))

        result = ReasoningAgent(gateway=Gateway()).reason(
            self._payload(), allow_llm=True, scope=ReasoningScope.NETWORK)

        assert "RECOMMEND EXACTLY THIS: Bring Delhi DC back" in seen["prompt"]
        assert result.recommendation
        assert "Delhi DC" in result.recommendation

    def test_the_template_path_is_unaffected_when_no_model_runs(self):
        """
        The gateway is absent in CI and on any build with no token. The
        recommendation still has to be a decision, and it still comes from the
        deterministic side.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        result = ReasoningAgent(gateway=None).reason(
            self._payload(), allow_llm=True, scope=ReasoningScope.NETWORK)
        assert result.summary
        assert not NAVIGATIONAL.search(result.recommendation or "")


class TestEveryCtaNamesTheChangeItIsAbout:
    """
    "Test this as a scenario" sat under "Expand capacity at Pune DC", under
    "Bring Delhi DC back into the network" and under "Test consolidating Kochi
    DC" — one phrase for four different decisions, on cards where the label was
    the only thing telling them apart. A control that says the same thing
    whatever it is attached to stops being read.

    The second rule is about DESTINATION. A button on Insights or Forecast
    leaves the page, so it says where it goes; the scenario planner's own card
    does not, because the reader is already there.
    """

    def test_each_rung_has_its_own_verb(self):
        from netgravity.orchestrator.reasoning.strategic_actions import (
            CTA_BY_ACTION,
        )
        acting = {k: v for k, v in CTA_BY_ACTION.items() if v}
        assert len(set(acting.values())) == len(acting), \
            "two rungs share a call to action"

    def test_every_action_key_is_covered(self):
        from netgravity.orchestrator.reasoning.strategic_actions import (
            CTA_BY_ACTION,
        )
        assert set(CTA_BY_ACTION) == set(ACTION_KEYS)

    def test_a_capacity_increase_says_so(self):
        action = next(a for a in build_actions(
            [site("A", "Pune DC", 97.0, region="West")])
            if a.key == "ADD_CAPACITY")
        assert action.to_dict()["cta"] == "Test the capacity increase"

    def test_a_reopening_says_so(self):
        action = build_actions([
            site("A", "Pune DC", 97.0, region="West"),
            site("B", "Delhi DC", 0.0, region="North", is_open=False),
        ], unserved_demand=0)[0]
        assert action.key == "REOPEN_FACILITY"
        assert action.to_dict()["cta"] == "Test the reopening"

    def test_a_statement_still_has_no_verb(self):
        healthy = build_actions([site("A", "Pune DC", 55.0)], unserved_demand=0)
        assert healthy[0].to_dict()["cta"] == ""

    def test_no_cta_names_a_destination(self):
        """
        The destination belongs to the SURFACE, not to the action: the same
        recommendation is drawn on a page that navigates and on the page it
        would navigate to.
        """
        from netgravity.orchestrator.reasoning.strategic_actions import (
            CTA_BY_ACTION,
        )
        for verb in CTA_BY_ACTION.values():
            assert "scenario planner" not in verb.lower(), verb

    def test_the_scenario_planner_reads_the_same_map(self):
        import app.backend.api.scenarios as scenarios
        from netgravity.orchestrator.reasoning.strategic_actions import (
            CTA_BY_ACTION,
        )
        assert scenarios._CTA_BY_ACTION is CTA_BY_ACTION

    def test_the_planner_button_does_not_name_where_it_already_is(self):
        source = (REPO_ROOT / "app" / "frontend" / "js"
                  / "scenarios.js").read_text(encoding="utf-8")
        block = source[source.index("function takeActionsHtml("):]
        block = block[:block.index("\n/**")]
        code = re.sub(r"<!--[\s\S]*?-->", "", block)
        assert "scn-take-action-go" in code
        assert "scenario planner" not in code, \
            "the planner's own button is telling the reader to go where they are"

    def test_the_insights_button_says_where_it_goes(self):
        source = (REPO_ROOT / "app" / "frontend" / "js"
                  / "app.js").read_text(encoding="utf-8")
        block = source[source.index("const testHtml = intervention"):]
        block = block[:block.index("return `")]
        assert "in the scenario planner" in block
