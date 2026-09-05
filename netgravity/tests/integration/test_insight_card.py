"""
The Contextual Explanation Pane and Suggested Analyses.

Both render reasoning that has ALREADY been computed and already been
grounded. The failure these tests exist to prevent is either of them quietly
becoming a second source of narrative or of numbers.

  * Contract: `/api/insights` returns every field the pane renders, and the
    frontend maps all of them. `missing_information` was returned by the
    endpoint and mapped nowhere; `suggested_questions` was mapped and rendered
    nowhere.

  * Assets: the pane's own module computes nothing and generates nothing, and
    every suggested analysis it offers is backed by a real capability.

  * Render: the pane produced for a briefing, run for real through node.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from app.backend.app import app

FRONTEND = pathlib.Path(app.root_path).parent / "frontend"
CARD_JS = FRONTEND / "js" / "insight-card.js"
SUGGEST_JS = FRONTEND / "js" / "suggested-analyses.js"
DATA_JS = FRONTEND / "js" / "data.js"
SCENARIOS_JS = FRONTEND / "js" / "scenarios.js"
INDEX_HTML = FRONTEND / "index.html"

requires_node = pytest.mark.skipif(shutil.which("node") is None,
                                   reason="node not installed")


def _without_comments(text: str) -> str:
    """A rule explained in a comment must not be failed by its explanation."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def _render(tmp_path, module, fn, *args):
    script = tmp_path / "render.mjs"
    script.write_text(
        f"import {{ {fn} }} from {json.dumps(str(module))};\n"
        f"process.stdout.write({fn}({', '.join(json.dumps(a) for a in args)}));\n",
        encoding="utf-8")
    return subprocess.run(["node", str(script)], capture_output=True,
                          text=True, check=True).stdout


BRIEFING = {
    "opening": "Your network serves 98.2% of demand at ₹4.20 crore.",
    "context": "Two of five sites run above 90% of capacity.",
    "insights": [{
        "headline": "Pune DC is at its capacity limit",
        "narrative": "It runs at 97.1% of throughput capacity.",
        "severity": "RISK",
        "evidence": [{"label": "Utilisation", "display_value": "97.1%"},
                     {"label": "Capacity", "display_value": "8,000 units/day"}],
    }],
    "keyDrivers": ["Demand is concentrated in the west"],
    "recommendation": "Test added capacity at Pune before the peak.",
    "limitation": "No demand history was supplied, so this assumes flat demand.",
    "evidenceCompleteness": "PARTIAL",
    "groundingStatus": "VERIFIED",
    "computedAt": "2026-09-05T09:00:00Z",
    "suggestedQuestions": ["What is driving this result?",
                           "What if we close the Nagpur DC?"],
    "missingInformation": [{"question_ref": "forecast",
                            "question": "Can you provide the missing forecast evidence?",
                            "impact": "It would let me complete this briefing.",
                            "blocking": False}],
}


class TestTheContractReachesTheScreen:

    def test_the_endpoint_returns_every_field_the_pane_renders(self):
        source = (pathlib.Path(app.root_path) / "api" / "insights.py").read_text(
            encoding="utf-8")
        for field in ("\"opening\"", "\"context\"", "\"key_drivers\"",
                      "\"recommendation\"", "\"limitation\"",
                      "\"suggested_questions\"", "\"missing_information\"",
                      "\"evidence_completeness\""):
            assert field in source, f"/api/insights does not return {field}"

    def test_the_frontend_maps_all_of_them(self):
        source = DATA_JS.read_text(encoding="utf-8")
        # These two were the gap: one returned and never mapped, one mapped
        # and never rendered.
        assert "response.missing_information" in source
        assert "response.opening" in source
        assert "response.context" in source
        assert "response.suggested_questions" in source

    def test_a_cleared_network_clears_the_narrative_too(self):
        """
        A previous client's briefing must not survive `clearNetworkModel`. It
        names their facilities.
        """
        source = DATA_JS.read_text(encoding="utf-8")
        clear_block = source.split("Object.assign(NETWORK_RECOMMENDATION, {")[1]
        clear_block = clear_block.split("});")[0]
        for field in ("opening", "context", "missingInformation",
                      "suggestedQuestions", "keyDrivers", "limitation"):
            assert field in clear_block, f"{field} survives a network clear"


