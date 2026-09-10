"""
"Where did that number come from?" — the forecast, as a document.

A demand plan is asked this first, and a chart cannot answer it: it shows the
answer, not the route to it. This suite holds three things.

  * **The document exists, on both paths.** A forecast this build produced
    gets the history it was fitted to, the method chosen and why, the figure
    for every period with its band, and what it does not establish. A forecast
    that arrived with the upload gets a document too — a different one, which
    says plainly that nothing was fitted and that this build cannot account
    for how the supplier arrived at the numbers.

  * **The screen and the file cannot disagree.** The route calls the same view
    the chart does, so a figure in the file is the figure that was plotted.

  * **The model may join the figures up and may not add one.** The prose in
    "The working, in plain terms" is written by the gateway; every number in
    it is checked against the figures already in the report, and any sentence
    quoting one that is not there is dropped. The worst case for a
    hallucinated quantity is a missing sentence.
"""

from __future__ import annotations

import io
import uuid

import pandas as pd
import pytest

from netgravity.orchestrator.agents.llm_gateway import LLMResponse


GOOD_PASSWORD = "forecast-doc-pw-1"

FACILITIES = pd.DataFrame([
    ("F001", "Pune Plant", "PLANT", "Pune", 18.5204, 73.8567, 40000, 250000.0,
     "West", "EXISTING"),
    ("F004", "Bhiwandi DC", "DC", "Bhiwandi", 19.2967, 73.0631, 20000, 120000.0,
     "West", "EXISTING"),
], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City",
            "Latitude", "Longitude", "Capacity_Units", "Fixed_Cost_Monthly",
            "Region", "Status"])

MARKETS = pd.DataFrame([
    ("M001", "Delhi Market", "Delhi", 28.7041, 77.1025, "North", 2),
], columns=["Market_ID", "Market_Name", "City", "Latitude", "Longitude",
            "Region", "Service_SLA_Days"])

LANES = pd.DataFrame([
    ("L001", "F001", "F004", "PLANT", "DC", 1434.4, 2.4, 200000, 4.2, True),
    ("L002", "F004", "M001", "DC", "MARKET", 20.0, 0.5, 200000, 1.8, True),
], columns=["Lane_ID", "Origin_ID", "Destination_ID", "Origin_Type",
            "Destination_Type", "Distance_KM", "Transit_Time_Days",
            "Capacity_Units", "Rate_Per_Unit", "Active"])

PRODUCTS = pd.DataFrame([
    ("P001", "Sparkling Water", 1.2, 30.0),
], columns=["Product_ID", "Product_Name", "Unit_Weight_KG", "Unit_Cost"])

#: Long enough for the engine to fit something to, which two periods are not.
#: A gentle trend with a little noise, so the method has a pattern to find.
HISTORY = pd.DataFrame(
    [(f"2024-{m:02d}" if m <= 12 else f"2025-{m - 12:02d}", "M001", "P001",
      5000.0 + 120.0 * m + (80.0 if m % 3 == 0 else -40.0))
     for m in range(1, 25)],
    columns=["Period", "Market_ID", "Product_ID", "Demand_Units"])

SUPPLIED = pd.DataFrame([
    ("2026-01", "M001", "P001", 9000.0, 8000.0, 10000.0),
    ("2026-02", "M001", "P001", 9500.0, 8400.0, 10600.0),
    ("2026-03", "M001", "P001", 9900.0, 8700.0, 11100.0),
], columns=["Period", "Market_ID", "Product_ID", "Forecast_Units",
            "Forecast_P10", "Forecast_P90"])


@pytest.fixture
def client():
    from app.backend.app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth(client):
    email = f"fcdoc-{uuid.uuid4().hex}@example.com"
    res = client.post("/api/auth/signup",
                      json={"email": email, "password": GOOD_PASSWORD})
    assert res.status_code == 201, res.get_json()
    return {"Authorization": f"Bearer {res.get_json()['token']}"}


@pytest.fixture
def project(client, auth):
    res = client.post("/api/projects", headers=auth,
                      json={"name": f"Doc {uuid.uuid4().hex[:6]}"})
    assert res.status_code in (200, 201), res.get_json()
    body = res.get_json()
    return body.get("project_id") or body.get("id") or body["project"]["id"]


