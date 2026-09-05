"""
The findings from the UI review, held so they cannot come back.

Each test names the thing a tester actually saw. They are grouped by the
defect rather than by the module, because that is how they were reported and
how they will be checked again.
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
API = pathlib.Path(app.root_path) / "api"
SCENARIOS_JS = FRONTEND / "js" / "scenarios.js"
CARD_JS = FRONTEND / "js" / "insight-card.js"
INDEX_HTML = FRONTEND / "index.html"

requires_node = pytest.mark.skipif(shutil.which("node") is None,
                                   reason="node not installed")


def _join(text: str) -> str:
    """
    Read source as the strings it builds.

    Collapses whitespace AND closes the gap between adjacent string literals,
    so a sentence the author wrapped across two lines — `"... currency "` then
    `"symbols."` — matches the sentence it actually produces.
    """
    joined = re.sub(r'"\s*\n\s*"', "", text)      # adjacent literals
    joined = re.sub(r"'\s*\n\s*'", "", joined)
    return re.sub(r"\s+", " ", joined)


def _joined(path) -> str:
    return _join(pathlib.Path(path).read_text(encoding="utf-8"))


def _node(tmp_path, module, fn, *args):
    script = tmp_path / "run.mjs"
    script.write_text(
        f"import {{ {fn} }} from {json.dumps(str(module))};\n"
        f"process.stdout.write(String({fn}({', '.join(json.dumps(a) for a in args)})));\n",
        encoding="utf-8")
    return subprocess.run(["node", str(script)], capture_output=True,
                          text=True, check=True).stdout


class TestTheDecisionPackageIsGone:
    """
    It repeated the scenario comparison, and it was where the internal
    validation text surfaced. One comparison remains: the optimized result
    against the user's scenarios.
    """

    def test_no_module_no_modal_no_styles(self):
        assert not (FRONTEND / "js" / "decision-package.js").exists()
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert "modal-decision-package" not in html
        assert "dpk-" not in (FRONTEND / "css" / "explanation-pane.css").read_text(
            encoding="utf-8")

    def test_nothing_still_calls_it(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        for gone in ("decisionPackageHtml", "openDecisionPackage",
                     "data-decision-package", "decisionRows"):
            assert gone not in source


class TestInternalValidationTextNeverReachesAReader:
    """
    `UNGROUNDED CLAIM REMOVED` appeared on screen. It is an audit note, and a
    sentence that lost its number is not a sentence worth showing.
    """

    def test_a_redacted_sentence_is_dropped_whole(self):
        from netgravity.orchestrator.reasoning.card import strip_redactions

        got = strip_redactions(
            "Cost fell by [UNGROUNDED CLAIM REMOVED — authoritative x = 1]. "
            "Fill rate held at 68.5%.")
        assert "UNGROUNDED" not in got
        assert "Cost fell by" not in got, (
            "a claim stripped of its number reads as an assertion with no "
            "evidence; the sentence goes with it")
        assert "Fill rate held at 68.5%." in got

    def test_the_loss_is_reported_in_the_technical_detail(self):
        from types import SimpleNamespace

        from netgravity.orchestrator.explanation_service import build_card

        card = build_card(SimpleNamespace(
            summary="Cost fell by [UNSUPPORTED FIGURE REMOVED]. Fill rate held.",
            risks=[], recommendation="", key_drivers=[], briefing=None,
            validation_warnings=[]))

        rendered = " ".join([card["headline"], card["meaning"],
                             card["warning"], card["next_step"]])
        assert "REMOVED" not in rendered
        assert any("could not be matched" in d for d in card["details"])

    def test_no_marker_survives_any_reader_facing_field(self):
        from types import SimpleNamespace

        from netgravity.orchestrator.explanation_service import build_card

        poisoned = "[UNGROUNDED CLAIM REMOVED — authoritative cost = 1]"
        card = build_card(SimpleNamespace(
            summary=poisoned, risks=[poisoned], recommendation=poisoned,
            key_drivers=[poisoned], briefing=None, validation_warnings=[]))
        for field in ("headline", "meaning", "warning", "next_step"):
            assert "REMOVED" not in card[field], field


class TestOneCurrencyOnAScreen:
    """A table in $ beside a recommendation in ₹."""

    def test_money_is_an_amount_not_a_written_figure(self):
        from netgravity.orchestrator.reasoning.card import FORMAT_CURRENCY, Figure

        figure = Figure.money("Cost", 244_100_000.0)
        assert figure.format == FORMAT_CURRENCY
        assert figure.amount == 244_100_000.0
        assert figure.value == "", (
            "money written as a string here is money formatted without "
            "knowing the project's currency")

    def test_the_model_is_told_to_write_no_figures_at_all(self):
        source = _joined("netgravity/orchestrator/agents/reasoning_agent.py")
        assert "Do NOT write any figures" in source
        assert "currency symbols" in source

    @requires_node
    def test_the_card_never_picks_a_symbol_itself(self, tmp_path):
        html = _node(tmp_path, CARD_JS, "insightCardHtml",
                     {"headline": "H", "figures": [
                         {"label": "Cost", "amount": 1000, "format": "currency"}]},
                     {})
        assert "$" not in html and "₹" not in html and "€" not in html


class TestCostIsNeverShownAlone:
    """
    The cheapest option served 68.5% of demand at high capacity risk, and the
    card led with cost.
    """

    def test_a_poor_fill_rate_produces_a_warning(self):
        from app.backend.api.scenarios import _service_warning

        warning = _service_warning({"fill_rate": 0.685}, {"capacity_risk": "High"})
        assert "68.5%" in warning
        assert "capacity risk remains high" in warning
        assert "not necessarily an acceptable one" in warning

    def test_a_healthy_plan_gets_no_warning(self):
        from app.backend.api.scenarios import _service_warning

        assert _service_warning({"fill_rate": 0.99},
                                {"capacity_risk": "Healthy"}) == ""

    def test_high_capacity_risk_alone_is_enough(self):
        from app.backend.api.scenarios import _service_warning

        assert "capacity risk" in _service_warning({"fill_rate": 0.99},
                                                   {"capacity_risk": "High"})

    def test_the_floor_is_a_policy_constant_not_a_number_in_a_screen(self):
        from netgravity.config.defaults import SERVICE_THRESHOLDS

        assert "fill_rate_floor" in SERVICE_THRESHOLDS

    def test_the_three_figures_are_cost_service_and_risk(self):
        from app.backend.api.scenarios import _comparison_figures

        labels = [f.label for f in _comparison_figures(
            {"cost": 1.0, "fill_rate": 0.9}, {"capacity_risk": "High"})]
        assert labels == ["Cost", "Demand served", "Capacity risk"]

    def test_the_card_is_not_called_a_recommendation(self):
        """
        Identifying the lowest-cost result is not recommending a business
        decision, and a heading that says otherwise invites exactly the
        reading the warning exists to prevent.
        """
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        rendered = re.sub(r"^\s*//.*$", "", source, flags=re.M)
        assert "Comparison summary" in rendered
        assert "NetGravity's Recommendation" not in rendered

    def test_the_verdict_is_plain_english(self):
        source = _joined(API / "scenarios.py")
        assert "costs less than the network you run today" in source
        # Only the code, not the comment explaining what it replaced.
        code = re.sub(r"^\s*#.*$", "", (API / "scenarios.py").read_text(
            encoding="utf-8"), flags=re.M)
        assert "on solved business network cost" not in _join(code), (
            "the verdict described a solver rather than a decision")


class TestScenarioNamesNameTheRealFacility:
    """"Expand Warehouse A" while the selected site was Atlanta."""

    @requires_node
    def test_the_default_name_uses_the_facility_and_the_amount(self, tmp_path):
        script = tmp_path / "name.mjs"
        # `defaultScenarioName` is module-private, so this asserts the source
        # builds the name from the chosen facility rather than a placeholder.
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "function defaultScenarioName(" in source
        assert "facilityLabel(facilityId)" in source
        block = source.split("function defaultScenarioName")[1].split("\n}\n")[0]
        assert "${site}" in block and "units" in block
        # No invented site names. ("Untitled scenario" survives as the
        # last-resort default for an action with no facility, which is
        # honest — it names nothing rather than naming the wrong thing.)
        assert "Warehouse A" not in block

    def test_a_typed_name_still_wins(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "typed || defaultScenarioName(" in source

    def test_the_label_comes_from_the_loaded_network(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        block = source.split("function facilityLabel")[1].split("\n}\n")[0]
        assert "DCS" in block and "PLANTS" in block
        assert "match.name" in block


class TestTheSelectionStateIsNeverStale:
    """
    After the first scenario finished the card said "No scenario is
    selected", and corrected itself only when something re-rendered it.
    """

    def test_only_the_newest_render_may_write(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "takeCardRenderToken" in source
        block = source.split("async function renderMultiScenarioTakeCard")[1]
        block = block.split("\n}\n")[0]
        assert "const token = ++takeCardRenderToken" in block
        assert "token !== takeCardRenderToken" in block, (
            "a slower earlier render can still land on top of a newer one")

    def test_every_write_goes_through_the_guard(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        block = source.split("async function renderMultiScenarioTakeCard")[1]
        block = block.split("\n}\n")[0]
        # The guard itself is the only place that touches innerHTML.
        assert block.count("container.innerHTML") == 1, (
            "a direct write bypasses the staleness guard")
        assert "write(" in block

    def test_the_empty_state_no_longer_claims_nothing_is_selected(self):
        source = SCENARIOS_JS.read_text(encoding="utf-8")
        assert "No scenario is selected, so there is nothing to recommend" not in source
        assert "Select a scenario to compare it against your optimized result." in source


class TestProvenanceIsVisible:
    """A tester could not tell AI prose from template prose."""

    def test_the_saved_explanation_records_how_it_was_produced(self):
        from netgravity.orchestrator.explanations import SavedExplanation

        saved = SavedExplanation(source="llm", model_requests=1)
        assert saved.as_dict()["source"] == "llm"
        assert saved.as_dict()["model_requests"] == 1

    def test_a_stored_answer_is_marked_as_stored(self, tmp_path):
        from netgravity.ingestion.storage.local import LocalStorage
        from netgravity.orchestrator.explanation_service import ExplanationService
        from netgravity.orchestrator.explanations import (
            KIND_SCENARIO,
            ExplanationStore,
        )
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        for zone in ("raw", "standardized", "curated"):
            (tmp_path / zone).mkdir(parents=True, exist_ok=True)

        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

        service = ExplanationService(ReasoningAgent(),
                                     ExplanationStore(LocalStorage(tmp_path)))
        payload = {"network_state": {"business_network_cost": 1.0,
                                     "demand_fill_rate": 0.9}}

        def explain():
            return service.explain(
                subject_id="p1", kind=KIND_SCENARIO, scope=ReasoningScope.SCENARIO,
                result_parts=["e1"], build_payload=lambda: payload, allow_llm=False)

        first, second = explain(), explain()
        assert first["cached"] is False
        assert second["cached"] is True
        assert second["card"]["cached"] is True

    @requires_node
    def test_the_chip_distinguishes_all_three_states(self, tmp_path):
        live = _node(tmp_path, CARD_JS, "insightCardHtml",
                     {"headline": "H", "source": "llm", "cached": False},
                     {"showProvenance": True})
        cached = _node(tmp_path, CARD_JS, "insightCardHtml",
                       {"headline": "H", "source": "llm", "cached": True},
                       {"showProvenance": True})
        template = _node(tmp_path, CARD_JS, "insightCardHtml",
                         {"headline": "H", "source": "template", "cached": False},
                         {"showProvenance": True})
        assert "AI-generated" in live and "cache" not in live
        assert "from cache" in cached
        assert "Standard summary" in template


class TestTheVoiceIsAReportNotANarration:

    def test_the_first_person_is_removed(self):
        from netgravity.orchestrator.reasoning.card import plain_voice

        assert "I see" not in plain_voice("I see a cost of 4 crore at the plant.")
        assert plain_voice("My models agree.").startswith("The models")
        assert not plain_voice("I recommend testing capacity.").startswith("I ")

    def test_unearned_urgency_is_removed(self):
        from netgravity.orchestrator.reasoning.card import plain_voice

        assert "immediately" not in plain_voice("Act immediately on this.")

    def test_the_model_is_told_the_same_rules(self):
        source = (pathlib.Path("netgravity/orchestrator/agents/reasoning_agent.py")
                  .read_text(encoding="utf-8"))
        for rule in ("Third person", "Never 'I'", "Warehouse A",
                     "no algorithm names", "Say each thing once"):
            assert rule in source, rule

    def test_duplicate_text_is_not_shown_twice(self):
        """
        The model returned the same paragraph as both the opening and the
        first insight, and the screen rendered both.
        """
        from netgravity.orchestrator.reasoning.card import card_from_briefing
        from netgravity.orchestrator.schemas.reasoning import (
            ExecutiveBriefing,
            KPIInsight,
        )

        same = "Demand is expected to fall slightly over the next six periods."
        card = card_from_briefing(ExecutiveBriefing(
            opening=same, context="",
            kpi_insights=[KPIInsight(theme="T", headline=same, narrative=same)]))
        assert card.headline
        assert card.meaning != card.headline
