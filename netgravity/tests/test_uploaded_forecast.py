"""
A forecast that arrives WITH the upload.
========================================

Before this existed, a workbook that carried its own forecast had two ways to
be read and both were wrong.

A sheet headed `Period | Market_ID | Forecast_Units` matched no demand alias, so
`classify_sheet` fell through every branch to "markets" — and because the
markets pass dedupes on the ids it has already seen, a forecast sheet appearing
before the real Markets sheet WON. Delhi Market lost its name, its city, its SLA
and its uploaded coordinates (28.70/77.10) to a bare id and a hash-grid position
in the Bay of Bengal, silently.

A sheet headed `Period | Market_ID | Quantity` was worse. `quantity` IS a demand
alias, so the projection was read as observed history: it set every market's
current demand from a period that has not happened, that demand reached the
MILP as fact, and the forecasting engine was handed the forecast as the history
to fit — then reported a backtested MASE measured against it.

These tests pin the separation. The rule they exist to defend is one sentence: a
projection must never be readable as an observation, at any point in the path
from a spreadsheet cell to the solver.
"""

from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.backend.api.network_extractor import (
    build_network_from_dataframes,
    classify_sheet,
    names_a_forecast,
    upload_schema,
)
from app.backend.services.demand_history_store import (
    UploadedForecastStore,
    build_series_from_structure,
    build_uploaded_forecast,
)
from app.backend.services.network_assembler import assemble_network_from_structure


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FACILITIES = pd.DataFrame([
    ("F001", "Mumbai Plant", "PLANT", "Mumbai", 19.076, 72.8777, 42150, 3852000, "ACTIVE"),
    ("F004", "Delhi DC", "DC", "Delhi", 28.7041, 77.1025, 28430, 1452300, "ACTIVE"),
], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City",
            "Latitude", "Longitude", "Capacity_Units", "Fixed_Cost", "Status"])

MARKETS = pd.DataFrame([
    ("M001", "Delhi Market", "Delhi", 28.7041, 77.1025, "North", 2),
], columns=["Market_ID", "Market_Name", "City", "Latitude", "Longitude",
            "Region", "Service_SLA_Days"])

LANES = pd.DataFrame([
    ("L001", "F001", "F004", "PLANT", "DC", 1434.4, 2.4, 11598, 4.2, True),
    ("L002", "F004", "M001", "DC", "MARKET", 20.0, 0.5, 9000, 1.8, True),
], columns=["Lane_ID", "Origin_ID", "Destination_ID", "Origin_Type",
            "Destination_Type", "Distance_KM", "Transit_Time_Days",
            "Capacity_Units", "Rate_Per_Unit", "Active"])

PRODUCTS = pd.DataFrame([
    ("P001", "Sparkling Water", 1.2, 30.0),
], columns=["Product_ID", "Product_Name", "Unit_Weight_KG", "Unit_Cost"])

HISTORY = pd.DataFrame([
    ("2025-11", "M001", "P001", 5000.0),
    ("2025-12", "M001", "P001", 5200.0),
], columns=["Period", "Market_ID", "Product_ID", "Demand_Units"])

FORECAST = pd.DataFrame([
    ("2026-01", "M001", "P001", 9000.0, 8000.0, 10000.0),
    ("2026-02", "M001", "P001", 9500.0, 8400.0, 10600.0),
], columns=["Period", "Market_ID", "Product_ID", "Forecast_Units",
            "Forecast_P10", "Forecast_P90"])


@pytest.fixture
def tables():
    """A workbook whose forecast sheet is listed FIRST — the ordering that used
    to let it overwrite the markets master."""
    return {
        "Forecast": FORECAST.copy(),
        "Facilities": FACILITIES.copy(),
        "Markets": MARKETS.copy(),
        "Lanes": LANES.copy(),
        "Products": PRODUCTS.copy(),
        "Demand_History": HISTORY.copy(),
    }


