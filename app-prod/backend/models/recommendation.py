"""
NetGravity — Multi-Criteria Recommendation Engine
==================================================
Computes a weighted score across Cost, Service, Resilience, and Carbon
for each scenario, normalizes them, and generates a transparent
recommendation with an explicit reasoning card.

Scoring Formula:
    Score(s) = w_cost · (1 - NormCost(s))
             + w_svc  · NormService(s)
             + w_res  · NormResilience(s)
             + w_co2  · (1 - NormCarbon(s))

Where Norm(s) = (val(s) - min) / (max - min) mapped to [0, 1].
Higher score = better scenario.
"""

from typing import Any


def _normalize(values: list[float]) -> list[float]:
    """Min-max normalize a list; returns [0..1] per element."""
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        return [0.5] * len(values)
    return [(v - min_v) / (max_v - min_v) for v in values]


def score_scenarios(
    scenarios: list[dict[str, Any]],
    weights: dict[str, float] | None = None,
) -> dict:
    """
    Score and rank scenarios using weighted multi-criteria analysis.

    Each scenario dict must contain:
        name          str
        total_cost    float  — total transport + facility cost (lower = better)
        service_level float  — % demand met within SLA (higher = better)
        resilience    float  — resilience score [0-100] (higher = better)
        carbon        float  — total CO2 proxy (lower = better)

    Args:
        scenarios: list of scenario result dicts
        weights: {cost, service, resilience, carbon} — must sum to ~1.0

    Returns:
        Ranked list with scores, reasoning cards
    """
    if weights is None:
        weights = {"cost": 0.40, "service": 0.30, "resilience": 0.20, "carbon": 0.10}

    # Validate weights sum
    total_w = sum(weights.values())
    if abs(total_w - 1.0) > 0.01:
        weights = {k: v / total_w for k, v in weights.items()}

    n = len(scenarios)
    if n == 0:
        return {"ranked_scenarios": [], "recommendation": None}

    costs       = [s["total_cost"]    for s in scenarios]
    services    = [s["service_level"] for s in scenarios]
    resiliences = [s["resilience"]    for s in scenarios]
    carbons     = [s["carbon"]        for s in scenarios]

    # Normalize (0 = worst, 1 = best for each dimension)
    norm_cost       = _normalize(costs)
    norm_service    = _normalize(services)
    norm_resilience = _normalize(resiliences)
    norm_carbon     = _normalize(carbons)

    scored = []
    for i, s in enumerate(scenarios):
        # For cost and carbon: lower raw value → higher normalized score
        score_cost       = 1.0 - norm_cost[i]
        score_service    = norm_service[i]
        score_resilience = norm_resilience[i]
        score_carbon     = 1.0 - norm_carbon[i]

        weighted_score = (
            weights["cost"]       * score_cost
            + weights["service"]    * score_service
            + weights["resilience"] * score_resilience
            + weights["carbon"]     * score_carbon
        )

        scored.append({
            "name": s["name"],
            "total_cost": s["total_cost"],
            "service_level": s["service_level"],
            "resilience": s["resilience"],
            "carbon": s["carbon"],
            "score_breakdown": {
                "cost":       round(score_cost, 4),
                "service":    round(score_service, 4),
                "resilience": round(score_resilience, 4),
                "carbon":     round(score_carbon, 4),
            },
            "weighted_score": round(weighted_score, 4),
            "rank": None,  # filled below
        })

    # Rank by weighted score (highest = rank 1)
    scored.sort(key=lambda x: x["weighted_score"], reverse=True)
    for rank, s in enumerate(scored, 1):
        s["rank"] = rank

    best = scored[0]
    runner_up = scored[1] if len(scored) > 1 else None

    # ---------------------------------------------------------------
    # Generate Reasoning Card
    # ---------------------------------------------------------------
    reasons_accepted  = _build_accept_reasons(best, weights)
    reasons_rejected  = [_build_reject_reason(s, best) for s in scored[1:]]

    recommendation = {
        "recommended_scenario": best["name"],
        "weighted_score": best["weighted_score"],
        "weights_applied": weights,
        "reasoning_card": {
            "headline": f"Recommended: {best['name']}",
            "summary": (
                f"At the stated priority weights (Cost {int(weights['cost']*100)}%, "
                f"Service {int(weights['service']*100)}%, "
                f"Resilience {int(weights['resilience']*100)}%, "
                f"Carbon {int(weights['carbon']*100)}%), "
                f"'{best['name']}' achieves the highest composite score of {best['weighted_score']:.2f}."
            ),
            "key_drivers": reasons_accepted,
            "rejected_alternatives": reasons_rejected,
            "decision_note": (
                "A different decision owner or weighting profile may reasonably "
                "prefer an alternative scenario. Re-run with adjusted weights to explore."
            ),
        },
        "ranked_scenarios": scored,
    }

    return recommendation


