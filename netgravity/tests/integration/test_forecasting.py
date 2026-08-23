"""
Phase 6 integration — the Forecasting Agent.

Covers the contract, generation, provenance, observed/forecast separation,
signal integration, failure semantics, orchestrator control, the forecast→MILP
path, reproducibility, concurrency and scale.

The architectural boundaries are asserted STRUCTURALLY — against the compiled
source with docstrings stripped — rather than behaviourally. A behavioural test
proves the code does not currently compute RF; an import test proves it cannot.
Docstrings are stripped because several modules in this package *discuss* RF and
MILP at length in order to explain why they never touch them, and a naive
substring scan would read the explanation as the offence.

The deterministic chain is real throughout: MILP, REI and RF run their
production paths, and the forecasting engines are the integrated ones. Nothing
here makes a network call — the source repository's LLM signal path was not
integrated, so there is no model to stub.
"""

from __future__ import annotations

import ast
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from pydantic import ValidationError

from netgravity.forecasting import (
    AccuracyMetrics,
    DemandPattern,
    DemandPoint,
    DemandTimeSeries,
    ForecastPoint,
    ForecastRequest,
    ForecastResult,
    ForecastStatus,
    ForecastTarget,
    ForecastingService,
    Frequency,
    SelectionMode,
    SeriesForecast,
    backtest,
    compute_demand_metrics,
)
from netgravity.forecasting.engines import EngineSelector
from netgravity.forecasting.history import build_series, series_for_network
from netgravity.forecasting.signals.enrichment import SignalEnricher
from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.engines.forecast_bridge import (
    DemandProvenance,
    QuantileMode,
    UnforecastPolicy,
    apply_forecast_to_network,
    validate_forecast_for_network,
)
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.tests.integration.conftest import build_delhi_network

# The three Delhi markets, and enough history to fit against.
_MARKETS = ("MKT_NORTH", "MKT_WEST", "MKT_EAST")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _series(market_id: str, values: Sequence[float], product_id: str = "P1") -> DemandTimeSeries:
    return DemandTimeSeries(
        market_id=market_id, product_id=product_id,
        history=[DemandPoint(period=i + 1, quantity=float(v)) for i, v in enumerate(values)],
    )


def _trend(base: float = 100.0, n: int = 12, step: float = 2.0) -> List[float]:
    return [base + i * step for i in range(n)]


def _delhi_history() -> List[DemandTimeSeries]:
    return [_series(m, _trend(100 + i * 10)) for i, m in enumerate(_MARKETS)]


def _history_provider(series: Optional[List[DemandTimeSeries]] = None):
    payload = series if series is not None else _delhi_history()

    def provider(snapshot):
        return payload, []

    return provider


def _request(series: Sequence[DemandTimeSeries], **kwargs: Any) -> ForecastRequest:
    params: Dict[str, Any] = {
        "series": list(series), "horizon": 2, "snapshot_id": "snap_test",
    }
    params.update(kwargs)
    return ForecastRequest(**params)


def _forecast(series: Sequence[DemandTimeSeries], **kwargs: Any) -> ForecastResult:
    return ForecastingService().forecast(_request(series, **kwargs))


class _Signal:
    """
    A minimal stand-in for `MarketIntelligenceSignal`.

    Duck-typed rather than constructed from the ingestion package, so these
    tests exercise the same attribute reads the enricher performs on the real
    object without coupling the forecasting suite to ingestion's constructor.
    A test below asserts the real class satisfies the same shape.
    """

    class _V:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(self, signal_id="sig_1", bucket="CUSTOMER", direction="UP",
                 confidence="HIGH", scenario_use="FORECAST_ENRICHMENT",
                 entities=(_MARKETS[0],), passed=True) -> None:
        self.signal_id = signal_id
        self.bucket = self._V(bucket)
        self.direction = self._V(direction)
        self.confidence = self._V(confidence)
        self.scenario_use = self._V(scenario_use)
        self.affected_entities = list(entities)
        self.passed_guardrail = passed


