"""
The Forecasting Agent.

    ForecastRequest  →  ForecastingService.forecast()  →  ForecastResult

Estimates future demand from observed history, optionally adjusted by external
market-intelligence signals. That is all it does.

**What it cannot do, structurally.** It is never handed a `CanonicalNetwork`, an
`OptimizationConfig` or a solver — `ForecastRequest` has no field for any of
them. So it cannot optimise flows, open or close a facility, compute REI or RF,
or reach the MILP, regardless of intent. Turning a forecast into a network is
the Orchestrator's job and lives in `bridge.py`, on the other side of this
boundary. A test asserts the import graph.

**Failure is typed, never numeric.** A series with too little history comes back
`INSUFFICIENT_HISTORY` with no points. A series whose engine raised comes back
`MODEL_FAILURE` with no points. Every requested series appears in the result
exactly once, so a caller iterating the output cannot silently lose an entity —
which is what the source bridge did, dropping unforecastable markets from the
network altogether.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from netgravity.forecasting.change_point import (
    ChangePointDetector,
    ChangePointResult,
)
from netgravity.forecasting.characterizer import compute_demand_metrics
from netgravity.forecasting.engines.base import BaseForecaster
from netgravity.forecasting.engines.selector import EngineSelector
from netgravity.forecasting.regime import (
    RegimeDecision,
    RegimeStrategy,
    StrategyBasis,
    select_regime,
)
from netgravity.forecasting.schemas import (
    DemandPattern,
    DemandTimeSeries,
    ForecastPoint,
    ForecastProvenance,
    ForecastRequest,
    ForecastResult,
    ForecastStatus,
    ForecastTarget,
    SelectionMode,
    SeriesForecast,
    SignalAdjustment,
)
from netgravity.forecasting.signals.enrichment import SignalEnricher
from netgravity.forecasting.validation import backtest

logger = logging.getLogger(__name__)

#: Version of the forecasting implementation as a whole. Stamped onto every
#: result so a forecast can be tied to the code that produced it.
MODEL_VERSION = "1.0.0"

#: Longest horizon the engines are willing to produce.
#:
#: The quantile engine forecasts recursively, feeding its own median back in as
#: the next lag, so uncertainty does not compound the way it should and the band
#: grows too slowly far out. Twelve periods is a full seasonal cycle at monthly
#: frequency; beyond that the recursion is extrapolating from its own output
#: more than from data, and refusing is more honest than returning a number with
#: an interval nobody should believe.
MAX_HORIZON = 12

#: Beyond this the recursive band is reported as understated rather than
#: silently trusted.
_RECURSION_WARNING_HORIZON = 6

#: Absolute floor on history. One observation is a value, not a series.
MIN_HISTORY = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForecastingService:
    """
    Demand forecasting, callable by the Orchestrator.

    Stateless across calls. The selector holds engine instances but they keep no
    per-call state, so one service is safe to share between concurrent requests.
    """

    def __init__(
        self,
        selector: Optional[EngineSelector] = None,
        enricher: Optional[SignalEnricher] = None,
        detector: Optional[ChangePointDetector] = None,
    ) -> None:
        self.selector = selector if selector is not None else EngineSelector()
        self.enricher = enricher if enricher is not None else SignalEnricher()
        self.detector = detector if detector is not None else ChangePointDetector()

    # ==================================================================

    def forecast(self, request: ForecastRequest) -> ForecastResult:
        """
        Produce a forecast for every series in the request.

        Never raises for data problems — a series that cannot be forecast comes
        back with a status saying why. Only a malformed `ForecastRequest`
        raises, and Pydantic catches that at construction.
        """
        started = time.perf_counter()
        warnings: List[str] = []
        errors: List[str] = []

        # ---- request-level refusals --------------------------------------
        if request.target is not ForecastTarget.DEMAND:
            return self._rejected(
                request, ForecastStatus.UNSUPPORTED_TARGET,
                f"Only {ForecastTarget.DEMAND.value} is implemented; no engine "
                f"forecasts {request.target}.",
            )

        if request.horizon > MAX_HORIZON:
            return self._rejected(
                request, ForecastStatus.UNSUPPORTED_HORIZON,
                f"Horizon {request.horizon} exceeds the supported maximum of "
                f"{MAX_HORIZON}. The recursive multi-step path understates "
                f"uncertainty beyond a seasonal cycle, so a longer forecast is "
                f"refused rather than returned with an interval nobody should "
                f"believe.",
            )

        if request.horizon > _RECURSION_WARNING_HORIZON:
            warnings.append(
                f"Horizon {request.horizon} exceeds {_RECURSION_WARNING_HORIZON}: "
                f"multi-step forecasts are recursive, so prediction intervals "
                f"widen more slowly than the true uncertainty does."
            )

        if not request.series:
            warnings.append("No series were supplied; the forecast is empty.")

        # ---- per series ----------------------------------------------------
        forecasts: List[SeriesForecast] = []
        engines_used: List[str] = []
        signal_ids: List[str] = []
        adapted: List[str] = []

        for series in request.series:
            sf = self._forecast_one(series, request, warnings, signal_ids)
            forecasts.append(sf)
            if sf.engine and sf.engine not in engines_used:
                engines_used.append(sf.engine)
            if sf.regime is not None and sf.regime.adapted:
                adapted.append(sf.key)

        overall = (
            ForecastStatus.OK if any(f.ok for f in forecasts)
            else ForecastStatus.NOT_COMPUTABLE
        )

        result = ForecastResult(
            status=overall,
            provenance=ForecastProvenance(
                snapshot_id=request.snapshot_id,
                data_version=request.data_version,
                network_id=request.network_id,
                target=request.target,
                horizon=request.horizon,
                frequency=request.frequency,
                execution_id=request.execution_id,
                request_id=request.request_id,
                scenario_id=request.scenario_id,
                engines_used=sorted(engines_used),
                selection_mode=request.selection_mode,
                model_version=MODEL_VERSION,
                signal_ids=sorted(set(signal_ids)),
                generated_at=_utc_now(),
                reproducibility={
                    "model_version": MODEL_VERSION,
                    "engine_override": request.engine_override,
                    "selection_mode": request.selection_mode.value,
                    "signal_enrichment": request.enable_signal_enrichment,
                    "backtest": request.run_backtest,
                    "backtest_folds": request.backtest_folds if request.run_backtest else 0,
                    "min_history": MIN_HISTORY,
                    "max_horizon": MAX_HORIZON,
                    "change_point_detection": request.detect_structural_break,
                    "change_point_method": (
                        self.detector.detection_method
                        if request.detect_structural_break else None
                    ),
                    "change_point_threshold": (
                        self.detector.threshold
                        if request.detect_structural_break else None
                    ),
                },
                #: Series keys whose forecast was fitted on a post-break window
                #: rather than the full history. Empty on every run where
                #: nothing adapted, which is the common case.
                adapted_series=sorted(adapted),
            ),
            series=forecasts,
            warnings=warnings,
            errors=errors,
        )

        logger.info(
            "forecasting.completed snapshot=%s horizon=%d series=%d statuses=%s "
            "engines=%s duration_s=%.4f",
            request.snapshot_id, request.horizon, len(forecasts),
            result.status_counts(), engines_used, time.perf_counter() - started,
        )
        return result

    # ==================================================================

    def _forecast_one(
        self,
        series: DemandTimeSeries,
        request: ForecastRequest,
        warnings: List[str],
        signal_ids: List[str],
    ) -> SeriesForecast:
        """Forecast one series, converting every failure into a typed status."""
        quantities = series.quantities
        n = len(quantities)

        def failed(status: ForecastStatus, reason: str) -> SeriesForecast:
            return SeriesForecast(
                market_id=series.market_id, product_id=series.product_id,
                target=request.target, status=status, reason=reason,
                n_history_periods=n, frequency=series.frequency,
            )

        if any(q < 0 for q in quantities):
            return failed(
                ForecastStatus.INVALID_INPUT,
                "history contains negative demand, which cannot be forecast",
            )

        if n < MIN_HISTORY:
            return failed(
                ForecastStatus.INSUFFICIENT_HISTORY,
                f"{n} observation(s); at least {MIN_HISTORY} are needed. No "
                f"forecast is produced — a default would be indistinguishable "
                f"from a measured one.",
            )

        # ---- structural break, and the history window that follows from it ---
        # Runs before engine selection because it decides what the engine is
        # selected ON. A series with no break takes the `FULL_HISTORY` branch
        # and every line below sees exactly the data it saw before this
        # feature existed.
        break_result: Optional[ChangePointResult] = None
        regime: Optional[RegimeDecision] = None
        fit_quantities: Sequence[float] = quantities
        regime_engine: str = ""

        if request.detect_structural_break:
            break_result = self.detector.detect(quantities)
            regime = select_regime(
                quantities, break_result, self.selector, horizon=request.horizon,
            )
            if regime.strategy is RegimeStrategy.RECENT_REGIME:
                fit_quantities = quantities[regime.window_start:]
                # Deploy the engine the regime comparison actually measured on
                # this window at this horizon. Re-selecting here would put the
                # measurement and the shipped forecast back out of step, which
                # is the defect `regime.py` documents at length.
                regime_engine = regime.selected_engine
                series_note = (
                    f"structural break at period {break_result.change_period}: "
                    f"forecasting from the {regime.n_periods_used} post-break "
                    f"observations rather than all {n}. {regime.reason}"
                )
                warnings.append(f"{series.key}: {series_note}")

        n_fit = len(fit_quantities)

        # ---- engine selection ----------------------------------------------
        selection_scores: Dict[str, Optional[float]] = {}
        try:
            if request.engine_override:
                engine: BaseForecaster = self.selector.by_name(request.engine_override)
            elif regime_engine:
                # Set only on the adapted path; the unadapted path below is
                # byte-for-byte the pre-existing selection.
                engine = self.selector.by_name(regime_engine)
            elif request.selection_mode is SelectionMode.BACKTEST:
                engine, selection_scores = self.selector.select_by_backtest(
                    fit_quantities, folds=request.backtest_folds,
                )
            else:
                engine = self.selector.select(fit_quantities)
        except KeyError as exc:
            return failed(ForecastStatus.INVALID_INPUT, str(exc))

        # Characterised on the window actually fitted, so the reported pattern
        # is the one that routed the engine rather than a description of data
        # the model never saw.
        metrics = compute_demand_metrics(fit_quantities)

        if n_fit < engine.min_history:
            return failed(
                ForecastStatus.INSUFFICIENT_HISTORY,
                f"{engine.name} needs at least {engine.min_history} observations, "
                f"got {n_fit}.",
            )

        # ---- fit and predict ------------------------------------------------
        try:
            output = engine.fit_predict(fit_quantities, request.horizon)
        except Exception as exc:  # noqa: BLE001 - engine faults are typed, not raised
            logger.exception(
                "forecasting.engine_failed engine=%s series=%s", engine.name, series.key,
            )
            return failed(
                ForecastStatus.MODEL_FAILURE,
                f"{engine.name} raised {type(exc).__name__}: {exc}",
            )

        if not output.points or len(output.points) != request.horizon:
            return failed(
                ForecastStatus.MODEL_FAILURE,
                f"{engine.name} returned {len(output.points)} points for a horizon "
                f"of {request.horizon}.",
            )

        points: List[ForecastPoint] = list(output.points)
        series_warnings: List[str] = []

        if output.diagnostics.get("degenerate_fit"):
            # A collapsed quantile fit predicts one constant at every quantile.
            # The forecast is still the series mean, which is defensible, but
            # the zero-width band around it is not a measured interval and must
            # not read as one.
            series_warnings.append(
                f"{engine.name} could not fit distinct quantiles on "
                f"{n_fit} observations: the prediction interval collapsed and the "
                f"forecast is effectively a conditional mean. Treat p10/p90 as "
                f"absent rather than narrow."
            )

        # ---- signal enrichment ----------------------------------------------
        adjustments: List[SignalAdjustment] = []
        if request.enable_signal_enrichment and request.signals:
            adjustments, signal_warnings = self.enricher.adjustments_for(
                request.signals, series.market_id,
            )
            series_warnings.extend(signal_warnings)

            if adjustments:
                if metrics.pattern in (DemandPattern.INTERMITTENT, DemandPattern.LUMPY):
                    # The intermittent band is deliberately zero-inflated and
                    # asymmetric; rebuilding it around an adjusted mean imposes
                    # a normal shape the data does not have.
                    series_warnings.append(
                        f"signal adjustment applied to a {metrics.pattern.value} "
                        f"series: the rebuilt quantile band assumes symmetry, "
                        f"which the zero-inflated original did not."
                    )
                points = self.enricher.apply(points, adjustments)
                signal_ids.extend(a.signal_id for a in adjustments)

        # ---- measured accuracy ------------------------------------------------
        accuracy = None
        if request.run_backtest:
            # Scored on the fitted window, not the full history: measuring the
            # chosen engine against pre-break periods would report error for a
            # regime this forecast deliberately excludes.
            accuracy = backtest(engine, fit_quantities, folds=request.backtest_folds)
            if accuracy is None:
                series_warnings.append(
                    "backtest requested but the history is too short to support "
                    "a single fold; accuracy is unmeasured, not good."
                )
            elif accuracy.beat_naive is False:
                # Said plainly. A forecast worse than "next period looks like
                # this one" is not worth the machinery, and the caller should
                # hear that rather than infer it from a MASE they may not read.
                series_warnings.append(
                    f"{engine.name} did not beat a naive-1 benchmark on this series "
                    f"(MASE {accuracy.mase:.2f} over {accuracy.n_folds} folds); "
                    f"treat the forecast as weak evidence."
                )

        return SeriesForecast(
            market_id=series.market_id,
            product_id=series.product_id,
            target=request.target,
            status=ForecastStatus.OK,
            points=points,
            engine=engine.name,
            engine_version=engine.version,
            pattern=metrics.pattern,
            metrics=metrics,
            accuracy=accuracy,
            signal_adjustments=adjustments,
            #: Observations SUPPLIED, which is not necessarily the number
            #: fitted on — `regime.n_periods_used` is that. Kept as the
            #: supplied count so the field means the same thing it always did.
            n_history_periods=n,
            frequency=series.frequency,
            selection_scores=selection_scores,
            structural_break=break_result,
            regime=regime,
            warnings=series_warnings,
        )

    # ==================================================================

    def _rejected(
        self,
        request: ForecastRequest,
        status: ForecastStatus,
        reason: str,
    ) -> ForecastResult:
        """
        A request-level refusal.

        Every requested series is still listed, carrying the refusal status, so
        a caller iterating the result sees each entity and why nothing came
        back for it.
        """
        return ForecastResult(
            status=status,
            provenance=ForecastProvenance(
                snapshot_id=request.snapshot_id,
                data_version=request.data_version,
                network_id=request.network_id,
                target=request.target,
                horizon=request.horizon,
                frequency=request.frequency,
                execution_id=request.execution_id,
                request_id=request.request_id,
                scenario_id=request.scenario_id,
                model_version=MODEL_VERSION,
                generated_at=_utc_now(),
            ),
            series=[
                SeriesForecast(
                    market_id=s.market_id, product_id=s.product_id,
                    target=request.target, status=status, reason=reason,
                    n_history_periods=s.total_periods, frequency=s.frequency,
                )
                for s in request.series
            ],
            errors=[reason],
        )


__all__ = ["ForecastingService", "MODEL_VERSION", "MAX_HORIZON", "MIN_HISTORY"]
