"""
Deterministic structural-break detection.

Answers one question about an observed demand series:

    "Has the statistical behaviour changed enough that forecasting from the
     whole history would describe a regime the series has already left?"

Nothing here forecasts. It reads history and returns a typed verdict; deciding
what to do about that verdict is `service.py`'s job.

── Why this exists ────────────────────────────────────────────────────────────
Measured on this codebase's own engines, a *recent* level shift does not merely
degrade the forecast — it makes it diverge. With forty-eight periods around 100
followed by two periods around 200, the pattern-selected quantile engine
forecasts a mean absolute error of **10,237 units against a naive-1 error of
7.8**, and its twelve-step path runs to five figures. The cause is structural
rather than statistical: `QuantileForecaster` fits autoregressive lags and then
forecasts recursively, appending its own median back onto the working history.
A level jump inside the lag window is read as an explosive AR coefficient, and
the recursion compounds it.

So the weakness this module addresses is not the under-forecasting one might
expect. Under-forecasting is what a *smoother* does after a break; an
autoregressive model tracks the new level immediately and then overshoots. Both
are the same underlying fault — a model fitted across two regimes describes
neither — but only one of them is unbounded, and it is the one this codebase
actually has. The measurements are in `docs/forecasting_structural_break.md`.

── Method ─────────────────────────────────────────────────────────────────────
A sup-F (Quandt–Andrews) scan for a level shift, tested against a null that
already contains the things most easily mistaken for one.

    null          y_t = a + b·t + c·t² + d·sin(2πt/s) + e·cos(2πt/s) + ε_t
    alternative   y_t = null + δ·1{t ≥ τ} + ε_t

    F(τ) = (RSS_null − RSS_alt(τ)) / (RSS_alt(τ) / (n − k))
    supF = max over admissible τ

Putting smooth trend and seasonality in the *null* is what makes the test
discriminating rather than merely sensitive. A trending series is explained by
`b`, an accelerating one by `c`, and a seasonal one by `d, e`, so none of them
buys an improvement by adding a step and none trips the test. What does trip it
is a discontinuity the smooth terms cannot absorb — which is the definition
being tested for.

The quadratic term is not decoration. With a purely linear null, compound
growth — a demand base rising 2.5% a month — was reported as a structural break
on **77.5%** of realisations, because a straight line cannot follow a curve and
a step absorbs the residual. Admitting curvature into the null took that to
17.5%, at no cost to true detection.

Scanning τ means the statistic is a supremum, not a single F, so its null
distribution is not F(1, n−k) and the usual tables do not apply. The threshold
used is Andrews' (1993) asymptotic sup-Wald critical value for one tested
parameter under 15% trimming. That value is asymptotic and assumes trimming this
implementation defines slightly differently (a minimum segment length, not a
fixed fraction), so it is treated as a calibrated threshold rather than an exact
5% test — and the *measured* false-positive rate on the ten benchmark patterns
is reported in the documentation instead of being asserted from theory.

── Materiality is judged on δ, not on segment means ───────────────────────────
Three further gates keep statistically real but operationally meaningless
breaks out. The step must be large relative to the model noise it sits in
(`MIN_SIGMA_SHIFT`), large relative to the level itself (`MIN_RELATIVE_SHIFT`),
and large relative to how far the series routinely swings on its own
(`MIN_SWING_SIGMA`). Each catches a different impostor, and each was added
because something got through without it.

Both are measured on **δ, the fitted step coefficient** — the jump net of trend
and seasonality — rather than on the difference between segment means. The
distinction is what remains of the false-positive problem after the quadratic
null. On a growing series the two segment means differ mostly because of the
trend, so a mean-difference gate leaks exactly where the trend is steepest; δ is
the discontinuity with the smooth part already removed. Switching the gate from
segment means to δ took the residual false-positive rate on every non-break
benchmark pattern to **zero**, with true detection unchanged at 100% and located
at the correct period.

Segment means are still reported, because `pre_break_level` and
`post_break_level` are what a reader wants to see. They are just not what the
decision is made on.

Intermittent and lumpy series are refused outright. A level-shift test on a
series that is zero four periods in five is measuring the arrival process, not
the level, and Croston already handles the regime change that matters there.

No LLM, no external call, no randomness, no future observation. The detector
sees a prefix of history and nothing else, so running it at forecast time and
running it inside a backtest fold give the same answer for the same input.
"""