@pytest.fixture
def structure(tables):
    return build_network_from_dataframes(tables)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestAForecastSheetIsRecognisedAsOne:

    @pytest.mark.parametrize("quantity_column", [
        "Forecast_Units", "Forecast_Quantity", "Forecast", "Forecast_Demand",
        "Projected_Demand", "Projected_Units", "Planned_Demand",
    ])
    def test_a_forecast_quantity_column_names_the_sheet(self, quantity_column):
        frame = pd.DataFrame(columns=["Period", "Market_ID", quantity_column])
        assert classify_sheet(frame) == "uploaded_forecast"

    def test_it_is_not_read_as_a_markets_master(self):
        """The bug: no demand alias matched, so it fell through to "markets"."""
        frame = pd.DataFrame(columns=["Period", "Market_ID", "Forecast_Units"])
        assert classify_sheet(frame) != "markets"

    def test_demand_history_is_untouched(self):
        frame = pd.DataFrame(columns=["Period", "Market_ID", "Demand_Units"])
        assert classify_sheet(frame) == "demand_history"

    def test_a_sheet_stating_both_stays_demand_history(self):
        """
        Actuals and a projection side by side. The observed half must never be
        lost to the forecast branch; the forecast column is read off it
        separately.
        """
        frame = pd.DataFrame(
            columns=["Period", "Market_ID", "Demand_Units", "Forecast_Units"])
        assert classify_sheet(frame) == "demand_history"

    def test_a_sheet_naming_no_market_is_not_a_forecast(self):
        frame = pd.DataFrame(columns=["Period", "Forecast_Units"])
        assert classify_sheet(frame) == "unknown"

    def test_the_other_roles_still_classify(self):
        assert classify_sheet(MARKETS) == "markets"
        assert classify_sheet(FACILITIES) == "facilities"
        assert classify_sheet(LANES) == "lanes"
        assert classify_sheet(pd.DataFrame(
            columns=["Facility_ID", "Period", "Available_Capacity_Units"])
        ) == "capacity_history"


class TestTheSheetNameBreaksTheOneTieColumnsCannot:
    """
    `Period | Market_ID | Quantity` is a forecast and a demand history with the
    same column signature. Only the name separates them, and this is the one
    decision in the extractor a name is allowed to make.
    """

    AMBIGUOUS = ["Period", "Market_ID", "Product_ID", "Quantity"]

    @pytest.mark.parametrize("name", [
        "Forecast", "Demand_Forecast", "FY26 Projection", "Budget_2026",
        "Planned_Volumes", "Outlook",
    ])
    def test_a_forecast_name_wins(self, name):
        assert classify_sheet(pd.DataFrame(columns=self.AMBIGUOUS), name) \
            == "uploaded_forecast"

    @pytest.mark.parametrize("name", [
        "Demand_History", "Historic_Demand", "Actuals", "Observed_Demand",
        "Sheet1", "",
    ])
    def test_anything_else_stays_history(self, name):
        assert classify_sheet(pd.DataFrame(columns=self.AMBIGUOUS), name) \
            == "demand_history"

    def test_a_sheet_naming_both_trusts_its_columns(self):
        """`Actual_vs_Forecast` holds actuals. The columns decide."""
        assert classify_sheet(pd.DataFrame(columns=self.AMBIGUOUS),
                              "Actual_vs_Forecast") == "demand_history"
        assert names_a_forecast("Actual_vs_Forecast") is False

    def test_an_explicit_forecast_column_ignores_the_name(self):
        """A name never has to rescue a sheet whose columns already say it."""
        frame = pd.DataFrame(columns=["Period", "Market_ID", "Forecast_Units"])
        assert classify_sheet(frame, "Demand_History") == "uploaded_forecast"

    def test_the_decision_is_never_silent(self):
        """A name overriding a column signature has to appear in the notes."""
        frame = pd.DataFrame([("2026-01", "M001", "P001", 9000.0)],
                             columns=self.AMBIGUOUS)
        built = build_network_from_dataframes({"Forecast_FY26": frame})
        assert any("named as a projection" in n for n in built["notes"])


# ---------------------------------------------------------------------------
# Extraction — the separation itself
# ---------------------------------------------------------------------------

