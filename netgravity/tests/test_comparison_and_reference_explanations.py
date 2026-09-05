"""
Two explanations that did not exist: why the winner beats the alternatives,
and what redesigning the footprint is worth.

  * COMPARISON. The Decision Package could say "Nagpur is cheapest" — a fact
    about Nagpur — but not "Nagpur costs less than expanding Delhi while
    serving the same demand", which is the sentence a decision needs. That is
    about a PAIR, and nothing assembled pairs.

  * OPTIMISED REFERENCE. The Overview shows an ACTUAL_AS_IS_EVALUATION: the
    footprint pinned open, because that is the network the client runs. So
    "explain the optimised result against the baseline" had no baseline — the
    screen IS the baseline. The optimised model already existed, computed
    only to attribute scenario savings.

Both narrate through the deterministic template and both must ground: a
correct figure coming back UNSUPPORTED would undermine the check everything
else relies on.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.reasoning.comparison_evidence import (
    comparison_reasoning_payload,
)
from netgravity.orchestrator.reasoning.evidence import with_optimised_reference
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

        assert "Open Nagpur costs 3,000,000.00 less than Expand Delhi" in trade.narrative
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


class TestWhatRedesigningTheFootprintIsWorth:

    def _state(self, **over):
        state = {"business_network_cost": 42_000_000.0, "demand_fill_rate": 0.982,
                 "n_facilities_open": 8, "total_demand": 100_000.0,
                 "served_demand": 98_200.0}
        state.update(over)
        return {"network_state": state}

    def _reference(self, cost=38_500_000.0, open_sites=6, status="VALID"):
        return {"business_network_cost": {"value": cost, "status": status},
                "demand_fill_rate": {"value": 0.982, "status": "VALID"},
                "n_facilities_open": {"value": open_sites, "status": "VALID"}}

    def test_it_states_the_value_of_redesigning(self):
        out = _explain(with_optimised_reference(self._state(), self._reference()),
                       ReasoningScope.NETWORK)
        footprint = _themes(out)["Footprint"]

        assert "38,500,000.00" in footprint.narrative
        assert "3,500,000.00 less" in footprint.narrative
        assert "value of redesigning" in footprint.narrative

    def test_it_warns_that_a_scenario_will_appear_to_contain_it(self):
        """
        The reason this figure matters: measured against today's number, every
        scenario appears to save the redesign as well as its own change.
        """
        out = _explain(with_optimised_reference(self._state(), self._reference()),
                       ReasoningScope.NETWORK)
        assert "appear to contain it" in _themes(out)["Footprint"].narrative

    def test_a_footprint_already_near_optimal_is_told_so(self):
        out = _explain(
            with_optimised_reference(self._state(),
                                     self._reference(cost=42_500_000.0)),
            ReasoningScope.NETWORK)
        assert "already at or near" in _themes(out)["Footprint"].narrative

    def test_a_different_site_count_is_named(self):
        out = _explain(with_optimised_reference(self._state(), self._reference()),
                       ReasoningScope.NETWORK)
        shape = _themes(out)["Footprint shape"]
        assert "6 sites against the 8 open today" in shape.narrative
        assert "different footprint rather than the same one run more cheaply" \
            in shape.narrative

    def test_no_reference_makes_no_claim(self):
        payload = with_optimised_reference(self._state(), None)
        assert "optimised_reference" not in payload
        out = _explain(payload, ReasoningScope.NETWORK)
        assert "Footprint" not in _themes(out)

    def test_a_refused_reference_metric_is_never_read_as_a_number(self):
        payload = with_optimised_reference(
            self._state(), self._reference(status="NOT_COMPUTABLE"))
        assert payload["optimised_reference"]["reference_cost"] is None
        out = _explain(payload, ReasoningScope.NETWORK)
        assert "Footprint" not in _themes(out)

    def test_every_reference_figure_grounds(self):
        out = _explain(with_optimised_reference(self._state(), self._reference()),
                       ReasoningScope.NETWORK)
        assert out.validation_warnings == [], out.validation_warnings

    def test_it_solves_nothing_of_its_own(self):
        """
        The reference is the scenario API's cached solve. Running a second one
        here would double every project's solve cost.
        """
        import pathlib

        source = pathlib.Path(
            "netgravity/orchestrator/reasoning/evidence.py").read_text(
                encoding="utf-8")
        block = source.split("def with_optimised_reference")[1].split("\ndef ")[0]
        for solving in ("milp_solve", "run_sync", "solve_result", "OrchestratorRequest"):
            assert solving not in block