from __future__ import annotations

import logging
import math
from enum import Enum
from typing import List, Optional, Sequence, Tuple

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from netgravity.forecasting.characterizer import compute_demand_metrics
from netgravity.forecasting.schemas import DemandPattern

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Andrews (1993) asymptotic sup-Wald critical value, one tested parameter,
#: 15% trimming, 5% nominal size. Used as a calibrated threshold — see the
#: module docstring on why it is not claimed as an exact test.
SUP_F_THRESHOLD: float = 8.85

#: The same table at 1%. Reported as evidence strength, not used as a gate.
SUP_F_STRONG: float = 12.35

#: Shortest admissible segment on either side of a candidate break.
#:
#: This is the single most important false-positive guard, and it is what
#: separates a structural break from a spike: one unusual observation cannot
#: form a segment, so it cannot be a regime. Four is the smallest post-break
#: window any engine here can fit on (`ETSForecaster.min_history`), which makes
#: it the smallest window worth *detecting* — a break too recent to forecast
#: from is not actionable.
MIN_SEGMENT: int = 4

#: Below this there is not enough series to distinguish a break from its
#: alternatives. With a trend and a seasonal harmonic in the null, twelve
#: observations against six fitted parameters is already thin.
MIN_HISTORY_FOR_DETECTION: int = 12

#: The fitted step δ must exceed this many residual standard deviations of the
#: break model. Stops a long, quiet series from reporting a break on a change
#: smaller than the noise it sits in.
#:
#: Three, not two, on measurement: at 2.0 the compound-growth and high-variance
#: benchmark patterns still produced 20% and 10% false positives respectively.
#: At 3.0 both go to zero while the true break is still detected on 100% of
#: realisations at the correct period, so the stricter gate costs nothing that
#: was measurable here.
MIN_SIGMA_SHIFT: float = 3.0

#: δ must also exceed this fraction of the pre-break level. Significance is not
#: materiality: a 3% move measured over sixty clean periods is real and not
#: worth discarding history for.
MIN_RELATIVE_SHIFT: float = 0.15

#: δ must ALSO exceed this many pooled WITHIN-SEGMENT standard deviations.
#:
#: A different question from `MIN_SIGMA_SHIFT`, and the two are both needed.
#: That one asks whether the step is distinguishable from the model's residual
#: noise; this one asks whether it is large compared with how much the series
#: routinely moves on its own.
#:
#: The case that forced it: a seasonal series whose AMPLITUDE changes — swings
#: of ±20 becoming swings of ±70, with the mean untouched. The null model
#: carries a constant-amplitude harmonic, so it cannot represent that, and the
#: residual it leaves is absorbed by a step placed near a seasonal trough. The
#: detector reported a −61 unit "level shift" at period 56 on 100% of
#: realisations, the adaptive layer then forecast a flat level from five
#: observations of one seasonal phase, and MASE went from 1.42 to 4.98. It was
#: the worst regression the feature produced.
#:
#: Measured against the series' own swing the step is only ~3σ, because a
#: series that oscillates by ±70 moving 61 units is doing what it always does.
#: At 3.5 that scenario is refused on 100% of realisations, every genuine level
#: shift is still detected on 100%, and the residual false-positive rate on
#: every non-break benchmark pattern reaches zero — 3.5 is the smallest
#: threshold at which all of that holds simultaneously.
MIN_SWING_SIGMA: float = 3.5