class TestAForecastNeverBecomesAnObservation:

    def test_it_lands_in_its_own_collection(self, structure):
        assert len(structure["uploadedForecast"]) == 2
        assert {r["period"] for r in structure["uploadedForecast"]} \
            == {"2026-01", "2026-02"}

    def test_it_is_absent_from_the_demand_history(self, structure):
        periods = {row["period"] for row in structure["demandHistory"]}
        assert periods == {"2025-11", "2025-12"}
        assert len(structure["demandHistory"]) == 2

    def test_it_does_not_set_the_market_current_demand(self, structure):
        """
        THE failure this module exists to prevent. Current demand is the latest
        OBSERVED period (5,200), never the latest forecast period (9,500) —
        which would reach the MILP as a fact about today.
        """
        market = next(m for m in structure["markets"] if m["id"] == "M001")
        assert market["demand"] == 5200.0

    def test_it_does_not_reach_the_forecasting_engine(self, structure):
        """
        `build_series_from_structure` feeds the ETS/Croston/quantile engines.
        A projection in there is a model fitted to its own answer.
        """
        series, _notes = build_series_from_structure(structure)
        assert len(series) == 1
        labels = [p.timestamp for p in series[0].history]
        assert labels == ["2025-11", "2025-12"]
        assert 9000.0 not in [p.quantity for p in series[0].history]

    def test_it_does_not_reach_the_canonical_network(self, structure):
        network, _assumptions, _issues = assemble_network_from_structure(
            structure, network_id="net_test", description="test")
        quantities = {d.quantity for d in network.demands}
        assert 9000.0 not in quantities and 9500.0 not in quantities

    def test_the_markets_master_is_not_overwritten(self, structure):
        """
        The forecast sheet is FIRST in this workbook. It used to win the
        markets role and replace the real row: name "M001", no city, no SLA,
        and coordinates invented on a hash grid.
        """
        markets = [m for m in structure["markets"] if m["id"] == "M001"]
        assert len(markets) == 1
        market = markets[0]
        assert market["name"] == "Delhi Market"
        assert market["city"] == "Delhi"
        assert market["slaDays"] == 2
        assert market["coordsExact"] is True
        assert market["lat"] == pytest.approx(28.7041, abs=0.2)
        assert market["lng"] == pytest.approx(77.1025, abs=0.2)

    def test_the_reading_is_stated_in_the_notes(self, structure):
        assert any("read as a PROJECTION" in n for n in structure["notes"])

    def test_a_workbook_without_one_is_unchanged(self):
        built = build_network_from_dataframes({
            "Facilities": FACILITIES.copy(), "Markets": MARKETS.copy(),
            "Lanes": LANES.copy(), "Products": PRODUCTS.copy(),
            "Demand_History": HISTORY.copy(),
        })
        assert built["uploadedForecast"] == []
        assert next(m for m in built["markets"]
                    if m["id"] == "M001")["demand"] == 5200.0


class TestACombinedSheetLosesNeitherHalf:
    """One table stating actuals and a projection in adjacent columns."""

    COMBINED = pd.DataFrame([
        ("2025-11", "M001", "P001", 5000.0, None),
        ("2025-12", "M001", "P001", 5200.0, None),
        ("2026-01", "M001", "P001", None, 9000.0),
    ], columns=["Period", "Market_ID", "Product_ID", "Demand_Units",
                "Forecast_Units"])

    def test_both_columns_are_read(self):
        built = build_network_from_dataframes({"Demand_Data": self.COMBINED.copy()})
        assert [r["period"] for r in built["demandHistory"]] == ["2025-11", "2025-12"]
        assert [r["period"] for r in built["uploadedForecast"]] == ["2026-01"]

    def test_current_demand_still_comes_from_the_observed_half(self):
        built = build_network_from_dataframes({
            "Markets": MARKETS.copy(), "Demand_Data": self.COMBINED.copy()})
        assert next(m for m in built["markets"] if m["id"] == "M001")["demand"] == 5200.0

    def test_a_projection_sounding_name_cannot_discard_the_actuals(self):
        """
        `Demand_Plan` reads as a projection to the name test. The sheet states
        BOTH columns, so it has already said what it is, and the name must not
        collapse it to one — that would silently drop the actuals.
        """
        built = build_network_from_dataframes({"Demand_Plan": self.COMBINED.copy()})
        assert [r["period"] for r in built["demandHistory"]] == ["2025-11", "2025-12"]
        assert [r["period"] for r in built["uploadedForecast"]] == ["2026-01"]


