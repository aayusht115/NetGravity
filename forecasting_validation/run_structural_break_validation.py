#!/usr/bin/env python3
"""
Phase 6.2 — Structural-break detection & adaptive forecasting validation.

Extends the Phase 6.1 harness rather than replacing it: the ten synthetic
demand patterns, the metric definitions and the train/test discipline are
imported from `run_validation.py`, so the before/after regression numbers here
are computed exactly the way the existing report computes its baseline.

What this adds:

  * six structural-break scenarios (A-F) on top of the ten existing patterns
  * a BEFORE vs AFTER comparison — detection disabled vs enabled — across
    multiple seeds, on every pattern
  * a false-positive census: how often the detector fires on each non-break
    shape
  * the specified critical case, ~100 stepping to ~200
  * runtime and memory at 10 / 100 / 500 / 1,000 series
  * plots showing history, detected change point, both regimes, both
    forecasts and the held-out actual

Train/test discipline, unchanged from 6.1 and load-bearing here: every series
is generated as 72 periods, the first 60 are handed to the forecaster and the
last 12 are held out. The detector only ever sees the 60. Nothing in this file
passes a held-out observation into detection, fitting or model selection.
"""

from __future__ import annotations

import json
import sys
import time
import tracemalloc
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from netgravity.forecasting import (
    DemandPoint,
    DemandTimeSeries,
    ForecastingService,
    ForecastRequest,
    SelectionMode,
)
from netgravity.forecasting.change_point import detect_change_point

# Reuse — not reimplement — the 6.1 generator and metric definitions.
from run_validation import (
    METRICS_DIR,
    PATTERN_NAMES,
    PLOTS_DIR,
    compute_metrics,
    generate_series,
)

BASE_DIR = REPO_ROOT / "forecasting_validation"
BREAK_DIR = BASE_DIR / "structural_break"
BREAK_DIR.mkdir(parents=True, exist_ok=True)

TRAIN_PERIODS = 60
HORIZON = 12
TOTAL_PERIODS = TRAIN_PERIODS + HORIZON


# ---------------------------------------------------------------------------
# 1. Structural-break scenarios, plus the specified critical case
# ---------------------------------------------------------------------------

BREAK_SCENARIOS = [
    "SB_A_Level_Shift",
    "SB_B_Trend_Shift",
    "SB_C_Demand_Surge",
    "SB_D_Demand_Collapse",
    "SB_E_Temporary_Spike",
    "SB_F_Seasonal_Regime_Change",
    "SB_G_Critical_100_to_200",
]


def generate_break_series(
    scenario: str, seed: int = 1, break_at: int = 48,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, Any]]:
    """
    Generate one structural-break scenario.

    `break_at` is the index of the first NEW-REGIME observation, so the
    forecaster sees `TRAIN_PERIODS - break_at` periods of the new regime. The
    default of 48 leaves twelve, which is the interesting middle of the range:
    long enough to measure, short enough that the stale prefix still dominates
    an unadapted fit.

    Returns `(train[60], test[12], metadata)`.
    """
    rng = np.random.default_rng(seed * 7919 + 13)
    t = np.arange(TOTAL_PERIODS)
    post = t >= break_at

    if scenario == "SB_A_Level_Shift":
        y = np.where(post, 180.0, 100.0) + rng.normal(0, 8.0, TOTAL_PERIODS)
        meta = {"true_break": break_at, "kind": "level_shift",
                "dgp": "100 -> 180 step"}

    elif scenario == "SB_B_Trend_Shift":
        # Flat, then a sustained ramp. No level jump at the break itself.
        ramp = np.clip(t - break_at, 0, None) * 5.0
        y = 120.0 + ramp + rng.normal(0, 8.0, TOTAL_PERIODS)
        meta = {"true_break": break_at, "kind": "trend_shift",
                "dgp": "flat 120 then +5/period"}

    elif scenario == "SB_C_Demand_Surge":
        y = np.where(post, 260.0, 90.0) + rng.normal(0, 10.0, TOTAL_PERIODS)
        meta = {"true_break": break_at, "kind": "surge",
                "dgp": "90 -> 260 step (+189%)"}

    elif scenario == "SB_D_Demand_Collapse":
        y = np.where(post, 70.0, 220.0) + rng.normal(0, 9.0, TOTAL_PERIODS)
        meta = {"true_break": break_at, "kind": "collapse",
                "dgp": "220 -> 70 step (-68%)"}

    elif scenario == "SB_E_Temporary_Spike":
        # A NEGATIVE control: the level returns. Nothing should adapt.
        y = 110.0 + rng.normal(0, 7.0, TOTAL_PERIODS)
        y[break_at:break_at + 2] += 240.0
        meta = {"true_break": None, "kind": "temporary_spike",
                "dgp": "110 flat with a 2-period spike at t=49"}

    elif scenario == "SB_F_Seasonal_Regime_Change":
        amp = np.where(post, 70.0, 20.0)
        y = (130.0 + amp * np.sin(2 * np.pi * t / 12)
             + rng.normal(0, 6.0, TOTAL_PERIODS))
        meta = {"true_break": break_at, "kind": "seasonal_regime_change",
                "dgp": "seasonal amplitude 20 -> 70, mean unchanged"}

    elif scenario == "SB_G_Critical_100_to_200":
        # The case named in the specification.
        y = np.where(post, 200.0, 100.0) + rng.normal(0, 4.0, TOTAL_PERIODS)
        meta = {"true_break": break_at, "kind": "level_shift",
                "dgp": "100 -> 200 step (the specified critical case)"}

    else:
        raise ValueError(f"Unknown structural-break scenario {scenario!r}")

    y = np.clip(y, 1.0, None)
    meta["scenario"] = scenario
    meta["seed"] = seed
    return y[:TRAIN_PERIODS], y[TRAIN_PERIODS:], meta