#: Seasonal cycle assumed by the null model, matching `QuantileForecaster`.
SEASONAL_PERIOD: int = 12


class BreakKind(str, Enum):
    """What changed."""
    LEVEL_SHIFT = "LEVEL_SHIFT"
    NONE = "NONE"


class DetectionStatus(str, Enum):
    """
    Why the detector returned what it did.

    Distinguishes "looked, found nothing" from "declined to look", which are
    different facts and lead to different follow-up.
    """
    DETECTED = "DETECTED"
    NO_BREAK = "NO_BREAK"
    #: Series shorter than `MIN_HISTORY_FOR_DETECTION`.
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    #: Intermittent or lumpy — a level-shift test does not apply.
    PATTERN_NOT_APPLICABLE = "PATTERN_NOT_APPLICABLE"
    #: Statistic cleared the threshold but the shift was too small to act on.
    BELOW_MATERIALITY = "BELOW_MATERIALITY"
    #: The regression was singular; no verdict is claimed.
    NOT_COMPUTABLE = "NOT_COMPUTABLE"


class ChangePointResult(BaseModel):
    """
    The detector's verdict on one series.

    Deliberately carries no probability. The sup-F statistic has a non-standard
    null distribution under a scanned break date, so a p-value derived from an
    F table would be wrong, and inventing one would be exactly the "confidence
    that is really a literal" failure the rest of this package exists to avoid.
    `sup_f` and `threshold` are reported instead: both are measurable, and a
    reader can see how far past the line the evidence went.
    """
    detected: bool
    status: DetectionStatus
    kind: BreakKind = BreakKind.NONE

    #: Index into the supplied history of the FIRST observation of the new
    #: regime. `history[change_index:]` is the post-break segment.
    change_index: Optional[int] = None
    #: Same location as a 1-based period number, for reading alongside a plot.
    change_period: Optional[int] = None

    #: Mean of each segment. Reported because it is what a reader wants to see;
    #: NOT what the materiality gates are applied to — see `magnitude`.
    pre_break_level: Optional[float] = None
    post_break_level: Optional[float] = None

    #: The fitted step coefficient δ: the signed discontinuity in demand units
    #: with trend and seasonality already removed. This, not the difference of
    #: the segment means, is the quantity the gates below are measured on.
    magnitude: Optional[float] = None
    #: |δ| as a fraction of the pre-break level.
    relative_magnitude: Optional[float] = None
    #: |δ| in residual standard deviations of the fitted break model — is the
    #: step distinguishable from noise?
    sigma_magnitude: Optional[float] = None
    #: |δ| in pooled WITHIN-SEGMENT standard deviations — is the step large
    #: compared with how far this series routinely swings on its own? Small
    #: here and large above means a scale change dressed as a level change,
    #: which is what a seasonal amplitude shift looks like to this test.
    swing_magnitude: Optional[float] = None

    #: The test statistic at the selected break date, and the line it had to
    #: clear. Evidence, not confidence.
    sup_f: Optional[float] = None
    threshold: float = SUP_F_THRESHOLD
    #: True when the statistic also cleared the 1% critical value.
    strong_evidence: bool = False

    detection_method: str = "SUP_F_LEVEL_SHIFT"
    #: Observations available to the detector. Recorded so a verdict can be
    #: reproduced against the exact prefix that produced it.
    n_observations: int = 0
    n_pre_break: Optional[int] = None
    n_post_break: Optional[int] = None

    reason: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Regression helpers
# ---------------------------------------------------------------------------

