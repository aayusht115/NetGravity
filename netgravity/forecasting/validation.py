"""
Rolling-origin backtesting.

Measures out-of-sample error by re-fitting on a prefix of the history and
scoring against the periods that follow, walking the origin forward:

    fold 1   |───── train ─────|─ test ─|························|
    fold 2   |──────── train ────────|─ test ─|·················|
    fold 3   |─────────── train ───────────|─ test ─|··········|

Nothing here forecasts. It calls an existing engine repeatedly and scores what
comes back, so it adds measurement rather than methodology.

── Why this exists ────────────────────────────────────────────────────────────
The source repository had no validation of any kind — no split, no backtest, no
error metric. What it had instead was a `confidence_score` literal on every
engine: 0.92 on the quantile regressor, 0.85 on Croston, 0.75 on the cold-start
prior. Those numbers were never measured. They read as accuracy, they propagated
into `ForecastResult`, and the signal fuser even multiplied them together —
`confidence_score * modifier.confidence` — compounding two invented quantities
into a third.

Deleting them and leaving nothing would have made every forecast unqualified.
Measuring is the alternative, and measuring an existing model is not inventing
one. `SeriesForecast.accuracy` is `None` unless a backtest actually ran, which
is an honest statement, and `AccuracyMetrics` names the fold count so one lucky
fold cannot pass for evidence.

WAPE is preferred over MAPE throughout: MAPE divides by the actual, and
intermittent demand is mostly zeros, so MAPE is undefined exactly where these
engines are most used.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

import numpy as np

from netgravity.forecasting.engines.base import BaseForecaster
from netgravity.forecasting.schemas import AccuracyMetrics

logger = logging.getLogger(__name__)

#: Smallest training prefix worth fitting on. Below this the fold measures the
#: cold-start prior rather than the engine under test.
MIN_TRAIN_PERIODS = 6

#: Periods scored per fold. One step keeps folds independent and maximises how
#: many fit into a short history.
_TEST_HORIZON = 1


def backtest(
    engine: BaseForecaster,
    quantities: Sequence[float],
    *,
    folds: int = 3,
    min_train: int = MIN_TRAIN_PERIODS,
) -> Optional[AccuracyMetrics]:
    """
    Score an engine by rolling-origin evaluation.

    Args:
        engine: The engine to measure. Re-fitted from scratch on each fold, so
            no information from the test period can leak into the fit.
        quantities: The full observed history.
        folds: Maximum folds. Fewer run when the history is too short.
        min_train: Minimum training prefix.

    Returns:
        Measured error, or `None` when the history cannot support even one
        fold. `None` means "not measured" — never "measured, and it was fine".
    """
    arr = np.asarray(quantities, dtype=np.float64)
    n = arr.size

    usable = min(folds, n - min_train)
    if usable < 1:
        logger.debug(
            "forecasting.backtest.skipped n=%d min_train=%d — history too short",
            n, min_train,
        )
        return None

    actuals: List[float] = []
    predictions: List[float] = []
    naive: List[float] = []

    for i in range(usable):
        # Walk the origin forward; the last fold trains on everything but the
        # final observation.
        split = n - usable + i
        train = arr[:split]
        actual = float(arr[split])

        try:
            output = engine.fit_predict(train, _TEST_HORIZON)
        except Exception as exc:  # noqa: BLE001 - a failed fold is data, not a crash
            logger.warning(
                "forecasting.backtest.fold_failed engine=%s split=%d error=%s",
                engine.name, split, exc,
            )
            continue

        if not output.points:
            continue

        predictions.append(float(output.points[0].mean))
        actuals.append(actual)
        # Naive-1: last observed value. The benchmark MASE is scaled against —
        # a model that cannot beat "tomorrow looks like today" is not earning
        # its complexity.
        naive.append(float(train[-1]))

    if not actuals:
        return None

    a = np.asarray(actuals, dtype=np.float64)
    p = np.asarray(predictions, dtype=np.float64)
    errors = np.abs(a - p)

    mae = float(np.mean(errors))
    rmse = float(np.sqrt(np.mean((a - p) ** 2)))

    # WAPE: total absolute error over total actual volume. Defined whenever the
    # series moved at all, unlike MAPE.
    total_actual = float(np.sum(np.abs(a)))
    wape = float(np.sum(errors) / total_actual) if total_actual > 1e-9 else None

    naive_errors = np.abs(a - np.asarray(naive, dtype=np.float64))
    naive_mae = float(np.mean(naive_errors))
    # A flat series makes the naive benchmark perfect, so the ratio is
    # undefined rather than infinite.
    mase = float(mae / naive_mae) if naive_mae > 1e-9 else None

    return AccuracyMetrics(
        mae=round(mae, 6),
        rmse=round(rmse, 6),
        wape=round(wape, 6) if wape is not None else None,
        mase=round(mase, 6) if mase is not None else None,
        n_folds=len(actuals),
        n_observations=n,
        method="ROLLING_ORIGIN",
    )


__all__ = ["backtest", "MIN_TRAIN_PERIODS"]
