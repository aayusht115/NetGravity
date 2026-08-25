"""
Structural-break detection and adaptive forecasting.

Organised around the claims the feature makes, not around the modules that
implement them:

    TestDetectsRealBreaks         a genuine regime change is found, and located
    TestRejectsLookalikes         noise, trend, seasonality and spikes are not
    TestNoFutureLeakage           the detector cannot see past its input
    TestDeterminismAndSafety      no randomness, no network, no probability
    TestExistingBehaviourPreserved  a series without a break is untouched
    TestAdaptiveForecastIsBetter  detection actually improves the forecast
    TestEvidenceIsHonest          measured says measured, ruled says ruled
    TestProvenance                a reader can reconstruct what happened
    TestOrchestratorProjection    the control plane can see it too

Series are built from a seeded generator so every assertion is reproducible.
Nothing here asserts a hardcoded forecast value: the critical case checks that
the adaptive forecast is closer to a held-out future it was never shown, which
is a claim about behaviour rather than about a number.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import List, Sequence

import numpy as np
import pytest

from netgravity.forecasting import (
    DemandPoint,
    DemandTimeSeries,
    ForecastRequest,
    ForecastingService,
    SelectionMode,
)
from netgravity.forecasting.change_point import (
    MIN_HISTORY_FOR_DETECTION,
    MIN_SEGMENT,
    SUP_F_THRESHOLD,
    BreakKind,
    ChangePointDetector,
    DetectionStatus,
    detect_change_point,
)
from netgravity.forecasting.regime import (
    RegimeStrategy,
    StrategyBasis,
    select_regime,
)

# ---------------------------------------------------------------------------
# Series builders — deterministic, seeded, and never reused across shapes
# ---------------------------------------------------------------------------

def _noise(rng, n: int, sd: float) -> np.ndarray:
    return rng.normal(0.0, sd, n)


def stable(seed: int = 0, n: int = 48, level: float = 100.0, sd: float = 5.0):
    rng = np.random.default_rng(seed)
    return np.clip(level + _noise(rng, n, sd), 1.0, None)


def noisy(seed: int = 0, n: int = 48):
    return stable(seed, n, 100.0, 40.0)


def seasonal(seed: int = 0, n: int = 48, amp: float = 35.0):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return np.clip(120 + amp * np.sin(2 * np.pi * t / 12) + _noise(rng, n, 5.0), 1.0, None)


def seasonal_trend(seed: int = 0, n: int = 48):
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return np.clip(
        80 + 1.2 * t + 30 * np.sin(2 * np.pi * t / 12) + _noise(rng, n, 6.0), 1.0, None
    )


def linear_growth(seed: int = 0, n: int = 48, slope: float = 1.5):
    rng = np.random.default_rng(seed)
    return np.clip(80 + slope * np.arange(n) + _noise(rng, n, 6.0), 1.0, None)


def linear_decline(seed: int = 0, n: int = 48):
    return linear_growth(seed, n, slope=-1.5) + 60.0


def compound_growth(seed: int = 0, n: int = 48):
    rng = np.random.default_rng(seed)
    return np.clip(50 * (1.025 ** np.arange(1, n + 1)) + _noise(rng, n, 8.0), 1.0, None)


def intermittent(seed: int = 0, n: int = 48):
    rng = np.random.default_rng(seed)
    occurred = rng.uniform(0, 1, n) < 0.30
    return np.where(occurred, rng.gamma(4.0, 10.0, n), 0.0)


def level_shift(seed: int = 0, n_pre: int = 36, n_post: int = 12,
                pre: float = 100.0, post: float = 200.0, sd: float = 6.0):
    rng = np.random.default_rng(seed)
    return np.clip(np.concatenate([
        pre + _noise(rng, n_pre, sd), post + _noise(rng, n_post, sd),
    ]), 1.0, None)


def collapse(seed: int = 0, n_pre: int = 36, n_post: int = 12):
    return level_shift(seed, n_pre, n_post, pre=200.0, post=80.0, sd=8.0)


def spike(seed: int = 0, n: int = 48, at: int = 30, size: float = 320.0):
    y = stable(seed, n).copy()
    y[at] = size
    return y


def with_future(builder, n_future: int = 12, level: float = 200.0,
                sd: float = 6.0, seed: int = 99):
    """A history plus a held-out future drawn from the POST-break regime."""
    rng = np.random.default_rng(seed)
    return builder, np.clip(level + _noise(rng, n_future, sd), 1.0, None)


def series_of(quantities: Sequence[float], market: str = "MKT_A") -> DemandTimeSeries:
    return DemandTimeSeries(
        market_id=market, product_id="SKU_1",
        history=[
            DemandPoint(period=i + 1, quantity=float(v))
            for i, v in enumerate(quantities)
        ],
    )


def forecast_of(quantities, *, detect: bool = True, horizon: int = 12,
                mode: SelectionMode = SelectionMode.PATTERN,
                service: ForecastingService | None = None):
    svc = service if service is not None else ForecastingService()
    return svc.forecast(ForecastRequest(
        series=[series_of(quantities)], horizon=horizon, snapshot_id="snap_test",
        selection_mode=mode, detect_structural_break=detect,
    ))


def mae(prediction: Sequence[float], actual: Sequence[float]) -> float:
    return float(np.mean(np.abs(np.asarray(prediction) - np.asarray(actual))))


CHANGE_POINT_SOURCE = Path("netgravity/forecasting/change_point.py")


def _code_only(path: Path) -> str:
    """
    Source with docstrings stripped.

    A structural check must read the code, not the prose about it. The
    module's own docstring says "no randomness", which a naive substring
    search for "random" then reports as a violation.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