class TestOverlappingPeriodsAreReportedNotMerged:

    def test_neither_value_is_overwritten(self):
        overlap = pd.DataFrame([
            ("2025-12", "M001", "P001", 7777.0),
        ], columns=["Period", "Market_ID", "Product_ID", "Forecast_Units"])
        built = build_network_from_dataframes({
            "Markets": MARKETS.copy(), "Demand_History": HISTORY.copy(),
            "Forecast": overlap,
        })
        assert next(m for m in built["markets"] if m["id"] == "M001")["demand"] == 5200.0
        assert built["uploadedForecast"][0]["mean"] == 7777.0
        assert any("also appear in the demand history" in n
                   for n in built["notes"])


class TestReferentialIntegrity:

    def test_a_forecast_for_an_unknown_market_is_reported(self):
        orphan = pd.DataFrame([
            ("2026-01", "M999", "P001", 100.0),
        ], columns=["Period", "Market_ID", "Product_ID", "Forecast_Units"])
        built = build_network_from_dataframes({
            "Facilities": FACILITIES.copy(), "Markets": MARKETS.copy(),
            "Demand_History": HISTORY.copy(), "Forecast": orphan,
        })
        orphans = [p for p in built["integrity"]
                   if p["type"] == "Orphan reference" and "M999" in p["missingIds"]]
        assert orphans, built["integrity"]


# ---------------------------------------------------------------------------
# Validation on the way into the store
# ---------------------------------------------------------------------------

class TestRowsAreValidatedAndNothingIsRepairedSilently:

    def _rows(self, *rows):
        return build_uploaded_forecast({"uploadedForecast": list(rows)})

    def _row(self, **kw):
        base = {"period": "2026-01", "marketId": "M001", "productId": "P001",
                "mean": 100.0, "p10": None, "p50": None, "p90": None}
        base.update(kw)
        return base

    def test_a_clean_row_survives(self):
        rows, notes = self._rows(self._row(p10=90.0, p90=110.0))
        assert len(rows) == 1 and rows[0]["p10"] == 90.0
        assert any("No model was fitted" in n for n in notes)

    def test_a_negative_forecast_is_dropped_not_clamped(self):
        rows, notes = self._rows(self._row(mean=-5.0))
        assert rows == []
        assert any("not read as zero" in n for n in notes)

    def test_a_row_with_no_period_is_dropped_and_counted(self):
        rows, notes = self._rows(self._row(period=""))
        assert rows == []
        assert any("named no period or market" in n for n in notes)

    def test_a_row_with_no_market_is_dropped_and_counted(self):
        rows, notes = self._rows(self._row(marketId=""))
        assert rows == []
        assert any("named no period or market" in n for n in notes)

    def test_an_unreadable_quantity_is_dropped(self):
        rows, _ = self._rows(self._row(mean="not a number"))
        assert rows == []

    def test_a_nan_quantity_is_dropped(self):
        rows, _ = self._rows(self._row(mean=float("nan")))
        assert rows == []

    def test_a_crossed_band_is_dropped_rather_than_swapped(self):
        """
        P10 above P90 means the two columns were mapped the wrong way round.
        Reordering them silently hides the mapping error.
        """
        rows, notes = self._rows(self._row(p10=110.0, p90=90.0))
        assert len(rows) == 1
        assert rows[0]["p10"] is None and rows[0]["p90"] is None
        assert any("the right way round" in n for n in notes)

    def test_a_forecast_with_no_band_keeps_none_not_a_derived_one(self):
        rows, notes = self._rows(self._row())
        assert rows[0]["p10"] is None and rows[0]["p90"] is None
        assert any("without a confidence band" in n for n in notes)

    def test_an_empty_structure_yields_nothing_and_says_nothing(self):
        assert build_uploaded_forecast({}) == ([], [])
        assert build_uploaded_forecast({"uploadedForecast": []}) == ([], [])


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class TestTheStoreCannotBeReadAsHistory:

    def test_it_has_no_history_provider_contract(self):
        """
        `for_snapshot()` is what the orchestrator reads observed history
        through. Its ABSENCE here is the structural guarantee that a projection
        can never reach the forecasting engines — not a rule someone remembers.
        """
        assert not hasattr(UploadedForecastStore, "for_snapshot")


