"""
Adaptive regime selection after a structural break.

The detector in `change_point.py` answers *whether* the series changed regime.
This module answers the only question that follows:

    "Given that it changed, does forecasting from the new regime alone
     actually beat forecasting from everything?"

That question is settled by measurement, not by assumption. A detected break is
not on its own a reason to throw history away — a break with twenty periods of
new regime behind it may still be forecast better from the full series, because
the extra data buys precision that outweighs the stale prefix. So both
candidates are scored, and the winner is the one that was measurably better on
observations the model had not seen when it was fitted.

── The evaluation, and why the origins are restricted ─────────────────────────
Rolling-origin, as `validation.backtest` does it — but with every origin taken
from **inside the post-break regime**:

    history   |———— pre-break ————|———— post-break ————|
    fold 1                        |train|···|          ← score against ···
    fold 2                        |—train—|···|
    fold 3                        |——train——|···|

    FULL candidate      trains on y[0 : origin]
    RECENT candidate    trains on y[change_index : origin]

Both are scored against the same actuals, so the comparison is paired.

Restricting origins to the new regime is the whole point. A fold scored on a
pre-break period measures how well each candidate describes a regime the series
has already left, and the full-history model wins those by construction — it has
more data about a period that no longer matters. Only the post-break folds
measure the thing being decided.

── Folds are scored over the horizon actually requested ───────────────────────
Each fold scores every step out to `eval_horizon`, not just the first, and
`eval_horizon` is the largest depth the post-break segment can support up to the
horizon the caller asked for.

This matters more than it sounds, and it was not the original design. Scoring
one step ahead, the recent-regime candidate won by a wide and *correct* margin
on a break with eight periods behind it — one-step MAE around 3.5 against 13.4
for full history — and the resulting twelve-step forecast was nonetheless worse
than the unadapted one on 37% of realisations, occasionally by a factor of
three. The cause was visible in the forecast paths: a damped-trend smoother
fitted to eight noisy observations reads the noise as a slope, and a slope is
almost free at one step and ruinous at twelve.

So the window and the model behind it are now measured the way they will be
used. An engine that drifts is charged for drifting.


**No future observation is ever touched.** Every origin is an index into the
history that was supplied, the actual scored against is an observation already
in that history, and each candidate is re-fitted from scratch on a strict prefix
of it. Running this inside a backtest fold and running it at forecast time do
the same arithmetic on the same data, which is what makes the measurement
honest rather than circular. A test asserts it directly.

── When there is not enough new regime to measure ─────────────────────────────
A break detected four periods ago leaves at most one usable fold, and one fold
is an anecdote. Rather than pretend to measure, the decision falls back to a
rule — take the recent regime — and says so, in `basis=RULE`.

That fallback is a judgement, so here is the argument for it. The detector has
already established that a step of at least three residual standard deviations
and at least 15% of the level occurred; the pre-break data is evidence about a
level the series demonstrably no longer sits at. And the failure it avoids is
not symmetric. Measured on this codebase's engines, keeping full history through
a recent break produces mean absolute errors in the thousands, because the
quantile engine reads the jump as an explosive autoregressive coefficient and
compounds it recursively. Using the recent regime on too little data costs
precision; using full history across a fresh break costs correctness. The
fallback is recorded as a rule and not dressed up as evidence, so a reader can
disagree with it.
"""

from __future__ import annotations

import logging
import math
from enum import Enum
from typing import List, Optional, Sequence, Tuple

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from netgravity.forecasting.change_point import ChangePointResult
from netgravity.forecasting.engines.base import BaseForecaster
from netgravity.forecasting.engines.selector import EngineSelector

logger = logging.getLogger(__name__)

#: Shortest training window a recent-regime candidate may be fitted on inside a
#: fold. Two observations is a value and a difference; below that there is
#: nothing for any engine to work with.
MIN_RECENT_TRAIN: int = 3

#: Folds required before the comparison counts as measured. One fold is an
#: anecdote — the same standard `AccuracyMetrics.n_folds` exists to expose.
MIN_FOLDS_FOR_MEASUREMENT: int = 2

