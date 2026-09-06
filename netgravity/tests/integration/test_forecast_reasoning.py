"""
The forecast screen showed the Overview's finding, because the forecast run
reasoned about nothing.

`_build_forecast` has always ended with `*_reason_and_govern(["forecast"])`, so
every forecast request paid for a reasoning pass. But `registry.synthesise`
assembles its payload from `optimization.solve*`, `kpi.summarise`,
`resilience.assess`, risk, external evidence and per-facility detail — and from
nothing else. The forecast's own output was never in it.

So the agent was handed an empty pack, produced a NETWORK briefing, and
`/api/forecast` returned none of it anyway. The Forecast screen, having nothing
of its own, drew Home's "Needs your attention" card into a second container —
and the markup carried a comment explaining that this was deliberate because
"the reasoning agent has no FORECAST scope". That was a consequence of the gap,
not a fact about forecasting.

Reported as: "what needs your attention card is repeated, which does not make
any sense."

The second half is the signals chip. It read a CONSTANT — `intendedUse`, set to
"not yet routed into a forecast" for every signal by `loadStructure` — and
regex-matched it to choose between three labels. Uploaded signals have been
routed since `_uploaded_signals_for` landed; "Not yet applied" was simply the
only answer the chip could give.
"""

from __future__ import annotations

import pathlib

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.schemas.reasoning import ReasoningScope

JS = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend" / "js"


def _js(*parts: str) -> str:
    return JS.joinpath(*parts).read_text(encoding="utf-8")