class TestTheCardSaysOneThingOnce:
    """
    The screens were showing one finding as six: a headline, a paragraph, an
    insight, an evidence table, a recommendation and a technical note. The
    card is the correction — conclusion, meaning, three numbers, one warning,
    one next step, everything else collapsed.
    """

    def test_the_card_is_mounted_where_the_pane_was(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        right = html.split('class="scn-single-right"')[1].split("</div>\n            </div>")[0]
        assert "scn-multi-explanation-card" in right
        assert 'id="scn-multi-take-card"' in right

    def test_home_did_not_quietly_get_one(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        home = html.split('id="tab-home"')[1].split('id="tab-')[0] \
            if 'id="tab-home"' in html else ""
        assert "scn-multi-explanation-card" not in home

    def test_the_card_module_does_no_arithmetic(self):
        source = _without_comments(CARD_JS.read_text(encoding="utf-8"))
        for operator in (" * ", " / ", ".toFixed(", "Math.round", "Math.abs",
                         "parseFloat", "parseInt", "reduce("):
            assert operator not in source, (
                f"the card computes something ({operator.strip()!r}); it may "
                f"only render what the server supplied")

    def test_the_card_decides_no_currency(self):
        """
        A second currency formatter is a second place for currency to be
        decided, and the two disagree — which is how a table in $ came to sit
        beside a recommendation in ₹.
        """
        source = _without_comments(CARD_JS.read_text(encoding="utf-8"))
        for symbol in ("'$'", '"$"', "'₹'", '"₹"', "toLocaleString",
                       "Intl.NumberFormat"):
            assert symbol not in source, f"the card decides currency ({symbol})"
        assert "opts.formatCurrency" in source, (
            "money must go through the app's one formatter")

    def test_the_card_does_not_call_an_api(self):
        source = _without_comments(CARD_JS.read_text(encoding="utf-8"))
        for call in ("fetch(", "apiClient", "XMLHttpRequest"):
            assert call not in source

    @requires_node
    def test_it_renders_the_card_it_is_given(self, tmp_path):
        card = {
            "headline": "M002/P001 demand is expected to fall slightly.",
            "meaning": "Plan for roughly 5,400 to 5,800 units in the final period.",
            "warning": "Three products have irregular demand.",
            "next_step": "Review those products before setting safety stock.",
            "figures": [{"label": "Final period", "value": "5,661 units",
                         "format": "text"}],
            "details": ["36 periods of history; ETS; MASE 0.72."],
            "source": "llm", "cached": False,
        }
        html = _render(tmp_path, CARD_JS, "insightCardHtml", card,
                       {"title": "Demand forecast", "showProvenance": True})

        assert card["headline"] in html
        assert card["meaning"] in html
        assert card["warning"] in html
        assert card["next_step"] in html
        assert "5,661 units" in html

    @requires_node
    def test_the_technical_account_starts_collapsed(self, tmp_path):
        html = _render(tmp_path, CARD_JS, "insightCardHtml",
                       {"headline": "H", "details": ["36 periods of history."]}, {})
        assert "How was this calculated?" in html
        assert 'aria-expanded="false"' in html
        assert "hidden" in html

    @requires_node
    def test_at_most_three_figures(self, tmp_path):
        card = {"headline": "H", "figures": [
            {"label": f"m{i}", "value": f"{i}", "format": "text"} for i in range(6)]}
        html = _render(tmp_path, CARD_JS, "insightCardHtml", card, {})
        assert html.count('class="ic-figure"') == 3

    @requires_node
    def test_provenance_says_where_the_words_came_from(self, tmp_path):
        """
        A tester could not tell AI prose from template prose, or a fresh
        answer from a stored one, without reading the server log.
        """
        base = {"headline": "H"}
        live = _render(tmp_path, CARD_JS, "insightCardHtml",
                       {**base, "source": "llm", "cached": False},
                       {"showProvenance": True})
        cached = _render(tmp_path, CARD_JS, "insightCardHtml",
                         {**base, "source": "llm", "cached": True},
                         {"showProvenance": True})
        template = _render(tmp_path, CARD_JS, "insightCardHtml",
                           {**base, "source": "template"},
                           {"showProvenance": True})

        assert "AI-generated" in live and "cache" not in live
        assert "AI-generated · from cache" in cached
        assert "Standard summary" in template

    @requires_node
    def test_provenance_is_off_unless_asked_for(self, tmp_path):
        html = _render(tmp_path, CARD_JS, "insightCardHtml",
                       {"headline": "H", "source": "llm"}, {})
        assert "AI-generated" not in html

    @requires_node
    def test_money_is_rendered_by_the_formatter_it_is_given(self, tmp_path):
        script = tmp_path / "money.mjs"
        script.write_text(
            f"import {{ insightCardHtml }} from {json.dumps(str(CARD_JS))};\n"
            "const card = {headline:'H', figures:[{label:'Cost', amount:4200000,"
            " format:'currency'}]};\n"
            "process.stdout.write(insightCardHtml(card, "
            "{formatCurrency: (n) => '\u20b9' + (n/10000000).toFixed(2) + 'Cr'}));\n",
            encoding="utf-8")
        html = subprocess.run(["node", str(script)], capture_output=True,
                              text=True, check=True).stdout
        assert "₹0.42Cr" in html

    @requires_node
    def test_money_with_no_formatter_shows_no_symbol(self, tmp_path):
        """A wrong currency symbol is worse than none."""
        html = _render(tmp_path, CARD_JS, "insightCardHtml",
                       {"headline": "H", "figures": [
                           {"label": "Cost", "amount": 4200000,
                            "format": "currency"}]}, {})
        assert "$" not in html and "₹" not in html

    @requires_node
    def test_an_uninterpreted_result_says_so(self, tmp_path):
        html = _render(tmp_path, CARD_JS, "insightCardHtml", {},
                       {"emptyText": "Explanation unavailable for this saved scenario."})
        assert "Explanation unavailable for this saved scenario." in html

    @requires_node
    def test_nothing_at_all_renders_nothing(self, tmp_path):
        assert _render(tmp_path, CARD_JS, "insightCardHtml", {}, {}).strip() == ""


class TestSuggestedAnalyses:
    """
    The next steps are PREDEFINED scenario templates, PREFILLED from the
    solved network — not the briefing's free-text questions, which on the
    deterministic path are two fixed strings ("What is driving this result?")
    that hand the user a sentence to translate into a form themselves.
    """

    @requires_node
    def test_templates_are_rendered_as_runnable_set_ups(self, tmp_path):
        templates = [
            {"id": "relieve_hottest", "action": "CHANGE_CAPACITY",
             "label": "Add capacity where the network is tightest",
             "why": "Pune DC is running at 97% of capacity.",
             "fields": {"facility": "DC1", "amount": 800}},
        ]
        html = _render(tmp_path, SUGGEST_JS, "suggestedAnalysesHtml", templates, [])

        assert "Add capacity where the network is tightest" in html
        assert "Pune DC is running at 97% of capacity." in html
        assert 'data-scenario-template="relieve_hottest"' in html
        assert ">Set up<" in html

    @requires_node
    def test_free_text_questions_are_no_longer_offered_as_actions(self, tmp_path):
        """
        The regression. `suggested_questions` used to be rendered as
        next-step buttons; a question is not a next step, and the two fixed
        strings the template path emits are not analyses.
        """
        html = _render(tmp_path, SUGGEST_JS, "suggestedAnalysesHtml",
                       BRIEFING["suggestedQuestions"], [])
        for question in BRIEFING["suggestedQuestions"]:
            assert question not in html

    @requires_node
    def test_missing_information_is_still_rendered(self, tmp_path):
        html = _render(tmp_path, SUGGEST_JS, "suggestedAnalysesHtml", [],
                       BRIEFING["missingInformation"])
        assert BRIEFING["missingInformation"][0]["question"] in html
        assert BRIEFING["missingInformation"][0]["impact"] in html

    @requires_node
    def test_the_suggestions_sit_beside_the_explanation_not_inside_it(self):
        """
        What to test next is a different thing from what happened, and
        folding one into the other is how a card came to hold six messages.
        """
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "insightCardHtml(" in source and "suggestedAnalysesHtml(" in source
        assert "+ suggestedAnalysesHtml(" in source

    def test_every_template_names_a_real_scenario_action(self):
        """
        A template that named an action the builder cannot submit would be a
        button that opens a form nobody can fill.
        """
        from netgravity.orchestrator.schemas.requests import ScenarioActionType

        source = (FRONTEND / "js" / "scenario-templates.js").read_text(encoding="utf-8")
        actions = set(re.findall(r"action: '([A-Z_]+)'", source))
        assert actions, "no templates declare an action"
        known = {a.value for a in ScenarioActionType}
        assert actions <= known, f"unknown scenario actions: {actions - known}"

    def test_the_catalogue_is_predefined_not_generated(self):
        source = (FRONTEND / "js" / "scenario-templates.js").read_text(encoding="utf-8")
        assert "export const TEMPLATES = [" in source, (
            "the templates must be a fixed catalogue, not produced from prose")

    @requires_node
    def test_a_template_with_no_subject_in_this_network_is_not_offered(self, tmp_path):
        """
        Prefilled means prefilled from THIS network. A site-specific template
        with no site to name is withheld rather than offered blank.
        """
        script = tmp_path / "templates.mjs"
        script.write_text(
            f"import {{ availableTemplates }} from "
            f"{json.dumps(str(FRONTEND / 'js' / 'scenario-templates.js'))};\n"
            "const thresholds = {utilization_over_pct: 90, utilization_under_pct: 40};\n"
            # isOpen is required: a site the solver never opened is not a
            # candidate for a closure test, whatever its utilisation reads.
            "const hot = availableTemplates({dcs:["
            "{id:'A',name:'A',capacity:100,utilPct:97,peakUtilPct:97,isOpen:true},"
            "{id:'B',name:'B',capacity:100,utilPct:20,peakUtilPct:20,isOpen:true}],"
            " plants:[], thresholds}, 5);\n"
            "const flat = availableTemplates({dcs:["
            "{id:'A',name:'A',capacity:100,utilPct:60,peakUtilPct:60,isOpen:true}],"
            " plants:[], thresholds}, 5);\n"
            "process.stdout.write(JSON.stringify({hot: hot.map(t=>t.id), "
            "flat: flat.map(t=>t.id), hotFields: hot[0].fields}));\n",
            encoding="utf-8")
        got = json.loads(subprocess.run(["node", str(script)], capture_output=True,
                                        text=True, check=True).stdout)

        assert "relieve_hottest" in got["hot"]
        assert "consolidate_coldest" in got["hot"]
        # Nothing is hot or cold here, so neither site-specific template applies.
        assert "relieve_hottest" not in got["flat"]
        assert "consolidate_coldest" not in got["flat"]
        # The generic sensitivity tests still are — they need no subject.
        assert "demand_upside" in got["flat"]
        # And the prefill names the site the solve actually found.
        assert got["hotFields"]["facility"] == "A"

    def test_the_builder_accepts_a_prefill(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "function openCreateToolbox(prefill = null)" in source
        assert "applyToolboxPrefill(prefill)" in source
        assert "openCreateToolbox(t)" in source, (
            "a template must open the builder already filled in")

    def test_a_prefill_never_submits_a_facility_this_network_lacks(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        block = source.split("function applyToolboxPrefill")[1].split("\n}\n")[0]
        assert "el.options" in block, (
            "a facility id the builder does not offer must be skipped, not "
            "written into a form the solver would reject")


# ---------------------------------------------------------------------------
# The explanation is about what was actually analysed
# ---------------------------------------------------------------------------

from netgravity.orchestrator.schemas.reasoning import ReasoningScope  # noqa: E402


class TestTheBriefingIsScopedToWhatRan:
    """
    The reasoning step always asked for a NETWORK briefing, including on a
    scenario run — so a what-if produced an explanation of the network in
    general, and a screen showing it beside a scenario's numbers was
    captioning the wrong picture.
    """

    def _ctx(self, scenario_id=None, scenario_ids=()):
        from types import SimpleNamespace

        return SimpleNamespace(scenario_id=scenario_id,
                               scenario_ids=list(scenario_ids))

    def test_one_scenario_is_scoped_to_that_scenario(self):
        from netgravity.orchestrator.registry import build_orchestrator  # noqa: F401
        import netgravity.orchestrator.registry as registry_module

        source = pathlib.Path(registry_module.__file__).read_text(encoding="utf-8")
        assert "def _reasoning_scope_for(ctx" in source
        assert "scope=_reasoning_scope_for(ctx)" in source, (
            "the reasoning step still asks for a NETWORK briefing whatever ran")

    def test_the_scope_follows_the_execution(self):
        """Read off what was solved, so it cannot disagree with it."""
        import netgravity.orchestrator.registry as registry_module

        source = pathlib.Path(registry_module.__file__).read_text(encoding="utf-8")
        block = source.split("def _reasoning_scope_for")[1].split("\n    async def")[0]
        assert "ReasoningScope.COMPARISON" in block
        assert "ReasoningScope.SCENARIO" in block
        assert "ReasoningScope.NETWORK" in block
        assert "ctx.scenario_ids" in block and "ctx.scenario_id" in block

    def test_the_scenario_api_returns_that_briefing(self):
        source = (pathlib.Path(app.root_path) / "api"
                  / "scenarios.py").read_text(encoding="utf-8")
        assert "def _scenario_explanation(ctx" in source
        assert '"explanation": _scenario_explanation(ctx)' in source

    def test_the_scenario_pane_renders_the_scenarios_own_briefing(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        block = source.split("function renderMultiScenarioExplanation")[1]
        block = block.split("\n}\n")[0]
        assert "scenarioBriefing(" in block, (
            "the pane must render the selected scenario's briefing")
        # The network briefing remains only as the no-selection fallback.
        assert "networkBriefing()" in block

    def test_a_scenario_with_no_briefing_does_not_borrow_the_networks(self):
        source = (pathlib.Path(app.root_path) / "api"
                  / "scenarios.py").read_text(encoding="utf-8")
        block = source.split("def _scenario_explanation")[1].split("\ndef ")[0]
        assert "return {}" in block, (
            "an absent briefing must come back empty, so the pane says it has "
            "nothing to explain rather than showing another scope's words")

    def test_forecast_is_a_scope_the_reasoning_layer_knows(self):
        assert ReasoningScope.FORECAST.value == "FORECAST"


class TestTheForecastScreenShowsItsExplanation:

    def test_the_forecast_tab_has_a_slot_for_it(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        forecast = html.split('id="tab-forecast"')[1].split("</section>")[0]
        assert 'id="fc-explanation"' in forecast

    def test_the_page_renders_into_it(self):
        source = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
        assert "function renderForecastExplanation()" in source
        assert "renderForecastExplanation();" in source

    def test_it_redraws_when_the_forecast_arrives(self):
        """
        The forecast is fetched after the screens render. A card written once
        at page load would keep whatever it said before the engine answered.
        """
        source = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
        listener = source.split("addEventListener('forecastSeriesLoaded'")[1]
        listener = listener.split("});")[0]
        assert "renderForecastExplanation()" in listener

    def test_the_explanation_travels_from_the_endpoint_to_the_screen(self):
        hydrate = (FRONTEND / "js" / "integration"
                   / "hydrate.js").read_text(encoding="utf-8")
        assert "explanation: fc.explanation" in hydrate, (
            "the endpoint has returned a briefing that nothing read")


class TestWarehouseSuggestionsRespectTheSolve:
    """
    Two failures the templates had, both from reading one figure too few.

      * `utilPct` is the horizon MEAN. A site averaging 60% and reaching 98%
        in its busiest period is under pressure, and the mean cannot say so.
        `peakUtilPct` is written beside it and was ignored.
      * `isOpen` is the solver's own decision. A proposed site it declined to
        open sits at 0%, sorted coldest, and was offered "test closing this
        warehouse" — for a warehouse that is not running.
    """

    def _templates(self, tmp_path, dcs, thresholds=None):
        thresholds = thresholds or {"utilization_over_pct": 90,
                                    "utilization_under_pct": 40}
        script = tmp_path / "tmpl.mjs"
        script.write_text(
            f"import {{ availableTemplates }} from "
            f"{json.dumps(str(FRONTEND / 'js' / 'scenario-templates.js'))};\n"
            f"const out = availableTemplates({json.dumps({'dcs': dcs, 'plants': [], 'thresholds': thresholds})}, 5);\n"
            "process.stdout.write(JSON.stringify(out));\n",
            encoding="utf-8")
        return json.loads(subprocess.run(["node", str(script)], capture_output=True,
                                         text=True, check=True).stdout)

    @requires_node
    def test_a_site_hot_only_at_peak_is_caught(self, tmp_path):
        got = self._templates(tmp_path, [
            {"id": "A", "name": "Pune", "capacity": 8000,
             "utilPct": 60, "peakUtilPct": 98, "isOpen": True},
            {"id": "B", "name": "Indore", "capacity": 5000,
             "utilPct": 55, "peakUtilPct": 70, "isOpen": True},
        ])
        relieve = next((t for t in got if t["id"] == "relieve_hottest"), None)
        assert relieve is not None, (
            "a site at 98% in its busiest period was missed because the "
            "average is 60%")
        assert relieve["fields"]["facility"] == "A"
        assert "busiest period" in relieve["why"]

    @requires_node
    def test_an_unopened_proposed_site_is_never_offered_for_closure(self, tmp_path):
        got = self._templates(tmp_path, [
            {"id": "NEW", "name": "Nagpur (proposed)", "capacity": 5000,
             "utilPct": 0, "peakUtilPct": 0, "isOpen": False},
            {"id": "A", "name": "Pune", "capacity": 8000,
             "utilPct": 80, "peakUtilPct": 85, "isOpen": True},
            {"id": "B", "name": "Indore", "capacity": 5000,
             "utilPct": 20, "peakUtilPct": 25, "isOpen": True},
        ])
        closure = next((t for t in got if t["id"] == "consolidate_coldest"), None)
        assert closure is not None
        assert closure["fields"]["facility"] == "B", (
            "the closure test named a site the solver never opened")

    @requires_node
    def test_a_site_with_no_open_decision_is_not_assumed_open(self, tmp_path):
        got = self._templates(tmp_path, [
            {"id": "A", "name": "Unknown", "capacity": 5000,
             "utilPct": 5, "peakUtilPct": 5, "isOpen": None},
            {"id": "B", "name": "Pune", "capacity": 8000,
             "utilPct": 80, "peakUtilPct": 85, "isOpen": True},
        ])
        assert not [t for t in got if t["id"] == "consolidate_coldest"]

    @requires_node
    def test_the_mean_still_decides_consolidation(self, tmp_path):
        """
        Consolidation is about how much a site is used over the horizon. The
        peak is reported beside it so a reader can see what closing it would
        have to answer for.
        """
        got = self._templates(tmp_path, [
            {"id": "A", "name": "Quiet", "capacity": 5000,
             "utilPct": 20, "peakUtilPct": 75, "isOpen": True},
            {"id": "B", "name": "Busy", "capacity": 8000,
             "utilPct": 80, "peakUtilPct": 85, "isOpen": True},
        ])
        closure = next(t for t in got if t["id"] == "consolidate_coldest")
        assert closure["fields"]["facility"] == "A"
        assert "75% at its busiest" in closure["why"]

    @requires_node
    def test_a_single_period_solve_falls_back_to_the_mean(self, tmp_path):
        """No peak exists on a single-period solve; the mean is then the only
        figure and is used as such rather than the site being skipped."""
        got = self._templates(tmp_path, [
            {"id": "A", "name": "Pune", "capacity": 8000,
             "utilPct": 96, "isOpen": True},
            {"id": "B", "name": "Indore", "capacity": 5000,
             "utilPct": 50, "isOpen": True},
        ])
        relieve = next(t for t in got if t["id"] == "relieve_hottest")
        assert relieve["fields"]["facility"] == "A"
        assert "busiest period" not in relieve["why"], (
            "a mean must not be described as a peak")