# ---------------------------------------------------------------------------

class TestDetectsRealBreaks:
    """A genuine regime change is found, and found in the right place."""

    def test_a_level_shift_is_detected(self):
        result = detect_change_point(level_shift(seed=1))
        assert result.detected
        assert result.status is DetectionStatus.DETECTED
        assert result.kind is BreakKind.LEVEL_SHIFT

    def test_the_break_is_located_at_the_true_change_point(self):
        # 36 pre-break observations, so the first new-regime observation is
        # index 36 / period 37.
        for seed in range(10):
            result = detect_change_point(level_shift(seed=seed, n_pre=36, n_post=12))
            assert result.detected, f"seed {seed} missed the break"
            assert result.change_index == 36, (
                f"seed {seed} located the break at index {result.change_index}, "
                f"expected 36"
            )
            assert result.change_period == 37

    def test_a_collapse_is_detected_as_readily_as_a_surge(self):
        # A detector that only fires on growth is half a detector.
        for seed in range(10):
            result = detect_change_point(collapse(seed=seed))
            assert result.detected, f"seed {seed} missed the collapse"
            assert result.magnitude is not None and result.magnitude < 0

    def test_detection_is_reliable_across_seeds(self):
        hits = sum(detect_change_point(level_shift(seed=s)).detected for s in range(30))
        assert hits == 30, f"only {hits}/30 realisations detected"

    def test_segment_levels_describe_the_two_regimes(self):
        result = detect_change_point(level_shift(seed=3, pre=100.0, post=200.0))
        assert result.pre_break_level == pytest.approx(100.0, abs=5.0)
        assert result.post_break_level == pytest.approx(200.0, abs=8.0)

    def test_the_statistic_clears_the_threshold_it_reports(self):
        result = detect_change_point(level_shift(seed=4))
        assert result.sup_f is not None
        assert result.sup_f > result.threshold
        assert result.threshold == SUP_F_THRESHOLD

    def test_a_large_break_is_flagged_as_strong_evidence(self):
        assert detect_change_point(level_shift(seed=5)).strong_evidence

    def test_the_reason_names_the_period_and_the_evidence(self):
        result = detect_change_point(level_shift(seed=6))
        assert "period 37" in result.reason
        assert "sup-F" in result.reason


