"""
Forecasting engines — the interface every model implements.

An engine takes an ordered sequence of observed quantities and returns forecast
points. That is its whole job.

Deliberately narrow. Engines do NOT build `SeriesForecast`, do not decide
status, do not attach provenance and do not know what a market is — the service
owns all of that. The source implementation had each engine construct its own
result object complete with a hardcoded `confidence_score`, which is how five
different literals ended up masquerading as measured accuracy in five different
files.

An engine also never raises for ordinary data. Too-short history is the
service's decision to make, because "insufficient" depends on the horizon and
the caller, not on the algorithm.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from netgravity.forecasting.schemas import ForecastPoint


@dataclass(frozen=True)
class EngineOutput:
    """
    What an engine returns.

    `diagnostics` carries fitted parameters — smoothing constants, quantile
    coefficients — so a forecast can be reproduced and argued with. Returned
    rather than stored on the engine because engines are shared across
    concurrent requests, and an engine that remembers its last call is a race
    waiting to happen.
    """
    points: List[ForecastPoint]
    diagnostics: Dict[str, Any] = field(default_factory=dict)


class BaseForecaster(ABC):
    """Interface for every forecasting algorithm."""

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Identifier of the algorithm that ACTUALLY RAN.

        Must never name a library that merely happens to be importable. The
        source implementation returned `"Google_TimesFM_ZeroShot"` whenever the
        `timesfm` package was installed, while always running its own weighted
        mean — a forecast attributed to a model that never saw it.
        """

    @property
    def version(self) -> str:
        """Version of this engine's implementation, for reproducibility."""
        return "1.0.0"

    @abstractmethod
    def fit_predict(self, quantities: Sequence[float], horizon: int) -> EngineOutput:
        """
        Fit on observed history and forecast `horizon` periods ahead.

        Args:
            quantities: Observed demand in period order. May contain zeros.
            horizon: Number of future periods, ≥ 1.

        Returns:
            Exactly `horizon` points, numbered 1..horizon.
        """

    #: Minimum observations this engine needs to produce anything meaningful.
    #: The service checks it and returns INSUFFICIENT_HISTORY rather than
    #: letting an engine extrapolate from two points.
    min_history: int = 1


__all__ = ["BaseForecaster", "EngineOutput"]