class TestTheStoreGroupsWhatWasUploaded:

    def _store(self, rows):
        store = UploadedForecastStore()
        store.put("net_x", rows)
        return store

    def _row(self, period, mean, market="M001", product="P001", **kw):
        base = {"period": period, "marketId": market, "productId": product,
                "mean": mean, "p10": None, "p50": None, "p90": None}
        base.update(kw)
        return base

    def test_periods_are_ordered_by_the_uploads_own_label(self):
        store = self._store([self._row("2026-03", 3.0), self._row("2026-01", 1.0),
                             self._row("2026-02", 2.0)])
        series, _ = store.series("net_x")
        assert [p["timestamp"] for p in series[0]["points"]] \
            == ["2026-01", "2026-02", "2026-03"]
        assert [p["mean"] for p in series[0]["points"]] == [1.0, 2.0, 3.0]

    def test_period_is_a_one_based_index_and_the_label_survives(self):
        store = self._store([self._row("2026-01", 1.0), self._row("2026-02", 2.0)])
        series, _ = store.series("net_x")
        assert [p["period"] for p in series[0]["points"]] == [1, 2]
        assert [p["timestamp"] for p in series[0]["points"]] == ["2026-01", "2026-02"]

    def test_a_missing_product_becomes_the_shared_key(self):
        store = self._store([self._row("2026-01", 1.0, product=None)])
        series, _ = store.series("net_x")
        assert series[0]["product_id"] == "PROD_ALL"

    def test_market_product_pairs_are_kept_apart(self):
        store = self._store([
            self._row("2026-01", 1.0, market="M001"),
            self._row("2026-01", 2.0, market="M002"),
        ])
        series, _ = store.series("net_x")
        assert len(series) == 2

    def test_a_duplicate_period_is_reported_not_summed(self):
        """
        Two observations of one month are two facts. Two FORECASTS of one month
        are a contradiction, so summing them would double a projection.
        """
        store = self._store([self._row("2026-01", 100.0), self._row("2026-01", 200.0)])
        series, warnings = store.series("net_x")
        assert len(series[0]["points"]) == 1
        assert series[0]["points"][0]["mean"] == 200.0
        assert any("duplicate forecast period" in w for w in warnings)

    def test_p50_falls_back_to_the_central_estimate(self):
        store = self._store([self._row("2026-01", 100.0)])
        series, _ = store.series("net_x")
        assert series[0]["points"][0]["p50"] == 100.0

    def test_a_stated_p50_is_kept(self):
        store = self._store([self._row("2026-01", 100.0, p50=95.0)])
        series, _ = store.series("net_x")
        assert series[0]["points"][0]["p50"] == 95.0

    def test_nothing_was_adjusted_so_there_is_no_baseline(self):
        store = self._store([self._row("2026-01", 100.0)])
        series, _ = store.series("net_x")
        assert series[0]["points"][0]["baseline_mean"] is None

    def test_a_longer_upload_is_truncated_to_the_horizon(self):
        store = self._store([self._row(f"2026-{m:02d}", float(m))
                             for m in range(1, 13)])
        series, _ = store.series("net_x", horizon=6)
        assert len(series[0]["points"]) == 6
        assert series[0]["points"][-1]["timestamp"] == "2026-06"

    def test_a_shorter_upload_is_never_extended(self):
        store = self._store([self._row("2026-01", 1.0), self._row("2026-02", 2.0)])
        series, warnings = store.series("net_x", horizon=6)
        assert len(series[0]["points"]) == 2
        assert any("rather than extended" in w for w in warnings)

    def test_rows_naming_no_period_are_reported(self):
        store = self._store([self._row("", 1.0), self._row("2026-01", 2.0)])
        _series, warnings = store.series("net_x")
        assert any("could not be placed on a series" in w for w in warnings)

    def test_periods_lists_what_was_uploaded(self):
        store = self._store([self._row("2026-02", 2.0), self._row("2026-01", 1.0)])
        assert store.periods("net_x") == ["2026-01", "2026-02"]

    def test_an_unknown_network_is_empty_not_an_error(self):
        store = UploadedForecastStore()
        assert store.get("nope") == []
        assert store.has("nope") is False
        assert store.series("nope") == ([], [])

    def test_clearing_forgets_it(self):
        store = self._store([self._row("2026-01", 1.0)])
        assert store.has("net_x") is True
        store.clear("net_x")
        assert store.has("net_x") is False

    def test_one_network_never_serves_another(self):
        store = self._store([self._row("2026-01", 1.0)])
        assert store.get("net_other") == []

    def test_stored_rows_are_copies(self):
        rows = [self._row("2026-01", 1.0)]
        store = self._store(rows)
        rows[0]["mean"] = 999.0
        assert store.get("net_x")[0]["mean"] == 1.0