class TestRejectsLookalikes:
    """
    Ordinary noise, trend, seasonality and spikes are not structural breaks.

    Each of these is a thing that *moves the mean of the second half of the
    series* — which is exactly why a naive two-sample test would fire on all of
    them, and why the null model contains trend and seasonality.
    """

    @pytest.mark.parametrize("name,builder", [
        ("stable", stable),
        ("noisy", noisy),
        ("seasonal", seasonal),
        ("seasonal_trend", seasonal_trend),
        ("linear_growth", linear_growth),
        ("linear_decline", linear_decline),
        ("compound_growth", compound_growth),
    ])
    def test_no_false_positives_across_seeds(self, name, builder):
        fired = [s for s in range(30) if detect_change_point(builder(seed=s)).detected]
        assert not fired, (
            f"{name} produced {len(fired)}/30 false positives at seeds {fired[:5]}"
        )

    def test_a_one_period_spike_is_not_a_break(self):
        # The single most important negative case: a spike moves the level
        # dramatically for exactly one period, and a regime is not one period.
        for at in (12, 20, 30, 40, 43):
            fired = [s for s in range(15) if detect_change_point(spike(seed=s, at=at)).detected]
            assert not fired, f"spike at index {at} read as a break on {fired}"

    def test_a_two_period_spike_that_reverts_is_not_a_break(self):
        for seed in range(15):
            y = stable(seed=seed).copy()
            y[30:32] = [300.0, 310.0]
            assert not detect_change_point(y).detected

    def test_intermittent_demand_is_refused_rather_than_tested(self):
        # Not merely "not detected" — the detector must decline, because a
        # level-shift test on a mostly-zero series measures the wrong thing.
        for seed in range(10):
            result = detect_change_point(intermittent(seed=seed))
            assert not result.detected
            assert result.status is DetectionStatus.PATTERN_NOT_APPLICABLE
            assert "zeros" in result.reason

    def test_a_short_series_is_refused_not_guessed(self):
        result = detect_change_point([100.0] * 6 + [200.0] * 4)
        assert not result.detected
        assert result.status is DetectionStatus.INSUFFICIENT_HISTORY

    def test_a_significant_but_immaterial_shift_is_not_actioned(self):
        # A 4% step in a very quiet, very long series: statistically visible,
        # operationally nothing. Distinguished by status, not silently dropped.
        rng = np.random.default_rng(11)
        y = np.concatenate([
            100 + rng.normal(0, 0.5, 40), 104 + rng.normal(0, 0.5, 20),
        ])
        result = detect_change_point(y)
        assert not result.detected
        assert result.status is DetectionStatus.BELOW_MATERIALITY
        # The evidence is still reported, so the judgement can be argued with.
        assert result.sup_f is not None and result.sup_f > SUP_F_THRESHOLD

    def test_a_seasonal_amplitude_change_is_not_a_level_shift(self):
        # The swings get much bigger; the mean does not move. A level-shift
        # test has nothing to say about that, and must not pretend otherwise.
        # Left unguarded this was the feature's worst regression: MASE 1.42 to
        # 4.98 on the seasonal-regime-change scenario.
        fired = []
        for seed in range(30):
            rng = np.random.default_rng(seed)
            t = np.arange(60)
            amplitude = np.where(t < 48, 20.0, 70.0)
            y = np.clip(
                130 + amplitude * np.sin(2 * np.pi * t / 12)
                + rng.normal(0, 6, 60),
                1.0, None,
            )
            if detect_change_point(y).detected:
                fired.append(seed)
        assert not fired, (
            f"a seasonal amplitude change was read as a level shift on "
            f"{len(fired)}/30 realisations (seeds {fired[:5]})"
        )

    def test_a_step_inside_the_series_own_swing_is_refused(self):
        # Statistically clear against the model residual, unremarkable against
        # how far the series routinely travels. Both gates exist because each
        # alone lets a different impostor through.
        rng = np.random.default_rng(77)
        t = np.arange(60)
        y = np.clip(
            200 + 60 * np.sin(2 * np.pi * t / 12)
            + np.where(t < 40, 0.0, 55.0) + rng.normal(0, 3, 60),
            1.0, None,
        )
        result = detect_change_point(y)
        assert not result.detected
        assert result.swing_magnitude is not None
        assert result.swing_magnitude < 3.5
        assert "routinely moves" in result.reason

    def test_a_break_needs_a_full_segment_on_each_side(self):
        # One post-break observation cannot form a regime, by construction.
        y = np.concatenate([stable(seed=2, n=47), [400.0]])
        result = detect_change_point(y)
        assert result.change_index is None or result.change_index <= len(y) - MIN_SEGMENT


