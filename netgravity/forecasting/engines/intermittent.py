"""
Croston / Syntetos-Boylan forecasting for intermittent and lumpy demand.

Croston (1972) splits a sporadic series into two smoothed quantities — the size
of demand when it occurs, and the interval between occurrences — and forecasts
the rate as size ÷ interval. Forecasting the raw series instead would average
the zeros into every period and systematically over-forecast the gaps.

Syntetos-Boylan (2005) corrects Croston's known positive bias by deflating the
rate by (1 − α/2). SBA is the default here for that reason.

Ported from the source repository with the algorithm intact — it was correctly
implemented. What changed: the hardcoded confidence score is gone, fitted
parameters are returned as diagnostics rather than discarded, and the quantile
construction is documented, because a zero-inflated band is not the normal band
the other engines produce and the difference matters downstream.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from netgravity.forecasting.engines.base import BaseForecaster, EngineOutput
from netgravity.forecasting.schemas import ForecastPoint

#: Normal deviate for the 10th/90th percentile.
_Z90 = 1.282


class IntermittentForecaster(BaseForecaster):
    """Croston / SBA forecaster for sporadic demand."""

    #: Croston needs at least one demand occurrence and something to smooth.
    min_history: int = 3

    def __init__(self, method: str = "SBA", alpha: float = 0.15) -> None:
        """
        Args:
            method: "SBA" (default, bias-corrected) or "CROSTON".
            alpha:  Smoothing constant. 0.15 is the source default and sits in
                    the 0.1–0.2 range the literature recommends for
                    intermittent series, where a larger value chases noise.
        """
        self.method = method.upper()
        if self.method not in ("SBA", "CROSTON"):
            raise ValueError(f"method must be SBA or CROSTON, got '{method}'")
        self.alpha = min(max(alpha, 0.01), 0.99)

    @property
    def name(self) -> str:
        return f"{self.method}_Intermittent"

    def fit_predict(self, quantities: Sequence[float], horizon: int) -> EngineOutput:
        arr = np.asarray(quantities, dtype=np.float64)

        if arr.size == 0 or not np.any(arr > 0):
            # Never any demand. Zero is the CORRECT forecast here, and is a
            # measured statement about a series that has been zero throughout —
            # not the "missing became zero" substitution the status enum exists
            # to prevent elsewhere.
            return EngineOutput(
                points=[
                    ForecastPoint(period=h, mean=0.0, std_dev=0.0,
                                  p10=0.0, p50=0.0, p90=0.0)
                    for h in range(1, horizon + 1)
                ],
                diagnostics={"all_zero": True},
            )

        # ---- Croston state ------------------------------------------------
        first_nz = int(np.argmax(arr > 0))
        z = float(arr[first_nz])       # smoothed demand size
        p = float(first_nz + 1)        # smoothed inter-arrival interval
        gap = 1                        # periods since the last occurrence

        for t in range(first_nz + 1, arr.size):
            d = float(arr[t])
            if d > 0:
                z = self.alpha * d + (1.0 - self.alpha) * z
                p = self.alpha * gap + (1.0 - self.alpha) * p
                gap = 1
            else:
                gap += 1

        p = max(p, 1.0)
        z = max(z, 0.0)

        if self.method == "SBA":
            # Croston's estimator is biased high; SBA deflates it by (1 − α/2).
            sba_factor = max(0.0, 1.0 - (self.alpha / 2.0))
            rate = sba_factor * (z / p)
        else:
            rate = z / p

        std_dev = float(np.std(arr, ddof=1)) if arr.size > 1 else float(rate * 0.5)

        # ---- Zero-inflated quantiles --------------------------------------
        # The band is NOT symmetric, deliberately. For a series that is zero
        # four periods in five, the 10th percentile is zero — not "the mean
        # minus 1.28σ", which would be a negative number clipped to zero and
        # would imply a spread the data does not have.
        prob_zero = max(0.0, min(1.0, 1.0 - (1.0 / p)))

        p10 = 0.0 if prob_zero >= 0.10 else max(0.0, rate - _Z90 * std_dev)
        p50 = 0.0 if prob_zero >= 0.50 else max(0.0, rate)
        # When demand DOES occur it arrives at size ≈ z, so the upper tail is
        # anchored on the occurrence size rather than on the deflated rate.
        p90 = max(rate, z + _Z90 * (std_dev if std_dev > 0 else z * 0.3))

        points = [
            ForecastPoint(period=h, mean=float(rate), std_dev=float(std_dev),
                          p10=float(p10), p50=float(p50), p90=float(p90))
            for h in range(1, horizon + 1)
        ]
        return EngineOutput(
            points=points,
            diagnostics={
                "method": self.method, "alpha": self.alpha,
                "smoothed_size_z": round(z, 6),
                "smoothed_interval_p": round(p, 6),
                "rate": round(rate, 6), "prob_zero": round(prob_zero, 6),
            },
        )


__all__ = ["IntermittentForecaster"]