def _workbook(with_forecast: bool) -> bytes:
    buffer = io.BytesIO()
    sheets = {"Facilities": FACILITIES, "Markets": MARKETS, "Lanes": LANES,
              "Products": PRODUCTS, "Demand_History": HISTORY}
    if with_forecast:
        sheets["Forecast"] = SUPPLIED
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _bind(client, auth, project, *, with_forecast: bool):
    res = client.post(
        "/api/ingestions/preview/upload-and-parse", headers=auth,
        data={"project_id": project,
              "files": (io.BytesIO(_workbook(with_forecast)), "book.xlsx")},
        content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    committed = client.post("/api/ingestions/preview/commit", headers=auth,
                            json={"project_id": project})
    assert committed.status_code == 201, committed.get_json()


def _text(data: bytes) -> str:
    """Everything a reader would see in the file, as one string."""
    from docx import Document

    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


class TestAModelledForecastCanLeaveTheApplication:
    def test_the_document_states_the_method_and_names_the_engine(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=False)
        forecast = client.get(f"/api/forecast?project_id={project}&horizon=6",
                              headers=auth).get_json()
        assert forecast["forecast_source"] == "model"
        series = forecast["series"][0]

        res = client.get(f"/api/forecast/document?project_id={project}&horizon=6",
                         headers=auth)
        assert res.status_code == 200, res.get_data()[:400]
        text = _text(res.get_data())

        assert series["engine"] in text, "the engine that produced this is named"
        # The METHOD, not just its name. A reader who is not a statistician
        # cannot act on "ETS".
        assert "smoothing" in text.lower() or "Croston" in text or "quantile" in text
        assert "No language model takes part" in text

    def test_every_period_is_in_the_file_with_its_band(
            self, client, auth, project):
        """
        The part a chart can only draw. A reader asked "what is the figure for
        March?" cannot answer it from a line, and this is the question a demand
        plan is taken away to answer.
        """
        _bind(client, auth, project, with_forecast=False)
        forecast = client.get(f"/api/forecast?project_id={project}&horizon=6",
                              headers=auth).get_json()
        series = forecast["series"][0]
        res = client.get(f"/api/forecast/document?project_id={project}&horizon=6",
                         headers=auth)
        text = _text(res.get_data())

        assert len(series["points"]) == 6
        for point in series["points"]:
            # The figure the chart plotted, formatted the way the screen
            # formats one — not a re-rounded second opinion about it.
            assert f"{point['mean']:,.0f} units" in text, point

    def test_the_total_is_labelled_as_a_sum_of_what_is_above_it(
            self, client, auth, project):
        """
        A total in a document about a forecast reads as a separately modelled
        figure unless it says otherwise. It is not one.
        """
        _bind(client, auth, project, with_forecast=False)
        res = client.get(f"/api/forecast/document?project_id={project}&horizon=6",
                         headers=auth)
        text = _text(res.get_data())
        assert "Sum of the periods above" in text
        assert "A sum over the periods listed above and nothing more" in text

    def test_the_file_is_named_for_the_series_not_the_url(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=False)
        res = client.get(f"/api/forecast/document?project_id={project}&horizon=6",
                         headers=auth)
        disposition = res.headers["Content-Disposition"]
        assert "Demand-forecast" in disposition
        assert "M001" in disposition
        assert disposition.endswith('.docx"')


class TestASuppliedForecastGetsADifferentDocument:
    def test_it_says_nothing_was_fitted(self, client, auth, project):
        """
        The one thing this document must not do is describe a supplied
        projection as the output of an engine that never ran.
        """
        _bind(client, auth, project, with_forecast=True)
        forecast = client.get(f"/api/forecast?project_id={project}",
                              headers=auth).get_json()
        assert forecast["forecast_source"] == "uploaded"

        res = client.get(f"/api/forecast/document?project_id={project}",
                         headers=auth)
        assert res.status_code == 200, res.get_data()[:400]
        text = _text(res.get_data())

        assert "supplied with the upload" in text.lower()
        assert "No forecast was produced" in text
        assert "cannot account for how the supplier arrived at it" in text
        # And it does not claim a method it did not use.
        assert "smoothing" not in text.lower()

    def test_it_reproduces_the_supplied_figures_exactly(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=True)
        res = client.get(f"/api/forecast/document?project_id={project}",
                         headers=auth)
        text = _text(res.get_data())
        for value in (9000, 9500, 9900):
            assert f"{value:,.0f} units" in text, value
        # The upload's own calendar, not "+1 / +2".
        assert "2026-01" in text and "2026-03" in text

    def test_a_missing_backtest_is_not_reported_as_a_good_one(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=True)
        res = client.get(f"/api/forecast/document?project_id={project}",
                         headers=auth)
        text = _text(res.get_data())
        assert "MASE" not in text
        assert "better than a naive" not in text


class TestTheDocumentAndTheScreenReadTheSamePayload:
    def test_a_named_series_that_does_not_exist_is_a_404(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=False)
        res = client.get(
            f"/api/forecast/document?project_id={project}&market_id=M999",
            headers=auth)
        assert res.status_code == 404
        assert "M999" in res.get_json()["error"]["message"]

    def test_a_project_with_no_forecast_says_so_rather_than_writing_a_blank(
            self, client, auth):
        """
        An empty document that opens and says nothing is worse than an error:
        it is indistinguishable from a forecast of nothing.
        """
        res = client.post("/api/projects", headers=auth,
                          json={"name": f"Empty {uuid.uuid4().hex[:6]}"})
        body = res.get_json()
        empty = body.get("project_id") or body.get("id") or body["project"]["id"]
        res = client.get(f"/api/forecast/document?project_id={empty}",
                         headers=auth)
        assert res.status_code in (404, 409), res.get_json()
        assert res.headers.get("Content-Type", "").startswith("application/json")


class TestTheModelJoinsUpAndDoesNotAddUp:
    """
    `netgravity.reporting.narration` — the only place in this codebase where a
    language model's prose reaches a document.
    """

    def test_a_figure_the_report_does_not_state_takes_its_sentence_with_it(self):
        from netgravity.reporting import DerivationReport, DerivationStep, Figure
        from netgravity.reporting.narration import allowed_figures, verify

        report = DerivationReport(
            subject="s", conclusion="c",
            steps=[DerivationStep(title="t", figures=(
                Figure("Forecast demand", "48,200 units"),
                Figure("Average utilisation", "56.23%"),
            ))])
        allowed = allowed_figures(report)
        kept, dropped = verify(
            "Demand is forecast at 48,200 units over the horizon. "
            "That leaves 17,400 units of headroom at the Bhiwandi site. "
            "Average utilisation is 56.23%.",
            allowed)
        assert len(kept) == 2, kept
        assert "17,400" in dropped
        assert not any("17,400" in s for s in kept)

    def test_rounding_the_report_s_own_figure_is_not_a_hallucination(self):
        """
        A model asked to explain "150,627.70" may write "150,628". Rejecting
        that would drop good sentences over a rounding convention.
        """
        from netgravity.reporting.narration import verify

        kept, dropped = verify("The network costs 150,628 per period.",
                               [150627.70])
        assert kept and not dropped

    def test_a_ratio_may_be_written_as_its_percentage(self):
        from netgravity.reporting.narration import verify

        kept, dropped = verify("Demand met reached 96.8%.", [0.968])
        assert kept and not dropped

    def test_counting_words_do_not_drop_a_sentence(self):
        """
        "in three steps" is grammar, not a quantity, and a rule that cannot
        tell them apart deletes correct prose.
        """
        from netgravity.reporting.narration import verify

        kept, dropped = verify("The derivation runs in 3 steps.", [])
        assert kept and not dropped

    def test_the_prompt_states_the_rule_it_will_be_judged_by(self):
        from netgravity.reporting import DerivationReport, DerivationStep, Figure
        from netgravity.reporting.narration import build_prompt

        prompt = build_prompt(DerivationReport(
            subject="Capacity", conclusion="Three sites are above threshold",
            steps=[DerivationStep(title="t", figures=(
                Figure("Average utilisation", "56.23%"),))]))
        assert "Do not state any number that is not in the list above" in prompt
        assert "56.23%" in prompt
        # No system role exists on this gateway, so the instruction has to be
        # in the one field it accepts.
        assert "senior leadership" in prompt

    def test_no_gateway_is_not_a_failed_download(self):
        """
        The budget is shared and small and the token may not be configured at
        all. A document is still a document without this section.
        """
        from netgravity.reporting import DerivationReport
        from netgravity.reporting.narration import narrate

        narration = narrate(DerivationReport(subject="s", conclusion="c"), None)
        assert narration.paragraphs == ()
        assert narration.source == "unavailable"
        assert narration.note, "the reader is told why the section is absent"

    def test_a_gateway_that_raises_is_not_a_failed_download(self):
        from netgravity.reporting import DerivationReport
        from netgravity.reporting.narration import narrate

        class Exploding:
            available = True

            def generate(self, prompt, purpose="x"):
                raise RuntimeError("gateway down")

        narration = narrate(DerivationReport(subject="s", conclusion="c"),
                            Exploding())
        assert narration.source == "unavailable"
        assert not narration.paragraphs

    def test_prose_that_is_entirely_invented_is_withheld_and_said_to_be(self):
        from netgravity.reporting import (
            DerivationReport, DerivationStep, Figure, build_derivation_docx)
        from netgravity.reporting.narration import narrate

        class Inventing:
            available = True

            def generate(self, prompt, purpose="x"):
                return LLMResponse(output=(
                    "The network moved 4,912,003 units last quarter "
                    "at a cost of 88,401,222."))

        report = DerivationReport(
            subject="s", conclusion="c",
            steps=[DerivationStep(title="t",
                                  figures=(Figure("Sites open", "5"),))])
        narration = narrate(report, Inventing())
        assert narration.source == "rejected"
        assert not narration.paragraphs
        assert "withheld" in narration.note

        # And the document is still written, with the withholding stated.
        report.narrative_note = narration.note
        text = _text(build_derivation_docx(report))
        assert "withheld" in text
        assert "4,912,003" not in text

    def test_it_reads_the_field_the_gateway_actually_returns(self):
        """
        THE BUG THIS PINS. `narrate` read `response.text`. `LLMResponse` has
        no such field — it is `output` — so every successful call returned the
        empty string, and the empty case is a legitimate outcome this module
        is built to absorb quietly. The budget was spent, 1,824 output tokens
        came back, and the document said "the service returned nothing to
        add". Nothing failed; the feature simply never worked.

        Constructing the REAL response type is the point of this test. A hand
        written double with a `text` attribute is what made the original bug
        invisible to its own tests.
        """
        from netgravity.reporting import DerivationReport, DerivationStep, Figure
        from netgravity.reporting.narration import narrate

        assert not hasattr(LLMResponse(output="x"), "text")

        class Real:
            available = True

            def generate(self, prompt, purpose="x"):
                return LLMResponse(output="Five sites are open.",
                                   request_id="req_1")

        report = DerivationReport(
            subject="s", conclusion="c",
            steps=[DerivationStep(title="t",
                                  figures=(Figure("Sites open", "5"),))])
        narration = narrate(report, Real())
        assert narration.source == "llm", narration.note
        assert narration.paragraphs

    def test_the_attribution_travels_with_the_passage(self):
        """
        A paragraph a model wrote must not be quotable out of this file
        without the sentence saying so, which is why the note is rendered
        immediately under the prose rather than in a footnote.
        """
        from netgravity.reporting import (
            DerivationReport, DerivationStep, Figure, build_derivation_docx)
        from netgravity.reporting.narration import narrate

        class Good:
            available = True

            def generate(self, prompt, purpose="x"):
                return LLMResponse(output=(
                    "Five sites are open across the network. "
                    "That is what the plan uses."))

        report = DerivationReport(
            subject="s", conclusion="c",
            steps=[DerivationStep(title="t",
                                  figures=(Figure("Sites open", "5"),))])
        narration = narrate(report, Good())
        assert narration.source == "llm"
        report.narrative = list(narration.paragraphs)
        report.narrative_note = narration.note

        text = _text(build_derivation_docx(report))
        assert "Five sites are open" in text
        assert "Written by the NetGravity text service" in text
        assert "no model took part in producing it" in text


_FRONTEND = None


def _asset(*parts) -> str:
    from pathlib import Path
    root = Path(__file__).resolve().parents[3] / "app" / "frontend"
    return (root.joinpath(*parts)).read_text(encoding="utf-8")


class TestASuppliedForecastStillLeadsWithAFinding:
    """
    The screen had the outlook rows and no lead sentence, so an uploaded
    forecast rendered as a table of deltas with nothing saying what they
    amounted to — while the modelled path, beside it, opened with a
    conclusion. The reader has the same question on both screens.
    """

    def test_the_card_says_what_the_projection_amounts_to(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=True)
        body = client.get(f"/api/forecast?project_id={project}",
                          headers=auth).get_json()
        card = (body["explanation"] or {}).get("card") or {}
        assert card.get("headline"), body["explanation"]
        assert "supplied forecast" in card["headline"].lower()
        assert card.get("next_step")

    def test_it_is_not_the_networks_briefing_wearing_a_forecast_hat(
            self, client, auth, project):
        """
        The bug this replaced: the forecast card showed Home's network-scoped
        briefing, which answers a different question in the same place.
        """
        _bind(client, auth, project, with_forecast=True)
        body = client.get(f"/api/forecast?project_id={project}",
                          headers=auth).get_json()
        explanation = body["explanation"]
        assert explanation["scope"] == "FORECAST"
        # Not "llm", and not the reasoning agent's "template": the screen
        # prints a different attribution for each, and all three are
        # different claims about who wrote the words.
        assert explanation["source"] == "uploaded_summary"
        assert body["provenance"]["llm_used"] is False

    def test_every_figure_in_it_is_one_the_upload_stated(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=True)
        body = client.get(f"/api/forecast?project_id={project}",
                          headers=auth).get_json()
        card = body["explanation"]["card"]
        outlook = body["outlook"]
        # 9000 + 9500 + 9900, summed from the sheet and nothing else.
        assert outlook["total_forecast_units"] == 28400.0
        assert f"{outlook['total_forecast_units']:,.0f}" in card["headline"]

    def test_it_counts_the_periods_it_has_not_the_ones_requested(
            self, client, auth, project):
        """
        `outlook.horizon` was the REQUEST's ceiling. An upload stating three
        periods against a request for six yields three points — nothing is
        extrapolated, deliberately — and the card read "28,400 units over 6
        periods" beside a chart showing three. That is the same total spread
        over twice the time: a different claim about the demand.
        """
        _bind(client, auth, project, with_forecast=True)
        body = client.get(f"/api/forecast?project_id={project}&horizon=6",
                          headers=auth).get_json()
        points = len(body["series"][0]["points"])
        assert points == 3, "the upload states three periods"
        assert body["outlook"]["horizon"] == 3
        assert "over 3 period(s)" in body["explanation"]["card"]["headline"]

    def test_an_uncovered_pair_is_a_warning_not_a_silence(
            self, client, auth, project):
        _bind(client, auth, project, with_forecast=True)
        body = client.get(f"/api/forecast?project_id={project}",
                          headers=auth).get_json()
        card = body["explanation"]["card"]
        if body["n_series_uncovered"]:
            assert "no forecast" in card["warning"]
        else:
            # Every pair is covered, so the warning slot carries the other
            # thing a reader of a supplied projection needs: whether it states
            # any uncertainty at all.
            assert card["warning"] == "" or "band" in card["warning"]


class TestTheButtonExistsOnlyWhereTheDerivationDoes:
    def test_a_supplied_forecast_offers_no_derivation(self):
        """
        There is none to give: nothing was calculated here, and this build
        cannot account for how the supplier arrived at the numbers. A button
        promising a calculation that cannot exist is the twin hover card's
        old failure in another place.
        """
        js = _asset("js", "app.js")
        fn = js[js.index("function forecastDownloadHtml()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "meta.source === 'uploaded'" in fn, fn
        assert "return ''" in fn, fn
        # Nor where there is no forecast at all.
        assert "FORECAST.months.length" in fn, fn

    def test_it_documents_the_series_the_chart_is_showing(self):
        """
        `window.__ngForecastMeta` is written once at hydration and names the
        series the screen OPENED on. The picker changes what is plotted; a
        document about a different market would be worse than none.
        """
        js = _asset("js", "app.js")
        fn = js[js.index("async function downloadForecastDerivation(button)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "FORECAST.marketId" in fn, fn
        assert "__ngForecastMeta" not in fn, fn

        data = _asset("js", "data.js")
        assert "FORECAST.marketId = arguments[0]?.marketId" in data

    def test_a_document_is_given_longer_than_a_request(self):
        """
        Building one runs a solve and a text-generation call the gateway
        allows itself 60 seconds for. On the 30-second request budget the
        fetch was aborted while the server was still writing the file, and
        the reader was told the document could not be built.
        """
        config = _asset("js", "integration", "config.js")
        assert "DOCUMENT_TIMEOUT_MS" in config
        client = _asset("js", "integration", "api-client.js")
        assert "async download(endpoint, params = {}, options = {})" in client
        assert "options.timeout || CONFIG.REQUEST_TIMEOUT_MS" in client
        for service in ("forecast-service.js", "insight-service.js"):
            src = _asset("js", "integration", "services", service)
            assert "CONFIG.DOCUMENT_TIMEOUT_MS" in src, service

    def test_the_wait_names_its_slow_half(self):
        """A minute behind a static "Preparing…" reads as a hung button."""
        for asset, fn_name in ((("js", "app.js"), "downloadForecastDerivation"),
                               (("js", "insight-detail.js"), "downloadDerivation")):
            js = _asset(*asset)
            fn = js[js.index(f"async function {fn_name}(button)"):]
            fn = fn[:fn.index("\n}\n")]
            assert "Writing the explanation" in fn, fn_name
            assert "clearTimeout(stage)" in fn, fn_name
