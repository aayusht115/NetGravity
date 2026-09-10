"""
Why the winner beats the alternatives.

The scenario planner could say "Nagpur is cheapest" — a fact about Nagpur —
but not "Nagpur costs less than expanding Delhi while serving the same
demand", which is the sentence a decision needs. That is about a PAIR, and
nothing assembled pairs.

The narrative comes from the deterministic template and must ground: a
correct figure coming back UNSUPPORTED would undermine the check everything
else relies on.

NOT COVERED HERE, deliberately. The branch this was taken from also narrates
what redesigning the footprint is worth, through
`evidence.with_optimised_reference`. That belongs to the Overview's own
briefing — a screen this round does not touch — so the helper is not ported
and its tests are not carried. The same attribution IS made for a scenario,
deterministically and in the ranking rather than in prose: see
`_attribution_caveat` in app/backend/api/scenarios.py, tested in
netgravity/tests/integration/test_scenario_recommendation.py.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.reasoning.comparison_evidence import (
    comparison_reasoning_payload,
)
from netgravity.orchestrator.schemas.reasoning import ReasoningScope
from netgravity.orchestrator.validation.numeric_grounding import _FACT_SPEC


def _row(sid, name, cost, fill=0.98, comparable=True, delta=None):
    return {"scenario_id": sid, "name": name, "cost": cost,
            "cost_delta": delta, "fill_rate": fill, "comparable": comparable}


def _explain(payload, scope):
    return ReasoningAgent().reason(payload, scope=scope, allow_llm=False)


def _themes(result):
    return {i.theme: i for i in result.briefing.kpi_insights}


class TestWhyTheWinnerBeatsTheAlternatives:

    def _compare(self, rows, recommended="A", verdict="A is cheapest."):
        return _explain(
            comparison_reasoning_payload(
                ranked=rows, recommended_scenario_id=recommended,
                verdict=verdict, baseline_cost=40_000_000.0),
            ReasoningScope.COMPARISON)

    def test_it_names_the_alternative_and_the_gap(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0),
                             _row("B", "Expand Delhi", 41_000_000.0)])
        trade = _themes(out)["Trade-off"]

        assert "Open Nagpur costs 3,000,000 less than Expand Delhi" in trade.narrative
        assert "Both serve the same demand." in trade.narrative

    def test_a_difference_in_demand_served_is_stated_not_glossed(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0, fill=0.982),
                             _row("B", "Close Kolkata", 39_000_000.0, fill=0.951)])
        trade = _themes(out)["Trade-off"]

        assert "serves 3.1 points less of demand" in trade.narrative, (
            "a cheaper plan that serves less must not read as simply cheaper")

    def test_an_incomparable_alternative_is_reported_not_dropped(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0),
                             _row("B", "Unmeasured", None, comparable=False)])
        assert "Not compared" in _themes(out)

    def test_it_never_reads_as_approval(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0),
                             _row("B", "Expand Delhi", 41_000_000.0)])
        text = out.briefing.visible_text().lower()
        for approving in ("approved", "go ahead", "proceed with", "we will open"):
            assert approving not in text
        assert "not one i make" in out.recommendation.lower()

    def test_a_near_tie_says_the_choice_is_not_about_cost(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0),
                             _row("B", "Expand Delhi", 38_000_000.5)])
        assert "something other than cost" in out.recommendation

    def test_nothing_comparable_recommends_re_running(self):
        out = self._compare([_row("A", "Unmeasured", None, comparable=False)],
                            recommended=None, verdict="Nothing comparable.")
        assert "re-running" in out.recommendation

    def test_every_comparison_figure_grounds(self):
        out = self._compare([_row("A", "Open Nagpur", 38_000_000.0),
                             _row("B", "Expand Delhi", 41_000_000.0),
                             _row("C", "Close Kolkata", 39_000_000.0, fill=0.951)])
        assert out.validation_warnings == [], out.validation_warnings

    def test_the_comparison_facts_are_registered(self):
        for key in ("cost_gap_vs_recommended", "fill_gap_vs_recommended_pts",
                    "recommended_cost", "n_not_comparable"):
            assert key in _FACT_SPEC, f"{key} is cited but is not an authoritative fact"

    def test_the_pack_ranks_nothing_itself(self):
        """The backend has already decided. This shapes, it does not choose."""
        payload = comparison_reasoning_payload(
            ranked=[_row("B", "Costlier", 41_000_000.0),
                    _row("A", "Cheaper", 38_000_000.0)],
            recommended_scenario_id="B", verdict="B was chosen.")
        # B is recommended even though A is cheaper — the pack does not
        # second-guess the ranking it was handed.
        assert payload["comparison"]["recommended_scenario_id"] == "B"