def _code(text: str) -> str:
    """
    The source with its comments removed.

    Every comment written for this change quotes the string it replaced — "not
    yet routed into a forecast", "Not yet applied" — because that is what makes
    the comment worth reading. It is also what makes a substring check on the
    raw file pass when the code still contains none of it, and fail when the
    code is correct. The suite's other screen tests strip comments for the same
    reason.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") \
                or stripped.startswith("/*"):
            continue
        out.append(line.split("  //")[0] if "  //" in line else line)
    return "\n".join(out)


@pytest.fixture(scope="module")
def app_js() -> str:
    return _js("app.js")


def _outlook(**over):
    base = {
        "horizon": 6,
        "n_series_forecast": 12,
        "n_series_total": 14,
        "total_forecast_units": 250000.0,
        "comparable_recent_units": 200000.0,
        "growth_pct": 25.0,
        "fastest_growing": [{
            "market_id": "M_TORONTO", "product_id": "P_BEV",
            "forecast_units": 40000.0, "recent_units": 25000.0,
            "growth_pct": 60.0, "n_history_periods": 18,
        }],
        "shrinking": [],
        "signal_adjustments": [],
        "n_signal_adjustments": 0,
        "structural_breaks": [],
        "n_structural_breaks": 0,
    }
    base.update(over)
    return base


def _reason(outlook):
    return ReasoningAgent(None, runtime=None).reason(
        {"forecast": outlook}, scope=ReasoningScope.FORECAST)


# ---------------------------------------------------------------------------
# A forecast run reasons about the forecast
# ---------------------------------------------------------------------------

class TestTheBriefingIsAboutTheProjection:

    def test_the_scope_exists_at_all(self):
        assert ReasoningScope.FORECAST.value == "FORECAST"

    def test_the_demand_outlook_leads(self):
        """
        `card_from_briefing` leads with `kpi_insights[0]`. On a demand
        projection the reader came for the projection.
        """
        briefing = _reason(_outlook()).briefing
        assert briefing.scope is ReasoningScope.FORECAST
        assert briefing.kpi_insights[0].theme == "Demand outlook"

    def test_every_figure_is_grounded(self):
        """
        The first version of this briefing came back as "I see [UNSUPPORTED
        FIGURE REMOVED] of demand over the next 6 periods" — every number
        authoritative, none of it in `_FACT_SPEC`, so the validator refused
        all of it. It was right to; nothing had told it these keys exist.
        """
        result = _reason(_outlook())
        assert result.grounding_status == "GROUNDED"
        text = result.briefing.visible_text()
        assert "UNSUPPORTED" not in text
        assert "250,000" in text
        assert "25.0%" in text

    def test_growth_is_never_asserted_without_a_window_to_measure_it(self):
        """
        `growth_pct` is None — not 0.0 — when no comparable observed window
        exists. Zero would read as "demand is flat", which is a claim nobody
        made.
        """
        briefing = _reason(_outlook(growth_pct=None,
                                    comparable_recent_units=None)).briefing
        lead = briefing.kpi_insights[0]
        assert "no comparable observed window" in lead.narrative.lower()
        assert "%" not in lead.narrative

    def test_where_the_growth_is_concentrated_is_named(self):
        themes = [i.theme for i in _reason(_outlook()).briefing.kpi_insights]
        assert "Where the growth is" in themes
        text = _reason(_outlook()).briefing.visible_text()
        assert "M_TORONTO" in text

    def test_a_structural_break_is_reported_as_a_risk(self):
        briefing = _reason(_outlook(n_structural_breaks=2)).briefing
        found = [i for i in briefing.kpi_insights
                 if i.theme == "History that changed"]
        assert found and found[0].severity.value == "RISK"

    def test_signal_adjustments_are_reported_as_assumptions(self):
        briefing = _reason(_outlook(n_signal_adjustments=3)).briefing
        found = [i for i in briefing.kpi_insights if i.theme == "External signals"]
        assert found
        assert "assumption" in found[0].narrative.lower()

    def test_a_run_with_no_forecast_says_nothing_about_one(self):
        briefing = ReasoningAgent(None, runtime=None).reason(
            {"network_state": {"business_network_cost": 1000.0}}).briefing
        themes = [i.theme for i in briefing.kpi_insights]
        assert "Demand outlook" not in themes


class TestTheRecommendationIsSomethingThisApplicationCanDo:

    def test_it_names_the_scenario_and_the_rate(self):
        """
        "Monitor demand" is what a briefing says when it has nothing to
        suggest. The rate to test at is a figure the forecaster already
        produced.
        """
        rec = _reason(_outlook()).briefing.recommendation
        assert "demand scenario" in rec
        assert "+25.0%" in rec
        assert "M_TORONTO" in rec        # and where to scope it

    def test_falling_demand_gets_a_different_step(self):
        rec = _reason(_outlook(growth_pct=-12.0, fastest_growing=[])).briefing.recommendation
        assert "lower volume" in rec
        assert "fixed cost" in rec

    def test_no_measurable_growth_recommends_no_rate(self):
        rec = _reason(_outlook(growth_pct=None)).briefing.recommendation
        assert "%" not in rec


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------

class TestTheForecastScreenShowsItsOwnFinding:

    def test_the_endpoint_returns_what_the_reasoning_produced(self):
        api = (pathlib.Path(__file__).resolve().parents[3] / "app" / "backend"
               / "api" / "forecast.py").read_text(encoding="utf-8")
        assert "def _forecast_explanation(ctx" in api
        assert '"explanation": explanation,' in api
        assert '"outlook": outlook,' in api

    def test_the_outlook_is_computed_once_for_both_readers(self):
        """
        The briefing and the screen must not arrive at different numbers for
        the same question, so the outlook is computed by the capability and
        read from its output by both.
        """
        reg = (pathlib.Path(__file__).resolve().parents[2] / "orchestrator"
               / "registry.py").read_text(encoding="utf-8")
        assert 'flattened["outlook"] = _forecast_outlook(result, matched)' in reg
        assert 'forecast_out = ctx.output_of("forecast.demand") or {}' in reg
        assert 'payload["forecast"] = forecast_out["outlook"]' in reg

    def test_the_outlook_forecasts_nothing(self):
        """
        §5. It sums points the forecaster produced and history it was given.
        A second forecasting path here would be a second engine.
        """
        reg = (pathlib.Path(__file__).resolve().parents[2] / "orchestrator"
               / "registry.py").read_text(encoding="utf-8")
        block = reg[reg.index("def _forecast_outlook("):]
        block = block[:block.index("\n    # ---- forecast.demand")]
        for forbidden in ("ForecastRequest", "svc[\"forecasting\"]", "predict"):
            assert forbidden not in block, forbidden


class TestTheSignalChipReportsWhatTheRouterDid:

    def test_it_no_longer_reads_a_hardcoded_string(self, app_js):
        block = _code(app_js[app_js.index("function renderHomeSignals("):])
        block = block[:block.index("\n}\n")]
        assert "sig.intendedUse" not in block
        assert "applied_signal_ids" in block

    def test_it_tells_apart_refused_from_not_yet_run(self, app_js):
        """
        Two very different causes, and a reader can act on only one of them.
        The old chip collapsed them into one label.
        """
        block = _code(app_js[app_js.index("function renderHomeSignals("):])
        block = block[:block.index("\n}\n")]
        assert "Did not change the forecast" in block
        assert "No forecast run yet" in block
        assert "Not yet applied" not in block

    def test_the_hydration_makes_no_claim_about_routing(self):
        hydrate = _js("integration", "hydrate.js")
        assert "not yet routed into a forecast" not in _code(hydrate)
        assert "Supplied with your upload as market intelligence" in hydrate

    def test_the_endpoint_reports_which_signal_moved_something(self):
        api = (pathlib.Path(__file__).resolve().parents[3] / "app" / "backend"
               / "api" / "forecast.py").read_text(encoding="utf-8")
        assert '"applied_signal_ids": applied_signal_ids,' in api
        block = api[api.index("applied_signal_ids = sorted({"):]
        block = block[:block.index("})")]
        assert "signal_adjustments" in block