class TestNoFutureLeakage:
    """
    The detector cannot see anything it was not handed.

    This is the property that makes it safe to call inside a backtest fold and
    legitimate to call at forecast time.
    """

    def test_a_verdict_depends_only_on_the_prefix_supplied(self):
        full = level_shift(seed=7, n_pre=36, n_post=12)
        prefix = full[:40]
        # Same first 40 observations, wildly different tails.
        other = np.concatenate([prefix, np.full(20, 5000.0)])
        assert detect_change_point(prefix) == detect_change_point(other[:40])

    def test_appending_future_data_cannot_change_an_earlier_verdict(self):
        history = stable(seed=8, n=40)
        before = detect_change_point(history)
        after_prefix = detect_change_point(
            np.concatenate([history, np.full(12, 900.0)])[:40]
        )
        assert before == after_prefix

    def test_the_detector_signature_takes_only_a_history(self):
        # Structural: there is no parameter through which a future observation,
        # a horizon or a held-out set could be passed.
        params = set(inspect.signature(ChangePointDetector.detect).parameters)
        assert params == {"self", "quantities"}

    def test_the_regime_comparison_scores_only_observed_periods(self):
        y = level_shift(seed=9, n_pre=30, n_post=14)
        decision = select_regime(y, detect_change_point(y))
        # Folds are one-step origins strictly inside the post-break segment.
        assert decision.n_folds <= 14


