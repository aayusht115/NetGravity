"""
NetGravity — Comparison evidence pack
=======================================
The figures behind "why this one rather than that one".

The Decision Package could say *"Nagpur is cheapest"* — a fact about one
scenario — but not *"Nagpur costs less than expanding Delhi while serving the
same demand"*, which is the sentence a decision actually needs. That sentence
is about a PAIR, and nothing was assembling pairs.

So this shapes the backend's already-computed ranking into the rows a
comparison narrative can be built from: for each alternative, how it differs
from the recommended one, on cost and on demand served.

Every number is read from the ranking. Nothing here re-ranks, and nothing
decides which scenario wins — `_comparison_verdict` in the scenarios API has
already done that, deterministically, from the authoritative KPI values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _num(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) else None


def comparison_reasoning_payload(
    *,
    ranked: List[Dict[str, Any]],
    recommended_scenario_id: Optional[str],
    verdict: str = "",
    baseline_cost: Optional[float] = None,
) -> Dict[str, Any]:
    """
    The deterministic payload for a COMPARISON-scope briefing.

    `ranked` is the compare endpoint's own output, in its own order.
    """
    rows = list(ranked or [])
    # STRICTLY the scenario the backend recommended. Falling back to the first
    # row named a winner when the backend had deliberately named none — which
    # happens exactly when nothing was comparable — and the narrative then
    # recommended reviewing a scenario whose cost the engine could not report.
    winner = next((r for r in rows
                   if r.get("scenario_id") == recommended_scenario_id), None)

    alternatives: List[Dict[str, Any]] = []
    for row in rows:
        if winner is None or row.get("scenario_id") == winner.get("scenario_id"):
            continue
        cost = _num(row.get("cost"))
        winner_cost = _num(winner.get("cost"))
        fill = _num(row.get("fill_rate"))
        winner_fill = _num(winner.get("fill_rate"))
        alternatives.append({
            "scenario_id": row.get("scenario_id"),
            "name": row.get("name"),
            "cost": cost,
            #: How much MORE the alternative costs than the recommended one.
            #: Positive means the recommendation is cheaper. None when either
            #: side has no comparable cost — never approximated.
            "cost_gap_vs_recommended": (None if cost is None or winner_cost is None
                                        else round(cost - winner_cost, 4)),
            "fill_rate": fill,
            #: Percentage points of demand served, relative to the winner.
            "fill_gap_vs_recommended_pts": (
                None if fill is None or winner_fill is None
                else round((fill - winner_fill) * 100.0, 4)),
            "comparable": bool(row.get("comparable")),
        })

    return {
        "comparison": {
            "verdict": verdict,
            "n_compared": len(rows),
            "recommended_scenario_id": (winner or {}).get("scenario_id"),
            "recommended_name": (winner or {}).get("name"),
            "recommended_cost": _num((winner or {}).get("cost")),
            "recommended_cost_delta": _num((winner or {}).get("cost_delta")),
            "recommended_fill_rate": _num((winner or {}).get("fill_rate")),
            "baseline_cost": _num(baseline_cost),
            "n_not_comparable": sum(1 for r in rows if not r.get("comparable")),
        },
        "comparison_alternatives": alternatives,
    }
