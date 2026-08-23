"""
Cold-start forecasting for series too short to model.

An exponentially-weighted mean of what little history exists, with an
uncertainty band that widens as √h. It is a *prior*, not a fitted model: with
five observations there is nothing to fit, and pretending otherwise produces a
confident-looking number with no support under it.

    ── On the name ────────────────────────────────────────────────────────
    The source repository called this `FoundationZeroShotForecaster` and
    reported `engine_name` as "Google_TimesFM_ZeroShot" or
    "Amazon_Chronos_ZeroShot" whenever the corresponding package was merely
    IMPORTABLE — while always running this same weighted mean. Neither library
    was ever called; there was no code path to either.

    That is a provenance defect rather than a naming quibble. A forecast
    attributed to TimesFM carries the credibility of a pre-trained foundation
    model, and the whole point of `ForecastProvenance` is that a reader can
    trust what produced a number. So the adapter is gone and the engine reports
    what it actually is. Wiring a real foundation model remains open, and is
    recorded as a gap rather than implied by a string.
"""

from __future__ import annotations

from typing import List, Sequence

import numpy as np

from netgravity.forecasting.engines.base import BaseForecaster, EngineOutput
from netgravity.forecasting.schemas import ForecastPoint

_Z90 = 1.282


class ColdStartForecaster(BaseForecaster):
    """Exponentially-weighted prior for short histories."""

    #: Runs on anything, including a single observation. It is the fallback the
    #: selector reaches for when no fitted model is defensible.
    min_history: int = 1

    def __init__(self, decay: float = 1.0, horizon_widening: float = 0.15) -> None:
        """
        Args:
            decay: Exponential weighting strength across the history. 1.0 gives
                the most recent observation roughly e× the weight of the oldest.
            horizon_widening: How fast the band widens per √period.
        """
        self.decay = decay
        self.horizon_widening = horizon_widening

    @property
    def name(self) -> str:
        return "ColdStart_WeightedPrior"

    def fit_predict(self, quantities: Sequence[float], horizon: int) -> EngineOutput:
        arr = np.asarray(quantities, dtype=np.float64)
        n = arr.size

        if n == 0:
            # No history at all. Zero with zero spread would assert certainty
            # about a series nothing is known about, so the service refuses this
            # case upstream with INSUFFICIENT_HISTORY and never calls here —
            # this branch exists only so direct callers get something inert.
            return EngineOutput(
                points=[
                    ForecastPoint(period=h, mean=0.0, std_dev=0.0,
                                  p10=0.0, p50=0.0, p90=0.0)
                    for h in range(1, horizon + 1)
                ],
                diagnostics={"empty_history": True},
            )

        weights = np.exp(np.linspace(-self.decay, 0.0, n))
        weights /= weights.sum()
        centre = float(np.dot(weights, arr))

        hist_sd = float(np.std(arr, ddof=1)) if n > 1 else centre * 0.35
        # Floor the spread at a quarter of the level. A two-point history that
        # happens to be flat has near-zero sample SD, and a narrow band on two
        # observations is a confidence nothing supports.
        base_sigma = max(hist_sd, centre * 0.25)

        points: List[ForecastPoint] = []
        for h in range(1, horizon + 1):
            sigma_h = float(base_sigma * (1.0 + self.horizon_widening * np.sqrt(h)))
            points.append(ForecastPoint(
                period=h, mean=centre, std_dev=sigma_h,
                p10=max(0.0, centre - _Z90 * sigma_h), p50=centre,
                p90=centre + _Z90 * sigma_h,
            ))

        return EngineOutput(
            points=points,
            diagnostics={
                "weighted_mean": round(centre, 6),
                "base_sigma": round(base_sigma, 6),
                "n_observations": n,
                "is_prior_not_fit": True,
            },
        )


__all__ = ["ColdStartForecaster"]