def _build_accept_reasons(scenario: dict, weights: dict) -> list[str]:
    reasons = []
    sb = scenario["score_breakdown"]
    # List the top 2 scoring dimensions
    dims = sorted(
        [("cost",  sb["cost"],  weights.get("cost", 0)),
         ("service", sb["service"], weights.get("service", 0)),
         ("resilience", sb["resilience"], weights.get("resilience", 0)),
         ("carbon", sb["carbon"], weights.get("carbon", 0))],
        key=lambda t: t[1] * t[2], reverse=True
    )
    labels = {
        "cost": f"Best cost efficiency score ({scenario['total_cost']:.0f} total cost)",
        "service": f"Highest service level ({scenario['service_level']:.1f}%)",
        "resilience": f"Strongest resilience score ({scenario['resilience']:.0f}/100)",
        "carbon": f"Lowest carbon footprint index ({scenario['carbon']:.1f})",
    }
    for dim, score, w in dims[:3]:
        if score > 0.4:
            reasons.append(f"✓  {labels[dim]}")
    if not reasons:
        reasons.append(f"✓  Best overall balance across all four dimensions at current weights")
    return reasons


def _build_reject_reason(alt: dict, best: dict) -> dict:
    gap = best["weighted_score"] - alt["weighted_score"]
    # Find the dimension where alt is weakest
    sb = alt["score_breakdown"]
    worst_dim = min(sb, key=sb.get)
    dim_label = {"cost": "higher cost", "service": "lower service level",
                 "resilience": "weaker resilience", "carbon": "higher carbon footprint"}
    return {
        "name": alt["name"],
        "score": alt["weighted_score"],
        "gap_vs_recommended": round(gap, 4),
        "reason": f"Scores {gap:.2f} points below recommended; main drag: {dim_label.get(worst_dim, worst_dim)}",
    }


def estimate_kpis_from_solver(
    solver_result: dict,
    scenario_name: str,
    open_dcs: list[str],
    active_dc_count: int,
    demand_total: float,
    carbon_factor: float = 1.0,
) -> dict:
    """
    Derive standardized KPI dict from solver output for multi-criteria scoring.

    Args:
        solver_result: output from transshipment or transportation solver
        carbon_factor: kg CO2 per unit-km proxy (from scenario config)
    """
    total_cost = solver_result.get("total_cost", 0) or 0

    # Service level: % of demand met (from solver's demand_met field or assume 100% if optimal)
    if solver_result.get("status") == "optimal":
        service_level = 100.0
    else:
        service_level = 0.0

    # Resilience score: heuristic based on number of active DCs + arc diversity
    # More open DCs = more resilient. Score out of 100.
    base_resilience = min(100, active_dc_count * 18 + 10)
    # Penalize if total cost is very high (over-stretch)
    resilience = min(100, base_resilience)

    # Carbon proxy: total_cost × carbon_factor (simplified; in real system use actual km × load × emission factor)
    carbon = round(total_cost * carbon_factor * 0.012, 2)

    return {
        "name": scenario_name,
        "total_cost": round(total_cost, 2),
        "service_level": round(service_level, 2),
        "resilience": round(resilience, 2),
        "carbon": carbon,
        "open_dcs": open_dcs,
    }