#: Slope t-statistic above which the post-break segment is judged to be
#: TRENDING rather than sitting at a new level.
#:
#: Only consulted on the unmeasured `RULE` path, and it is there because the
#: rule's justification depends on it. The argument for taking the recent
#: regime without measuring is that the pre-break data describes "a level the
#: series has left" — which presumes the new regime IS a level. When demand
#: stops being flat and starts ramping, that presumption fails: the detector
#: reports a level shift somewhere in the middle of the ramp, the short window
#: routes to a level-only prior, and the forecast goes flat while the series
#: keeps climbing. Measured on a flat-then-ramping scenario it cost MASE 3.46
#: to 4.20, the only regression the feature produced once seasonal amplitude
#: changes were excluded.
#:
#: Genuine level shifts leave a post-break slope t of about 0.7 (90th
#: percentile 2.0); the ramping scenario sits at 3.4. Where the measurement
#: does run it already reaches the right answer on its own, so this guard is
#: deliberately confined to the branch that cannot measure.
MAX_POST_BREAK_SLOPE_T: float = 2.5

#: Fraction of the requested horizon the folds must be able to score before the
#: comparison is trusted over the rule.
#:
#: A short post-break window can only be scored a few steps ahead, and a
#: two-step measurement is a poor proxy for a twelve-step forecast: the
#: full-history model's lag-1 feature still tracks the new level at two steps
#: and has drifted badly by twelve. Measured across realisations of a break
#: with six new observations, trusting that weak comparison beat the unadapted
#: pipeline on 83% of runs, while applying the rule beat it on 97%. Below this
#: depth the comparison is not evidence about the forecast being made, so the
#: rule stands and `basis` says RULE.
MIN_EVAL_HORIZON_FRACTION: float = 0.5


class RegimeStrategy(str, Enum):
    """Which slice of history the forecast is built from."""
    #: Everything observed. The existing behaviour, and the default.
    FULL_HISTORY = "FULL_HISTORY"
    #: Post-break observations only.
    RECENT_REGIME = "RECENT_REGIME"


class StrategyBasis(str, Enum):
    """How the strategy was arrived at — the part a reader should weigh."""
    #: No break detected; nothing was compared and nothing changed.
    NO_BREAK = "NO_BREAK"
    #: Both candidates were backtested on post-break origins; this one won.
    MEASURED = "MEASURED"
    #: A break was detected but the new regime is too short to score. Chosen by
    #: the documented rule, not by evidence.
    RULE = "RULE"


class RegimeDecision(BaseModel):
    """
    The adaptive choice for one series, and the evidence behind it.

    Carries no probability and no confidence score. `basis` says whether the
    choice was measured or ruled, and the two error figures say by how much —
    which is everything a reader needs to agree or disagree.
    """
    strategy: RegimeStrategy
    basis: StrategyBasis

    #: Index of the first observation used. 0 for full history.
    window_start: int = 0
    #: Observations the forecast will actually be fitted on.
    n_periods_used: int = 0

    #: Measured MAE over post-break origins, averaged across every scored step
    #: of every fold. None when unmeasured — which is not the same as zero, and
    #: never means "it was fine".
    full_history_mae: Optional[float] = None
    recent_regime_mae: Optional[float] = None
    n_folds: int = 0
    #: Steps scored per fold. Reported because a comparison made one step ahead
    #: says much less about a twelve-step forecast than one made twelve steps
    #: ahead, and a reader should be able to tell which they are looking at.
    eval_horizon: int = 0

    #: Best-measured engine for each window, for the audit trail. A regime
    #: change often changes the engine as well as the window.
    full_history_engine: str = ""
    recent_regime_engine: str = ""

    #: The engine the service should deploy, when the measurement pinned one.
    #: Empty when nothing was measured, in which case the service falls back to
    #: its ordinary selection — the unchanged path.
    selected_engine: str = ""

    reason: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def adapted(self) -> bool:
        """True when the forecast departed from the full-history default."""
        return self.strategy is RegimeStrategy.RECENT_REGIME


