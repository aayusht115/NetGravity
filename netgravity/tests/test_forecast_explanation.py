"""
Forecast Explanation — the FORECAST reasoning scope.

The forecasting engine computes real numbers (ETS / intermittent / quantile
modelling, structural-break detection, p10/p90 bands, measured backtest error)
and none of it was narrated anywhere: `ReasoningScope` had no FORECAST member,
so no evidence pack was shaped for forecast output.

Three claims under test:

  1. The evidence pack SELECTS the engine's figures; it derives nothing.
  2. The narration says what the forecast says and how confident it is — and
     never why demand moves, because nothing in netgravity/forecasting/
     computes a cause. A structural break is a detected shift, not a reason.
  3. Every figure it cites grounds. `numeric_grounding` must be able to source
     forecast numbers, or a correct claim comes back UNSUPPORTED and the
     grounding discipline the rest of the system relies on is undermined.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.reasoning.evidence import build_evidence_pack
from netgravity.orchestrator.reasoning.forecast_evidence import (
    forecast_reasoning_payload,
)
from netgravity.orchestrator.schemas.reasoning import ReasoningScope
from netgravity.orchestrator.validation.numeric_grounding import _FACT_SPEC


def _point(period, mean, p10, p90):
    return NS(period=period, mean=mean, p10=p10, p50=mean, p90=p90,
              baseline_mean=None)


def _accuracy(mase=0.72, n_folds=4):
    return NS(mae=12.0, rmse=18.0, wape=0.08, mase=mase, n_folds=n_folds,
              n_observations=20, method="ROLLING_ORIGIN")


def _series(market_id="M_PUNE", status="OK", points=None, accuracy=None,
            break_detected=None, pattern="SMOOTH", engine="ETS",
            n_history=24, reason=""):
    return NS(
        market_id=market_id, product_id="P1", status=NS(value=status),
        engine=engine, pattern=(NS(value=pattern) if pattern else None),
        n_history_periods=n_history, reason=reason,
        points=list(points or []), accuracy=accuracy,
        structural_break=(None if break_detected is None
                          else NS(detected=break_detected, period=7, method="PELT")),
    )


def _result(series, status="OK", warnings=None, errors=None):
    return NS(status=NS(value=status), series=list(series),
              warnings=list(warnings or []), errors=list(errors or []))


GOOD = _series(points=[_point(1, 1000, 900, 1120), _point(6, 1080, 820, 1390)],
               accuracy=_accuracy(), break_detected=True)
UNFORECASTABLE = _series(market_id="M_INDORE", status="INSUFFICIENT_HISTORY",
                         pattern=None, engine="", n_history=2, points=[],
                         reason="only 2 periods of history")


def _explain(result, horizon=6):
    payload = forecast_reasoning_payload(result, horizon=horizon)
    return payload, ReasoningAgent().reason(
        payload, scope=ReasoningScope.FORECAST, allow_llm=False)


class TestTheScopeExists:

    def test_forecast_is_a_reasoning_scope(self):
        assert ReasoningScope.FORECAST.value == "FORECAST"


class TestThePackSelectsAndDoesNotDerive:

    def test_it_carries_the_engines_own_band(self):
        payload = forecast_reasoning_payload(_result([GOOD]), horizon=6)
        series = payload["forecast_series"][0]
        assert series["last_period_p10"] == 820
        assert series["last_period_mean"] == 1080
        assert series["last_period_p90"] == 1390
        assert series["first_period_p10"] == 900

    def test_it_carries_the_measured_error_not_a_score(self):
        payload = forecast_reasoning_payload(_result([GOOD]), horizon=6)
        accuracy = payload["forecast_series"][0]["accuracy"]
        assert accuracy["mase"] == 0.72
        assert accuracy["n_folds"] == 4
        # There is deliberately no invented quality signal anywhere.
        assert "confidence_score" not in accuracy
        assert "score" not in accuracy

    def test_an_unforecastable_series_is_reported_and_carries_no_numbers(self):
        payload = forecast_reasoning_payload(_result([GOOD, UNFORECASTABLE]), horizon=6)
        assert payload["forecast"]["n_series_requested"] == 2
        assert payload["forecast"]["n_series_forecast"] == 1
        assert payload["forecast"]["n_series_unavailable"] == 1

        failed = next(s for s in payload["forecast_series"]
                      if s["market_id"] == "M_INDORE")
        assert failed["status"] == "INSUFFICIENT_HISTORY"
        assert failed["reason"] == "only 2 periods of history"
        assert "last_period_mean" not in failed, (
            "a series that did not forecast must carry no quantity at all")

    def test_a_break_that_was_looked_for_and_not_found_is_still_reported(self):
        no_break = _series(points=[_point(1, 500, 400, 620)], break_detected=False)
        payload = forecast_reasoning_payload(_result([no_break]), horizon=1)
        assert payload["forecast_series"][0]["structural_break"]["detected"] is False
        assert payload["forecast"]["n_structural_breaks_detected"] == 0

    def test_counts_are_taken_over_every_series_not_only_the_cited_ones(self):
        """`max_series` bounds what can be cited, never what is reported."""
        many = [_series(market_id=f"M{i}", points=[_point(1, 100, 80, 130)])
                for i in range(30)]
        payload = forecast_reasoning_payload(_result(many), horizon=1, max_series=5)
        assert len(payload["forecast_series"]) == 5
        assert payload["forecast"]["n_series_requested"] == 30
        assert payload["forecast"]["n_series_forecast"] == 30

    def test_the_pack_carries_no_causal_field(self):
        payload = forecast_reasoning_payload(_result([GOOD]), horizon=6)
        flat = str(payload).lower()
        for causal in ("cause", "driver", "because", "reason_for", "explanation"):
            assert causal not in flat, (
                f"the forecast pack carries {causal!r}; nothing in "
                f"netgravity/forecasting/ computes why demand moves")


class TestTheNarration:

    def test_it_says_what_was_forecast_and_what_was_not(self):
        _, out = _explain(_result([GOOD, UNFORECASTABLE]))
        themes = {i.theme for i in out.briefing.kpi_insights}
        assert "Forecast coverage" in themes

        coverage = next(i for i in out.briefing.kpi_insights
                        if i.theme == "Forecast coverage")
        assert "1 of the 2" in coverage.narrative
        assert "no numbers at all" in coverage.narrative

    def test_it_states_a_range_rather_than_a_single_number(self):
        _, out = _explain(_result([GOOD]))
        confidence = next(i for i in out.briefing.kpi_insights
                          if i.theme == "Forecast confidence")
        assert "820" in confidence.narrative and "1,390" in confidence.narrative
        assert "uncertainty" in confidence.narrative

    def test_it_reports_measured_error_and_says_when_there_is_none(self):
        _, out = _explain(_result([GOOD]))
        accuracy = next(i for i in out.briefing.kpi_insights
                        if i.theme == "Forecast accuracy")
        assert "MASE 0.72" in accuracy.narrative
        assert "better than simply repeating last period" in accuracy.narrative

        unmeasured = _series(points=[_point(1, 500, 400, 620)], accuracy=None)
        _, out = _explain(_result([unmeasured]))
        assert "Forecast accuracy" not in {i.theme for i in out.briefing.kpi_insights}
        assert "unmeasured" in out.recommendation

    def test_a_worse_than_naive_forecast_is_said_to_be_worse(self):
        poor = _series(points=[_point(1, 500, 400, 620)], accuracy=_accuracy(mase=1.6))
        _, out = _explain(_result([poor]))
        accuracy = next(i for i in out.briefing.kpi_insights
                        if i.theme == "Forecast accuracy")
        assert "no better than simply repeating last period" in accuracy.narrative

    def test_a_structural_break_is_described_never_explained(self):
        _, out = _explain(_result([GOOD]))
        brk = next(i for i in out.briefing.kpi_insights
                   if i.theme == "Structural break")
        assert "level" in brk.narrative
        assert "What caused the shift is not something I can see" in brk.narrative

    def test_no_insight_claims_a_cause_for_demand(self):
        _, out = _explain(_result([GOOD, UNFORECASTABLE]))
        text = out.briefing.visible_text().lower()
        for causal in ("because demand", "driven by", "due to rising",
                       "caused by", "as a result of growth"):
            assert causal not in text, f"the narration explains a cause: {causal!r}"

    def test_the_recommendation_names_a_next_step_not_a_result(self):
        _, out = _explain(_result([GOOD]))
        assert "p90" in out.recommendation
        assert "I have not run" in out.recommendation

    def test_no_history_means_no_forecast_and_it_says_so(self):
        _, out = _explain(_result([UNFORECASTABLE], status="INSUFFICIENT_HISTORY"))
        assert "supplying more demand history" in out.recommendation
        assert out.briefing.kpi_insights, "an empty forecast still deserves an answer"

    def test_an_empty_result_narrates_nothing_rather_than_inventing(self):
        _, out = _explain(_result([]))
        themes = {i.theme for i in out.briefing.kpi_insights}
        assert "Forecast coverage" not in themes


class TestEveryFigureGrounds:

    def test_the_forecast_facts_are_registered(self):
        for key in ("last_period_p10", "last_period_p90", "last_period_mean",
                    "mase", "wape", "n_series_forecast",
                    "n_structural_breaks_detected"):
            assert key in _FACT_SPEC, (
                f"{key} is cited by the forecast narration but is not an "
                f"authoritative fact, so a correct claim would be UNSUPPORTED")
        assert _FACT_SPEC["mase"][1] == "forecast_engine"

    def test_a_full_forecast_briefing_grounds_cleanly(self):
        _, out = _explain(_result([GOOD, UNFORECASTABLE]))
        assert out.validation_warnings == [], (
            f"real forecast figures failed grounding: {out.validation_warnings}")

    def test_the_evidence_pack_indexes_the_forecast_metrics(self):
        payload = forecast_reasoning_payload(_result([GOOD]), horizon=6)
        pack = build_evidence_pack(payload, scope=ReasoningScope.FORECAST)
        refs = [r for r in pack.metrics if r.endswith(".last_period_p90")]
        assert refs, "the p90 band is not addressable as evidence"
        assert pack.scope is ReasoningScope.FORECAST

    def test_it_never_raises_on_a_malformed_result(self):
        """Reasoning is advisory; it must not take down a good forecast."""
        payload = forecast_reasoning_payload(None)
        assert payload["forecast"]["n_series_requested"] == 0
        out = ReasoningAgent().reason(payload, scope=ReasoningScope.FORECAST,
                                      allow_llm=False)
        assert out is not None


class TestTheApiExposesIt:

    def test_the_forecast_endpoint_builds_one_overall_explanation(self, tmp_path,
                                                                  monkeypatch):
        """
        ONE explanation of the run, not sixty. The engine forecasts every
        market-product pair — 60 on the US dataset — and a response carrying
        sixty paragraphs is large, slow and mostly unread. The per-series
        words are deterministic and travel with each series instead.
        """
        from app.backend.api.forecast import _forecast_explanation

        monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
        for zone in ("raw", "standardized", "curated"):
            (tmp_path / zone).mkdir(parents=True, exist_ok=True)

        explanation = _forecast_explanation(
            _result([GOOD, UNFORECASTABLE]), 6, "proj1", "exec_1")
        assert explanation["scope"] == "FORECAST"
        assert explanation["insights"]
        assert explanation["recommendation"]
        assert explanation["grounding"]["warnings"] == []

    def test_reopening_the_tab_does_not_re_explain(self, tmp_path, monkeypatch):
        """Viewing the same forecast run is a view, not a new analysis."""
        from app.backend.api.forecast import _forecast_explanation
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.storage import get_storage
        from netgravity.orchestrator.explanations import (
            KIND_FORECAST,
            ExplanationStore,
            fingerprint,
        )

        monkeypatch.setenv("NETGRAVITY_DATA_ROOT", str(tmp_path))
        for zone in ("raw", "standardized", "curated"):
            (tmp_path / zone).mkdir(parents=True, exist_ok=True)

        first = _forecast_explanation(_result([GOOD]), 6, "proj1", "exec_1")
        again = _forecast_explanation(_result([GOOD]), 6, "proj1", "exec_1")
        # Same words; the second says it came from the store.
        assert again["card"]["headline"] == first["card"]["headline"]
        assert first["cached"] is False and again["cached"] is True

        saved = ExplanationStore(get_storage(IngestionConfig())).get(
            "proj1", KIND_FORECAST, fingerprint("exec_1", 6))
        assert saved is not None, "the explanation was not saved against the run"
        # A re-forecast is a different run, and a different analysis.
        assert ExplanationStore(get_storage(IngestionConfig())).get(
            "proj1", KIND_FORECAST, fingerprint("exec_2", 6)) is None

    def test_a_failure_to_explain_does_not_fail_the_forecast(self):
        from app.backend.api.forecast import _forecast_explanation

        class Exploding:
            @property
            def series(self):
                raise RuntimeError("boom")

        assert _forecast_explanation(Exploding(), 6, "proj1", "exec_1") == {}


class TestTheSelectedSeriesHasItsOwnWords:
    """
    Switching from Delhi's forecast to Mumbai's must change the words with
    the chart. These are deterministic and per-series, so it costs nothing.
    """

    def _explain(self, **overrides):
        from app.backend.api.forecast import _series_explanation

        row = {
            "market_id": "M_MUMBAI", "product_id": "P1", "status": "OK",
            "engine": "ETS", "n_history_periods": 24,
            "points": [{"period": 1, "mean": 1000, "p10": 900, "p90": 1120},
                       {"period": 6, "mean": 1180, "p10": 820, "p90": 1520}],
            "accuracy": {"mase": 0.72, "n_folds": 4},
        }
        row.update(overrides)
        return _series_explanation(row)

    def test_it_names_the_series_it_is_about(self):
        assert "M_MUMBAI" in self._explain()["headline"]

    def test_it_states_direction_range_and_measured_accuracy(self):
        got = self._explain()
        # Plain business English in the headline; the algorithm and the
        # backtest score belong in the collapsed detail, not the main line.
        assert "rise" in got["headline"]
        assert "820" in got["narrative"] and "1,520" in got["narrative"]
        assert "ETS" not in got["headline"] and "MASE" not in got["headline"]
        joined = " ".join(got["details"])
        assert "MASE 0.72" in joined
        assert "better than repeating last period" in joined
        # And a word for what the score means, beside the score.
        accuracy = next(f for f in got["figures"] if f["label"] == "Forecast accuracy")
        assert accuracy["value"] == "Good"
        assert "MASE 0.72" in accuracy["note"]

    def test_a_falling_series_is_not_called_rising(self):
        got = self._explain(points=[{"period": 1, "mean": 1200, "p10": 1100, "p90": 1300},
                                    {"period": 6, "mean": 800, "p10": 600, "p90": 1000}])
        assert "fall" in got["headline"] and "rise" not in got["headline"]

    def test_a_flat_series_is_not_given_a_trend(self):
        """A 2% move is rounding, not a direction."""
        got = self._explain(points=[{"period": 1, "mean": 1000, "p10": 900, "p90": 1100},
                                    {"period": 6, "mean": 1005, "p10": 900, "p90": 1100}])
        assert "hold steady" in got["headline"]
        assert "rise" not in got["headline"] and "fall" not in got["headline"]

    def test_the_size_of_the_move_is_qualified(self):
        slight = self._explain(points=[{"period": 1, "mean": 1000, "p10": 900, "p90": 1100},
                                       {"period": 6, "mean": 950, "p10": 850, "p90": 1050}])
        sharp = self._explain(points=[{"period": 1, "mean": 1000, "p10": 900, "p90": 1100},
                                      {"period": 6, "mean": 600, "p10": 500, "p90": 700}])
        assert "slightly" in slight["headline"]
        assert "sharply" in sharp["headline"]

    def test_an_unmeasured_series_says_so(self):
        got = self._explain(accuracy=None)
        joined = " ".join(got["details"])
        assert "unmeasured" in joined
        assert "not the same as small" in joined
        assert not [f for f in got["figures"] if f["label"] == "Forecast accuracy"]

    def test_it_never_invents_a_cause(self):
        got = self._explain()
        text = f"{got['headline']} {got['narrative']} {' '.join(got['details'])}".lower()
        for invented in ("promotion", "economic", "seasonal demand rose",
                         "because", "driven by"):
            assert invented not in text

    def test_a_series_that_could_not_be_forecast_carries_no_numbers(self):
        got = self._explain(status="INSUFFICIENT_HISTORY", points=[],
                            reason="only 2 periods of history")
        assert "No forecast" in got["headline"]
        assert got["figures"] == []
        assert "only 2 periods of history" in got["narrative"]

    def test_at_most_three_figures(self):
        assert len(self._explain()["figures"]) <= 3

    def test_the_endpoint_attaches_it_to_every_series(self):
        import pathlib

        from app.backend.app import app

        source = (pathlib.Path(app.root_path) / "api"
                  / "forecast.py").read_text(encoding="utf-8")
        assert 'row["explanation"] = _series_explanation(row)' in source

    def test_the_explanation_reaches_a_model_only_when_the_switch_says_so(self):
        """
        Not hardcoded off any more — wired to the one switch every
        explanation flow reads, which is off by default.
        """
        import pathlib

        from app.backend.app import app

        source = (pathlib.Path(app.root_path) / "api" / "forecast.py").read_text(
            encoding="utf-8")
        block = source.split("def _forecast_explanation")[1].split("\ndef ")[0]
        assert "allow_llm=explanations_llm_enabled()" in block
        assert "ReasoningScope.FORECAST" in block

    def test_the_credential_is_the_switch(self, monkeypatch):
        """
        `TEXT_API_TOKEN` blank means templates; set means the model. There is
        no separate flag, because two settings answering one question
        eventually disagree.
        """
        from netgravity.orchestrator.explanation_llm import explanations_llm_enabled

        monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
        monkeypatch.delenv("NETGRAVITY_DISABLE_LLM", raising=False)
        assert explanations_llm_enabled() is False

        monkeypatch.setenv("TEXT_API_TOKEN", "test-token")
        assert explanations_llm_enabled() is True