# ---------------------------------------------------------------------------
# 2. Before / after evaluation
# ---------------------------------------------------------------------------

def _series(train: Sequence[float]) -> DemandTimeSeries:
    return DemandTimeSeries(
        market_id="MKT_VALIDATION", product_id="SKU_VAL",
        history=[DemandPoint(period=i + 1, quantity=float(v))
                 for i, v in enumerate(train)],
    )


def _forecast(service, train, detect: bool, mode: SelectionMode):
    result = service.forecast(ForecastRequest(
        series=[_series(train)], horizon=HORIZON, snapshot_id="snap_sb_val",
        selection_mode=mode, detect_structural_break=detect,
    ))
    return result, result.series[0]


def evaluate(num_seeds: int = 30) -> List[Dict[str, Any]]:
    """
    Score every pattern with detection OFF and ON, over `num_seeds` seeds.

    The two runs differ in exactly one flag, so any difference in the metrics
    is attributable to the structural-break layer and nothing else.
    """
    service = ForecastingService()
    records: List[Dict[str, Any]] = []

    all_patterns = (
        [(p, "baseline") for p in PATTERN_NAMES]
        + [(s, "structural_break") for s in BREAK_SCENARIOS]
    )

    for name, family in all_patterns:
        print(f"  evaluating {name} ...")
        for seed in range(1, num_seeds + 1):
            if family == "baseline":
                train, test, meta = generate_series(
                    name, total_periods=TOTAL_PERIODS, seed=seed, series_idx=seed,
                )
            else:
                train, test, meta = generate_break_series(name, seed=seed)

            detection = detect_change_point(train)

            for mode in (SelectionMode.PATTERN, SelectionMode.BACKTEST):
                for detect in (False, True):
                    result, sf = _forecast(service, train, detect, mode)
                    if not sf.ok:
                        continue
                    m = compute_metrics(train, [p.mean for p in sf.points], test)
                    records.append({
                        "pattern": name,
                        "family": family,
                        "seed": seed,
                        "mode": mode.value,
                        "detection": "ON" if detect else "OFF",
                        "engine": sf.engine,
                        "adapted": bool(result.provenance.adapted_series),
                        "strategy": sf.regime.strategy.value if sf.regime else None,
                        "basis": sf.regime.basis.value if sf.regime else None,
                        "break_detected": detection.detected,
                        "break_status": detection.status.value,
                        "break_period": detection.change_period,
                        "true_break": meta.get("true_break"),
                        "sup_f": detection.sup_f,
                        **asdict(m),
                    })

    (BREAK_DIR / "before_after_raw.json").write_text(
        json.dumps(records, indent=2), encoding="utf-8",
    )
    return records