class TestItSurvivesARestart:

    def test_rows_are_written_through_and_reloaded(self):
        saved = {}
        store = UploadedForecastStore()
        store.bind_persistence(
            lambda nid, rows: saved.__setitem__(nid, rows),
            lambda: saved,
        )
        store.put("net_x", [{"period": "2026-01", "marketId": "M001",
                             "productId": "P001", "mean": 42.0,
                             "p10": None, "p50": None, "p90": None}])
        assert saved["net_x"]

        restored = UploadedForecastStore()
        restored.bind_persistence(lambda nid, rows: None, lambda: saved)
        assert restored.load() == 1
        assert restored.get("net_x")[0]["mean"] == 42.0

    def test_the_durability_binding_names_its_own_kind(self):
        """A new `kind` in `network_data`, not a new table — and not one of the
        kinds another store already owns."""
        import inspect

        from app.backend.services import durability
        source = inspect.getsource(durability._bind_upload_stores)
        assert '"uploaded_forecast"' in source
        assert "uploaded_forecast_store.bind_persistence" in source


# ---------------------------------------------------------------------------
# The upload template and the mapping-review screen
# ---------------------------------------------------------------------------

class TestTheTemplateOffersIt:

    def test_the_role_is_in_the_schema(self):
        assert "uploaded_forecast" in {s["role"] for s in upload_schema()}

    def test_the_template_sheet_classifies_back_to_its_own_role(self):
        sheet = next(s for s in upload_schema()
                     if s["role"] == "uploaded_forecast")
        headers = [c["header"] for c in sheet["columns"]]
        assert classify_sheet(pd.DataFrame(columns=headers)) == "uploaded_forecast"

    def test_every_template_column_is_one_the_parser_reads(self):
        from app.backend.api.network_extractor import classify_column_name
        sheet = next(s for s in upload_schema()
                     if s["role"] == "uploaded_forecast")
        for column in sheet["columns"]:
            field, status, _ = classify_column_name(column["header"],
                                                    "uploaded_forecast")
            assert status == "auto", (column["header"], status)
            assert field == column["label"]

    def test_the_review_screen_can_name_the_role(self):
        """
        `ROLE_NOUNS` gates the mapping summary — a role missing from it is
        dropped from the summary entirely (`if (!noun) return;`), so the user
        is never told the sheet was read.
        """
        import io
        source = io.open("app/frontend/js/ingestion.js", encoding="utf-8").read()
        assert source.count("uploaded_forecast:") == 2


# ---------------------------------------------------------------------------
# End to end, over HTTP
# ---------------------------------------------------------------------------

GOOD_PASSWORD = "uploaded-forecast-pw-1"


@pytest.fixture
def client():
    from app.backend.app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth(client):
    email = f"fc-{uuid.uuid4().hex}@example.com"
    res = client.post("/api/auth/signup",
                      json={"email": email, "password": GOOD_PASSWORD})
    assert res.status_code == 201, res.get_json()
    return {"Authorization": f"Bearer {res.get_json()['token']}"}


@pytest.fixture
def project(client, auth):
    res = client.post("/api/projects", headers=auth,
                      json={"name": f"Forecast {uuid.uuid4().hex[:6]}"})
    assert res.status_code in (200, 201), res.get_json()
    body = res.get_json()
    return body.get("project_id") or body.get("id") or body["project"]["id"]


