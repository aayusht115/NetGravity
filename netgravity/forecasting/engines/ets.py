"""
Holt's linear trend exponential smoothing, with a damped trend.

Level and trend are smoothed with constants α and β chosen by grid search on
in-sample SSE. The trend is damped by φ = 0.95 per period so a forecast does not
extrapolate a straight line indefinitely — an undamped trend fitted to eight
periods of growth will happily predict a market ten times its size by period 40.

Prediction intervals come from the residual standard error scaled by horizon,
which widens the band the further out the forecast goes.

Ported from the source repository with the algorithm intact. Changed: the
in-sample SSE grid search is documented as such (it selects smoothing constants,
it is NOT out-of-sample validation and must not be read as accuracy), the
hardcoded confidence score is gone, and the fitted parameters are returned.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import numpy as np

from netgravity.forecasting.engines.base import BaseForecaster, EngineOutput
from netgravity.forecasting.schemas import ForecastPoint

_Z90 = 1.282

#: Grid searched for the smoothing constants. Coarse on purpose: a finer grid
#: fits in-sample noise more closely without forecasting better, and the search
#: is O(|α| × |β| × n) per series.
_ALPHA_GRID: Tuple[float, ...] = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9)
_BETA_GRID: Tuple[float, ...] = (0.0, 0.05, 0.1, 0.2, 0.3)

#: Trend damping. Below 1 so the trend contribution converges rather than
#: compounding over a long horizon.
_PHI = 0.95


def _sse(arr: np.ndarray, alpha: float, beta: float) -> float:
    """One-step-ahead in-sample squared error for a parameter pair."""
    level = float(arr[0])
    trend = float(arr[1] - arr[0]) if arr.size > 1 else 0.0
    total = 0.0
    for t in range(1, arr.size):
        err = float(arr[t]) - (level + trend)
        total += err * err
        last_level = level
        level = alpha * float(arr[t]) + (1.0 - alpha) * (level + trend)
        trend = beta * (level - last_level) + (1.0 - beta) * trend
    return total


class ETSForecaster(BaseForecaster):
    """Holt linear trend smoother with damping and analytic intervals."""

    min_history: int = 4

    def __init__(self, damped: bool = True) -> None:
        self.damped = damped

    @property
    def name(self) -> str:
        return "Holt_ETS_Damped" if self.damped else "Holt_ETS"

    def fit_predict(self, quantities: Sequence[float], horizon: int) -> EngineOutput:
        arr = np.asarray(quantities, dtype=np.float64)

        if arr.size < 2:
            mean_val = float(arr[0]) if arr.size else 0.0
            std_val = mean_val * 0.2
            return EngineOutput(
                points=_flat_band(mean_val, std_val, horizon),
                diagnostics={"degenerate": "fewer than two observations"},
            )

        # ---- fit -----------------------------------------------------------
        # Grid search on IN-SAMPLE error. This picks smoothing constants; it is
        # not a measure of forecast accuracy, and nothing downstream may read it
        # as one. Out-of-sample error comes from `validation.backtest`.
        best_alpha, best_beta, best_sse = 0.3, 0.1, float("inf")
        for alpha in _ALPHA_GRID:
            for beta in _BETA_GRID:
                sse = _sse(arr, alpha, beta)
                if sse < best_sse:
                    best_alpha, best_beta, best_sse = alpha, beta, sse

        # ---- run forward with the chosen parameters -------------------------
        level = float(arr[0])
        trend = float(arr[1] - arr[0])
        residuals: List[float] = []
        for t in range(1, arr.size):
            residuals.append(float(arr[t]) - (level + trend))
            last_level = level
            level = best_alpha * float(arr[t]) + (1.0 - best_alpha) * (level + trend)
            trend = best_beta * (level - last_level) + (1.0 - best_beta) * trend

        sigma_e = float(np.std(residuals, ddof=1)) if len(residuals) > 1 else float(np.std(arr))
        # Floor the residual SD at 5% of the mean. A perfectly-fitted straight
        # line has zero residuals, and a zero-width interval on a forecast is a
        # claim of certainty no smoother has earned.
        sigma_e = max(sigma_e, float(np.mean(arr)) * 0.05)

        phi = _PHI if self.damped else 1.0
        points: List[ForecastPoint] = []
        for h in range(1, horizon + 1):
            if self.damped:
                damped_trend = trend * sum(phi ** i for i in range(1, h + 1))
                y = max(0.0, float(level + damped_trend))
            else:
                y = max(0.0, float(level + h * trend))

            # Variance grows with the horizon: each step compounds the level and
            # trend errors.
            widen = math.sqrt(
                1.0 + (h - 1) * (best_alpha ** 2 + best_alpha * best_beta * h)
            )
            sigma_h = float(sigma_e * widen)
            points.append(ForecastPoint(
                period=h, mean=y, std_dev=sigma_h,
                p10=max(0.0, y - _Z90 * sigma_h), p50=y, p90=y + _Z90 * sigma_h,
            ))

        return EngineOutput(
            points=points,
            diagnostics={
                "alpha": best_alpha, "beta": best_beta, "phi": phi,
                "residual_sd": round(sigma_e, 6),
                "in_sample_sse": round(best_sse, 6),
            },
        )


def _flat_band(mean_val: float, std_val: float, horizon: int) -> List[ForecastPoint]:
    """A constant forecast with a symmetric band — the degenerate-history case."""
    return [
        ForecastPoint(
            period=h, mean=mean_val, std_dev=std_val,
            p10=max(0.0, mean_val - _Z90 * std_val), p50=mean_val,
            p90=mean_val + _Z90 * std_val,
        )
        for h in range(1, horizon + 1)
    ]


__all__ = ["ETSForecaster"]
