"""
NetGravity — Why a solve came back infeasible
==============================================
An infeasible answer used to be an answer with nothing in it. Measured on the
case-16 fixture at 2x demand:

    status      : INFEASIBLE
    warnings    : []
    total_cost  : 0.0
    unmet_demand: 0.0

Every line is true and the set of them is useless. The worst is
`unmet_demand: 0.0` — it means "not computed" and reads as "nothing is short",
on a network that could not reach part of its demand. A screen rendering that
showed an empty network rather than "you are short of capacity".

WHAT THIS DOES
--------------
One diagnostic re-solve, only when the strict model proved infeasible, asking
the same network a weaker question: serve what you can, and tell me what is
left. Its SERVICE figures become the diagnosis.

WHAT IT IS NOT
--------------
Not a plan, and not a result. The solve it describes is still INFEASIBLE and
still has no KPIs. Its cost figures are read by nobody: with shortage
permitted the objective is `business_cost + 1e6 x unserved`, which is not
money anyone pays, and reporting any part of it as such is the exact failure
this codebase has had to undo elsewhere.

That separation is not new. `resilience/rei.py::_service_diagnostic` has
always done exactly this for a disrupted network, for the same reason: a bare
"infeasible" carries no measure of severity, and two infeasible networks
cannot be told apart.

WHY IT LIVES HERE AND NOT IN `milp.py`
--------------------------------------
Because it is reporting, not formulation. `milp.py` is guarded — see
`test_extraction_agent.py::test_solver_internals_are_not_edited_by_accident` —
and the guard is right: an edit to the MILP must be a deliberate, separately
tested change, and a second solve plus a sentence generator is not part of the
model. `orchestrator/engines/deterministic.py` already owns what to do when a
solve proves infeasible (`_solve_relaxed_to_shortage`), and it calls this.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig
from netgravity.schemas.results import (
    InfeasibilityDiagnosis,
    OptimizationResult,
    SolverStatus,
)

logger = logging.getLogger(__name__)

#: How many short markets a diagnosis names before it stops listing them. A
#: diagnosis naming forty markets has named none of them.
_MARKET_LIMIT = 5

_MARKET_ROLES = {"MARKET", "CUSTOMER"}

#: `(network, config, scenario_id) -> OptimizationResult`
SolveFn = Callable[[CanonicalNetwork, OptimizationConfig, Optional[str]],
                   OptimizationResult]


def _role(facility: Any) -> str:
    return getattr(facility.role, "value", str(facility.role))


def structural_diagnosis(network: CanonicalNetwork,
                         result: OptimizationResult) -> InfeasibilityDiagnosis:
    """
    A network that failed a validation check before any model was built.

    `milp_solve` fast-fails on the critical codes (V-014, V-007, V-008, V-010)
    and returns without solving, putting the validator's own words in
    `solver.warnings`. There is no diagnostic solve to run here and none is
    wanted: the network failed a STRUCTURAL check, and re-solving it with
    shortage permitted would fail the same check and cost a solve to learn
    nothing.

    It still returns a diagnosis, because a consumer reading
    `result.infeasibility` should not have to handle "sometimes present" for
    the same status — and because the reason here is known exactly rather than
    unavailable.
    """
    reason = "; ".join(result.solver.warnings) or "a structural validation check failed"
    return InfeasibilityDiagnosis(
        diagnosed=False,
        total_demand=round(sum(float(d.quantity) for d in network.demands), 4),
        summary=(f"This network fails a structural check before it can be "
                 f"solved: {reason} No amount of capacity fixes this — the "
                 f"network as described cannot be modelled."),
        reason=reason,
    )


def shortfall_from(network: CanonicalNetwork,
                   relaxed: OptimizationResult) -> Dict[str, Any]:
    """
    What a SOLVED shortage-permitted plan could not serve, read off its flows.

    Pure reading — no solve. Shared by the two callers that already hold such
    a plan:

      * `diagnose()`, which had to build one because nothing else would;
      * `OptimizationClient._solve_relaxed_to_shortage`, whose relaxed plan is
        the same model and which would otherwise pay for a second identical
        solve to learn the same thing.

    Service quantities only. The relaxed objective is
    `business_cost + shortage_penalty x unserved`, which is not money.
    """
    total_demand = round(sum(float(d.quantity) for d in network.demands), 4)
    market_ids = {f.id for f in network.facilities if _role(f) in _MARKET_ROLES}

    served_by_market: Dict[str, float] = {}
    for flow in relaxed.flow_decisions:
        if flow.destination_id in market_ids:
            served_by_market[flow.destination_id] = (
                served_by_market.get(flow.destination_id, 0.0) + float(flow.flow_units))

    demand_by_market: Dict[str, float] = {}
    for demand in network.demands:
        demand_by_market[demand.market_id] = (
            demand_by_market.get(demand.market_id, 0.0) + float(demand.quantity))

    short: List[Dict[str, Any]] = [
        {"market_id": mid,
         "unserved": round(want - served_by_market.get(mid, 0.0), 2),
         "demand": round(want, 2)}
        for mid, want in demand_by_market.items()
        if want - served_by_market.get(mid, 0.0) > 1.0
    ]
    short.sort(key=lambda r: -r["unserved"])

    served_total = round(sum(served_by_market.values()), 4)
    unserved = round(max(0.0, total_demand - served_total), 4)

    # Sites the plan opens. On a network carrying CANDIDATE facilities this is
    # the answer to "where would more capacity go", and it is the one thing an
    # infeasible screen could never say.
    would_open = sorted(
        d.facility_id for d in relaxed.facility_decisions
        if getattr(d, "is_open", False) and d.facility_id not in market_ids)
    proposed = {f.id for f in network.facilities
                if getattr(f.status, "value", str(f.status)) == "CANDIDATE"}

    return {
        "total_demand": total_demand,
        "unserved_demand": unserved,
        "unserved_rate": (round(unserved / total_demand, 6)
                          if total_demand > 0 else None),
        "short_markets": short[:_MARKET_LIMIT],
        "would_open": would_open,
        "would_open_candidates": sorted(set(would_open) & proposed),
    }


def diagnose(
    network: CanonicalNetwork,
    config: OptimizationConfig,
    scenario_id: Optional[str],
    solve_fn: SolveFn,
) -> InfeasibilityDiagnosis:
    """
    Why this network could not serve its demand, in units.

    `solve_fn` is injected rather than imported so this module never reaches
    into the solver itself; the caller passes whatever it already uses.

    Never raises. A diagnosis that fails leaves the caller with exactly what it
    had before this existed, which is what every infeasible solve used to
    return.
    """
    total_demand = round(sum(float(d.quantity) for d in network.demands), 4)
    relaxed_config = config.model_copy(update={"allow_shortage": True})

    try:
        relaxed = solve_fn(network, relaxed_config, scenario_id)
    except Exception as exc:  # noqa: BLE001 — a failed diagnosis is not a failed solve
        logger.warning("optimization.diagnosis_failed scenario=%s error=%s",
                       scenario_id, exc)
        return InfeasibilityDiagnosis(
            diagnosed=False, total_demand=total_demand,
            reason=f"the diagnostic model could not be built ({type(exc).__name__}).")

    if relaxed.solver.status == SolverStatus.INFEASIBLE:
        # Stronger than a shortfall, and worth saying plainly: the network
        # cannot be solved even when it is allowed to give up on some of the
        # demand. Something other than capacity is binding — a contract that
        # pins a site shut, a market no lane reaches, two overrides that cannot
        # both hold. Saying "you are short of capacity" here would send a
        # planner to buy something that does not help.
        return InfeasibilityDiagnosis(
            diagnosed=False, total_demand=total_demand,
            summary=("This network has no solution even when the model is "
                     "allowed to leave demand unserved, so the obstacle is not "
                     "the amount of capacity. Something is making the problem "
                     "itself contradictory — a site pinned open or shut, a "
                     "market no open lane reaches, or two overrides that cannot "
                     "both hold."),
            reason="the diagnostic model was itself infeasible.")

    if not relaxed.is_solved:
        return InfeasibilityDiagnosis(
            diagnosed=False, total_demand=total_demand,
            reason=(f"the diagnostic model returned "
                    f"{relaxed.solver.status.value} rather than a solution."))

    reading = shortfall_from(network, relaxed)
    short = reading["short_markets"]
    unserved = reading["unserved_demand"]
    rate = reading["unserved_rate"]
    would_open = reading["would_open"]
    would_open_candidates = reading["would_open_candidates"]

    if unserved > 0:
        where = ", ".join(r["market_id"] for r in short[:_MARKET_LIMIT])
        # A proposed site the diagnostic opens is the actionable half of this.
        build = ""
        if would_open_candidates:
            names = {f.id: (f.name or f.id) for f in network.facilities}
            listed = ", ".join(names.get(fid, fid) for fid in would_open_candidates)
            plural = ("is a site" if len(would_open_candidates) == 1 else "are sites")
            build = (f" Serving what it can, the optimiser opens {listed}, "
                     f"which {plural} you have proposed but not built.")
        summary = (
            f"This network cannot serve {unserved:,.0f} of its "
            f"{total_demand:,.0f} units of demand within its own service "
            f"levels — {rate * 100:.1f}% of it"
            f"{f', concentrated in {where}' if where else ''}. "
            f"That is why no fully-served plan exists.{build} The figures come "
            f"from a diagnostic solve that was allowed to leave demand "
            f"unserved; it is not a plan, and no cost is reported from it."
        )
    else:
        summary = (
            "The diagnostic model served all of the demand, so the strict "
            "solve failed on something other than volume — most often a "
            "service level no open lane can meet.")

    logger.info(
        "optimization.diagnosed scenario=%s unserved=%s of %s markets_short=%d",
        scenario_id, unserved, total_demand, len(short),
    )
    return InfeasibilityDiagnosis(
        diagnosed=True,
        unserved_demand=unserved,
        total_demand=total_demand,
        unserved_rate=rate,
        short_markets=short[:_MARKET_LIMIT],
        would_open=would_open,
        would_open_candidates=would_open_candidates,
        summary=summary,
    )


__all__ = ["diagnose", "shortfall_from", "structural_diagnosis"]
