"""
NetGravity — Forecast evidence pack
=====================================
Selects the figures a `ForecastResult` actually contains, in the shape
`build_evidence_pack` indexes and the reasoning template narrates.

WHY THIS IS A SELECTION AND NOT A CALCULATION
----------------------------------------------
Every number below is read off the result. Nothing here averages, projects,
scores or rescales — the forecasting engine has already done the modelling,
and a second opinion formed in this module would be a number nobody validated
and that `numeric_grounding.py` would then be checking against itself.

WHAT IT DELIBERATELY DOES NOT CARRY
-----------------------------------
Any statement about WHY demand moves. Nothing in `netgravity/forecasting/`
computes a cause: the engines fit a series, detect a level shift, and measure
their own error. A payload carrying a "driver" field would invite the narrator
to explain a cause the system never established, which is the one thing a
forecast explanation must not do. Structural breaks are reported as WHAT was
detected (a shift, where, how large) — never as why it happened.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _value(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field off a model or a dict, whichever the caller has."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum_value(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    return getattr(obj, "value", None) or str(obj)


def _series_summary(series: Any) -> Dict[str, Any]:
    """One series, reduced to the figures that can be spoken about."""
    points = list(_value(series, "points", []) or [])
    accuracy = _value(series, "accuracy")
    break_result = _value(series, "structural_break")

    summary: Dict[str, Any] = {
        "market_id": _value(series, "market_id", ""),
        "product_id": _value(series, "product_id", ""),
        "status": _enum_value(_value(series, "status")),
        "engine": _value(series, "engine", ""),
        "pattern": _enum_value(_value(series, "pattern")),
        "n_history_periods": _value(series, "n_history_periods", 0),
        "n_forecast_periods": len(points),
        # Why the status is what it is. Present and empty on a healthy series.
        "reason": _value(series, "reason", "") or "",
    }

    if points:
        first, last = points[0], points[-1]
        summary.update({
            "first_period": _value(first, "period"),
            "last_period": _value(last, "period"),
            # The band, at both ends of the horizon. Read, not derived: a
            # forecast's uncertainty widening is a fact the engine states in
            # its own p10/p90, and computing a "widening rate" here would be
            # this module forming an opinion.
            "first_period_mean": _value(first, "mean"),
            "first_period_p10": _value(first, "p10"),
            "first_period_p90": _value(first, "p90"),
            "last_period_mean": _value(last, "mean"),
            "last_period_p10": _value(last, "p10"),
            "last_period_p90": _value(last, "p90"),
        })

    if accuracy is not None:
        # Measured out-of-sample error. `mase < 1` means the model beat a naive
        # forecast; there is deliberately no "confidence score" in this
        # codebase, and inventing one here would undo that.
        summary["accuracy"] = {
            "mae": _value(accuracy, "mae"),
            "rmse": _value(accuracy, "rmse"),
            "wape": _value(accuracy, "wape"),
            "mase": _value(accuracy, "mase"),
            "n_folds": _value(accuracy, "n_folds", 0),
            "method": _value(accuracy, "method", ""),
        }

    if break_result is not None:
        # Reported whether or not a break was found: "we looked and there was
        # none" is a result, and it is what makes an unchanged forecast on a
        # stable series auditable rather than merely unchanged.
        summary["structural_break"] = {
            "detected": bool(_value(break_result, "detected", False)),
            "period": _value(break_result, "period"),
            "method": _value(break_result, "method", ""),
        }

    return summary


def forecast_reasoning_payload(result: Any, *, horizon: Optional[int] = None,
                               max_series: int = 25) -> Dict[str, Any]:
    """
    The deterministic payload for a FORECAST-scope briefing.

    `max_series` bounds the pack: a briefing cites figures, and a network with
    four hundred market-product pairs would otherwise index tens of thousands
    of metrics to narrate six sentences over. The COUNTS below are taken over
    every series, so the bound changes what can be cited, never what is
    reported.
    """
    all_series: List[Any] = list(_value(result, "series", []) or [])
    ok_series = [s for s in all_series
                 if _enum_value(_value(s, "status")) == "OK"]

    status_counts: Dict[str, int] = {}
    for series in all_series:
        key = _enum_value(_value(series, "status")) or "UNKNOWN"
        status_counts[key] = status_counts.get(key, 0) + 1

    # Patterns present, because the pattern decides which engine ran and is
    # therefore part of "how was this produced", not an internal detail.
    patterns: Dict[str, int] = {}
    engines: Dict[str, int] = {}
    for series in ok_series:
        pattern = _enum_value(_value(series, "pattern")) or "UNCLASSIFIED"
        patterns[pattern] = patterns.get(pattern, 0) + 1
        engine = _value(series, "engine", "") or "unnamed"
        engines[engine] = engines.get(engine, 0) + 1

    breaks = [s for s in ok_series
              if bool(_value(_value(s, "structural_break"), "detected", False))]
    backtested = [s for s in ok_series if _value(s, "accuracy") is not None]

    return {
        "forecast": {
            "status": _enum_value(_value(result, "status")),
            "horizon_periods": horizon,
            "n_series_requested": len(all_series),
            "n_series_forecast": len(ok_series),
            "n_series_unavailable": len(all_series) - len(ok_series),
            "series_status_counts": status_counts,
            "patterns": patterns,
            "engines": engines,
            "n_structural_breaks_detected": len(breaks),
            "n_series_backtested": len(backtested),
            "warnings": list(_value(result, "warnings", []) or []),
            "errors": list(_value(result, "errors", []) or []),
        },
        "forecast_series": [_series_summary(s) for s in all_series[:max_series]],
    }