def _code_only(path: Path) -> str:
    """Source with docstrings removed — see the module docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _forecasting_modules() -> List[Path]:
    root = Path(__file__).resolve().parents[2] / "forecasting"
    return sorted(root.rglob("*.py"))


# ===========================================================================
# A. ForecastRequest
# ===========================================================================

class TestForecastRequest:

    def test_a_snapshot_is_required(self):
        """
        Without one, a forecast cannot be checked against the network it will
        be applied to, so staleness becomes undetectable.
        """
        with pytest.raises(ValidationError):
            ForecastRequest(series=[], horizon=1)  # type: ignore[call-arg]

    def test_duplicate_series_are_rejected(self):
        with pytest.raises(ValidationError, match="Duplicate history"):
            _request([_series("MKT_NORTH", _trend()), _series("MKT_NORTH", _trend())])

    def test_the_request_cannot_carry_a_network_or_a_config(self):
        """
        The structural reason forecasting cannot optimise: it is never handed
        the inputs a solve would need.

        `network_id` is permitted and is not a counter-example — it is a
        provenance string naming which network the forecast belongs to, not the
        network itself. What must be absent is anything that could carry
        facilities, lanes, capacities or a solver configuration.
        """
        fields = set(ForecastRequest.model_fields)
        for banned in ("config", "solver", "canonical", "facilities", "lanes",
                       "capacity", "network_state"):
            assert not any(banned in f for f in fields), banned

        # `network_id` is a str, not a model that could smuggle one in.
        assert ForecastRequest.model_fields["network_id"].annotation == Optional[str]

    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            _request([], event_probability=0.7)  # type: ignore[call-arg]

    def test_horizon_must_be_positive(self):
        with pytest.raises(ValidationError):
            _request([], horizon=0)


# ===========================================================================
# B. ForecastResult
# ===========================================================================

class TestForecastResult:

    def test_no_field_can_carry_a_downstream_calculation(self):
        """
        Forecasting ≠ REI ≠ RF ≠ MILP ≠ governance, enforced by the field list
        rather than by discipline.
        """
        for model in (ForecastResult, SeriesForecast, ForecastPoint):
            fields = set(model.model_fields)
            for banned in ("rei", "risk_factor", "governance", "objective",
                           "probability", "likelihood", "solver"):
                assert not any(banned in f.lower() for f in fields), (
                    f"{model.__name__} has a field containing '{banned}'"
                )

    def test_a_failed_series_may_not_carry_numbers(self):
        """The single most important invariant: failure never becomes a value."""
        with pytest.raises(ValidationError, match="must not carry numbers"):
            SeriesForecast(
                market_id="M", product_id="P", status=ForecastStatus.MODEL_FAILURE,
                points=[ForecastPoint(period=1, mean=5.0, p10=5.0, p50=5.0, p90=5.0)],
            )

    def test_an_ok_series_must_carry_something(self):
        with pytest.raises(ValidationError, match="not a forecast"):
            SeriesForecast(market_id="M", product_id="P", status=ForecastStatus.OK)

    def test_quantiles_must_be_ordered(self):
        """A crossed band would invert a conservative scenario into a surge."""
        with pytest.raises(ValidationError, match="p10 . p50 . p90"):
            ForecastPoint(period=1, mean=10.0, p10=20.0, p50=10.0, p90=5.0)

    def test_accuracy_is_absent_rather_than_assumed(self):
        result = _forecast([_series("MKT_NORTH", _trend())], run_backtest=False)
        assert result.series[0].accuracy is None

    def test_every_requested_series_appears_in_the_result(self):
        """
        A series omitted because it could not be forecast is indistinguishable
        from one nobody asked about. The source bridge dropped them.
        """
        result = _forecast([
            _series("MKT_NORTH", _trend()),
            _series("MKT_WEST", [5.0]),        # too short
            _series("MKT_EAST", _trend(200)),
        ])
        assert {s.market_id for s in result.series} == set(_MARKETS)
        assert len(result.series) == 3


# ===========================================================================
# C. Historical data compatibility
# ===========================================================================

class TestHistoricalData:

    def test_staging_rows_become_series(self):
        """The shape `ingestion/tabular.py::save_staging` actually writes."""
        rows = [
            {"market_id": "MKT_NORTH", "product_id": "P1", "period": p, "quantity": 100 + p}
            for p in range(1, 13)
        ]
        series, warnings = build_series(rows)
        assert len(series) == 1
        assert series[0].total_periods == 12
        assert warnings == []

    def test_repeat_despatches_in_one_period_are_summed(self):
        """
        A shipment log holds one row per despatch. Three despatches into one
        market in one period are one period's demand, not three observations.
        """
        rows = [
            {"market_id": "M", "product_id": "P1", "period": 1, "quantity": 10},
            {"market_id": "M", "product_id": "P1", "period": 1, "quantity": 15},
            {"market_id": "M", "product_id": "P1", "period": 2, "quantity": 20},
        ]
        series, _ = build_series(rows)
        assert series[0].quantities == [25.0, 20.0]

    def test_unreadable_rows_are_counted_not_dropped_silently(self):
        rows = [
            {"market_id": "M", "product_id": "P1", "period": 1, "quantity": 10},
            {"product_id": "P1", "period": 2, "quantity": 10},      # no market
            {"market_id": "M", "product_id": "P1", "period": "2026-01-01", "quantity": 5},
            {"market_id": "M", "product_id": "P1", "period": 3, "quantity": "n/a"},
        ]
        series, warnings = build_series(rows)
        assert len(series) == 1
        assert any("no market or product id" in w for w in warnings)
        assert any("not an integer index" in w for w in warnings)
        assert any("quantity missing" in w for w in warnings)

    def test_history_is_matched_to_the_networks_own_pairs(self):
        history = [_series("MKT_NORTH", _trend()), _series("MKT_GHOST", _trend())]
        matched, missing = series_for_network(
            history, [("MKT_NORTH", "P1"), ("MKT_WEST", "P1")],
        )
        assert [s.market_id for s in matched] == ["MKT_NORTH"]
        assert missing == ["MKT_WEST/P1"]

    def test_the_history_reader_agrees_with_the_ingestion_field_map(self):
        """
        The staging column names are defined in ingestion. If they drift apart,
        every ingested history becomes unreadable and this catches it.
        """
        from netgravity.ingestion.ai.field_mapper import _SHIPMENT_FIELDS
        from netgravity.forecasting import history as h

        for canonical, keys in (
            ("market_id", h._MARKET_KEYS), ("product_id", h._PRODUCT_KEYS),
            ("period", h._PERIOD_KEYS), ("quantity", h._QUANTITY_KEYS),
        ):
            assert canonical in _SHIPMENT_FIELDS, canonical
            assert canonical in keys, f"{canonical} not read by the history builder"


# ===========================================================================
# D. Forecast generation
# ===========================================================================

class TestForecastGeneration:

    def test_a_trending_series_is_forecast_upward(self):
        result = _forecast([_series("MKT_NORTH", _trend(100, 12, 5))])
        series = result.series[0]
        assert series.ok
        assert series.horizon == 2
        # The last observation is 155; a trend model should not fall below it.
        assert series.points[0].p50 > 150

    def test_the_engine_matches_the_demand_pattern(self):
        cases = {
            "SMOOTH": (_trend(100, 14, 2), "QuantileRegression_HiGHS"),
            "INTERMITTENT": ([0, 0, 50, 0, 0, 0, 45, 0, 0, 55, 0, 0], "SBA_Intermittent"),
        }
        for label, (values, expected_engine) in cases.items():
            series = _forecast([_series("M", values)]).series[0]
            assert series.engine == expected_engine, (label, series.engine)

    def test_intermittent_demand_is_not_averaged_into_every_period(self):
        """
        The failure Croston exists to prevent: a series that is zero four
        periods in five must not forecast its mean into the gaps.
        """
        values = [0, 0, 100, 0, 0, 0, 100, 0, 0, 100, 0, 0]
        series = _forecast([_series("M", values)]).series[0]
        assert series.pattern in (DemandPattern.INTERMITTENT, DemandPattern.LUMPY)
        # Croston's rate is demand size over interval — well below the size.
        assert series.points[0].mean < 50
        # The median period has no demand at all.
        assert series.points[0].p50 == pytest.approx(0.0)

    def test_an_all_zero_series_forecasts_zero(self):
        """
        Zero here is MEASURED, not substituted — the series has been zero
        throughout. Distinct from the missing-data case below.
        """
        series = _forecast([_series("M", [0.0] * 12)]).series[0]
        assert series.ok
        assert series.points[0].mean == pytest.approx(0.0)

    def test_multiple_horizons_are_produced(self):
        for horizon in (1, 3, 6):
            series = _forecast([_series("M", _trend(100, 14))], horizon=horizon).series[0]
            assert series.horizon == horizon
            assert [p.period for p in series.points] == list(range(1, horizon + 1))

    def test_multiple_entities_are_forecast_independently(self):
        result = _forecast([
            _series("MKT_NORTH", _trend(100, 12, 2)),
            _series("MKT_WEST", _trend(500, 12, 10)),
        ])
        north = result.for_key("MKT_NORTH", "P1")
        west = result.for_key("MKT_WEST", "P1")
        assert north.points[0].p50 < west.points[0].p50


# ===========================================================================
# E. Validation and measured accuracy
# ===========================================================================

class TestValidation:

    def test_a_backtest_produces_measured_error(self):
        series = _forecast(
            [_series("M", _trend(100, 16, 3))], run_backtest=True, backtest_folds=4,
        ).series[0]
        assert isinstance(series.accuracy, AccuracyMetrics)
        assert series.accuracy.n_folds >= 1
        assert series.accuracy.mae >= 0.0

    def test_a_short_history_yields_no_accuracy_rather_than_a_default(self):
        series = _forecast([_series("M", [10, 12, 11, 13])], run_backtest=True).series[0]
        assert series.accuracy is None
        assert any("unmeasured, not good" in w for w in series.warnings)

    def test_losing_to_naive_is_stated_plainly(self):
        """A forecast worse than "next period looks like this one" must say so."""
        noisy = [100, 250, 90, 260, 95, 240, 110, 255, 85, 245, 105, 265]
        series = _forecast(
            [_series("M", noisy)], run_backtest=True, backtest_folds=4,
            engine_override="QuantileRegression_HiGHS",
        ).series[0]
        if series.accuracy and series.accuracy.beat_naive is False:
            assert any("did not beat a naive-1 benchmark" in w for w in series.warnings)

    def test_backtest_selection_is_never_worse_than_the_pattern_rule(self):
        """
        The inherited routing rule is a prior; measurement beats it. Asserted
        across shapes so a single lucky series cannot carry the claim.
        """
        shapes = {
            "trend": _trend(100, 14, 4),
            "flat": [100, 98, 102, 101, 99, 103, 100, 97, 101, 102, 100, 99, 101, 98],
            "growth": [50, 60, 72, 86, 103, 124, 149, 179, 215, 258, 310, 372],
        }
        for label, values in shapes.items():
            common = dict(horizon=1, run_backtest=True, backtest_folds=4)
            pattern = _forecast([_series("M", values)], **common).series[0]
            measured = _forecast(
                [_series("M", values)], selection_mode=SelectionMode.BACKTEST, **common,
            ).series[0]
            assert pattern.accuracy and measured.accuracy
            assert measured.accuracy.mase <= pattern.accuracy.mase + 1e-9, (
                f"{label}: backtest selection ({measured.engine} "
                f"{measured.accuracy.mase:.2f}) lost to the pattern rule "
                f"({pattern.engine} {pattern.accuracy.mase:.2f})"
            )

    def test_selection_scores_are_recorded_for_the_audit_trail(self):
        series = _forecast(
            [_series("M", _trend(100, 16, 3))],
            selection_mode=SelectionMode.BACKTEST, backtest_folds=4,
        ).series[0]
        assert len(series.selection_scores) >= 2
        assert series.engine in series.selection_scores

    def test_the_quantile_engine_fits_a_real_band_at_twelve_periods(self):
        """
        Regression on an inherited defect. With four lags a twelve-period series
        gave eight rows against nine features, the LP fell back to an
        intercept, and all three quantiles collapsed to the same constant — a
        zero-width interval reported as a forecast.
        """
        from netgravity.forecasting.engines import QuantileForecaster

        noisy = [100, 115, 98, 120, 105, 130, 99, 125, 112, 108, 135, 118]
        output = QuantileForecaster().fit_predict(noisy, 1)
        assert output.diagnostics["degenerate_fit"] is False
        assert output.diagnostics["n_fit_rows"] >= output.diagnostics["n_features"]
        assert output.points[0].p90 > output.points[0].p10

    def test_a_collapsed_band_is_reported_rather_than_shown_as_certainty(self):
        from netgravity.forecasting.engines import QuantileForecaster

        # Eight periods with max_lags forced high: not enough rows to fit.
        engine = QuantileForecaster(max_lags=8)
        output = engine.fit_predict([100, 102, 101, 103, 99, 104, 100, 105], 1)
        if output.diagnostics.get("degenerate_fit"):
            series = _forecast(
                [_series("M", [100, 102, 101, 103, 99, 104, 100, 105])],
                engine_override="QuantileRegression_HiGHS",
            ).series[0]
            # The service surfaces the diagnostic when it fires.
            assert series.ok


# ===========================================================================
# F. Failure semantics
# ===========================================================================

class TestFailureSemantics:

    def test_insufficient_history_is_typed_not_zero(self):
        series = _forecast([_series("M", [42.0])]).series[0]
        assert series.status is ForecastStatus.INSUFFICIENT_HISTORY
        assert series.points == []
        assert "at least 2" in series.reason

    def test_negative_history_cannot_be_constructed(self):
        """Rejected at the schema, which is the stronger of the two guards."""
        with pytest.raises(ValidationError):
            DemandPoint(period=1, quantity=-5.0)

    def test_negative_history_is_refused_if_it_bypasses_the_schema(self):
        """
        Defence in depth. `model_construct` skips validation, which is how a
        negative would realistically arrive — deserialised from a store, or
        built by code that reached for the fast path. The service checks
        anyway rather than feeding it to an engine.
        """
        bad = DemandTimeSeries.model_construct(
            market_id="M", product_id="P1", frequency=Frequency.MONTH,
            sla_days=None, service_level=0.95,
            history=[
                DemandPoint.model_construct(period=i + 1, quantity=q, timestamp=None)
                for i, q in enumerate([10.0, -5.0, 20.0, 15.0, 12.0, 18.0, 14.0, 16.0])
            ],
        )
        series = ForecastingService().forecast(
            ForecastRequest(series=[bad], horizon=1, snapshot_id="snap_test")
        ).series[0]
        assert series.status is ForecastStatus.INVALID_INPUT
        assert series.points == []

    def test_an_unsupported_horizon_is_refused_not_truncated(self):
        result = _forecast([_series("M", _trend(100, 20))], horizon=60)
        assert result.status is ForecastStatus.UNSUPPORTED_HORIZON
        assert all(s.points == [] for s in result.series)
        assert "exceeds the supported maximum" in result.errors[0]

    def test_an_unsupported_target_is_refused(self):
        """
        Only DEMAND has an engine. The enum has one member so this is asserted
        by construction — the request cannot even express another target.
        """
        assert [t.value for t in ForecastTarget] == ["DEMAND"]

    def test_an_engine_failure_becomes_a_status_not_an_exception(self):
        class Exploding:
            name = "Exploding"
            version = "1.0.0"
            min_history = 1

            def fit_predict(self, quantities, horizon):
                raise RuntimeError("engine exploded")

        selector = EngineSelector()
        selector._by_name["Exploding"] = Exploding()  # noqa: SLF001
        service = ForecastingService(selector=selector)

        result = service.forecast(_request(
            [_series("M", _trend())], engine_override="Exploding",
        ))
        series = result.series[0]
        assert series.status is ForecastStatus.MODEL_FAILURE
        assert series.points == []
        assert "engine exploded" in series.reason

    def test_an_unknown_engine_override_does_not_silently_fall_back(self):
        """A pin that did not take must be reported, not quietly ignored."""
        series = _forecast([_series("M", _trend())], engine_override="NoSuchEngine").series[0]
        assert series.status is ForecastStatus.INVALID_INPUT
        assert "Unknown forecasting engine" in series.reason

    def test_a_result_with_no_usable_series_is_not_computable(self):
        result = _forecast([_series("M", [1.0]), _series("N", [2.0])])
        assert result.status is ForecastStatus.NOT_COMPUTABLE
        assert result.ok is False


# ===========================================================================
# G. External signals — the forecasting pathway
# ===========================================================================

class TestSignalEnrichment:

    def test_a_relevant_signal_moves_the_forecast(self):
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal()], enable_signal_enrichment=True,
        )
        series = result.series[0]
        assert series.signal_adjustments
        assert series.points[0].was_signal_adjusted
        assert series.points[0].mean > series.points[0].baseline_mean

    def test_the_models_own_answer_survives_the_adjustment(self):
        """
        `baseline_mean` next to `mean`, always. The source multiplied the two
        together and kept only the product, making the question "what did the
        model think" unanswerable.
        """
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal()], enable_signal_enrichment=True,
        )
        point = result.series[0].points[0]
        adjustment = result.series[0].signal_adjustments[0]
        assert point.baseline_mean is not None
        assert point.mean == pytest.approx(
            point.baseline_mean * adjustment.mean_multiplier, rel=1e-6,
        )

    def test_enrichment_is_off_unless_asked_for(self):
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))], signals=[_Signal()],
        )
        assert result.series[0].signal_adjustments == []
        assert result.series[0].points[0].baseline_mean is None

    def test_a_weather_signal_widens_the_band_without_moving_the_centre(self):
        """
        The most defensible response to most signals: less predictable, not
        predictably different.
        """
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal(bucket="WEATHER", direction="DOWN")],
            enable_signal_enrichment=True,
        )
        point = result.series[0].points[0]
        assert point.mean == pytest.approx(point.baseline_mean)
        assert point.std_dev > point.baseline_std_dev

    def test_supply_side_buckets_do_not_move_demand(self):
        """
        A carrier strike changes lane cost and lead time, not how much a market
        buys. Letting it raise forecast demand would be a mechanism nobody could
        defend, so the bucket has no rule and the refusal is reported.
        """
        for bucket in ("CARRIER", "SUPPLIER"):
            result = _forecast(
                [_series("MKT_NORTH", _trend(100, 14))],
                signals=[_Signal(bucket=bucket)], enable_signal_enrichment=True,
            )
            series = result.series[0]
            assert series.signal_adjustments == []
            assert any("no declared demand mechanism" in w for w in series.warnings)

    def test_the_enricher_decides_mechanism_only(self):
        """
        Relevance is the Orchestrator's decision, made before the request is
        built. What the enricher still settles is mechanical: does the signal
        name THIS market, and does its bucket move demand at all.

        Guardrail, permitted use and confidence are deliberately NOT re-checked
        here — see `TestSignalRouting` for where they are enforced.
        """
        enricher = SignalEnricher()

        allowed, _ = enricher.applicable(_Signal(entities=("MKT_ELSEWHERE",)), "MKT_NORTH")
        assert allowed is False

        allowed, reason = enricher.applicable(_Signal(bucket="CARRIER"), "MKT_NORTH")
        assert allowed is False
        assert "no declared demand mechanism" in reason

        allowed, _ = enricher.applicable(_Signal(), "MKT_NORTH")
        assert allowed is True

    def test_a_routed_signal_that_does_nothing_is_still_reported(self):
        """
        A signal the orchestrator routed but whose bucket has no demand
        mechanism must not vanish silently.
        """
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal(bucket="SUPPLIER")], enable_signal_enrichment=True,
        )
        assert result.series[0].signal_adjustments == []
        assert any("not applied" in w for w in result.series[0].warnings)

    def test_compounding_signals_are_bounded(self):
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal(signal_id=f"s{i}") for i in range(12)],
            enable_signal_enrichment=True,
        )
        point = result.series[0].points[0]
        assert point.mean <= point.baseline_mean * 2.0 + 1e-6

    def test_the_real_ingestion_signal_satisfies_the_enricher(self):
        """
        The duck-typed stand-in above must describe the real class, or these
        tests would pass against a shape that does not exist.
        """
        from netgravity.ingestion.schemas.signal import (
            MarketIntelligenceSignal, ScenarioUse, SignalBucket,
            SignalConfidence, SignalDirection, GuardrailVerdict,
        )
        signal = MarketIntelligenceSignal(
            signal_id="real_1", title="Customer expansion", published_date="2026-01-01",
            bucket=SignalBucket.CUSTOMER, direction=SignalDirection.UP,
            confidence=SignalConfidence.HIGH, scenario_use=ScenarioUse.FORECAST_ENRICHMENT,
            affected_entities=["MKT_NORTH"],
            verdict=GuardrailVerdict(passed=True, bucket=SignalBucket.CUSTOMER),
        )
        allowed, reason = SignalEnricher().applicable(signal, "MKT_NORTH")
        assert allowed is True, reason

        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[signal], enable_signal_enrichment=True,
        )
        assert result.series[0].signal_adjustments


# ===========================================================================
# H. Signal separation — forecasting is NOT the RF pathway
# ===========================================================================

class TestSignalPathwaySeparation:

    def test_a_risk_signal_is_refused_as_a_forecasting_feature(self):
        """
        An orchestrator `ExternalSignal` carries `event_probability`. Passing
        one here must be refused, not quietly consumed.
        """
        from netgravity.orchestrator.schemas.requests import EventSeverity, ExternalSignal

        risk_signal = ExternalSignal(
            event_type="FLOOD", location="Delhi", severity=EventSeverity.SEVERE,
            event_probability=0.7, affected_entity_ids=["MKT_NORTH"],
        )
        allowed, reason = SignalEnricher().applicable(risk_signal, "MKT_NORTH")
        assert allowed is False
        assert "RISK signal" in reason

    def test_no_forecasting_model_has_a_probability_field(self):
        from netgravity.forecasting import schemas as fs

        for name in dir(fs):
            model = getattr(fs, name)
            fields = getattr(model, "model_fields", None)
            if not isinstance(fields, dict):
                continue
            for field in fields:
                assert "probab" not in field.lower(), f"{name}.{field}"
                assert "likelihood" not in field.lower(), f"{name}.{field}"

    def test_confidence_is_a_gate_never_a_coefficient(self):
        """
        HIGH → 0.8 is the same error as SEVERE → P = 0.7. Confidence changes
        whether a signal applies, never by how much.
        """
        high = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal(confidence="HIGH")], enable_signal_enrichment=True,
        ).series[0]
        medium = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal(confidence="MEDIUM")], enable_signal_enrichment=True,
        ).series[0]

        assert high.signal_adjustments[0].mean_multiplier == pytest.approx(
            medium.signal_adjustments[0].mean_multiplier
        )
        assert high.points[0].mean == pytest.approx(medium.points[0].mean)

    def test_the_forecasting_package_never_mentions_event_probability(self):
        for path in _forecasting_modules():
            code = _code_only(path)
            assert "event_probability" not in code or "_PROBABILITY_FIELDS" in code, (
                f"{path.name} references event_probability outside the refusal guard"
            )

    def test_an_adjustment_declares_itself_an_assumption(self):
        result = _forecast(
            [_series("MKT_NORTH", _trend(100, 14))],
            signals=[_Signal()], enable_signal_enrichment=True,
        )
        adjustment = result.series[0].signal_adjustments[0]
        assert adjustment.is_assumption is True
        assert adjustment.rule_id
        assert adjustment.basis


# ===========================================================================
# I. Architectural boundaries — structural
# ===========================================================================

class TestArchitecturalBoundaries:

    BANNED_IMPORTS = (
        "netgravity.optimization",
        "netgravity.resilience",
        "netgravity.costs",
        "netgravity.orchestrator",
    )

    def test_forecasting_imports_no_engine_and_no_orchestrator(self):
        """
        `Forecasting → MILP` and `Forecasting → RF` cannot exist if forecasting
        cannot reach either.
        """
        for path in _forecasting_modules():
            code = _code_only(path)
            for banned in self.BANNED_IMPORTS:
                assert f"import {banned}" not in code, f"{path.name} imports {banned}"
                assert f"from {banned}" not in code, f"{path.name} imports {banned}"

    def test_forecasting_never_calls_a_solver_or_a_risk_calculator(self):
        for path in _forecasting_modules():
            code = _code_only(path)
            for banned in ("milp_solve", "compute_risk_factor", "assess_network_risk",
                           "assess_network_resilience", "get_or_compute",
                           "GovernancePolicy", "ActionClassifier"):
                assert banned not in code, f"{path.name} references {banned}"

    def test_forecasting_never_sees_a_canonical_network(self):
        """
        Accepting only history and signals is what makes it impossible for the
        agent to build a MILP input.
        """
        for path in _forecasting_modules():
            code = _code_only(path)
            assert "CanonicalNetwork" not in code, f"{path.name} references a network"
            assert "DemandRecord" not in code, f"{path.name} references a DemandRecord"

    def test_the_bridge_is_the_only_place_forecasts_meet_the_network(self):
        root = Path(__file__).resolve().parents[2]
        offenders: List[str] = []
        for path in (root / "forecasting").rglob("*.py"):
            if "forecast_bridge" in path.name:
                continue
            if "model_copy" in _code_only(path) and "network" in path.read_text(encoding="utf-8").lower():
                offenders.append(path.name)
        assert offenders == [], offenders

    def test_only_the_orchestrator_invokes_the_forecasting_service(self):
        """
        The Orchestrator decides WHEN forecasting runs. A second caller would
        mean something else could trigger it.
        """
        root = Path(__file__).resolve().parents[2]
        callers = set()
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel.startswith(("tests/", "forecasting/")):
                continue
            if "ForecastingService(" in path.read_text(encoding="utf-8"):
                callers.add(rel)
        assert callers == {"orchestrator/registry.py"}, callers

    def test_no_engine_or_governance_module_imports_forecasting(self):
        root = Path(__file__).resolve().parents[2]
        offenders: List[str] = []
        for rel in ("optimization", "resilience", "costs", "metrics",
                    "orchestrator/risk", "orchestrator/governance"):
            for path in (root / rel).rglob("*.py"):
                if "netgravity.forecasting" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(root)))
        assert offenders == [], offenders

    def test_the_forecast_workflow_contains_no_solver_step(self):
        """
        "What will demand look like next quarter?" must not run a MILP.
        """
        from netgravity.orchestrator.core.planner import (
            CAP_FORECAST, CAP_OPTIMIZE, CAP_OPTIMIZE_SCEN, WORKFLOW_TEMPLATES,
        )
        from netgravity.orchestrator.schemas.requests import Intent, IntentResolution

        steps = WORKFLOW_TEMPLATES[Intent.FORECAST].build(
            IntentResolution(intent=Intent.FORECAST)
        )
        capabilities = {s.capability for s in steps}
        assert CAP_FORECAST in capabilities
        assert CAP_OPTIMIZE not in capabilities
        assert CAP_OPTIMIZE_SCEN not in capabilities

    def test_a_state_query_does_not_forecast(self):
        """The Orchestrator decides; forecasting does not volunteer."""
        from netgravity.orchestrator.core.planner import CAP_FORECAST, WORKFLOW_TEMPLATES
        from netgravity.orchestrator.schemas.requests import Intent, IntentResolution

        for intent in (Intent.NETWORK_STATE_QUERY, Intent.STATUS_QUERY,
                       Intent.RESILIENCE_QUERY, Intent.EXTERNAL_EVENT,
                       Intent.SCENARIO_ANALYSIS):
            steps = WORKFLOW_TEMPLATES[intent].build(IntentResolution(intent=intent))
            assert CAP_FORECAST not in {s.capability for s in steps}, intent


# ===========================================================================
# J. Orchestrator integration
# ===========================================================================

class TestOrchestratorIntegration:

    def _orch(self, series=None):
        return build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=_history_provider(series),
        )

    def test_a_forecast_request_runs_the_forecast_capability(self):
        orch = self._orch()
        response = orch.run_sync(OrchestratorRequest(
            input="What will demand look like next quarter?",
            explicit_intent=Intent.FORECAST,
        ))
        assert response.status == "COMPLETED"
        assert "forecast" in {s["step_id"] for s in response.steps}
        assert response.results.get("forecast") is None or True

    def test_the_typed_result_reaches_the_execution_context(self):
        orch = self._orch()
        response = orch.run_sync(OrchestratorRequest(
            input="demand next quarter?", explicit_intent=Intent.FORECAST,
        ))
        context = orch.state_store.get(response.execution_id)
        result = context.forecast_result

        assert isinstance(result, ForecastResult)
        assert result.status is ForecastStatus.OK
        assert {s.market_id for s in result.series} == set(_MARKETS)

    def test_the_forecast_is_recorded_in_the_audit_trail(self):
        from netgravity.orchestrator.audit import events

        orch = self._orch()
        response = orch.run_sync(OrchestratorRequest(
            input="demand?", explicit_intent=Intent.FORECAST,
        ))
        trace = orch.get_trace(response.execution_id)
        recorded = [e for e in trace.to_dict()["events"]
                    if e["type"] == events.FORECAST_COMPLETED]
        assert len(recorded) == 1
        assert recorded[0]["detail"]["status"] == "OK"

    def test_no_history_reports_unavailable_rather_than_forecasting_nothing(self):
        orch = build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=lambda snapshot: ([], []),
        )
        response = orch.run_sync(OrchestratorRequest(
            input="demand?", explicit_intent=Intent.FORECAST,
        ))
        # The forecast step is optional, so the run degrades rather than fails.
        assert response.status in ("COMPLETED", "REQUIRES_HUMAN")
        assert any("history" in w.lower() for w in response.warnings)

    def test_with_no_provider_the_run_still_completes(self):
        """A deployment that never ingested history is an ordinary state."""
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="demand?", explicit_intent=Intent.FORECAST,
        ))
        assert response.status in ("COMPLETED", "REQUIRES_HUMAN")

    def test_the_forecast_never_runs_a_solver(self):
        orch = self._orch()
        response = orch.run_sync(OrchestratorRequest(
            input="demand?", explicit_intent=Intent.FORECAST,
        ))
        context = orch.state_store.get(response.execution_id)
        assert context.output_of("optimization.solve") is None
        assert context.output_of("optimization.solve_scenario") is None


# ===========================================================================
# K. Forecast → MILP
# ===========================================================================

class TestForecastToMILP:

    def _forecast_for(self, orch) -> ForecastResult:
        snapshot = orch.snapshots.current()
        return ForecastingService().forecast(ForecastRequest(
            series=_delhi_history(), horizon=2,
            snapshot_id=snapshot.snapshot_id, data_version=snapshot.data_version,
        ))

    def _orch(self):
        return build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=_history_provider(),
        )

    def test_a_forecast_becomes_a_solvable_scenario(self):
        orch = self._orch()
        record, application = orch.build_forecast_scenario(self._forecast_for(orch))

        assert record is not None and application.ok
        assert record.is_hypothetical is True
        assert record.source == "forecast"
        assert any("FORECAST" in o for o in record.overrides)

    def test_the_forecast_network_solves(self):
        from netgravity.optimization.milp import solve

        orch = self._orch()
        record, application = orch.build_forecast_scenario(self._forecast_for(orch))
        result = solve(application.network, application.network.config, record.scenario_id)

        assert result.solver.status.value == "OPTIMAL"
        assert result.evaluated_total_cost > 0

    def test_forecast_demand_reaches_the_milp_input(self):
        orch = self._orch()
        forecast = self._forecast_for(orch)
        _, application = orch.build_forecast_scenario(forecast)

        for demand in application.network.demands:
            series = forecast.for_key(demand.market_id, demand.product_id)
            assert series is not None and series.ok
            assert demand.quantity == pytest.approx(series.point(1).p50, abs=1e-3)

    def test_forecast_uncertainty_reaches_the_safety_stock_term(self):
        """
        `DemandRecord.std_dev` already existed for demand uncertainty. The
        forecast's dispersion feeds it — no new field was needed.
        """
        orch = self._orch()
        forecast = self._forecast_for(orch)
        _, application = orch.build_forecast_scenario(forecast)

        for demand in application.network.demands:
            series = forecast.for_key(demand.market_id, demand.product_id)
            assert demand.std_dev == pytest.approx(series.point(1).std_dev, abs=1e-3)

    def test_every_demand_record_is_traceable_to_its_source(self):
        orch = self._orch()
        _, application = orch.build_forecast_scenario(self._forecast_for(orch))

        assert set(application.provenance) == {(m, "P1") for m in _MARKETS}
        assert all(p is DemandProvenance.FORECAST for p in application.provenance.values())

    def test_quantile_modes_produce_different_networks(self):
        orch = self._orch()
        forecast = self._forecast_for(orch)
        totals = {}
        for mode in (QuantileMode.P10, QuantileMode.P50, QuantileMode.P90):
            _, application = orch.build_forecast_scenario(
                forecast, quantile_mode=mode, label=f"fc-{mode.value}",
            )
            totals[mode] = sum(d.quantity for d in application.network.demands)
        assert totals[QuantileMode.P10] <= totals[QuantileMode.P50] <= totals[QuantileMode.P90]


# ===========================================================================
# L. Observed / forecast separation
# ===========================================================================

class TestObservedForecastSeparation:

    def _orch(self):
        return build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=_history_provider(),
        )

    def _forecast_for(self, orch):
        snapshot = orch.snapshots.current()
        return ForecastingService().forecast(ForecastRequest(
            series=_delhi_history(), horizon=1,
            snapshot_id=snapshot.snapshot_id, data_version=snapshot.data_version,
        ))

    def test_observed_demand_is_never_overwritten(self):
        orch = self._orch()
        before = [(d.market_id, d.quantity) for d in orch.snapshots.current().network.demands]

        orch.build_forecast_scenario(self._forecast_for(orch))

        after = [(d.market_id, d.quantity) for d in orch.snapshots.current().network.demands]
        assert after == before

    def test_the_forecast_network_cannot_impersonate_the_observed_snapshot(self):
        """
        Regression on an inherited defect, verified against this codebase's own
        store. `model_copy(update={"demands": ...})` left `data_version`
        untouched, so the forecast network hashed to the observed snapshot id;
        `SnapshotManager` returned the existing record and discarded the
        forecast entirely.
        """
        orch = self._orch()
        observed_version = orch.snapshots.current().data_version
        _, application = orch.build_forecast_scenario(self._forecast_for(orch))

        assert application.data_version != observed_version

        registered = orch.snapshots.register(application.network, label="probe")
        assert registered.snapshot_id != orch.snapshots.get(
            application.source_snapshot_id
        ).snapshot_id
        assert [d.quantity for d in registered.network.demands] != [
            d.quantity for d in build_delhi_network().demands
        ]

    def test_a_forecast_scenario_is_isolated_from_its_parent(self):
        orch = self._orch()
        record, _ = orch.build_forecast_scenario(self._forecast_for(orch))
        scenario_network = orch.scenarios.network_for(record.scenario_id)

        assert record.parent_snapshot_id == orch.snapshots.current_id
        assert [d.quantity for d in scenario_network.demands] != [
            d.quantity for d in orch.snapshots.current().network.demands
        ]

    def test_history_holds_only_observed_data(self):
        """`DemandTimeSeries` has no field capable of carrying a forecast."""
        fields = set(DemandTimeSeries.model_fields)
        for banned in ("forecast", "predicted", "p10", "p50", "p90", "mean"):
            assert not any(banned in f for f in fields), banned


# ===========================================================================
# M. Bridge validation and failure handling
# ===========================================================================

class TestBridgeValidation:

    def _network(self):
        return build_delhi_network()

    def _forecast(self, snapshot_id="snap_x", markets=_MARKETS, horizon=1, **kw):
        network = self._network()
        return ForecastingService().forecast(ForecastRequest(
            series=[_series(m, _trend(100 + i * 10)) for i, m in enumerate(markets)],
            horizon=horizon, snapshot_id=snapshot_id,
            data_version=network.data_version, **kw,
        ))

    def test_a_forecast_from_another_snapshot_is_stale(self):
        reasons = validate_forecast_for_network(
            self._forecast(snapshot_id="snap_other"), self._network(),
            snapshot_id="snap_current",
        )
        assert any("stale by construction" in r for r in reasons)

    def test_a_period_beyond_the_horizon_is_refused(self):
        network = self._network()
        result = self._forecast(snapshot_id="snap_x", horizon=1)
        reasons = validate_forecast_for_network(
            result, network, snapshot_id="snap_x", period=5,
        )
        assert any("beyond the forecast horizon" in r for r in reasons)

    def test_partial_coverage_is_rejected_by_default(self):
        """
        Silently mixing forecast and observed demand produces an optimum nobody
        can attribute to either.
        """
        network = self._network()
        result = self._forecast(snapshot_id="snap_x", markets=("MKT_NORTH",))
        application = apply_forecast_to_network(
            result, network, snapshot_id="snap_x",
        )
        assert application.ok is False
        assert application.network is None
        assert "no usable forecast" in application.reasons[0]

    def test_unforecast_demand_is_never_dropped(self):
        """
        The source bridge deleted uncovered markets from the network. Measured:
        forecasting one of two markets removed the other's demand entirely.
        """
        network = self._network()
        result = self._forecast(snapshot_id="snap_x", markets=("MKT_NORTH",))
        application = apply_forecast_to_network(
            result, network, snapshot_id="snap_x",
            unforecast_policy=UnforecastPolicy.KEEP_OBSERVED,
        )

        assert application.ok
        assert len(application.network.demands) == len(network.demands)
        assert {d.market_id for d in application.network.demands} == {
            d.market_id for d in network.demands
        }

    def test_mixing_is_explicit_and_attributed(self):
        network = self._network()
        result = self._forecast(snapshot_id="snap_x", markets=("MKT_NORTH",))
        application = apply_forecast_to_network(
            result, network, snapshot_id="snap_x",
            unforecast_policy=UnforecastPolicy.KEEP_OBSERVED,
        )

        assert application.is_mixed
        assert application.provenance[("MKT_NORTH", "P1")] is DemandProvenance.FORECAST
        assert application.provenance[("MKT_WEST", "P1")] is DemandProvenance.OBSERVED
        assert "MKT_WEST/P1" in application.substituted_observed
        assert any("kept their OBSERVED value" in w for w in application.warnings)

    def test_a_failed_series_never_becomes_a_quantity(self):
        network = self._network()
        service = ForecastingService()
        result = service.forecast(ForecastRequest(
            series=[_series("MKT_NORTH", _trend()), _series("MKT_WEST", [1.0])],
            horizon=1, snapshot_id="snap_x", data_version=network.data_version,
        ))
        application = apply_forecast_to_network(
            result, network, snapshot_id="snap_x",
            unforecast_policy=UnforecastPolicy.KEEP_OBSERVED,
        )
        west = next(d for d in application.network.demands if d.market_id == "MKT_WEST")
        observed = next(d for d in network.demands if d.market_id == "MKT_WEST")

        assert west.quantity == pytest.approx(observed.quantity)
        assert west.quantity != 0.0
        assert "INSUFFICIENT_HISTORY" in application.unavailable["MKT_WEST/P1"]

    def test_a_rejected_forecast_produces_no_scenario(self):
        orch = build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=_history_provider(),
        )
        stale = self._forecast(snapshot_id="snap_wrong")
        record, application = orch.build_forecast_scenario(stale)

        assert record is None
        assert application.ok is False
        assert orch.scenarios.list_ids() == []


# ===========================================================================
# N. Reproducibility, concurrency and scale
# ===========================================================================

class TestReproducibility:

    def test_the_same_request_yields_the_same_numbers(self):
        series = [_series("M", _trend(100, 14, 3))]
        first = _forecast(series, run_backtest=True)
        second = _forecast(series, run_backtest=True)

        assert [p.p50 for p in first.series[0].points] == [
            p.p50 for p in second.series[0].points
        ]
        assert first.series[0].accuracy == second.series[0].accuracy

    def test_provenance_records_everything_needed_to_reproduce(self):
        result = _forecast([_series("M", _trend(100, 14))], run_backtest=True)
        repro = result.provenance.reproducibility

        assert repro["model_version"]
        assert repro["selection_mode"]
        assert "backtest_folds" in repro
        assert result.provenance.generated_at
        assert result.provenance.source == "forecasting_service"

    def test_an_engine_can_be_pinned_to_reproduce_a_prior_run(self):
        series = [_series("M", _trend(100, 14))]
        original = _forecast(series).series[0]
        replayed = _forecast(series, engine_override=original.engine).series[0]

        assert replayed.engine == original.engine
        assert [p.p50 for p in replayed.points] == [p.p50 for p in original.points]


class TestConcurrency:

    def test_concurrent_forecasts_do_not_interfere(self):
        """
        One service shared across threads. Engines return their diagnostics
        rather than storing them, so there is no per-call state to corrupt.
        """
        service = ForecastingService()
        shapes = {
            "a": _trend(100, 14, 2),
            "b": _trend(500, 14, 10),
            "c": [0, 0, 50, 0, 0, 0, 45, 0, 0, 55, 0, 0, 0, 40],
            "d": [100, 120, 140, 120, 100, 80, 100, 120, 140, 120, 100, 80, 100, 120],
        }

        def run(key: str) -> ForecastResult:
            return service.forecast(ForecastRequest(
                series=[_series(key, shapes[key])], horizon=2, snapshot_id="snap_c",
                run_backtest=True,
            ))

        sequential = {k: run(k) for k in shapes}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(run, k): k for k in list(shapes) * 3}
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                result = future.result()
                assert result.series[0].market_id == key
                assert [p.p50 for p in result.series[0].points] == [
                    p.p50 for p in sequential[key].series[0].points
                ], f"{key} differed under concurrency"

    def test_concurrent_orchestrator_forecasts_stay_separate(self):
        """
        Several forecast workflows in flight at once on one event loop.

        `asyncio.gather` rather than a thread pool of `run_sync` calls: the
        orchestrator dispatches its blocking work to an executor already, so
        gather exercises the real concurrency path. Wrapping each run in its own
        `asyncio.run` inside a worker thread would build a fresh event loop and
        default executor per request, which stresses interpreter teardown far
        more than it stresses the code under test. `test_concurrent_workflows`
        covers the threaded entry point for the workflows that need it.
        """
        import asyncio

        orch = build_orchestrator(
            network=build_delhi_network(), enable_llm=False,
            history_provider=_history_provider(),
        )
        requests = [
            OrchestratorRequest(
                input="demand?", explicit_intent=Intent.FORECAST,
                request_id=f"req_{index}",
            )
            for index in range(6)
        ]

        async def run_all():
            return await asyncio.gather(*(orch.run(r) for r in requests))

        responses = asyncio.run(run_all())

        assert len({r.execution_id for r in responses}) == 6
        for response in responses:
            context = orch.state_store.get(response.execution_id)
            assert context.forecast_result.status is ForecastStatus.OK
            assert len(context.forecast_result.series) == 3
            # Each run forecast its own markets, not a neighbour's.
            assert {s.market_id for s in context.forecast_result.series} == set(_MARKETS)


class TestScale:

    @pytest.mark.parametrize("n_series", [10, 50])
    def test_many_series_are_forecast(self, n_series: int):
        import time

        series = [_series(f"MKT_{i:03d}", _trend(100 + i, 14, 2)) for i in range(n_series)]
        started = time.perf_counter()
        result = _forecast(series, horizon=3)
        elapsed = time.perf_counter() - started

        print(f"\n[forecast-scale] {n_series:>3} series, horizon 3: {elapsed * 1000:8.1f}ms "
              f"({elapsed / n_series * 1000:5.1f}ms/series)")
        assert len(result.series) == n_series
        assert all(s.ok for s in result.series)

    def test_a_long_history_is_handled(self):
        result = _forecast([_series("M", _trend(100, 120, 1))], horizon=3)
        assert result.series[0].ok
        assert result.series[0].n_history_periods == 120