def _workbook(with_forecast: bool) -> bytes:
    import io as _io
    buffer = _io.BytesIO()
    sheets = {"Facilities": FACILITIES, "Markets": MARKETS, "Lanes": LANES,
              "Products": PRODUCTS, "Demand_History": HISTORY}
    if with_forecast:
        sheets["Forecast"] = FORECAST
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buffer.getvalue()


def _upload(client, auth, project, *, with_forecast: bool):
    import io as _io
    res = client.post(
        "/api/ingestions/preview/upload-and-parse", headers=auth,
        data={"project_id": project,
              "files": (_io.BytesIO(_workbook(with_forecast)), "book.xlsx")},
        content_type="multipart/form-data")
    assert res.status_code == 200, res.get_json()
    preview = res.get_json()
    committed = client.post("/api/ingestions/preview/commit", headers=auth,
                            json={"project_id": project})
    assert committed.status_code == 201, committed.get_json()
    return preview, committed.get_json()


class TestTheWholePathOverHttp:

    def test_the_review_screen_reports_the_sheet_as_a_forecast(
            self, client, auth, project):
        preview, _ = _upload(client, auth, project, with_forecast=True)
        roles = {row["sheetRole"] for rows in preview["mapping"].values()
                 for row in rows}
        assert "uploaded_forecast" in roles

    def test_the_commit_states_which_source_the_forecast_has(
            self, client, auth, project):
        _preview, committed = _upload(client, auth, project, with_forecast=True)
        summary = committed["network_summary"]
        assert summary["forecast_source"] == "uploaded"
        assert summary["uploaded_forecast_rows"] == 2

    def test_a_workbook_with_no_forecast_says_model(
            self, client, auth, project):
        _preview, committed = _upload(client, auth, project, with_forecast=False)
        assert committed["network_summary"]["forecast_source"] == "model"
        assert committed["network_summary"]["uploaded_forecast_rows"] == 0

    def test_the_endpoint_serves_the_upload_and_runs_no_model(
            self, client, auth, project):
        _upload(client, auth, project, with_forecast=True)
        res = client.get("/api/forecast", headers=auth,
                         query_string={"project_id": project})
        assert res.status_code == 200, res.get_json()
        body = res.get_json()

        assert body["status"] == "OK"
        assert body["forecast_source"] == "uploaded"
        # No orchestrator run produced this.
        assert body["execution_id"] is None
        assert body["provenance"]["recalculated"] is False
        assert body["provenance"]["authoritative_source"] == "upload"

        assert len(body["series"]) == 1
        series = body["series"][0]
        assert series["engine"] == "UPLOADED"
        # Measured error requires a model and a backtest. There was neither.
        assert series["accuracy"] is None
        assert series["signal_adjustments"] == []
        assert series["status"] == "OK"

    def test_the_uploads_own_period_labels_survive_to_the_response(
            self, client, auth, project):
        """Without these the chart can only show "+1", "+2"."""
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        points = body["series"][0]["points"]
        assert [p["timestamp"] for p in points] == ["2026-01", "2026-02"]
        assert [p["period"] for p in points] == [1, 2]
        assert [p["mean"] for p in points] == [9000.0, 9500.0]
        assert [p["p10"] for p in points] == [8000.0, 8400.0]

    def test_the_observed_history_is_attached_beside_it(
            self, client, auth, project):
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        history = body["series"][0]["history"]
        assert [h["timestamp"] for h in history] == ["2025-11", "2025-12"]
        assert [h["quantity"] for h in history] == [5000.0, 5200.0]

    def test_the_horizon_follows_the_upload_not_the_default(
            self, client, auth, project):
        """The endpoint's default is 6. Truncating a 2-period upload to 6 would
        be harmless; truncating a 12-period one to 6 would discard half the
        file the user is being shown their own numbers from."""
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        assert body["horizon"] == 2

    def test_an_explicit_horizon_still_wins(self, client, auth, project):
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project,
                                        "horizon": 1}).get_json()
        assert len(body["series"][0]["points"]) == 1

    def test_the_outlook_is_summed_from_the_upload(self, client, auth, project):
        """The attention card renders "no forecast has been produced" without
        an outlook, which would contradict the chart beside it."""
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        outlook = body["outlook"]
        assert outlook["total_forecast_units"] == 18500.0
        # The two observed periods immediately before the forecast.
        assert outlook["comparable_recent_units"] == 10200.0
        assert outlook["growth_pct"] == pytest.approx(81.37, abs=0.01)
        assert outlook["n_structural_breaks"] == 0

    def test_no_signal_was_applied_and_it_says_so(self, client, auth, project):
        _upload(client, auth, project, with_forecast=True)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        assert body["signals"]["attached"] == 0
        assert body["signals"]["series_adjusted"] == 0
        assert "not recalculated" in body["signals"]["notice"]

    def test_a_project_without_one_still_runs_the_engine(
            self, client, auth, project):
        """The whole point: this path must be unchanged for everyone else."""
        _upload(client, auth, project, with_forecast=False)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}).get_json()
        assert body["forecast_source"] == "model"
        assert body["provenance"]["authoritative_source"] == "netgravity.forecasting"

    def test_re_uploading_without_a_forecast_returns_the_project_to_the_model(
            self, client, auth, project):
        """
        A stale projection bound to a network it no longer describes is worse
        than no projection: it is the previous upload's answer, presented as
        this one's.
        """
        _upload(client, auth, project, with_forecast=True)
        first = client.get("/api/forecast", headers=auth,
                           query_string={"project_id": project}).get_json()
        assert first["forecast_source"] == "uploaded"

        _upload(client, auth, project, with_forecast=False)
        second = client.get("/api/forecast", headers=auth,
                            query_string={"project_id": project}).get_json()
        assert second["forecast_source"] == "model"

    def test_the_audit_record_keeps_what_was_read(self, client, auth, project):
        _upload(client, auth, project, with_forecast=True)
        record = client.get("/api/ingestions/preview/dataset", headers=auth,
                            query_string={"project_id": project}).get_json()
        assumptions = record["committed"]["assumptions"]
        assert any("read as a PROJECTION" in a for a in assumptions)
        assert any("No model was fitted" in a for a in assumptions)
        assert record["committed"]["network_summary"]["forecast_source"] == "uploaded"