# ---------------------------------------------------------------------------

def _slope_t(segment: np.ndarray) -> float:
    """
    |t| for the slope of a straight line through a segment.

    A scale-free measure of "is this drifting or is it sitting still", which
    raw slope is not: five units a period is a steep ramp on demand of 100 and
    noise on demand of 10,000. Returns 0.0 when the segment is too short for
    the statistic to mean anything.
    """
    n = int(segment.size)
    if n < 4:
        return 0.0
    x = np.arange(n, dtype=np.float64)
    x = x - x.mean()
    y = segment - segment.mean()
    sxx = float(x @ x)
    if sxx <= 0.0:
        return 0.0
    slope = float(x @ y) / sxx
    residual = y - slope * x
    dof = n - 2
    if dof < 1:
        return 0.0
    se = math.sqrt(float(residual @ residual) / dof / sxx)
    return abs(slope) / max(se, 1e-12)


def _eval_horizon(n: int, first_origin: int, requested: int) -> int:
    """
    Deepest evaluation horizon the post-break segment can support.

    The largest `h ≤ requested` for which at least `MIN_FOLDS_FOR_MEASUREMENT`
    origins leave `h` observed periods to score against. Falls back to 1, which
    always has the most folds available.
    """
    for h in range(min(requested, n), 0, -1):
        n_folds = max(0, (n - h) - first_origin + 1)
        if n_folds >= MIN_FOLDS_FOR_MEASUREMENT:
            return h
    return 1


def _score_engine(
    arr: np.ndarray,
    window_start: int,
    engine: BaseForecaster,
    origins: Sequence[int],
    eval_horizon: int,
) -> Optional[float]:
    """
    Mean multi-step error for one engine on one window, over fixed origins.

    The engine is re-fitted from scratch at every origin on `arr[window_start :
    origin]`, so nothing from the scored periods can reach the fit. Returns
    `None` when the engine could not be scored at every one of the origins —
    every candidate is measured on exactly the same folds or not compared at
    all, which is what keeps the ranking paired.
    """
    errors: List[float] = []

    for origin in origins:
        train = arr[window_start:origin]
        if train.size < engine.min_history:
            return None

        try:
            output = engine.fit_predict(train, eval_horizon)
        except Exception as exc:  # noqa: BLE001 - a failed fold is data
            logger.warning(
                "forecasting.regime.fold_failed engine=%s origin=%d error=%s",
                engine.name, origin, exc,
            )
            return None

        if len(output.points) < eval_horizon:
            return None

        path = np.array([p.mean for p in output.points[:eval_horizon]])
        errors.append(float(np.mean(np.abs(path - arr[origin:origin + eval_horizon]))))

    return float(np.mean(errors)) if errors else None


def _best_engine(
    arr: np.ndarray,
    window_start: int,
    candidates: Sequence[BaseForecaster],
    origins: Sequence[int],
    eval_horizon: int,
) -> Tuple[Optional[BaseForecaster], Optional[float]]:
    """
    The candidate engine with the lowest measured multi-step error on a window.

    Ranking the engines rather than taking the pattern rule's pick is what
    keeps the *window* comparison honest. Holt's damped trend is the pattern
    rule's choice for a window of eight to eleven observations, and on a
    post-break window that is nearly flat it initialises its trend from a
    single first difference of noise and then extrapolates it: forecasts drift
    208 → 233 against an actual near 200. Scored three steps ahead against the
    other candidates, a level-only prior wins that window on 28 of 30
    realisations. The measurement already knew; it just was not being asked.

    Both windows are ranked the same way. Giving the recent candidate
    best-of-N against the full candidate's best-of-one would bias the
    comparison toward adapting, which is the direction it must not be biased.
    """
    best: Optional[BaseForecaster] = None
    best_error: Optional[float] = None

    for engine in candidates:
        error = _score_engine(arr, window_start, engine, origins, eval_horizon)
        if error is None:
            continue
        if best_error is None or error < best_error:
            best, best_error = engine, error

    return best, best_error