def _null_design(n: int, seasonal_period: int) -> np.ndarray:
    """
    Smooth trend and, when the series is long enough to identify one, a
    seasonal harmonic.

    Time is scaled to [0, 1] so the quadratic column stays conditioned
    alongside the intercept at any series length.

    The harmonic is included only at a full cycle plus a few residual degrees
    of freedom. Fitting sin/cos to eight periods of a twelve-period cycle
    describes noise, and an over-parameterised null suppresses real breaks by
    absorbing them.
    """
    t = np.arange(n, dtype=np.float64)
    scaled = t / max(1.0, float(n))
    cols: List[np.ndarray] = [np.ones(n), scaled, scaled ** 2]

    if n >= seasonal_period + 4:
        angle = 2.0 * math.pi * (t % seasonal_period) / seasonal_period
        cols.append(np.sin(angle))
        cols.append(np.cos(angle))

    return np.column_stack(cols)


def _fit(X: np.ndarray, y: np.ndarray) -> Optional[Tuple[np.ndarray, float]]:
    """OLS coefficients and residual sum of squares, or None if singular."""
    try:
        beta, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    resid = y - X @ beta
    return beta, float(np.dot(resid, resid))


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class ChangePointDetector:
    """
    Sup-F level-shift detection over a trend-and-seasonality null.

    Stateless and deterministic: the same history always yields the same
    verdict, so a detection made inside a backtest fold is the same detection
    that would have been made at that point in real time.
    """

    #: Names the test that actually runs, for provenance. Same rule as
    #: `BaseForecaster.name` — it must never name a method the code does not
    #: implement.
    detection_method: str = "SUP_F_LEVEL_SHIFT"

    def __init__(
        self,
        *,
        min_segment: int = MIN_SEGMENT,
        min_history: int = MIN_HISTORY_FOR_DETECTION,
        threshold: float = SUP_F_THRESHOLD,
        min_sigma_shift: float = MIN_SIGMA_SHIFT,
        min_relative_shift: float = MIN_RELATIVE_SHIFT,
        min_swing_sigma: float = MIN_SWING_SIGMA,
        seasonal_period: int = SEASONAL_PERIOD,
    ) -> None:
        if min_segment < 2:
            raise ValueError(
                f"min_segment must be at least 2; got {min_segment}. A segment "
                f"of one observation is a spike, not a regime."
            )
        self.min_segment = min_segment
        self.min_history = min_history
        self.threshold = threshold
        self.min_sigma_shift = min_sigma_shift
        self.min_relative_shift = min_relative_shift
        self.min_swing_sigma = min_swing_sigma
        self.seasonal_period = seasonal_period

    # ------------------------------------------------------------------

    def detect(self, quantities: Sequence[float]) -> ChangePointResult:
        """
        Test one history for a structural break.

        Args:
            quantities: Observed demand in period order. Only observations
                actually supplied are used — there is no way for this function
                to reach future data, which is what makes it safe to call from
                inside a backtest fold.

        Returns:
            A verdict, always. Never raises for ordinary data.
        """
        arr = np.asarray(quantities, dtype=np.float64)
        n = int(arr.size)

        def refuse(status: DetectionStatus, reason: str, **extra) -> ChangePointResult:
            return ChangePointResult(
                detected=False, status=status, threshold=self.threshold,
                n_observations=n, reason=reason, **extra,
            )

        if n < self.min_history:
            return refuse(
                DetectionStatus.INSUFFICIENT_HISTORY,
                f"{n} observations; at least {self.min_history} are needed to "
                f"separate a level shift from a trend or a seasonal cycle.",
            )

        if n < 2 * self.min_segment:
            return refuse(
                DetectionStatus.INSUFFICIENT_HISTORY,
                f"{n} observations cannot form two segments of "
                f"{self.min_segment}.",
            )

        # ---- pattern gate --------------------------------------------------
        pattern = compute_demand_metrics(arr).pattern
        if pattern in (DemandPattern.INTERMITTENT, DemandPattern.LUMPY):
            return refuse(
                DetectionStatus.PATTERN_NOT_APPLICABLE,
                f"series is {pattern.value}: a level-shift test on demand that "
                f"is mostly zeros measures the arrival process rather than the "
                f"level, and Croston already adapts to changes in it.",
            )

        # ---- sup-F scan ----------------------------------------------------
        X0 = _null_design(n, self.seasonal_period)
        null_fit = _fit(X0, arr)
        if null_fit is None:
            return refuse(
                DetectionStatus.NOT_COMPUTABLE,
                "the null regression was singular; no verdict is claimed.",
            )
        _, rss0 = null_fit

        if rss0 <= 1e-12:
            # A smooth trend-and-season model already explains the series
            # exactly. There is no residual for a break to account for.
            return refuse(
                DetectionStatus.NO_BREAK,
                "a smooth trend-and-seasonality model fits the series exactly; "
                "there is no unexplained variation for a level shift to "
                "explain.",
            )

        k_alt = X0.shape[1] + 1
        df_resid = n - k_alt
        if df_resid < 1:
            return refuse(
                DetectionStatus.INSUFFICIENT_HISTORY,
                f"{n} observations leave no residual degrees of freedom once a "
                f"trend, a seasonal cycle and a step are fitted.",
            )

        best_f = -1.0
        best_tau: Optional[int] = None
        best_delta = 0.0
        best_resid_sd = 0.0

        for tau in range(self.min_segment, n - self.min_segment + 1):
            step = np.zeros(n, dtype=np.float64)
            step[tau:] = 1.0

            alt_fit = _fit(np.column_stack([X0, step]), arr)
            if alt_fit is None:
                continue
            beta1, rss1 = alt_fit
            if rss1 <= 0.0:
                continue

            f_stat = ((rss0 - rss1) / 1.0) / (rss1 / df_resid)
            if f_stat > best_f:
                best_f = f_stat
                best_tau = tau
                # The step coefficient is the last column by construction.
                best_delta = float(beta1[-1])
                best_resid_sd = math.sqrt(rss1 / df_resid)

        if best_tau is None:
            return refuse(
                DetectionStatus.NOT_COMPUTABLE,
                "no admissible break date produced a computable statistic.",
            )

        pre = arr[:best_tau]
        post = arr[best_tau:]
        pre_level = float(np.mean(pre))
        post_level = float(np.mean(post))

        # Materiality is judged on the fitted step, not on the difference of
        # the segment means: on a trending series most of that difference is
        # the trend, and gating on it leaks precisely where the trend is
        # steepest. See the module docstring.
        magnitude = best_delta
        scale = max(best_resid_sd, abs(pre_level) * 1e-3, 1e-9)
        sigma_magnitude = abs(magnitude) / scale
        relative = abs(magnitude) / max(abs(pre_level), 1e-9)

        # Pooled within-segment spread: how much this series moves anyway.
        var_pre = float(np.var(pre, ddof=1)) if pre.size > 1 else 0.0
        var_post = float(np.var(post, ddof=1)) if post.size > 1 else 0.0
        pooled = math.sqrt(
            ((pre.size - 1) * var_pre + (post.size - 1) * var_post)
            / max(1, pre.size + post.size - 2)
        )
        swing_magnitude = abs(magnitude) / max(pooled, abs(pre_level) * 1e-3, 1e-9)

        common = dict(
            kind=BreakKind.LEVEL_SHIFT,
            change_index=best_tau,
            change_period=best_tau + 1,
            pre_break_level=round(pre_level, 6),
            post_break_level=round(post_level, 6),
            magnitude=round(magnitude, 6),
            relative_magnitude=round(relative, 6),
            sigma_magnitude=round(sigma_magnitude, 6),
            swing_magnitude=round(swing_magnitude, 6),
            sup_f=round(float(best_f), 6),
            threshold=self.threshold,
            strong_evidence=bool(best_f >= SUP_F_STRONG),
            n_observations=n,
            n_pre_break=int(pre.size),
            n_post_break=int(post.size),
        )

        if best_f < self.threshold:
            return ChangePointResult(
                detected=False, status=DetectionStatus.NO_BREAK,
                reason=(
                    f"strongest candidate at period {best_tau + 1} scored "
                    f"sup-F {best_f:.2f}, below the {self.threshold:.2f} "
                    f"threshold; a trend-and-seasonality model explains the "
                    f"series as well as one with a step in it."
                ),
                **common,
            )

        # ---- materiality ----------------------------------------------------
        if sigma_magnitude < self.min_sigma_shift:
            return ChangePointResult(
                detected=False, status=DetectionStatus.BELOW_MATERIALITY,
                reason=(
                    f"step of {magnitude:+.2f} at period {best_tau + 1} "
                    f"(sup-F {best_f:.2f}), but that is only "
                    f"{sigma_magnitude:.2f} residual standard deviations, "
                    f"under the {self.min_sigma_shift:.2f} required. "
                    f"Statistically visible, operationally inside the noise."
                ),
                **common,
            )

        if relative < self.min_relative_shift:
            return ChangePointResult(
                detected=False, status=DetectionStatus.BELOW_MATERIALITY,
                reason=(
                    f"step of {magnitude:+.2f} at period {best_tau + 1} "
                    f"(sup-F {best_f:.2f}) is {relative * 100:.1f}% of the "
                    f"prior level, under the "
                    f"{self.min_relative_shift * 100:.0f}% required. Real, but "
                    f"not large enough to justify discarding history."
                ),
                **common,
            )

        if swing_magnitude < self.min_swing_sigma:
            # Large against the model's residual, ordinary against the series'
            # own movement. That combination is the signature of a change in
            # SCALE rather than in level — a seasonal amplitude shift being the
            # case that motivated this gate — and a level-shift detector has no
            # business claiming it.
            return ChangePointResult(
                detected=False, status=DetectionStatus.BELOW_MATERIALITY,
                reason=(
                    f"step of {magnitude:+.2f} at period {best_tau + 1} "
                    f"(sup-F {best_f:.2f}) is only {swing_magnitude:.2f} "
                    f"within-segment standard deviations, under the "
                    f"{self.min_swing_sigma:.2f} required — this series "
                    f"routinely moves that far on its own. Consistent with a "
                    f"change in variability or seasonal amplitude rather than "
                    f"in level, which this test does not measure."
                ),
                **common,
            )

        logger.info(
            "forecasting.change_point.detected period=%d sup_f=%.2f "
            "level=%.2f->%.2f n_post=%d",
            best_tau + 1, best_f, pre_level, post_level, post.size,
        )
        return ChangePointResult(
            detected=True, status=DetectionStatus.DETECTED,
            reason=(
                f"level stepped {magnitude:+.2f} ({relative * 100:.1f}% of the "
                f"prior level, {sigma_magnitude:.2f} residual SD) at period "
                f"{best_tau + 1}; sup-F {best_f:.2f} against a "
                f"{self.threshold:.2f} threshold, with {post.size} "
                f"observations in the new regime "
                f"(mean {pre_level:.1f} -> {post_level:.1f})."
            ),
            **common,
        )


#: Shared instance. The detector holds no per-call state, so one is safe
#: across concurrent requests.
_DEFAULT = ChangePointDetector()


def detect_change_point(quantities: Sequence[float]) -> ChangePointResult:
    """Detect a structural break using the default thresholds."""
    return _DEFAULT.detect(quantities)


__all__ = [
    "BreakKind",
    "ChangePointDetector",
    "ChangePointResult",
    "DetectionStatus",
    "detect_change_point",
    "MIN_HISTORY_FOR_DETECTION",
    "MIN_RELATIVE_SHIFT",
    "MIN_SEGMENT",
    "MIN_SIGMA_SHIFT",
    "MIN_SWING_SIGMA",
    "SEASONAL_PERIOD",
    "SUP_F_STRONG",
    "SUP_F_THRESHOLD",
]
