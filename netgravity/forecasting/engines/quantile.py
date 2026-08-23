"""
Linear quantile regression, solved exactly as a linear program.

Fits three separate models — the 10th, 50th and 90th conditional quantiles —
against lag, rolling-mean, trend and seasonal features, then forecasts
recursively. Because each quantile is fitted directly rather than derived from a
mean and an assumed distribution, the band it produces reflects the asymmetry
actually present in the data.

Minimising pinball loss is a linear program:

    min  Σ τ·uᵢ + (1−τ)·vᵢ
    s.t. Xβ + u − v = y,   u, v ≥ 0

solved with HiGHS through SciPy — the same solver family the MILP uses, so the
dependency is already present and the result is exact rather than iterative.

Ported from the source repository with the formulation intact. Changed: the
engine no longer claims to be LightGBM (`Quantile_LightGBM_Highs` was the
reported name while LightGBM was never imported, even where installed), the
hardcoded confidence score is gone, and the recursive step reuses the ORIGINAL
feature width rather than recomputing it as the history grows.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import numpy as np
from scipy.optimize import linprog

from netgravity.forecasting.engines.base import BaseForecaster, EngineOutput
from netgravity.forecasting.schemas import ForecastPoint

#: Interquantile width of a standard normal between the 10th and 90th
#: percentiles, used to back out a comparable σ from the fitted band.
_P10_P90_SPREAD = 2.563


class QuantileForecaster(BaseForecaster):
    """Exact linear quantile regression at P10 / P50 / P90."""

    #: Below this there are too few rows after lagging to fit anything.
    min_history: int = 8

    def __init__(self, max_lags: int = 4, seasonal_period: int = 12) -> None:
        self.max_lags = max_lags
        self.seasonal_period = seasonal_period

    @property
    def name(self) -> str:
        return "QuantileRegression_HiGHS"

    # ------------------------------------------------------------------

    #: Non-lag columns in a feature row: intercept, rolling mean, trend, sin,
    #: cos. A row therefore has `lags + _FIXED_FEATURES` columns, and lagging
    #: costs `lags` rows — so the design matrix is only determined when
    #: `n - lags >= lags + _FIXED_FEATURES`, i.e. `n >= 2·lags + 5`.
    _FIXED_FEATURES = 5

    def _n_lags(self, n: int) -> int:
        """
        Deepest lag depth this series can actually support.

        ── The defect this replaces ──────────────────────────────────────────
        The source used `min(max_lags, n // 3)`, which is not the binding
        constraint. On a twelve-period series that gives four lags: eight rows
        against nine features. The LP is underdetermined, `_fit_quantile` takes
        its intercept-only fallback, and all three quantiles collapse to the
        same constant — the series mean.

        The engine did not report any of this. It returned a flat forecast with
        `p10 == p50 == p90`, a zero-width prediction interval, and a hardcoded
        `confidence_score=0.92`. Measured against naive-1 it scored MASE 2.89,
        because it was predicting the mean of a trending series.

        Worse, the selector routes every SMOOTH series with twelve or more
        observations here — so twelve periods, the exact threshold, hit the
        degenerate path by default.

        This picks the largest depth that keeps the system determined, which
        restores an actual quantile fit at twelve periods (three lags: nine rows,
        eight features).
        """
        feasible = (n - self._FIXED_FEATURES) // 2
        return max(1, min(self.max_lags, feasible))

    def _row(self, history: Sequence[float], t: int, lags: int, n: int) -> List[float]:
        """Feature row for position `t`: intercept, lags, rolling mean, trend, season."""
        lag_vals = [float(history[t - i]) for i in range(1, lags + 1)]
        roll_mean = float(np.mean(history[max(0, t - 3):t])) if t > 0 else float(history[0])
        trend = float(t / max(1, n))
        angle = 2.0 * math.pi * (t % self.seasonal_period) / self.seasonal_period
        return [1.0, *lag_vals, roll_mean, trend, math.sin(angle), math.cos(angle)]

    def _design(self, arr: np.ndarray, lags: int) -> Tuple[np.ndarray, np.ndarray]:
        n = arr.size
        rows = [self._row(arr, t, lags, n) for t in range(lags, n)]
        targets = [float(arr[t]) for t in range(lags, n)]
        return np.asarray(rows, dtype=np.float64), np.asarray(targets, dtype=np.float64)

    @staticmethod
    def _fit_quantile(
        X: np.ndarray, y: np.ndarray, tau: float,
    ) -> Tuple[np.ndarray, bool]:
        """
        Exact pinball-loss fit for one quantile.

        Returns:
            `(coefficients, degenerate)`. `degenerate` is True when the fit fell
            back to an intercept-only model — every quantile then predicts the
            same constant and the band collapses. The caller surfaces it in
            diagnostics, because a zero-width prediction interval that looks
            like certainty is exactly the thing not to report silently.
        """
        n_samples, n_features = X.shape
        if n_samples < n_features:
            # Underdetermined: an intercept-only model beats letting the LP pick
            # an arbitrary point on a solution manifold, but it is not a fit.
            beta = np.zeros(n_features)
            beta[0] = float(np.mean(y))
            return beta, True

        c = np.concatenate([
            np.zeros(n_features),
            tau * np.ones(n_samples),
            (1.0 - tau) * np.ones(n_samples),
        ])
        A_eq = np.hstack([X, np.eye(n_samples), -np.eye(n_samples)])
        bounds = [(None, None)] * n_features + [(0.0, None)] * (2 * n_samples)

        res = linprog(c, A_eq=A_eq, b_eq=y, bounds=bounds, method="highs")
        if res.success:
            return np.asarray(res.x[:n_features], dtype=np.float64), False
        # The LP failed. Least squares fits the MEAN, not the quantile, so the
        # band it produces is not a quantile band — flagged as degenerate.
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        return beta, True

    # ------------------------------------------------------------------

    def fit_predict(self, quantities: Sequence[float], horizon: int) -> EngineOutput:
        arr = np.asarray(quantities, dtype=np.float64)
        n = arr.size
        lags = self._n_lags(n)
        X, y = self._design(arr, lags)

        if y.size < 3:
            mean_val = float(np.mean(arr)) if n else 0.0
            std_val = float(np.std(arr)) if n > 1 else mean_val * 0.2
            return EngineOutput(
                points=[
                    ForecastPoint(
                        period=h, mean=mean_val, std_dev=std_val,
                        p10=max(0.0, mean_val - 1.282 * std_val),
                        p50=mean_val, p90=mean_val + 1.282 * std_val,
                    )
                    for h in range(1, horizon + 1)
                ],
                diagnostics={"degenerate": "too few rows after lagging", "lags": lags},
            )

        beta_10, deg_10 = self._fit_quantile(X, y, 0.10)
        beta_50, deg_50 = self._fit_quantile(X, y, 0.50)
        beta_90, deg_90 = self._fit_quantile(X, y, 0.90)
        degenerate = deg_10 or deg_50 or deg_90

        # ---- recursive multi-step ------------------------------------------
        # Each step feeds its own median back in as the next lag. Uncertainty
        # therefore does NOT compound the way it should — see the horizon
        # warning the service attaches beyond a few steps.
        working = [float(v) for v in arr]
        points: List[ForecastPoint] = []

        for h in range(1, horizon + 1):
            t = len(working)
            feat = np.asarray(self._row(working, t, lags, n), dtype=np.float64)

            raw = [
                max(0.0, float(np.dot(feat, beta_10))),
                max(0.0, float(np.dot(feat, beta_50))),
                max(0.0, float(np.dot(feat, beta_90))),
            ]
            # Separately-fitted quantiles can cross on unseen feature values.
            # Sorting restores the ordering the band is supposed to have.
            p10, p50, p90 = sorted(raw)

            sigma = ((p90 - p10) / _P10_P90_SPREAD) if p90 > p10 else p50 * 0.1

            points.append(ForecastPoint(
                period=h, mean=p50, std_dev=float(sigma),
                p10=float(p10), p50=float(p50), p90=float(p90),
            ))
            working.append(p50)

        return EngineOutput(
            points=points,
            diagnostics={
                "lags": lags, "seasonal_period": self.seasonal_period,
                "n_fit_rows": int(y.size), "n_features": int(X.shape[1]),
                "recursive": True,
                # True when the quantile fit collapsed to an intercept. The
                # band is then zero-width and means nothing.
                "degenerate_fit": degenerate,
            },
        )


__all__ = ["QuantileForecaster"]