def select_regime(
    quantities: Sequence[float],
    break_result: ChangePointResult,
    selector: Optional[EngineSelector] = None,
    horizon: int = 1,
) -> RegimeDecision:
    """
    Choose the history window to forecast from.

    Args:
        quantities: The full observed history, in period order.
        break_result: The detector's verdict on that same history.
        selector: Engine selector used inside the evaluation. Defaults to a
            fresh one; passing the service's own keeps the folds and the final
            forecast on identical routing.
        horizon: The horizon the forecast will actually be produced at. Folds
            are scored as deep as the post-break segment allows, up to this —
            a candidate is measured the way it will be used.

    Returns:
        A decision, always. Falls back to full history — the existing
        behaviour — whenever anything is unclear.
    """
    arr = np.asarray(quantities, dtype=np.float64)
    n = int(arr.size)
    selector = selector if selector is not None else EngineSelector()

    def full(basis: StrategyBasis, reason: str, **extra) -> RegimeDecision:
        return RegimeDecision(
            strategy=RegimeStrategy.FULL_HISTORY, basis=basis,
            window_start=0, n_periods_used=n, reason=reason, **extra,
        )

    if not break_result.detected or break_result.change_index is None:
        return full(
            StrategyBasis.NO_BREAK,
            "no structural break detected; forecasting from the full history, "
            "which is the unchanged default path.",
        )

    change_index = int(break_result.change_index)
    n_post = n - change_index

    if n_post < MIN_RECENT_TRAIN:
        # Cannot even fit the recent candidate once. Detection stands, but
        # there is nothing to switch to.
        return full(
            StrategyBasis.RULE,
            f"a break was detected at period {change_index + 1}, but only "
            f"{n_post} observation(s) follow it — fewer than the "
            f"{MIN_RECENT_TRAIN} needed to fit anything on the new regime. "
            f"Forecasting from full history; treat the result with caution.",
        )

    # ---- measure the pipeline that will actually ship ----------------------
    # Engines are ranked here and then held fixed, rather than re-selected
    # inside each fold. Letting the selector re-route per fold measures a
    # configuration that will never ship: on a twelve-period post-break window
    # the folds trained a cold-start prior on three observations and scored
    # superbly, while deployment routed the same window to the quantile engine,
    # whose recursive path then ran from 167 to 390 against an actual near 200.
    # The decision was right about the *window* and wrong about what it had
    # measured.
    recent_candidates = selector.candidates_for(arr[change_index:])
    first_origin = change_index + max(
        MIN_RECENT_TRAIN,
        min((e.min_history for e in recent_candidates), default=MIN_RECENT_TRAIN),
    )
    eval_horizon = _eval_horizon(n, first_origin, max(1, horizon))
    origins = list(range(first_origin, n - eval_horizon + 1))

    full_engine, full_mae_opt = _best_engine(
        arr, 0, selector.candidates_for(arr), origins, eval_horizon,
    )
    recent_engine, recent_mae_opt = _best_engine(
        arr, change_index, recent_candidates, origins, eval_horizon,
    )

    n_folds = len(origins) if (full_mae_opt is not None
                               and recent_mae_opt is not None) else 0
    full_eng = full_engine.name if full_engine is not None else ""
    recent_eng = recent_engine.name if recent_engine is not None else ""

    required_depth = max(1, math.ceil(max(1, horizon) * MIN_EVAL_HORIZON_FRACTION))
    deep_enough = eval_horizon >= required_depth

    if n_folds < MIN_FOLDS_FOR_MEASUREMENT or not deep_enough:
        # Too little new regime to score, so the rule applies — but only where
        # the rule's premise holds. A post-break segment that is still climbing
        # or falling is not a new *level*, and forecasting a flat line from it
        # is worse than leaving the existing path alone.
        slope_t = _slope_t(arr[change_index:])
        if slope_t > MAX_POST_BREAK_SLOPE_T:
            return full(
                StrategyBasis.RULE,
                f"break detected at period {change_index + 1}, but the "
                f"{n_post} observations after it are still trending "
                f"(slope t={slope_t:.2f} against a "
                f"{MAX_POST_BREAK_SLOPE_T:.2f} ceiling), so they describe a "
                f"ramp rather than a new level — and there is too little of "
                f"the new regime to measure the alternative "
                f"({n_folds} usable fold(s) at {eval_horizon} step(s)). "
                f"Keeping full history: the rule that would discard it assumes "
                f"a level the series is not sitting at.",
                full_history_engine=full_eng,
                recent_regime_engine=recent_eng,
                n_folds=n_folds,
                eval_horizon=eval_horizon if n_folds else 0,
            )

        # Rule, and labelled as one.
        return RegimeDecision(
            strategy=RegimeStrategy.RECENT_REGIME,
            basis=StrategyBasis.RULE,
            window_start=change_index,
            n_periods_used=n_post,
            n_folds=n_folds,
            eval_horizon=eval_horizon if n_folds else 0,
            full_history_engine=full_eng,
            recent_regime_engine=recent_eng,
            # Ranking the engines and ranking the windows are two different
            # measurements, and a shallow proxy is far more reliable for the
            # first: both engines see the same window, so a bias that affects
            # them equally cancels, which it does not when the windows differ.
            # So the engine ranking is kept even where the window comparison is
            # discarded. Without this, a break with eight new observations fell
            # back to the pattern rule's damped-trend smoother and beat the
            # unadapted pipeline on 63% of realisations instead of 87%.
            selected_engine=recent_eng,
            reason=(
                f"break detected at period {change_index + 1} with {n_post} "
                f"observations in the new regime — too little to compare the "
                f"two windows on the forecast being made "
                f"({n_folds} usable fold(s) scored {eval_horizon} step(s) "
                f"ahead; {MIN_FOLDS_FOR_MEASUREMENT} folds at "
                f"{required_depth} steps required for a horizon of {horizon}). "
                f"Forecasting from the new regime by rule: the detected step "
                f"of {break_result.magnitude:+.2f} makes the pre-break history "
                f"evidence about a level the series has left. Not measured."
            ),
        )

    full_mae = float(full_mae_opt)
    recent_mae = float(recent_mae_opt)

    common = dict(
        full_history_mae=round(full_mae, 6),
        recent_regime_mae=round(recent_mae, 6),
        n_folds=n_folds,
        eval_horizon=eval_horizon,
        full_history_engine=full_eng,
        recent_regime_engine=recent_eng,
    )

    if recent_mae < full_mae:
        logger.info(
            "forecasting.regime.adapted change_index=%d n_post=%d "
            "recent_mae=%.3f full_mae=%.3f folds=%d",
            change_index, n_post, recent_mae, full_mae, n_folds,
        )
        return RegimeDecision(
            strategy=RegimeStrategy.RECENT_REGIME,
            basis=StrategyBasis.MEASURED,
            window_start=change_index,
            n_periods_used=n_post,
            selected_engine=recent_eng,
            reason=(
                f"break detected at period {change_index + 1}. Over {n_folds} "
                f"post-break origins scored {eval_horizon} step(s) ahead, the "
                f"new regime forecast with MAE {recent_mae:.2f} against "
                f"{full_mae:.2f} for full history, so history before the break "
                f"is discarded."
            ),
            **common,
        )

    # Full history won on measurement. A detected break is not automatically a
    # reason to forget — this branch is why the comparison exists.
    return full(
        StrategyBasis.MEASURED,
        f"break detected at period {change_index + 1}, but over {n_folds} "
        f"post-break origins scored {eval_horizon} step(s) ahead, full history "
        f"forecast with MAE {full_mae:.2f} against {recent_mae:.2f} for the "
        f"new regime alone. History is kept: the break is real, and discarding "
        f"the prefix would have made the forecast worse.",
        **common,
    )


__all__ = [
    "MIN_FOLDS_FOR_MEASUREMENT",
    "MAX_POST_BREAK_SLOPE_T",
    "MIN_RECENT_TRAIN",
    "RegimeDecision",
    "RegimeStrategy",
    "StrategyBasis",
    "select_regime",
]
