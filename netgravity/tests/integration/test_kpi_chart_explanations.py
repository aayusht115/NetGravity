"""
EXPLAIN THIS CHART — four buttons, not fourteen, and no number the model wrote.

The KPI screen carries one short briefing per visual that takes more than a
glance to read. Three things have to hold, and each is a way the feature would
otherwise do harm rather than nothing:

  * WHAT IT IS ABOUT. A briefing is about the rows that were on screen when it
    was asked for. Built over the whole network while the reader looks at three
    southern sites, it is a confident paragraph about the wrong thing — worse
    than no paragraph.
  * WHAT IT COSTS. One request per chart per analysis, spent only when someone
    presses the button. Re-opening spends nothing; a re-solve invalidates.
  * WHO WROTE THE NUMBERS. Code. `card.py` states the rule and this holds it
    for the four chart payloads: the model writes sentences, and every figure
    comes from the backend's own solved rows.

The fourth thing is the one that keeps the button meaningful: the ranked lists
and the share-of-total donuts do NOT get one, because a sorted list with its
values printed on it already says what it says.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace as NS

import pytest

from netgravity.orchestrator.reasoning.card import FORMAT_CURRENCY
from netgravity.orchestrator.reasoning.kpi_chart_evidence import (
    CHART_CAPACITY_CARRIED,
    CHART_PEAK_VS_AVERAGE,
    CHART_STOCK_HELD,
    CHART_THROUGHPUT_HORIZON,
    CHARTS,
    NETWORK_CHARTS,
    chart_payload,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
JS = REPO_ROOT / "app" / "frontend" / "js"


def _asset(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def _row(**kw):
    """
    A solved facility row, with every field the builders read.

    That includes `throughput_by_period`. A row reporting twelve periods
    observed and carrying no series for them is not a row any solve produces,
    and the throughput builder now reads the series rather than inferring a
    horizon from the scalars derived from it.
    """
    base = dict(
        facility_id="F", facility_name="A Site", role="DC", region="North",
        is_open=True, health_band="HEALTHY",
        rated_capacity_per_period=1000.0, avg_throughput_units=500.0,
        peak_throughput_units=600.0, avg_utilization_pct=50.0,
        peak_utilization_pct=60.0, peak_period="Jan-26",
        bottleneck_periods_count=0, periods_observed=12,
        avg_inventory_units=100.0, peak_inventory_units=120.0,
        total_facility_cost=1000.0,
        throughput_by_period={str(i): 500.0 for i in range(1, 12)} | {"12": 600.0},
    )
    base.update(kw)
    return NS(**base)


class TestEachChartLeadsWithItsOwnFinding:
    """
    One generic "explain these numbers" payload produces four briefings that
    all read the same, because the model is left to decide what the point is —
    and the point is different for every chart. Each builder decides its own
    lead, in code, and hands it over already made.
    """

    def test_peak_vs_average_leads_with_what_the_average_hides(self):
        rows = [
            # Fine on average, over the line in its peak: the whole reason
            # this chart draws two bars rather than one.
            _row(facility_id="A", facility_name="Delhi",
                 avg_utilization_pct=88.0, peak_utilization_pct=96.0),
            _row(facility_id="B", facility_name="Mumbai",
                 avg_utilization_pct=40.0, peak_utilization_pct=45.0),
        ]
        payload, _ = chart_payload(CHART_PEAK_VS_AVERAGE, rows, threshold_pct=90.0)
        lead = payload["utilisation_peak_vs_average"]
        assert lead["n_below_threshold_on_average_but_not_in_peak"] == 1
        assert lead["hidden_by_the_average"] == ["Delhi"]
        assert lead["widest_peak_above_average"]["facility"] == "Delhi"

    def test_peak_vs_average_separates_a_seasonal_peak_from_a_permanent_one(self):
        """Tight in 12 of 12 periods is not a peak at all. A briefing that
        called it one would send a planner looking for a season."""
        rows = [_row(facility_id="C", facility_name="Chicago",
                     avg_utilization_pct=99.6, peak_utilization_pct=100.0,
                     bottleneck_periods_count=12, periods_observed=12)]
        payload, _ = chart_payload(CHART_PEAK_VS_AVERAGE, rows, threshold_pct=90.0)
        lead = payload["utilisation_peak_vs_average"]
        assert lead["n_tight_in_every_period"] == 1
        assert lead["tight_in_every_period"] == ["Chicago"]

    def test_capacity_carried_leads_with_the_ranking_inversion(self):
        """
        The reason this chart exists. 95% of 200 is ten units spare; 80% of
        40,000 is eight thousand. The per-cent chart above it ranks those the
        wrong way round for "where can the next volume actually go".
        """
        rows = [
            _row(facility_id="BIG", facility_name="Delhi",
                 rated_capacity_per_period=18000.0, peak_throughput_units=17280.0,
                 peak_utilization_pct=96.0),
            _row(facility_id="SMALL", facility_name="Seattle",
                 rated_capacity_per_period=200.0, peak_throughput_units=40.0,
                 peak_utilization_pct=19.9),
        ]
        payload, _ = chart_payload(CHART_CAPACITY_CARRIED, rows)
        lead = payload["capacity_headroom"]
        assert lead["unit_and_percent_rankings_disagree"] is True
        # Most ROOM is the busy site; emptiest by per cent is the small one.
        assert lead["most_headroom_in_units"]["facility"] == "Delhi"
        assert lead["emptiest_by_percent"]["facility"] == "Seattle"

    def test_stock_held_leads_with_the_gap_between_the_two_bars(self):
        """A seasonal build and a flat buffer are different operations, and an
        average reports them identically."""
        rows = [
            _row(facility_id="S", facility_name="Seasonal",
                 avg_inventory_units=1000.0, peak_inventory_units=2500.0),
            _row(facility_id="F", facility_name="Flat",
                 avg_inventory_units=1000.0, peak_inventory_units=1050.0),
        ]
        payload, _ = chart_payload(CHART_STOCK_HELD, rows)
        lead = payload["stock_profile"]
        assert lead["building_for_a_season"] == ["Seasonal"]
        assert lead["holding_a_flat_buffer"] == ["Flat"]

    def test_throughput_horizon_leads_with_room_in_the_peak_period(self):
        rows = [_row(facility_id="X", facility_name="Atlanta",
                     rated_capacity_per_period=32000.0,
                     peak_throughput_units=11456.0)]
        payload, _ = chart_payload(CHART_THROUGHPUT_HORIZON, rows, facility_id="X")
        lead = payload["facility_throughput"]
        assert lead["facility"] == "Atlanta"
        assert lead["headroom_in_peak_period_units"] == pytest.approx(20544.0)

    def test_the_four_leads_are_four_different_shapes(self):
        """If two charts produced the same payload key, they would produce the
        same briefing, and the button would be decoration on one of them."""
        rows = [_row(facility_id="A"), _row(facility_id="B", facility_name="B")]
        keys = set()
        for chart in NETWORK_CHARTS:
            payload, _ = chart_payload(chart, rows)
            keys.add(tuple(sorted(payload)))
        assert len(keys) == len(NETWORK_CHARTS)


class TestCodeWritesEveryNumber:

    def test_every_chart_hands_over_its_own_figures(self):
        rows = [_row(facility_id="A", avg_utilization_pct=88.0,
                     peak_utilization_pct=96.0)]
        for chart in CHARTS:
            _, figures = chart_payload(
                chart, rows, threshold_pct=90.0, facility_id="A")
            assert figures, chart
            # A card holds at most three. A fourth is a table.
            assert len(figures) <= 3, chart
            for figure in figures:
                assert figure.label
                # Either a rendered value or a money amount — never an empty
                # figure, which renders as a labelled blank.
                assert figure.value or figure.format == FORMAT_CURRENCY

    def test_an_absent_reading_is_never_a_zero(self):
        """A site with no stock reading is not a site holding none."""
        rows = [_row(facility_id="A", avg_inventory_units=None,
                     peak_inventory_units=None)]
        payload, figures = chart_payload(CHART_STOCK_HELD, rows)
        # Nothing to explain, rather than a briefing about zero stock.
        assert payload == {}
        assert figures == []

    def test_a_missing_utilisation_does_not_become_a_reading(self):
        rows = [_row(facility_id="A", avg_utilization_pct=None,
                     peak_utilization_pct=None)]
        payload, _ = chart_payload(CHART_PEAK_VS_AVERAGE, rows, threshold_pct=90.0)
        site = payload["utilisation_sites"][0]
        assert site["avg_utilization_pct"] is None
        assert site["peak_above_average_pts"] is None

    def test_a_chart_with_nothing_on_it_asks_for_nothing(self):
        """Spending a model request to be told the chart is empty is the waste
        this whole path exists to avoid."""
        assert chart_payload(CHART_PEAK_VS_AVERAGE, []) == ({}, [])
        assert chart_payload(CHART_THROUGHPUT_HORIZON, [_row()],
                             facility_id="not-in-this-network") == ({}, [])

    def test_a_facility_with_no_series_is_a_chart_with_nothing_on_it(self):
        """
        THE CHART IS THE SERIES.

        Every scalar this builder used to read — the peak, the average, the
        count of tight periods — survives on a row whose per-period series is
        empty, which is exactly what a single-period solve produces BY DESIGN.
        So the card drew "this solve reported no per-period throughput for
        this site" and the button beside it returned a confident paragraph
        about the busiest period of a horizon nobody could see. Right figures,
        no subject.
        """
        for series in ({}, {"1": 500.0}):
            payload, figures = chart_payload(
                CHART_THROUGHPUT_HORIZON,
                [_row(facility_id="F", throughput_by_period=series)],
                facility_id="F")
            assert payload == {}, series
            assert figures == [], series

    def test_a_facility_with_a_series_is_still_explained(self):
        """The guard above must not silence the chart it was written for."""
        payload, figures = chart_payload(
            CHART_THROUGHPUT_HORIZON, [_row(facility_id="F")], facility_id="F")
        assert payload["kpi_chart"]["finding"]
        assert figures


class TestTheEndpoint:

    def _source(self) -> str:
        return (REPO_ROOT / "app" / "backend" / "api" / "kpis.py").read_text(
            encoding="utf-8")

    def test_the_rows_come_from_the_projects_own_analysis(self):
        """
        The client says WHICH rows were on screen; it does not supply them.
        An id that is not in this project's solved analysis is dropped, so a
        crafted body cannot put a facility into a briefing.
        """
        src = self._source()
        block = src[src.index("def explain_kpi_chart()"):]
        block = block[:block.index("@bp.route(\"/evidence\"")]
        assert "report.health_kpis" in block
        assert "k.facility_id in order" in block
        # The analysis is fetched through the same project-scoped path as
        # every other KPI route, so it cannot read another project's network.
        assert "_scoped_analysis()" in block

    def test_the_fingerprint_carries_the_rows_that_were_drawn(self):
        """
        Same chart, different filter, different question. A fingerprint on the
        execution alone would serve a briefing about nine sites to a reader
        looking at three.
        """
        src = self._source()
        block = src[src.index("def explain_kpi_chart()"):]
        block = block[:block.index("@bp.route(\"/evidence\"")]
        parts = block[block.index("result_parts=["):]
        parts = parts[:parts.index("]")]
        for carried in ("execution_id", "data_version", "chart", "drawn"):
            assert carried in parts, carried

    def test_each_chart_is_its_own_record(self):
        """Four charts of one analysis are four questions. Answering one must
        not oblige the reader to pay for the other three."""
        src = self._source()
        assert 'kind=f"{KIND_KPI_CHART}:{chart}"' in src

    def test_an_unknown_chart_is_refused(self):
        src = self._source()
        block = src[src.index("def explain_kpi_chart()"):]
        assert "if chart not in CHARTS" in block
        assert "ValidationError" in block

    def test_an_explanation_can_never_take_down_the_screen(self):
        """The chart is perfectly readable without a briefing."""
        src = self._source()
        block = src[src.index("def _chart_explanation("):]
        block = block[:block.index("\ndef create_kpi_blueprint")]
        assert "except Exception" in block
        assert "return {}" in block

    def test_it_uses_the_shared_reasoning_connection(self):
        """A bare `ReasoningAgent()` has no gateway and returns templates
        however the credential is set."""
        src = self._source()
        assert "explanation_reasoning_agent()" in src
        assert "explanations_llm_enabled()" in src


class TestTheButtonMeansSomething:

    def test_only_four_charts_carry_one(self):
        js = _asset("kpi-explain.js")
        block = js[js.index("const EXPLAINABLE = ["):]
        block = block[:block.index("];")]
        for chart in ("peak_vs_average", "capacity_carried", "stock_held",
                      "throughput_horizon"):
            assert chart in block, chart
        assert block.count("chart:") == 4

    def test_the_ranked_lists_and_donuts_do_not(self):
        """
        A sorted list with its values printed on it already says what it says.
        A button on every card teaches a reader that the button means nothing.
        """
        js = _asset("kpi-explain.js")
        block = js[js.index("const EXPLAINABLE = ["):]
        block = block[:block.index("];")]
        for self_explanatory in ("chart-wh-spend", "chart-wh-mix",
                                 "chart-dash-costs", "chart-dash-lanes",
                                 "table-wh-health", "table-kpi-lanes"):
            assert self_explanatory not in block, self_explanatory

    def test_nothing_is_requested_until_someone_presses_it(self):
        js = _asset("kpi-explain.js")
        # The one call site sits inside the click handler.
        assert js.count("kpiService.explainChart") == 1
        handler = js[js.index("async function onExplain("):]
        handler = handler[:handler.index("\n}")]
        assert "kpiService.explainChart" in handler

    def test_reopening_costs_nothing_at_all(self):
        """Not even a round trip. The backend keeps one record per chart per
        analysis; this keeps its own copy so re-opening is instant."""
        js = _asset("kpi-explain.js")
        handler = js[js.index("async function onExplain("):]
        handler = handler[:handler.index("\n}")]
        assert "answers.has(key)" in handler
        assert "answers.get(key)" in handler

    def test_a_filter_change_closes_an_open_panel(self):
        """
        A briefing describes the rows that were on screen when it was asked
        for. Left open while the filter narrows, it is a paragraph about nine
        sites sitting over a chart of three.
        """
        view = _asset("kpi-view.js")
        block = view[view.index("export function applyView()"):]
        block = block[:block.index("\n}")]
        assert "closeKpiExplainPanels()" in block

    def test_the_briefing_is_about_the_bars_the_chart_actually_drew(self):
        """
        NOT every row the filters left on screen. Each chart applies its own
        selection on top of the filters — the utilisation and headroom charts
        take OPEN sites only, stock takes the sites that report a level — so a
        site can sit in the table and be absent from the chart above it.

        Briefing over the visible rows described a proposed site the headroom
        chart had deliberately left out: a confident paragraph about a bar that
        is not there.
        """
        js = _asset("kpi-explain.js")
        block = js[js.index("function subjectOf(entry)"):]
        block = block[:block.index("\n}")]
        assert "warehouseDrawnIds(entry.chart)" in block
        # The looser list is not reachable from here any more.
        assert "warehouseFacets" not in js

    def test_each_chart_records_what_it_drew_as_it_draws_it(self):
        """Two copies of a filter would drift the first time either changed.
        The ids are captured at the moment of drawing instead."""
        js = _asset("warehouse.js")
        assert "export function warehouseDrawnIds(" in js
        for chart, renderer in (
            ("drawn.peak_vs_average =", "renderWarehouseUtilisationChart"),
            ("drawn.capacity_carried =", "renderWarehouseHeadroomChart"),
            ("drawn.stock_held =", "held.map"),
        ):
            assert chart in js, chart
        # Recorded from the SAME list handed to the chart, on the line above it.
        util = js[js.index("drawn.peak_vs_average ="):]
        assert "renderWarehouseUtilisationChart('chart-wh-utilisation', open," in util[:220]
        head = js[js.index("drawn.capacity_carried ="):]
        assert "renderWarehouseHeadroomChart('chart-wh-headroom', open," in head[:220]

    def test_the_facility_chart_records_what_it_drew_too(self):
        """
        The fourth chart, and the one this module does not draw.

        It is drawn from the facility drill-down, which is the only place that
        knows which site is on screen — so it records through the same
        registry rather than `subjectOf` assuming that a selected facility
        means a chart with something on it. It does not: the chart draws
        nothing when the solve carries no per-period series.
        """
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderFacilityThroughputChart"):]
        block = block[:block.index("\nexport function ", 1)]
        # Absent series and no canvas both report that nothing was drawn.
        assert block.count("return false;") == 2
        assert "return true;" in block

        warehouse = _asset("warehouse.js")
        assert "export function recordWarehouseDrawn(" in warehouse
        assert "throughput_horizon: []" in warehouse

        app = _asset("app.js")
        call = app[app.index("recordWarehouseDrawn('throughput_horizon'"):]
        assert "renderFacilityThroughputChart('chart-dash-throughput', whRow)" in call[:260]

    def test_the_facility_briefing_needs_a_drawn_chart(self):
        """The same test every other chart already applied."""
        js = _asset("kpi-explain.js")
        block = js[js.index("function subjectOf(entry)"):]
        block = block[:block.index("\n}")]
        assert "ids.includes(view.entityId)" in block

    def test_a_new_network_drops_the_saved_briefings(self):
        """They are about the network that produced them."""
        app_js = _asset("app.js")
        block = app_js[app_js.index("window.addEventListener('networkDataLoaded'"):]
        block = block[:block.index("\n});")]
        assert "clearKpiExplainCache()" in block

    def test_the_card_carries_no_separate_evidence_list(self):
        """
        The figures moved INTO the sentences, checked on the way (see
        `_FACT_SPEC`). A card that states a finding and then lists the same
        numbers underneath says everything twice, and the second copy is the
        one that looks like a spreadsheet.
        """
        js = _asset("kpi-explain.js")
        assert "figureRow" not in js
        # ...and with the list gone, so is the currency formatter it needed.
        assert "formatCurrency" not in js

    def test_an_empty_panel_is_never_shown(self):
        """An empty panel reads as a briefing that said nothing."""
        js = _asset("kpi-explain.js")
        assert "function noteMarkup(" in js
        handler = js[js.index("async function onExplain("):]
        handler = handler[:handler.index("\n}")]
        assert "noteMarkup(" in handler


DEMO_PROJECT = "pr-demo-case16"


@pytest.fixture()
def client():
    from app.backend.app import app as flask_app
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as test_client:
        yield test_client


@pytest.fixture()
def auth(client):
    import uuid
    email = f"kpi-explain-{uuid.uuid4().hex}@example.com"
    response = client.post("/api/auth/signup",
                           json={"email": email, "password": "explain-test-pw-1"})
    assert response.status_code == 201, response.get_json()
    return {"Authorization": f"Bearer {response.get_json()['token']}"}


class TestAgainstTheRealChain:
    """
    Against the bound demo project, so these exercise solve → stored analysis
    → route → card rather than a mock of it.
    """

    def _ask(self, client, auth, chart, **body):
        return client.post(
            f"/api/kpis/explain?project_id={DEMO_PROJECT}",
            json={"chart": chart, "project_id": DEMO_PROJECT, **body},
            headers=auth)

    def test_it_explains_a_network_chart(self, client, auth):
        response = self._ask(client, auth, CHART_PEAK_VS_AVERAGE)
        assert response.status_code == 200, response.get_json()
        body = response.get_json()
        assert body["chart"] == CHART_PEAK_VS_AVERAGE
        assert body["status"] in {"OK", "UNAVAILABLE", "NOTHING_TO_EXPLAIN"}
        if body["status"] == "OK":
            # Figures are code's, so they are present whatever the model did.
            assert body["card"]["figures"]

    def test_the_second_ask_spends_nothing(self, client, auth):
        """
        The rule: a new analysis can justify a new call; viewing the same
        analysis cannot. The store is what makes the second answer free, and
        it reports that it came from there.
        """
        first = self._ask(client, auth, CHART_PEAK_VS_AVERAGE).get_json()
        if first["status"] != "OK":
            pytest.skip("this network produced no briefing to re-read")
        second = self._ask(client, auth, CHART_PEAK_VS_AVERAGE).get_json()
        assert second["cached"] is True
        # Same answer, not merely a fast one.
        assert second["card"]["headline"] == first["card"]["headline"]

    def test_a_narrower_screen_is_a_different_question(self, client, auth):
        """
        Same chart, fewer sites. A briefing keyed on the analysis alone would
        hand the reader a paragraph about every site while they look at one.
        """
        wide = self._ask(client, auth, CHART_PEAK_VS_AVERAGE).get_json()
        if wide["status"] != "OK" or len(wide.get("facility_ids") or []) < 2:
            pytest.skip("this network has too few sites to narrow")
        one = wide["facility_ids"][:1]
        narrow = self._ask(client, auth, CHART_PEAK_VS_AVERAGE,
                           facility_ids=one).get_json()
        assert narrow["facility_ids"] == one
        # A fresh question, not the stored answer to the wider one.
        assert narrow["cached"] is False

    def test_an_id_from_another_network_is_dropped_not_trusted(self, client, auth):
        """The client says which rows were on screen; it does not supply them."""
        response = self._ask(client, auth, CHART_PEAK_VS_AVERAGE,
                             facility_ids=["NOT_A_REAL_FACILITY"])
        assert response.status_code == 200
        body = response.get_json()
        assert body["facility_ids"] == [] or "NOT_A_REAL_FACILITY" not in body["facility_ids"]
        assert body["status"] == "NOTHING_TO_EXPLAIN"

    def test_an_unknown_chart_is_refused(self, client, auth):
        response = self._ask(client, auth, "explain_everything")
        assert response.status_code == 400

    def test_it_requires_authentication(self, client):
        response = client.post(
            f"/api/kpis/explain?project_id={DEMO_PROJECT}",
            json={"chart": CHART_PEAK_VS_AVERAGE})
        assert response.status_code == 401

    def test_it_refuses_to_answer_without_a_project(self, client, auth):
        response = client.post("/api/kpis/explain",
                               json={"chart": CHART_PEAK_VS_AVERAGE},
                               headers=auth)
        assert response.status_code == 400


class TestTheCardIsUsefulWithNoModelAtAll:
    """
    THE DEFAULT STATE. `TEXT_API_TOKEN` blank means every explanation is
    written by the deterministic template — that is what the whole test suite
    runs as, and what any deployment without a credential ships as.

    The shared template writer produces prose only for the payload blocks it
    recognises, and it does not recognise a chart. So each chart writes its own
    reading beside the numbers it is about. Without that, pressing Explain
    returned "I could not find a deterministic result to explain": a button
    that promises a briefing and delivers an apology.
    """

    def _card(self, chart, rows, **kw):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.explanation_service import build_card
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        payload, figures = chart_payload(chart, rows, threshold_pct=90.0, **kw)
        # No gateway: the template path, explicitly.
        reasoning = ReasoningAgent(None).reason(
            payload, allow_llm=False, scope=ReasoningScope.NETWORK,
            single_request=True)
        return build_card(reasoning, figures=figures)

    def _rows(self):
        return [
            _row(facility_id="A", facility_name="Chicago Plant",
                 avg_utilization_pct=99.6, peak_utilization_pct=100.0,
                 bottleneck_periods_count=12, periods_observed=12,
                 rated_capacity_per_period=40000.0, peak_throughput_units=40000.0,
                 avg_inventory_units=2000.0, peak_inventory_units=2100.0),
            _row(facility_id="B", facility_name="Delhi DC",
                 avg_utilization_pct=88.0, peak_utilization_pct=96.0,
                 bottleneck_periods_count=2, periods_observed=12,
                 rated_capacity_per_period=18000.0, peak_throughput_units=17280.0,
                 avg_inventory_units=1000.0, peak_inventory_units=2500.0),
        ]

    def test_every_chart_says_something_without_a_model(self):
        rows = self._rows()
        for chart in CHARTS:
            card = self._card(chart, rows, facility_id="B")
            assert card["headline"], chart
            assert card["meaning"], chart
            assert card["figures"], chart

    def test_no_chart_answers_with_an_apology(self):
        rows = self._rows()
        for chart in CHARTS:
            card = self._card(chart, rows, facility_id="B")
            blob = " ".join([card["headline"], card["meaning"],
                             card["warning"], card["next_step"]])
            assert "could not find a deterministic result" not in blob, chart
            # The generic network recommendation contradicts a card that has
            # just stated a finding, so it must never appear on one.
            assert "no deterministic finding to base a recommendation on" not in blob, chart

    def test_a_chart_with_no_action_says_nothing_rather_than_something_generic(self):
        """
        Some readings call for no action — "both rankings agree here". Falling
        through to the network recommendation printed "I have no deterministic
        finding to base a recommendation on" directly beneath a stated finding.
        """
        # One site: the unit and percentage rankings cannot disagree.
        rows = [_row(facility_id="B", facility_name="Delhi DC",
                     rated_capacity_per_period=18000.0,
                     peak_throughput_units=17280.0, peak_utilization_pct=96.0)]
        card = self._card(CHART_CAPACITY_CARRIED, rows)
        assert card["headline"]
        assert card["next_step"] == ""

    def test_the_prose_states_no_number_the_grounding_cannot_source(self):
        """
        `card.py` states the rule — code produces figures, the sentences carry
        none — and the numeric grounding pass enforces it by STRIPPING any
        sentence whose figures it cannot source. Numerals in this prose were
        silently deleting the sentence that contained them.
        """
        import re

        rows = self._rows()
        for chart in CHARTS:
            payload, _ = chart_payload(chart, rows, threshold_pct=90.0,
                                       facility_id="B")
            block = payload["kpi_chart"]
            for field in ("finding", "matters", "next_step"):
                text = block.get(field) or ""
                assert not re.search(r"\d", text), f"{chart}.{field}: {text!r}"

    def test_each_chart_writes_its_own_reading(self):
        """Four charts that produced one sentence would make the button
        decoration on three of them."""
        rows = self._rows()
        findings = {chart: chart_payload(chart, rows, threshold_pct=90.0,
                                         facility_id="B")[0]["kpi_chart"]["finding"]
                    for chart in CHARTS}
        assert len(set(findings.values())) == len(CHARTS), findings


class TestTheModelMayCiteNumbersButOnlyVerifiedOnes:
    """
    A chart explanation is read BESIDE the chart, so a sentence carrying no
    quantities says less than the picture under it. Everywhere else in this
    product the model writes no figures at all — the currency is applied in one
    place afterwards — so this is a deliberate, bounded exception:

        code selects the facts -> the model may use only those ->
        grounding checks every number -> anything else is removed.

    The bound is `numeric_grounding._FACT_SPEC`. A key that is not in it is not
    a quantity any narrative may assert, so registering the chart's values is
    what turns "the model invented a number" into "the model quoted one".
    """

    def _spec(self):
        from netgravity.orchestrator.validation.numeric_grounding import _FACT_SPEC
        return _FACT_SPEC

    def test_every_figure_a_chart_supplies_is_citable(self):
        """
        Otherwise the sentence quoting it is deleted by the validator — which
        is what happened, silently, and left cards with prose that had lost
        its quantities.
        """
        spec = self._spec()
        for key in ("peak_utilization_pct", "avg_utilization_pct", "threshold_pct",
                    "rated_capacity_per_period", "peak_throughput_units",
                    "headroom_units", "total_headroom_units",
                    "headroom_in_peak_period_units", "avg_inventory_units",
                    "peak_inventory_units", "peak_to_average_ratio",
                    "peak_above_average_pts"):
            assert key in spec, key

    def test_the_chart_facts_are_sourced_as_chart_facts(self):
        """Their provenance says where they came from, so a figure cannot be
        traced back to the wrong engine."""
        spec = self._spec()
        assert spec["headroom_units"][1] == "kpi_chart"
        assert spec["peak_to_average_ratio"][1] == "kpi_chart"

    def test_a_chart_payloads_numbers_become_facts(self):
        """The allowlist is only half of it — the walk has to find them."""
        from types import SimpleNamespace as NS
        from netgravity.orchestrator.validation.numeric_grounding import (
            build_authoritative_facts,
        )
        rows = [_row(facility_id="A", rated_capacity_per_period=18000.0,
                     peak_throughput_units=17280.0, peak_utilization_pct=96.0)]
        payload, _ = chart_payload(CHART_CAPACITY_CARRIED, rows)
        facts = build_authoritative_facts(payload)
        values = {round(f.value, 2) for f in facts.values()}
        # The headroom the card would quote, reachable as a citable fact.
        assert 720.0 in values, sorted(values)

    def test_only_the_chart_flow_is_allowed_to_write_figures(self):
        """
        The exception is bounded to this one payload shape. Every other flow
        keeps the rule that produced it — a model-written amount arriving in
        the wrong currency.
        """
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
               / "reasoning_agent.py").read_text(encoding="utf-8")
        assert 'if payload.get("kpi_chart") else' in src
        assert '"RULES: no figures, amounts, percentages or currency symbols. "' in src

    def test_the_figures_are_written_for_a_reader_not_dumped(self):
        """It quoted "3400.0 headroom_units" — the raw key and an unrounded
        float, straight off the evidence block."""
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
               / "reasoning_agent.py").read_text(encoding="utf-8")
        block = src[src.index('"RULES: use ONLY figures'):]
        block = block[:block.index('if payload.get("kpi_chart")')]
        assert "never a field name" in block
        assert "thousands separated" in block


class TestAChartExplanationDoesNotPrescribe:
    """
    It says what the chart SHOWS. Telling a reader to close a site or test a
    scenario needs closure economics, contractual constraints and a second
    solve — none of which a utilisation chart has. Those belong to the Overview
    and the Scenario Planner, which have them.
    """

    def test_no_chart_payload_carries_a_next_step(self):
        rows = [_row(facility_id="A", avg_utilization_pct=88.0,
                     peak_utilization_pct=96.0)]
        for chart in CHARTS:
            payload, _ = chart_payload(chart, rows, threshold_pct=90.0,
                                       facility_id="A")
            if not payload:
                continue
            assert "next_step" not in payload["kpi_chart"], chart

    def test_the_template_path_recommends_nothing(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.explanation_service import build_card
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        rows = [_row(facility_id="A", avg_utilization_pct=88.0,
                     peak_utilization_pct=96.0, bottleneck_periods_count=2)]
        for chart in CHARTS:
            payload, figures = chart_payload(chart, rows, threshold_pct=90.0,
                                             facility_id="A")
            if not payload:
                continue
            reasoning = ReasoningAgent(None).reason(
                payload, allow_llm=False, scope=ReasoningScope.NETWORK,
                single_request=True)
            card = build_card(reasoning, figures=figures)
            assert card["next_step"] == "", (chart, card["next_step"])

    def test_the_model_path_recommends_nothing_either(self):
        """
        The template path already returned none; the model's own
        `recommendation` came straight through and a utilisation chart advised
        shifting volume between sites.
        """
        src = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
               / "reasoning_agent.py").read_text(encoding="utf-8")
        assert 'recommendation = ("" if payload.get("kpi_chart")' in src

    def test_the_card_never_renders_a_next_step(self):
        js = _asset("kpi-explain.js")
        block = js[js.index("function cardMarkup(card) {"):]
        block = block[:block.index("\n}")]
        assert "next_step" not in block


class TestOneOverlayAnchoredToItsChart:
    """
    The explanation used to expand inside the chart card it described, which
    changed that card's height and pushed its neighbour out of the grid row —
    the chart moved in order to explain itself. It is now drawn OVER the chart,
    out of flow, so the grid cannot move at all.
    """

    def test_it_is_an_overlay_not_a_panel(self):
        js = _asset("kpi-explain.js")
        assert "kpi-explain-panel" not in js
        assert "function placeOverlay(entry)" in js
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-ai-overlay {"):]
        block = block[:block.index("}")]
        # Out of flow is the whole mechanism: without it the card grows.
        assert "position: absolute" in block

    def test_the_chart_card_is_the_positioning_context(self):
        js = _asset("kpi-explain.js")
        assert "chartCard.classList.add('has-ai-overlay')" in js
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".card.has-ai-overlay { position: relative; }" in css

    def test_a_second_chart_moves_the_one_overlay(self):
        """Two briefings open at once leaves the reader working out which one
        belongs to the thing they just clicked."""
        js = _asset("kpi-explain.js")
        block = js[js.index("function placeOverlay(entry) {"):]
        block = block[:block.index("\n}")]
        # Moved, never copied.
        assert "chartCard.appendChild(overlay)" in block
        assert "createElement" not in block

    def test_it_moves_between_the_rollup_and_the_drill_down(self):
        """One element, moved — two copies could not guarantee only one is
        ever open."""
        js = _asset("kpi-explain.js")
        assert "overlay.parentElement !== chartCard" in js

    def test_the_close_button_is_the_only_way_out(self):
        """
        A card that also vanishes when the button behind it is pressed again
        gives the reader two ways out, one of which is invisible from where
        they are looking — the overlay covers the chart, not the header.
        """
        js = _asset("kpi-explain.js")
        block = js[js.index("async function onExplain(entry) {"):]
        block = block[:block.index("\n}")]
        assert "setOverlayOpen(entry, false)" not in block
        # The × still closes it, and so does moving to another chart.
        assert "kpi-explain-close" in js

    def test_the_button_never_changes_its_label(self):
        """It read "✓ Explained" after a response landed — a state the overlay
        beside it already shows, on a control that then said something other
        than what pressing it does."""
        js = _asset("kpi-explain.js")
        assert "Explained'" not in js
        assert js.count("AI Explain") >= 2


class TestTheButtonInvitesOnceAndThenStops:
    """
    The feature is otherwise a small control in a chart header on a screen of
    eleven cards, and a reader has little reason to look for it.
    """

    def test_one_reflection_and_not_a_pulse(self):
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".kpi-explain-btn.is-shimmering::after {"):]
        block = block[:block.index("}")]
        # A bounded count, never `infinite` — a control that keeps advertising
        # itself is what makes a dashboard tiring.
        assert "infinite" not in block
        assert "animation: kpi-explain-shimmer" in block

    def test_it_never_runs_for_a_reader_who_asked_for_less_motion(self):
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        assert "@media (prefers-reduced-motion: reduce)" in css
        js = _asset("kpi-explain.js")
        assert "prefers-reduced-motion: reduce" in js

    def test_it_stops_for_good_once_the_feature_is_used(self):
        """It was an invitation. Once accepted it has done its job."""
        js = _asset("kpi-explain.js")
        block = js[js.index("async function onExplain(entry) {"):]
        assert "stopShimmer()" in block[:900]
        stop = js[js.index("function stopShimmer() {"):]
        stop = stop[:stop.index("\n}")]
        assert "shimmerDone = true" in stop

    def test_a_new_network_offers_it_again(self):
        js = _asset("kpi-explain.js")
        block = js[js.index("export function clearKpiExplainCache()"):]
        block = block[:block.index("\n}")]
        assert "resetKpiExplainShimmer()" in block

    def test_the_button_says_what_it_is_doing(self):
        js = _asset("kpi-explain.js")
        assert "AI Explain" in js
        assert "Analysing" in js
        assert "Explained" in js


class TestTheChartHeaderIsOneShape:

    def test_a_legend_name_never_pushes_its_value_out_of_the_card(self):
        """
        The amount is the half a reader came for.

        This used to be a `legendName()` helper cutting at a fixed character
        count, because a canvas legend is drawn text and cannot measure itself
        against the room it has — so it clipped the value instead. The legend
        is DOM now: the browser truncates the NAME against the actual width
        and the helper is gone.
        """
        charts = _asset("charts.js")
        assert "legendName" not in charts, "the dead helper is still here"
        css = (REPO_ROOT / "app" / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        name = css[css.index(".ng-legend-name {"):]
        name = name[:name.index("}")]
        assert "text-overflow: ellipsis" in name
        value = css[css.index(".ng-legend-value {"):]
        value = value[:value.index("}")]
        assert "flex: 0 0 auto" in value

    def test_the_full_name_survives_in_the_tooltip(self):
        """Truncated in the legend only — `data.labels` keeps the whole name,
        so nothing is lost, only deferred."""
        charts = _asset("charts.js")
        block = charts[charts.index("export function renderWarehouseSpendChart("):]
        block = block[:block.index("\n}")]
        # The legend is DOM now, so the name is ellipsised by CSS against the
        # room it actually has rather than cut at a fixed character count.
        assert "renderHtmlLegend(" in block
        assert "labels: slices.map((sl) => sl.label)" in block
        assert "facility_name || d.facility_id" in block