class TestDeterminismAndSafety:
    """No randomness, no model, no network, no probability."""

    def test_the_same_history_always_gives_the_same_verdict(self):
        y = level_shift(seed=10)
        verdicts = [detect_change_point(y) for _ in range(20)]
        assert all(v == verdicts[0] for v in verdicts)

    def test_the_same_history_always_gives_the_same_forecast(self):
        y = level_shift(seed=12)
        runs = [
            [p.mean for p in forecast_of(y).series[0].points] for _ in range(5)
        ]
        assert all(r == runs[0] for r in runs)

    def test_detection_imports_no_model_client_or_network_library(self):
        tree = ast.parse(CHANGE_POINT_SOURCE.read_text(encoding="utf-8"))
        imported: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        banned = ("requests", "urllib", "httpx", "anthropic", "openai",
                  "socket", "netgravity.ingestion", "random")
        for name in imported:
            assert not any(name.startswith(b) for b in banned), (
                f"change_point.py imports {name}; detection must be local, "
                f"deterministic and offline"
            )

    def test_detection_uses_no_random_number_generator(self):
        code = _code_only(CHANGE_POINT_SOURCE)
        for token in ("random", "default_rng", "shuffle", "seed("):
            assert token not in code, f"change_point.py references {token!r}"

    def test_the_verdict_carries_no_probability_field(self):
        from netgravity.forecasting.change_point import ChangePointResult
        fields = set(ChangePointResult.model_fields)
        for banned in ("probability", "p_value", "pvalue", "confidence",
                       "likelihood", "event_probability"):
            assert banned not in fields, (
                f"ChangePointResult exposes {banned!r}. The sup-F null "
                f"distribution under a scanned break date is non-standard, so "
                f"any probability derived from it would be invented."
            )

    def test_the_regime_decision_carries_no_confidence_score(self):
        from netgravity.forecasting.regime import RegimeDecision
        fields = set(RegimeDecision.model_fields)
        for banned in ("confidence", "confidence_score", "probability", "score"):
            assert banned not in fields

    def test_a_signal_cannot_reach_the_detector(self):
        # Structural: detection is a function of demand history alone, so an
        # external signal cannot manufacture a regime change.
        tree = ast.parse(CHANGE_POINT_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = {a.arg for a in node.args.args + node.args.kwonlyargs}
                assert not any("signal" in n for n in names), (
                    f"{node.name} accepts a signal argument"
                )


class TestExistingBehaviourPreserved:
    """A series without a break is forecast exactly as it was before."""

    @pytest.mark.parametrize("name,builder", [
        ("stable", stable),
        ("noisy", noisy),
        ("seasonal", seasonal),
        ("seasonal_trend", seasonal_trend),
        ("linear_growth", linear_growth),
        ("compound_growth", compound_growth),
        ("intermittent", intermittent),
    ])
    @pytest.mark.parametrize("mode", [SelectionMode.PATTERN, SelectionMode.BACKTEST])
    def test_forecast_is_identical_with_detection_on_and_off(self, name, builder, mode):
        for seed in range(5):
            y = builder(seed=seed)
            off = forecast_of(y, detect=False, mode=mode).series[0]
            on = forecast_of(y, detect=True, mode=mode).series[0]
            assert on.engine == off.engine, f"{name} seed {seed} changed engine"
            assert [p.mean for p in on.points] == [p.mean for p in off.points], (
                f"{name} seed {seed} changed forecast with detection enabled"
            )

    def test_no_break_reports_the_full_history_strategy(self):
        result = forecast_of(stable(seed=1)).series[0]
        assert result.regime.strategy is RegimeStrategy.FULL_HISTORY
        assert result.regime.basis is StrategyBasis.NO_BREAK
        assert result.regime.window_start == 0

    def test_no_break_leaves_the_adapted_list_empty(self):
        assert forecast_of(seasonal(seed=2)).provenance.adapted_series == []

    def test_detection_can_be_switched_off_entirely(self):
        result = forecast_of(level_shift(seed=1), detect=False).series[0]
        assert result.structural_break is None
        assert result.regime is None


class TestAdaptiveForecastIsBetter:
    """
    The point of the exercise.

    Detecting a break is worth nothing unless the forecast improves. These
    compare against a held-out future the model never saw, and never assert a
    particular forecast value.
    """

    def test_the_critical_case_beats_the_unadapted_pipeline(self):
        # The specified case: ~100 for 44 periods, then ~200, with a future
        # that stays in the new regime. Nothing here asserts that the forecast
        # equals 200 — the assertions are that the adapted forecast lands
        # closer to a future it was never shown, and that it tracks the new
        # regime rather than splitting the difference between the two.
        rng = np.random.default_rng(21)
        history = np.concatenate([
            100 + rng.normal(0, 3, 44), 200 + rng.normal(0, 6, 6),
        ])
        future = 200 + rng.normal(0, 6, 12)

        before = forecast_of(history, detect=False).series[0]
        after = forecast_of(history, detect=True).series[0]

        mae_before = mae([p.mean for p in before.points], future)
        mae_after = mae([p.mean for p in after.points], future)

        assert mae_after < mae_before, (
            f"adaptation did not help: {mae_after:.2f} vs {mae_before:.2f}"
        )

        # The substantive claim, stated without a hardcoded expectation: the
        # adapted forecast sits nearer the level the series has ALREADY been
        # observed at than the unadapted one does. Both quantities come from
        # data the model was given.
        observed_new_level = float(np.mean(history[44:]))
        gap_before = abs(np.mean([p.mean for p in before.points]) - observed_new_level)
        gap_after = abs(np.mean([p.mean for p in after.points]) - observed_new_level)
        assert gap_after < gap_before

    def test_the_critical_case_improves_materially_across_seeds(self):
        # "Materially" needs more than one realisation behind it, so the size
        # of the improvement is asserted on the median of thirty.
        ratios = []
        for seed in range(30):
            rng = np.random.default_rng(400 + seed)
            history = np.concatenate([
                100 + rng.normal(0, 3, 44), 200 + rng.normal(0, 6, 6),
            ])
            future = 200 + rng.normal(0, 6, 12)
            before = mae(
                [p.mean for p in forecast_of(history, detect=False).series[0].points],
                future,
            )
            after = mae(
                [p.mean for p in forecast_of(history, detect=True).series[0].points],
                future,
            )
            ratios.append(after / before)

        median_ratio = float(np.median(ratios))
        assert median_ratio < 0.6, (
            f"median error ratio {median_ratio:.3f} — adaptation is not a "
            f"material improvement on this case"
        )

    def test_a_recent_break_no_longer_diverges(self):
        # The defect this feature exists for. With a level shift inside the
        # quantile engine's lag window, the recursive forecast reads the jump
        # as an explosive AR coefficient. Unadapted it runs to five figures.
        rng = np.random.default_rng(22)
        history = np.concatenate([
            100 + rng.normal(0, 4, 44), 200 + rng.normal(0, 8, 4),
        ])
        after = forecast_of(history, detect=True).series[0]
        peak = max(p.mean for p in after.points)
        observed_peak = float(np.max(history))
        assert peak < 3 * observed_peak, (
            f"forecast peaked at {peak:.0f} against an observed maximum of "
            f"{observed_peak:.0f} — the recursion is still diverging"
        )

    def test_adaptation_helps_on_the_majority_of_realisations(self):
        # One seed is an anecdote.
        wins = 0
        trials = 20
        for seed in range(trials):
            rng = np.random.default_rng(100 + seed)
            history = np.concatenate([
                100 + rng.normal(0, 4, 42), 200 + rng.normal(0, 8, 6),
            ])
            future = 200 + rng.normal(0, 8, 12)
            before = mae(
                [p.mean for p in forecast_of(history, detect=False).series[0].points],
                future,
            )
            after = mae(
                [p.mean for p in forecast_of(history, detect=True).series[0].points],
                future,
            )
            wins += after < before
        assert wins >= 0.8 * trials, f"adaptation won only {wins}/{trials}"

    def test_a_collapse_is_adapted_to_as_well_as_a_surge(self):
        rng = np.random.default_rng(23)
        history = np.concatenate([
            200 + rng.normal(0, 6, 42), 80 + rng.normal(0, 4, 6),
        ])
        future = 80 + rng.normal(0, 4, 12)
        before = mae(
            [p.mean for p in forecast_of(history, detect=False).series[0].points],
            future,
        )
        after = mae(
            [p.mean for p in forecast_of(history, detect=True).series[0].points],
            future,
        )
        assert after < before

    def test_the_recent_regime_actually_reaches_the_engine(self):
        # Not just recorded as chosen — the fitted window really is the short
        # one, which the engine choice makes visible: a 6-period window cannot
        # route to the quantile engine, whose min_history is 8.
        rng = np.random.default_rng(24)
        history = np.concatenate([
            100 + rng.normal(0, 3, 44), 200 + rng.normal(0, 6, 6),
        ])
        result = forecast_of(history).series[0]
        assert result.regime.strategy is RegimeStrategy.RECENT_REGIME
        assert result.regime.n_periods_used == 6
        assert result.regime.window_start == 44
        assert result.engine != "QuantileRegression_HiGHS"


class TestEvidenceIsHonest:
    """A measured decision says measured; a ruled decision says ruled."""

    def test_a_measured_decision_reports_both_candidate_errors(self):
        y = level_shift(seed=30, n_pre=36, n_post=12)
        decision = select_regime(y, detect_change_point(y))
        assert decision.basis is StrategyBasis.MEASURED
        assert decision.full_history_mae is not None
        assert decision.recent_regime_mae is not None
        assert decision.n_folds >= 2

    def test_too_short_a_new_regime_is_labelled_a_rule_not_a_measurement(self):
        y = level_shift(seed=31, n_pre=44, n_post=4)
        decision = select_regime(y, detect_change_point(y))
        assert decision.strategy is RegimeStrategy.RECENT_REGIME
        assert decision.basis is StrategyBasis.RULE
        assert decision.full_history_mae is None
        assert "Not measured" in decision.reason

    def test_history_is_kept_when_measurement_says_to_keep_it(self):
        # A detected break is NOT automatically a reason to forget, and this is
        # the branch that makes the comparison worth running rather than being
        # an elaborate way to reach a decision already made.
        #
        # The case is a strongly seasonal series that also steps up sharply.
        # The break is real, large enough to clear every materiality gate, and
        # detected — but the post-break window is shorter than one seasonal
        # cycle, so forecasting from it alone throws away the only evidence of
        # the seasonality. On some realisations the measurement says exactly
        # that, and the decision keeps history.
        kept = 0
        measured = 0
        for seed in range(40):
            rng = np.random.default_rng(seed)
            t = np.arange(46)
            y = np.clip(
                150 + 40 * np.sin(2 * np.pi * t / 12)
                + np.where(t < 36, 0.0, 200.0) + rng.normal(0, 5, 46),
                1.0, None,
            )
            decision = select_regime(
                y, detect_change_point(y), horizon=12,
            )
            if decision.basis is StrategyBasis.MEASURED:
                measured += 1
                kept += decision.strategy is RegimeStrategy.FULL_HISTORY

        assert measured > 0, "nothing was measured; the case proves nothing"
        assert kept > 0, (
            "the comparison never once kept full history on a series whose "
            "post-break window is shorter than its seasonal cycle; it is not "
            "really comparing, it is rationalising a decision already made"
        )

    def test_the_decision_names_the_measured_errors_in_its_reason(self):
        y = level_shift(seed=32, n_pre=36, n_post=12)
        decision = select_regime(y, detect_change_point(y))
        assert "MAE" in decision.reason
        assert str(decision.n_folds) in decision.reason

    def test_the_rule_declines_when_the_new_regime_is_still_ramping(self):
        # The rule's argument is that history describes "a level the series has
        # left". On a series that stops being flat and starts climbing there is
        # no new level to sit at, and applying the rule anyway forecasts a flat
        # line through a ramp.
        #
        # The guard is a slope test, so it fires on ramps it can actually
        # resolve — the majority, not all of them. A ramp buried in enough
        # noise is not statistically distinguishable from a level on seven
        # observations, and that limitation is documented rather than asserted
        # away here.
        declined = 0
        fired = 0
        for seed in range(30):
            rng = np.random.default_rng(seed)
            t = np.arange(60)
            y = np.clip(
                120 + np.clip(t - 48, 0, None) * 5.0 + rng.normal(0, 6, 60),
                1.0, None,
            )
            break_result = detect_change_point(y)
            if not break_result.detected:
                continue
            decision = select_regime(y, break_result, horizon=12)
            if decision.basis is not StrategyBasis.RULE:
                continue
            fired += 1
            if decision.strategy is RegimeStrategy.FULL_HISTORY:
                declined += 1
                assert "trending" in decision.reason

        assert fired > 0, "the rule branch never ran; the case proves nothing"
        assert declined > 0.6 * fired, (
            f"the rule discarded history on {fired - declined} of {fired} "
            f"clearly ramping series"
        )

    def test_a_flat_new_regime_still_takes_the_rule(self):
        # The guard above must not suppress the case the rule exists for.
        y = level_shift(seed=60, n_pre=44, n_post=4)
        decision = select_regime(y, detect_change_point(y), horizon=12)
        assert decision.basis is StrategyBasis.RULE
        assert decision.strategy is RegimeStrategy.RECENT_REGIME

    def test_an_unmeasured_comparison_reports_none_not_zero(self):
        y = level_shift(seed=33, n_pre=44, n_post=4)
        decision = select_regime(y, detect_change_point(y))
        assert decision.recent_regime_mae is None, (
            "an unmeasured error must be None; zero would read as perfect"
        )


class TestProvenance:
    """A reader can reconstruct what happened and why."""

    def test_an_adapted_forecast_records_the_break(self):
        result = forecast_of(level_shift(seed=40, n_pre=42, n_post=6)).series[0]
        brk = result.structural_break
        assert brk.detected
        assert brk.change_period == 43
        assert brk.detection_method == "SUP_F_LEVEL_SHIFT"
        assert brk.pre_break_level is not None
        assert brk.post_break_level is not None

    def test_an_adapted_forecast_records_the_window_it_used(self):
        result = forecast_of(level_shift(seed=41, n_pre=42, n_post=6)).series[0]
        assert result.regime.window_start == 42
        assert result.regime.n_periods_used == 6

    def test_the_result_lists_which_series_adapted(self):
        result = forecast_of(level_shift(seed=42, n_pre=42, n_post=6))
        assert result.provenance.adapted_series == ["MKT_A/SKU_1"]

    def test_provenance_records_the_detection_settings(self):
        repro = forecast_of(stable(seed=43)).provenance.reproducibility
        assert repro["change_point_detection"] is True
        assert repro["change_point_method"] == "SUP_F_LEVEL_SHIFT"
        assert repro["change_point_threshold"] == SUP_F_THRESHOLD

    def test_a_no_break_series_still_records_that_it_was_checked(self):
        # "We looked and found nothing" is a different fact from "we did not
        # look", and both are reconstructable.
        result = forecast_of(stable(seed=44)).series[0]
        assert result.structural_break is not None
        assert result.structural_break.detected is False
        assert result.structural_break.status is DetectionStatus.NO_BREAK

    def test_the_adaptation_is_surfaced_as_a_warning(self):
        result = forecast_of(level_shift(seed=45, n_pre=42, n_post=6))
        assert any("structural break" in w for w in result.warnings)

    def test_a_failed_series_still_carries_no_numbers(self):
        # The package's central invariant, re-checked on the new path.
        result = forecast_of([100.0], horizon=3).series[0]
        assert not result.ok
        assert result.points == []


class TestOrchestratorProjection:
    """The control plane can see the adaptation without typed access."""

    def test_the_flattened_forecast_exposes_the_break(self):
        from netgravity.orchestrator.engines.deterministic import (
            flatten_forecast_result,
        )
        flat = flatten_forecast_result(
            forecast_of(level_shift(seed=50, n_pre=42, n_post=6))
        )
        assert flat["adapted_series"] == ["MKT_A/SKU_1"]
        row = flat["series"][0]
        assert row["structural_break"]["detected"] is True
        assert row["structural_break"]["change_period"] == 43
        assert row["regime"]["strategy"] == "RECENT_REGIME"
        assert row["regime"]["n_periods_used"] == 6
        assert row["regime"]["reason"]

    def test_the_flattened_forecast_is_json_serialisable(self):
        import json
        from netgravity.orchestrator.engines.deterministic import (
            flatten_forecast_result,
        )
        flat = flatten_forecast_result(
            forecast_of(level_shift(seed=51, n_pre=42, n_post=6))
        )
        json.dumps(flat)  # raises if a typed object leaked into the projection

    def test_a_forecast_without_detection_projects_nulls_not_defaults(self):
        from netgravity.orchestrator.engines.deterministic import (
            flatten_forecast_result,
        )
        flat = flatten_forecast_result(
            forecast_of(stable(seed=52), detect=False)
        )
        row = flat["series"][0]
        assert row["structural_break"] is None
        assert row["regime"] is None
