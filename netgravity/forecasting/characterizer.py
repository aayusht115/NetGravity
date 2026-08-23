"""
NetGravity — Demand pattern characterisation.

Syntetos-Boylan ADI vs CV² classification (Syntetos, Babai & Gardner 2005),
which decides which engine forecasts a series:

                    CV² < 0.49          CV² ≥ 0.49
    ADI < 1.32      SMOOTH              ERRATIC
    ADI ≥ 1.32      INTERMITTENT        LUMPY

The classification matters because applying a trend model to spare-parts demand
over-forecasts badly: a series that is zero four periods in five has a mean that
describes none of its periods. Croston-family methods exist precisely for that
case, and this module is what routes to them.

Integrated from the source repository essentially unchanged — the arithmetic was
correct, the thresholds are the published ones, and the sample variance uses
`ddof=1` as it should. The changes here are the minimum-history floor being
named rather than inline, and the all-zero case being classified honestly.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from netgravity.forecasting.schemas import (
    CharacterizationMetrics,
    DemandPattern,
    DemandTimeSeries,
)

#: Published Syntetos-Boylan cutoffs. Not tuning parameters — changing them
#: changes which literature the classification refers to.
ADI_CUTOFF: float = 1.32
CV2_CUTOFF: float = 0.49

#: Below this, ADI and CV² are too noisy to classify on. Eight periods is the
#: source repository's floor and is retained: with fewer, a single zero swings
#: ADI across the cutoff.
MIN_SERIES_LENGTH: int = 8


def compute_demand_metrics(quantities: Sequence[float]) -> CharacterizationMetrics:
    """
    Classify a demand series.

    Args:
        quantities: Observed demand in period order.

    Returns:
        The ADI / CV² measurements and the resulting pattern. Short series are
        `COLD_START` — the metrics are still reported, but they describe too
        little data to route on, so the selector sends them to the cold-start
        engine rather than trusting the classification.
    """
    arr = np.asarray(quantities, dtype=np.float64)
    total_periods = int(arr.size)

    if total_periods == 0:
        return CharacterizationMetrics(
            adi=0.0, cv2=0.0, non_zero_periods=0, total_periods=0,
            zero_ratio=1.0, pattern=DemandPattern.COLD_START,
        )

    non_zero = arr[arr > 0]
    non_zero_count = int(non_zero.size)
    zero_ratio = float((total_periods - non_zero_count) / total_periods)

    def _cv2() -> float:
        """Squared coefficient of variation of the NON-ZERO demands."""
        if non_zero_count <= 1:
            return 0.0
        mean_nz = float(np.mean(non_zero))
        if mean_nz <= 1e-9:
            return 0.0
        return float(np.var(non_zero, ddof=1) / (mean_nz ** 2))

    if total_periods < MIN_SERIES_LENGTH:
        return CharacterizationMetrics(
            adi=float(total_periods / max(1, non_zero_count)),
            cv2=_cv2(),
            non_zero_periods=non_zero_count,
            total_periods=total_periods,
            zero_ratio=zero_ratio,
            pattern=DemandPattern.COLD_START,
        )

    if non_zero_count == 0:
        # Never any demand. Classified INTERMITTENT because that is what the
        # routing needs — Croston handles an all-zero series correctly and
        # returns zero, whereas a trend model would extrapolate noise.
        return CharacterizationMetrics(
            adi=float(total_periods), cv2=0.0, non_zero_periods=0,
            total_periods=total_periods, zero_ratio=1.0,
            pattern=DemandPattern.INTERMITTENT,
        )

    adi = float(total_periods / non_zero_count)
    cv2 = _cv2()

    if adi < ADI_CUTOFF:
        pattern = DemandPattern.SMOOTH if cv2 < CV2_CUTOFF else DemandPattern.ERRATIC
    else:
        pattern = DemandPattern.INTERMITTENT if cv2 < CV2_CUTOFF else DemandPattern.LUMPY

    return CharacterizationMetrics(
        adi=adi, cv2=cv2, non_zero_periods=non_zero_count,
        total_periods=total_periods, zero_ratio=zero_ratio, pattern=pattern,
    )


def characterize_series(series: DemandTimeSeries) -> CharacterizationMetrics:
    """Classify a `DemandTimeSeries`."""
    return compute_demand_metrics(series.quantities)


__all__ = [
    "ADI_CUTOFF",
    "CV2_CUTOFF",
    "MIN_SERIES_LENGTH",
    "compute_demand_metrics",
    "characterize_series",
]