# ---------------------------------------------------------------------------
# 3. False-positive census
# ---------------------------------------------------------------------------

def false_positive_census(num_seeds: int = 50) -> Dict[str, Any]:
    """
    How often the detector fires on each shape, and where.

    The non-break patterns are the ones that matter: every firing there is a
    false positive by construction. `SB_E_Temporary_Spike` is counted among
    them because a spike that reverts is not a regime change.
    """
    census: Dict[str, Any] = {}

    shapes = (
        [(p, "baseline") for p in PATTERN_NAMES]
        + [(s, "structural_break") for s in BREAK_SCENARIOS]
    )

    for name, family in shapes:
        fired, statuses, locations, stats = 0, {}, [], []
        meta: Dict[str, Any] = {}
        for seed in range(1, num_seeds + 1):
            if family == "baseline":
                train, _, meta = generate_series(
                    name, total_periods=TOTAL_PERIODS, seed=seed, series_idx=seed,
                )
            else:
                train, _, meta = generate_break_series(name, seed=seed)

            r = detect_change_point(train)
            statuses[r.status.value] = statuses.get(r.status.value, 0) + 1
            if r.sup_f is not None:
                stats.append(r.sup_f)
            if r.detected:
                fired += 1
                locations.append(r.change_period)

        true_break = meta.get("true_break") if family == "structural_break" else None
        # I_Structural_Break is the one baseline pattern that genuinely breaks.
        expects_break = true_break is not None or name == "I_Structural_Break"

        census[name] = {
            "family": family,
            "expects_break": expects_break,
            "fire_rate_pct": round(100.0 * fired / num_seeds, 1),
            "n_seeds": num_seeds,
            "statuses": statuses,
            "median_sup_f": round(float(np.median(stats)), 3) if stats else None,
            "median_detected_period": (
                int(np.median(locations)) if locations else None
            ),
            "true_break_period": (true_break + 1) if true_break is not None else None,
        }

    (BREAK_DIR / "false_positive_census.json").write_text(
        json.dumps(census, indent=2), encoding="utf-8",
    )
    return census


# ---------------------------------------------------------------------------
# 4. Performance
# ---------------------------------------------------------------------------

def benchmark(scales: Sequence[int] = (10, 100, 500, 1000)) -> List[Dict[str, Any]]:
    """Detection and end-to-end forecasting cost, with and without detection."""
    service = ForecastingService()
    results: List[Dict[str, Any]] = []

    for n in scales:
        print(f"  benchmarking N = {n} ...")
        histories = []
        series_list = []
        for i in range(n):
            if i % 3 == 0:
                train, _, _ = generate_break_series(
                    BREAK_SCENARIOS[i % len(BREAK_SCENARIOS)], seed=100 + i,
                )
            else:
                train, _, _ = generate_series(
                    PATTERN_NAMES[i % len(PATTERN_NAMES)],
                    total_periods=TOTAL_PERIODS, seed=100 + i, series_idx=i,
                )
            histories.append(train)
            series_list.append(DemandTimeSeries(
                market_id=f"MKT_{i}", product_id=f"SKU_{i}",
                history=[DemandPoint(period=t + 1, quantity=float(v))
                         for t, v in enumerate(train)],
            ))

        # Detection alone.
        t0 = time.perf_counter()
        for h in histories:
            detect_change_point(h)
        t_detect = time.perf_counter() - t0

        timings: Dict[str, float] = {}
        peaks: Dict[str, float] = {}
        for label, detect in (("off", False), ("on", True)):
            request = ForecastRequest(
                series=series_list, horizon=HORIZON, snapshot_id="snap_sb_bench",
                selection_mode=SelectionMode.PATTERN,
                detect_structural_break=detect,
            )
            tracemalloc.start()
            t0 = time.perf_counter()
            service.forecast(request)
            timings[label] = time.perf_counter() - t0
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            peaks[label] = peak / (1024 * 1024)

        results.append({
            "num_series": n,
            "detection_only_sec": round(t_detect, 4),
            "detection_per_series_ms": round(1000 * t_detect / n, 4),
            "forecast_detection_off_sec": round(timings["off"], 4),
            "forecast_detection_on_sec": round(timings["on"], 4),
            "overhead_factor": round(timings["on"] / max(timings["off"], 1e-9), 3),
            "throughput_off_series_per_sec": round(n / max(timings["off"], 1e-9), 1),
            "throughput_on_series_per_sec": round(n / max(timings["on"], 1e-9), 1),
            "peak_memory_off_mb": round(peaks["off"], 2),
            "peak_memory_on_mb": round(peaks["on"], 2),
        })
        print(f"    N={n}: detect={t_detect:.3f}s  off={timings['off']:.3f}s  "
              f"on={timings['on']:.3f}s  x{results[-1]['overhead_factor']}")

    (METRICS_DIR / "structural_break_benchmark.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8",
    )
    return results