class TestItIsKeyedByTheSnapshotTheProjectActuallyResolves:
    """
    `SnapshotManager` addresses a snapshot by the content hash of its network
    (`snap_{data_version[:12]}`), so two projects uploading networks with
    identical content are handed the SAME snapshot — and it keeps the network
    id of whichever registered first.

    A forecast written under the id passed to the assembler therefore landed
    where nothing would ever look: `/api/forecast` resolves its project's
    snapshot and reads `snapshot.network.network_id`. An upload carrying a
    forecast was served the engine's forecast instead, and an upload carrying
    none was served the forecast belonging to whichever project owned the
    shared snapshot.
    """

    def _second_project(self, client, auth):
        res = client.post("/api/projects", headers=auth,
                          json={"name": f"Second {uuid.uuid4().hex[:6]}"})
        body = res.get_json()
        return body.get("project_id") or body.get("id") or body["project"]["id"]

    def test_a_second_project_with_no_forecast_is_not_served_the_first_ones(
            self, client, auth, project):
        """The networks are byte-identical, so both land on one snapshot."""
        _upload(client, auth, project, with_forecast=True)
        assert client.get("/api/forecast", headers=auth,
                          query_string={"project_id": project}
                          ).get_json()["forecast_source"] == "uploaded"

        other = self._second_project(client, auth)
        _upload(client, auth, other, with_forecast=False)
        body = client.get("/api/forecast", headers=auth,
                          query_string={"project_id": other}).get_json()
        assert body["forecast_source"] == "model"

    def test_the_forecast_is_stored_where_the_endpoint_looks(
            self, client, auth, project):
        from app.backend.app import _orchestrator
        from app.backend.services.demand_history_store import (
            uploaded_forecast_store,
        )

        _upload(client, auth, project, with_forecast=True)
        snapshot_id = client.get("/api/forecast", headers=auth,
                                 query_string={"project_id": project}
                                 ).get_json()["snapshot_id"]
        resolved = _orchestrator.snapshots.get(snapshot_id).network.network_id
        assert uploaded_forecast_store.has(resolved), (
            "the endpoint resolves this network id, so the rows must be under it")