# ---------------------------------------------------------------------------
# 5. Plots
# ---------------------------------------------------------------------------

def plot_scenarios(seed: int = 1) -> List[str]:
    """
    One plot per scenario: history, detected break, both regimes, both
    forecasts, held-out actual.
    """
    service = ForecastingService()
    written: List[str] = []

    for scenario in BREAK_SCENARIOS:
        train, test, meta = generate_break_series(scenario, seed=seed)
        detection = detect_change_point(train)

        _, sf_off = _forecast(service, train, False, SelectionMode.PATTERN)
        _, sf_on = _forecast(service, train, True, SelectionMode.PATTERN)

        pred_off = [p.mean for p in sf_off.points]
        pred_on = [p.mean for p in sf_on.points]
        m_off = compute_metrics(train, pred_off, test)
        m_on = compute_metrics(train, pred_on, test)

        t_train = np.arange(1, len(train) + 1)
        t_test = np.arange(len(train) + 1, len(train) + len(test) + 1)

        fig, ax = plt.subplots(figsize=(12, 5.5))

        if detection.detected and detection.change_index is not None:
            ci = detection.change_index
            ax.plot(t_train[:ci], train[:ci], color="#8c8c8c", lw=1.6,
                    label="Pre-break regime (observed)")
            ax.plot(t_train[ci - 1:], train[ci - 1:], color="#1f77b4", lw=2.0,
                    label="Post-break regime (observed)")
            ax.axvline(detection.change_period, color="#d62728", ls="-", lw=2.0,
                       alpha=0.75,
                       label=f"Detected change point (t={detection.change_period})")
            ax.axvspan(detection.change_period, len(train), color="#1f77b4",
                       alpha=0.06)
        else:
            ax.plot(t_train, train, color="#1f77b4", lw=1.8,
                    label="Observed history (no break detected)")

        if meta.get("true_break") is not None:
            ax.axvline(meta["true_break"] + 1, color="#2ca02c", ls="--", lw=1.6,
                       alpha=0.8, label=f"True break (t={meta['true_break'] + 1})")

        ax.plot(t_test, test, color="#2ca02c", lw=2.4, ls="--",
                label="Held-out actual future")
        ax.plot(t_test, pred_off, color="#ff7f0e", lw=2.0, ls="-.",
                label=f"Existing forecast (MAE {m_off.mae:.1f}, MASE {m_off.mase:.2f})")
        ax.plot(t_test, pred_on, color="#d62728", lw=2.4,
                label=f"Adaptive forecast (MAE {m_on.mae:.1f}, MASE {m_on.mase:.2f})")

        ax.fill_between(t_test, [p.p10 for p in sf_on.points],
                        [p.p90 for p in sf_on.points], color="#d62728", alpha=0.13,
                        label="Adaptive P10-P90")
        ax.axvline(len(train), color="gray", ls=":", lw=1.4,
                   label="Forecast origin (t=60)")

        strategy = sf_on.regime.strategy.value if sf_on.regime else "n/a"
        basis = sf_on.regime.basis.value if sf_on.regime else "n/a"
        ax.set_title(
            f"{scenario} — {meta['dgp']}\n"
            f"detected={detection.detected}  strategy={strategy} ({basis})  "
            f"engine={sf_on.engine}",
            fontsize=11, fontweight="bold",
        )
        ax.set_xlabel("Period")
        ax.set_ylabel("Demand quantity")
        ax.legend(loc="upper left", fontsize=8, frameon=True, ncol=2)
        ax.grid(True, ls="--", alpha=0.4)
        plt.tight_layout()

        path = PLOTS_DIR / f"break_{scenario}.png"
        fig.savefig(path, dpi=180)
        plt.close(fig)
        written.append(path.name)

    return written


def plot_before_after(records: List[Dict[str, Any]]) -> str:
    """Per-pattern MASE, detection off vs on, across every seed."""
    names = PATTERN_NAMES + BREAK_SCENARIOS
    off_means, on_means = [], []
    for name in names:
        off = [r["mase"] for r in records
               if r["pattern"] == name and r["detection"] == "OFF"
               and r["mode"] == "PATTERN" and r["mase"] is not None]
        on = [r["mase"] for r in records
              if r["pattern"] == name and r["detection"] == "ON"
              and r["mode"] == "PATTERN" and r["mase"] is not None]
        off_means.append(float(np.mean(off)) if off else 0.0)
        on_means.append(float(np.mean(on)) if on else 0.0)

    x = np.arange(len(names))
    width = 0.38
    top = max(max(off_means), max(on_means))
    fig, ax = plt.subplots(figsize=(13, 6))
    ax.bar(x - width / 2, off_means, width, label="Detection OFF (existing)",
           color="#ff7f0e")
    ax.bar(x + width / 2, on_means, width, label="Detection ON (adaptive)",
           color="#1f77b4")
    ax.axhline(1.0, color="#d62728", ls="--", lw=1.4,
               label="MASE = 1 (naive-1 benchmark)")
    ax.axvline(len(PATTERN_NAMES) - 0.5, color="gray", ls=":", lw=1.6)
    ax.text(len(PATTERN_NAMES) / 2 - 0.5, top * 0.96,
            "existing benchmark — must not regress", ha="center", fontsize=9,
            style="italic", color="#555555")
    ax.text(len(PATTERN_NAMES) + len(BREAK_SCENARIOS) / 2 - 0.5, top * 0.96,
            "structural-break scenarios", ha="center", fontsize=9,
            style="italic", color="#555555")
    ax.set_xticks(x)
    ax.set_xticklabels(names, rotation=42, ha="right", fontsize=8)
    ax.set_ylabel("Mean out-of-sample MASE (lower is better)")
    ax.set_title("Structural-break adaptation: before vs after, by pattern",
                 fontsize=12, fontweight="bold")
    ax.legend()
    ax.grid(True, axis="y", ls="--", alpha=0.4)
    plt.tight_layout()
    path = PLOTS_DIR / "break_before_after_mase.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    return path.name


def plot_critical_case(seed: int = 1) -> str:
    """The specified case, plotted on its own: ~100 stepping to ~200."""
    service = ForecastingService()
    train, test, meta = generate_break_series("SB_G_Critical_100_to_200", seed=seed)
    detection = detect_change_point(train)

    _, sf_off = _forecast(service, train, False, SelectionMode.PATTERN)
    _, sf_on = _forecast(service, train, True, SelectionMode.PATTERN)
    pred_off = [p.mean for p in sf_off.points]
    pred_on = [p.mean for p in sf_on.points]
    m_off = compute_metrics(train, pred_off, test)
    m_on = compute_metrics(train, pred_on, test)

    t_train = np.arange(1, len(train) + 1)
    t_test = np.arange(len(train) + 1, len(train) + len(test) + 1)

    fig, ax = plt.subplots(figsize=(12, 5.5))
    ci = detection.change_index or 0
    ax.plot(t_train[:ci], train[:ci], color="#8c8c8c", lw=1.7,
            label="Old regime (~100)")
    ax.plot(t_train[ci - 1:], train[ci - 1:], color="#1f77b4", lw=2.2,
            label="New regime (~200)")
    ax.axvline(detection.change_period, color="#d62728", lw=2.0, alpha=0.75,
               label=f"Detected break t={detection.change_period}")
    ax.axvline(meta["true_break"] + 1, color="#2ca02c", ls="--", lw=1.6,
               label=f"True break t={meta['true_break'] + 1}")
    ax.plot(t_test, test, color="#2ca02c", lw=2.5, ls="--",
            label="Held-out actual")
    ax.plot(t_test, pred_off, color="#ff7f0e", lw=2.0, ls="-.",
            label=f"Existing (MAE {m_off.mae:.1f}, bias {m_off.bias:+.1f})")
    ax.plot(t_test, pred_on, color="#d62728", lw=2.5,
            label=f"Adaptive (MAE {m_on.mae:.1f}, bias {m_on.bias:+.1f})")
    ax.axvline(len(train), color="gray", ls=":", lw=1.4, label="Forecast origin")
    ax.set_title(
        "Critical case: demand steps from ~100 to ~200, future stays in the "
        "new regime", fontsize=12, fontweight="bold",
    )
    ax.set_xlabel("Period")
    ax.set_ylabel("Demand quantity")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(True, ls="--", alpha=0.4)
    plt.tight_layout()
    path = PLOTS_DIR / "break_critical_case.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    return path.name


# ---------------------------------------------------------------------------
# 6. Summary
# ---------------------------------------------------------------------------

def summarise(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate before/after by pattern and by mode."""
    summary: Dict[str, Any] = {"by_pattern": {}, "overall": {}}

    def agg(rows, field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        return float(np.mean(vals)) if vals else None

    for name in PATTERN_NAMES + BREAK_SCENARIOS:
        entry: Dict[str, Any] = {}
        for mode in ("PATTERN", "BACKTEST"):
            block: Dict[str, Any] = {}
            for det in ("OFF", "ON"):
                rows = [r for r in records if r["pattern"] == name
                        and r["mode"] == mode and r["detection"] == det]
                if not rows:
                    continue
                block[det] = {
                    "mase": agg(rows, "mase"),
                    "mae": agg(rows, "mae"),
                    "wape": agg(rows, "wape"),
                    "bias": agg(rows, "bias"),
                    "beats_naive_pct": round(
                        100.0 * float(np.mean([r["beats_naive"] for r in rows])), 1,
                    ),
                    "adapted_pct": round(
                        100.0 * float(np.mean([r["adapted"] for r in rows])), 1,
                    ),
                }
            if "OFF" in block and "ON" in block:
                block["delta_mase"] = round(
                    block["ON"]["mase"] - block["OFF"]["mase"], 4,
                )
            entry[mode] = block
        summary["by_pattern"][name] = entry

    for mode in ("PATTERN", "BACKTEST"):
        for family in ("baseline", "structural_break"):
            for det in ("OFF", "ON"):
                rows = [r for r in records if r["mode"] == mode
                        and r["family"] == family and r["detection"] == det]
                if not rows:
                    continue
                summary["overall"][f"{family}/{mode}/{det}"] = {
                    "mase": agg(rows, "mase"),
                    "mae": agg(rows, "mae"),
                    "wape": agg(rows, "wape"),
                    "bias": agg(rows, "bias"),
                    "beats_naive_pct": round(
                        100.0 * float(np.mean([r["beats_naive"] for r in rows])), 1,
                    ),
                }

    (BREAK_DIR / "before_after_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8",
    )
    return summary


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    started = time.perf_counter()

    print("=== Phase 6.2 structural-break validation ===")
    print("\n[1/5] before vs after across all patterns")
    records = evaluate(num_seeds=30)

    print("\n[2/5] false-positive census")
    census = false_positive_census(num_seeds=50)

    print("\n[3/5] performance benchmark")
    bench = benchmark()

    print("\n[4/5] plots")
    plots = plot_scenarios()
    plots.append(plot_before_after(records))
    plots.append(plot_critical_case())
    print("   ", ", ".join(plots))

    print("\n[5/5] summary")
    summary = summarise(records)

    print("\n--- False positives (non-break shapes) ---")
    for name, row in census.items():
        if not row["expects_break"]:
            print(f"  {name:32} {row['fire_rate_pct']:5.1f}%")
    print("--- Detection (break shapes) ---")
    for name, row in census.items():
        if row["expects_break"]:
            print(f"  {name:32} {row['fire_rate_pct']:5.1f}%  "
                  f"located t={row['median_detected_period']} "
                  f"(true t={row['true_break_period']})")

    print("\n--- Mean MASE, PATTERN mode ---")
    for name in PATTERN_NAMES + BREAK_SCENARIOS:
        blk = summary["by_pattern"][name].get("PATTERN", {})
        if "OFF" in blk and "ON" in blk:
            print(f"  {name:32} {blk['OFF']['mase']:7.3f} -> "
                  f"{blk['ON']['mase']:7.3f}  ({blk['delta_mase']:+.3f})  "
                  f"adapted {blk['ON']['adapted_pct']:5.1f}%")

    print(f"\nCompleted in {time.perf_counter() - started:.1f}s")
