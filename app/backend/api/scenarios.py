"""
NetGravity — Scenario Planning & Simulation API Blueprint
=========================================================
Project-scoped what-if scenarios, solved by the real MILP engine through the
orchestrator and reported through the Phase 9.1 authoritative KPI layer.

Phase 10.0 rewrite. The prototype version of this blueprint:

  * shipped two fully hardcoded "canonical" scenarios, complete with fabricated
    cost/SLA/carbon figures, fabricated robustness tests all marked PASS, and a
    fabricated `aiAssessment` narrative;
  * on `/simulate`, ran a REAL orchestrator solve, obtained REAL
    `ScenarioMetricDelta` objects — and then discarded them, returning
    `totalCost: 1205000`, `sla_val = 95.5`, `avgUtil: 68.2`, `carbonKg: 102400`
    as literals, with a fabricated `-6.5` fallback for the one delta it did read;
  * stored every user's scenarios in one process-global list.

Every figure returned by this module now originates in `KPIRegistry` and carries
its `KPIStatus`. Where a value cannot be computed, the status says so and the
value is null — never a plausible substitute (brief §9, §24).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.correlation import orchestrator_request_id
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.metrics.registry import KPIRegistry
from netgravity.orchestrator.schemas.requests import (
    NETWORK_WIDE_ACTIONS,
    Actor,
    ActorRole,
    GreenfieldSiteSpec,
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

from netgravity.orchestrator.explanation_llm import (
    explanation_reasoning_agent,
    explanations_llm_enabled,
)

logger = logging.getLogger(__name__)

_ACTION_MAP = {
    "CHANGE_CAPACITY": ScenarioActionType.CHANGE_CAPACITY,
    "CHANGE_DEMAND": ScenarioActionType.CHANGE_DEMAND,
    "OPEN_FACILITY": ScenarioActionType.OPEN_FACILITY,
    "CLOSE_FACILITY": ScenarioActionType.CLOSE_FACILITY,
    # A greenfield site, NOT an alias for OPEN_FACILITY.
    #
    # It used to be one, which meant "open a facility" could only ever pin open
    # a site the client already operates — the builder offered a dropdown of
    # their own DCs and plants, and choosing one asked the solver to keep open
    # something it was already free to keep open. "Where should we put a new
    # DC?" was unanswerable through this API.
    "ADD_FACILITY": ScenarioActionType.ADD_FACILITY,
    "NEW_FACILITY": ScenarioActionType.ADD_FACILITY,
    "REMOVE_FACILITY": ScenarioActionType.CLOSE_FACILITY,
    "SHIFT_VOLUME": ScenarioActionType.SHIFT_VOLUME,
    "VOLUME_SHIFT": ScenarioActionType.SHIFT_VOLUME,
    "CHANGE_TRANSPORT_COST": ScenarioActionType.CHANGE_TRANSPORT_COST,
    "CHANGE_SLA": ScenarioActionType.CHANGE_SLA,
}

#: KPIs surfaced on the scenario comparison cards, in display order.
_HEADLINE_METRICS = (
    "business_network_cost",
    "pct_demand_in_sla",
    "demand_fill_rate",
    "avg_utilization_pct",
    "max_utilization_pct",
    "total_carbon_kg",
)


def _scenario_state_key(context: Any) -> Optional[str]:
    """The `scenario:<id>` key this execution wrote, if any."""
    for key in getattr(context, "network_states", {}):
        if key.startswith("scenario:"):
            return key
    return None


def _facility_states(registry: Any, context: Any, key: Optional[str]) -> Dict[str, Any]:
    """
    Per-facility utilisation, throughput and open/closed for one solved state.

    Flattened to plain values because this feeds a map, not an audit trail; the
    full `KPIResult` with its status is available from `/api/kpis/facilities`.
    A metric the solve did not report stays None rather than becoming zero.
    """
    if not key:
        return {}
    out: Dict[str, Any] = {}
    for facility_id, metrics in registry.facility_kpis(context, key=key).items():
        def value(metric_id: str) -> Any:
            result = metrics.get(metric_id)
            return result.value if result and result.status.value == "VALID" else None

        out[facility_id] = {
            "utilPct": value("utilization_pct"),
            "throughput": value("throughput_units"),
            "capacity": value("capacity_units"),
            "isOpen": value("is_open"),
        }
    return out


def _lane_flows(registry: Any, context: Any, key: Optional[str]) -> List[Dict[str, Any]]:
    """Solved volume and cost per lane for one state, keyed origin->destination."""
    if not key:
        return []
    return registry.flow_kpis(context, key=key)


def _new_sites(engine: Any, scenario_key: Optional[str],
               snapshot_id: str) -> List[Dict[str, Any]]:
    """
    Facilities that exist in the scenario network and not in the snapshot.

    A greenfield site is in no uploaded network, so the map has no coordinates
    for it and would draw a scenario that opens a new DC without ever showing
    the DC. `FacilitySummary` — which is what the KPI layer reports per
    facility — carries no latitude or longitude, by design: it is a solver
    outcome, not topology. So the position is read from the materialised
    scenario network the builder actually solved, which is the only place it is
    authoritative.

    Returns [] for every scenario that adds nothing, which is most of them.
    """
    if not scenario_key or not scenario_key.startswith("scenario:"):
        return []
    scenario_id = scenario_key.split(":", 1)[1]
    try:
        record = engine.scenarios.get(scenario_id)
        baseline = engine.snapshots.get(snapshot_id).network
    except Exception:  # noqa: BLE001 — an absent record is simply no new site
        return []

    known = {f.id for f in baseline.facilities}
    out: List[Dict[str, Any]] = []
    for facility in record.network.facilities:
        if facility.id in known:
            continue
        out.append({
            "id": facility.id,
            "name": facility.name,
            "role": getattr(facility.role, "value", str(facility.role)),
            "lat": facility.latitude,
            "lng": facility.longitude,
            "capacity": facility.capacity_units_per_period,
            "handlingCost": facility.handling_cost_per_unit,
            "fixedCostPerYear": facility.fixed_cost_per_year,
        })
    return out


#: A site the plan has no more room in. Not 100.0 — a solve reports 99.97%
#: when it has filled a site to the unit, and a reader told that site has
#: headroom because of a rounding tail has been told something false.
_SATURATED_PCT = 99.0

#: Running hot, but not yet at the ceiling. The band the facility panel and
#: the mapper already use for "high", so one site is not "hot" on one screen
#: and "healthy" on the next.
_LOADED_PCT = 85.0

#: How many sites a capacity account names before it stops listing them. A
#: recommendation that names twenty sites has recommended nothing.
_CAPACITY_SITE_LIMIT = 5


def _facility_meta(engine: Any, scenario_key: Optional[str],
                   snapshot_id: str) -> Dict[str, Dict[str, Any]]:
    """
    Name, role and region per facility id, from the network that was solved.

    The KPI layer reports per-facility OUTCOMES and carries no topology — no
    name, no region — by design. So "which region has no room left" cannot be
    answered from KPIs alone, and is read here from the materialised scenario
    network (which includes any greenfield site the scenario added), falling
    back to the uploaded snapshot.

    Returns {} when neither network can be read. A missing region stays None:
    an upload that does not state regions cannot be told which region needs a
    site, and saying so is the only honest answer available.
    """
    networks = []
    if scenario_key and scenario_key.startswith("scenario:"):
        try:
            networks.append(engine.scenarios.get(
                scenario_key.split(":", 1)[1]).network)
        except Exception:  # noqa: BLE001 — an absent record is simply no names
            pass
    try:
        networks.append(engine.snapshots.get(snapshot_id).network)
    except Exception:  # noqa: BLE001
        pass

    out: Dict[str, Dict[str, Any]] = {}
    for network in networks:
        for facility in getattr(network, "facilities", []) or []:
            if facility.id in out:
                continue
            region = getattr(facility, "region", None)
            out[facility.id] = {
                "name": facility.name or facility.id,
                "role": getattr(facility.role, "value", str(facility.role)),
                "region": (str(region).strip() or None) if region else None,
            }
    return out


def _site_row(facility_id: str, meta: Dict[str, Dict[str, Any]],
              scenario: Dict[str, Any],
              baseline: Dict[str, Any]) -> Dict[str, Any]:
    """One site's load under the scenario, and how much of it is new."""
    info = meta.get(facility_id) or {}
    throughput = scenario.get("throughput")
    was = baseline.get("throughput")
    return {
        "id": facility_id,
        "name": info.get("name") or facility_id,
        "role": info.get("role"),
        "region": info.get("region"),
        "util_pct": scenario.get("utilPct"),
        "baseline_util_pct": baseline.get("utilPct"),
        "throughput": throughput,
        "capacity": scenario.get("capacity"),
        # The extra volume this site has to carry BECAUSE of the change.
        # None — not zero — when either side is unavailable.
        "added_units": (round(throughput - was, 2)
                        if isinstance(throughput, (int, float))
                        and isinstance(was, (int, float)) else None),
        "headroom_units": (round(scenario["capacity"] - throughput, 2)
                           if isinstance(throughput, (int, float))
                           and isinstance(scenario.get("capacity"), (int, float))
                           else None),
    }


def _capacity_response(engine: Any, snapshot_id: str,
                       scenario_key: Optional[str],
                       baseline_states: Dict[str, Any],
                       scenario_states: Dict[str, Any],
                       kpis: Dict[str, Any]) -> Dict[str, Any]:
    """
    What this plan asks of the existing sites, and where it runs out of them.

    The question a demand scenario is actually asking. The recommendation card
    answered it with the network's cost narration, which is the same answer it
    gives every scenario; this is the part that differs between raising demand
    by 5% and raising it by 50%.

    Four facts, each read from authoritative per-facility values:

      * `at_ceiling`   — sites the plan fills completely. These are where more
                         capacity has to come from if anything is to change.
      * `working_harder` — sites carrying materially more than they did, with
                         room still on them. The utilisation a planner has to
                         actually achieve.
      * `idle`         — capacity the plan left closed. Reopening is cheaper
                         than building, so it is named before any new site is.
      * `regions_without_room` — regions whose every site is at its ceiling and
                         which have nothing closed left to reopen. Only these
                         can honestly be called places a new site is needed,
                         and only on an upload that states regions at all.

    Returns {} when the solve reported no per-facility state — an empty block,
    not an invented one.
    """
    if not scenario_states:
        return {}

    meta = _facility_meta(engine, scenario_key, snapshot_id)

    at_ceiling: List[Dict[str, Any]] = []
    working_harder: List[Dict[str, Any]] = []
    idle: List[Dict[str, Any]] = []
    open_headroom = 0.0
    open_headroom_known = False
    idle_capacity = 0.0
    by_region: Dict[str, Dict[str, Any]] = {}

    for facility_id, state in scenario_states.items():
        base = baseline_states.get(facility_id) or {}
        row = _site_row(facility_id, meta, state, base)
        util = row["util_pct"]
        region = row["region"]

        if state.get("isOpen") is False:
            # Capacity the plan chose not to use. `utilPct` is written as 0 for
            # a site the solve did not open, so it is the OPEN FLAG that
            # distinguishes an unused site from an empty one.
            if isinstance(row["capacity"], (int, float)) and row["capacity"] > 0:
                idle_capacity += row["capacity"]
                idle.append(row)
                if region:
                    slot = by_region.setdefault(region, {
                        "region": region, "at_ceiling": 0,
                        "open_headroom_units": 0.0, "idle_capacity_units": 0.0})
                    slot["idle_capacity_units"] += row["capacity"]
            continue

        if isinstance(row["headroom_units"], (int, float)):
            open_headroom += max(row["headroom_units"], 0.0)
            open_headroom_known = True

        if not isinstance(util, (int, float)):
            continue

        slot = None
        if region:
            slot = by_region.setdefault(region, {
                "region": region, "at_ceiling": 0,
                "open_headroom_units": 0.0, "idle_capacity_units": 0.0})
            if isinstance(row["headroom_units"], (int, float)):
                slot["open_headroom_units"] += max(row["headroom_units"], 0.0)

        if util >= _SATURATED_PCT:
            at_ceiling.append(row)
            if slot is not None:
                slot["at_ceiling"] += 1
        elif util >= _LOADED_PCT and (row["added_units"] or 0) > 0:
            working_harder.append(row)

    # Busiest first: the site a planner has to deal with is the fullest one.
    at_ceiling.sort(key=lambda r: -(r["added_units"] or 0))
    working_harder.sort(key=lambda r: -(r["util_pct"] or 0))
    idle.sort(key=lambda r: -(r["capacity"] or 0))

    # A region qualifies as needing its own site only when it has a site the
    # plan filled, nothing left to reopen, and no meaningful room on anything
    # still open. Anything weaker than that recommends building where a
    # reopening or a transfer would have done.
    regions_without_room = [
        dict(slot) for slot in by_region.values()
        if slot["at_ceiling"] > 0
        and slot["idle_capacity_units"] <= 0
        and slot["open_headroom_units"] < 1.0
    ]
    regions_without_room.sort(key=lambda r: -r["at_ceiling"])

    unserved = _valid(kpis, "unserved_demand")
    total_demand = _valid(kpis, "total_demand")

    return {
        "at_ceiling": at_ceiling[:_CAPACITY_SITE_LIMIT],
        "at_ceiling_count": len(at_ceiling),
        "working_harder": working_harder[:_CAPACITY_SITE_LIMIT],
        "working_harder_count": len(working_harder),
        "idle": idle[:_CAPACITY_SITE_LIMIT],
        "idle_count": len(idle),
        "idle_capacity_units": round(idle_capacity, 2) if idle else 0.0,
        # None rather than 0.0 when no open site reported both figures — a
        # network whose headroom is unknown must not read as a network with
        # none.
        "open_headroom_units": (round(open_headroom, 2)
                                if open_headroom_known else None),
        "regions_without_room": regions_without_room,
        # Whether the upload states regions at all. Without it, "which region
        # needs a site" has no answer and the card says that instead of
        # guessing one from coordinates.
        "regions_known": any(m.get("region") for m in meta.values()),
        "unserved_units": unserved,
        "total_demand_units": total_demand,
        "verdict": _capacity_verdict(
            unserved, open_headroom if open_headroom_known else None,
            idle_capacity, len(at_ceiling)),
    }


def _capacity_verdict(unserved: Optional[float], headroom: Optional[float],
                      idle_capacity: float, at_ceiling: int) -> str:
    """
    One sentence naming the binding constraint, or admitting there isn't one.

    The distinction that matters and that a cost ranking hides: demand can go
    unserved on a network with capacity to spare, because capacity in the
    wrong place, or out of reach of a service promise, is capacity that cannot
    be used. Telling a planner to add capacity in that situation would be
    advice to spend money on a constraint that is not binding.
    """
    if unserved is None:
        return ""
    if unserved <= 0:
        if at_ceiling:
            return (f"This plan serves all of the demand, with {at_ceiling} "
                    f"{'site' if at_ceiling == 1 else 'sites'} run to their "
                    "ceiling. There is no room left at those for anything "
                    "further.")
        return "This plan serves all of the demand without filling any site."

    if headroom is not None and headroom > unserved:
        spare = f"{headroom:,.0f}"
        return (f"Capacity is not what is binding here: {spare} units of room "
                f"stay unused on the sites this plan opens, while "
                f"{unserved:,.0f} units go unserved. The demand that is missed "
                "is out of reach of the sites that have room — by distance, by "
                "lane, or by the service promise — so more capacity at those "
                "sites would not serve it.")

    if idle_capacity > 0:
        return (f"{unserved:,.0f} units go unserved and the open sites are "
                f"full, but {idle_capacity:,.0f} units of capacity sit in "
                "sites this plan left closed. Reopening comes before building.")

    return (f"{unserved:,.0f} units go unserved and there is no room left to "
            "serve them from: every site this plan opens is at its ceiling and "
            "there is nothing closed to reopen.")


def _overrides_of(engine: Any, scenario_key: Optional[str]) -> List[str]:
    """The builder's own description of what this scenario changed."""
    if not scenario_key or not scenario_key.startswith("scenario:"):
        return []
    try:
        return list(engine.scenarios.get(scenario_key.split(":", 1)[1]).overrides)
    except Exception:  # noqa: BLE001
        return []


def _serialise_kpis(results: Dict[str, Any]) -> Dict[str, Any]:
    """`KPIResult` -> JSON, status and provenance preserved verbatim."""
    return {k: v.model_dump(mode="json") for k, v in results.items()}


# ---------------------------------------------------------------------------
# Comparing scenarios
#
# The ranking and the recommendation are made HERE, from the authoritative
# KPI values, not in the browser. A screen that ranks its own rows decides
# what to recommend in JavaScript, where the decision is invisible to the
# audit trail, untestable from the backend suite, and free to disagree with
# whatever the same numbers say elsewhere.
# ---------------------------------------------------------------------------

#: A metric is only usable when its own status says so. A None value with a
#: non-VALID status is a refusal, and must never be read as a number.
def _valid(kpis: Dict[str, Any], metric_id: str) -> Optional[float]:
    result = (kpis or {}).get(metric_id) or {}
    if result.get("status") != "VALID":
        return None
    value = result.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def _rank_scenarios(baseline: Dict[str, Any],
                    records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Cheapest first, on the solver's own business network cost.

    A scenario whose cost is not VALID is still returned — it was compared,
    and dropping it would silently shorten the comparison — but it ranks last
    and is marked not comparable rather than being given a position it did
    not earn.

    Each row also carries the split the headline needs. `cost_delta` measures
    a scenario against the network AS IT RUNS, which is the figure a client
    recognises but is not the effect of the change: the baseline pins the
    footprint open and a scenario may close sites, so the gap contains the
    whole value of re-optimising the footprint as well. `change_effect`
    measures the scenario against the SAME network re-solved with that same
    freedom, and is therefore what the change itself did. Both are reported;
    neither is inferred from the other.
    """
    baseline_cost = _valid(baseline, "business_network_cost")
    baseline_fill = _valid(baseline, "demand_fill_rate")

    rows: List[Dict[str, Any]] = []
    for record in records:
        kpis = record.get("scenario_kpis") or {}
        cost = _valid(kpis, "business_network_cost")
        fill = _valid(kpis, "demand_fill_rate")
        reference_cost = _valid(record.get("reference_kpis") or {},
                                "business_network_cost")
        rows.append({
            "scenario_id": record.get("id"),
            "name": record.get("name"),
            "cost": cost,
            "cost_delta": (None if cost is None or baseline_cost is None
                           else round(cost - baseline_cost, 4)),
            # The network re-solved with a scenario's own freedom and NO
            # change, and this scenario measured against it.
            "reference_cost": reference_cost,
            "reoptimisation_effect": (
                None if reference_cost is None or baseline_cost is None
                else round(reference_cost - baseline_cost, 4)),
            "change_effect": (None if cost is None or reference_cost is None
                              else round(cost - reference_cost, 4)),
            "fill_rate": fill,
            "fill_delta": (None if fill is None or baseline_fill is None
                           else round((fill - baseline_fill) * 100.0, 4)),
            "comparable": cost is not None and baseline_cost is not None,
        })
    # Deterministic regardless of the order the ids arrived in.
    #
    # Sorting on cost alone left ties — two scenarios with equal cost, or two
    # with no comparable cost at all — resolved by input order. So comparing
    # A and B named a different winner than comparing B and A, which is the
    # same analysis asked twice. The id is the tiebreak: arbitrary, but
    # stable, which is the property that matters.
    rows.sort(key=lambda r: (r["cost_delta"] is None,
                             r["cost_delta"] if r["cost_delta"] is not None else 0.0,
                             str(r["scenario_id"] or "")))
    return rows


def _capacity_risk(kpis: Dict[str, Any]) -> str:
    """
    The capacity-risk band for a solved side, from peak utilisation.

    Derived HERE and stored on the record because `_service_warning` needs it
    and the browser was the only place it existed — `capacityRiskFrom()` in
    scenario-mapper.js. A ranking that must not bury a capacity problem cannot
    depend on a band computed after the ranking, in another process.

    The thresholds are the mapper's own (90 / 75), reproduced rather than
    re-chosen so the two surfaces cannot disagree while both exist. "Unknown"
    when utilisation is unavailable — never "Low", which would report an
    unmeasured network as safe.
    """
    peak = _valid(kpis, "max_utilization_pct")
    if peak is None:
        return "Unknown"
    if peak >= 90:
        return "High"
    if peak >= 75:
        return "Medium"
    return "Low"


def _scenario_explanation(ctx: Any, kpis: Dict[str, Any],
                          attribution: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    The scenario's grounded briefing, in the shape the recommendation card reads.

    Already computed and already grounded — the scenario workflow runs
    `reasoning.synthesise` on every simulate, and `numeric_grounding` has
    re-checked every numeric claim by the time it gets here. Nothing is
    generated or recomputed; this selects fields off `ExecutiveBriefing`.

    Returns {} when the run produced no briefing, so the card says it has
    nothing to explain rather than showing the network's briefing in its place.
    """
    reasoning = getattr(ctx, "reasoning", None)
    briefing = getattr(reasoning, "briefing", None) if reasoning else None
    if briefing is None:
        return {}

    from netgravity.orchestrator.explanation_service import build_card

    return {
        # The ONE card the screen renders. Everything below it is the fuller
        # record, kept for the drawer rather than for the card.
        "card": build_card(reasoning, figures=_scenario_figures(kpis),
                           # The figure-free half in the technical detail; the
                           # amounts travel beside the card, below.
                           details=([attribution["text"]] if attribution else [])),
        # Where the difference against today comes from, as amounts. A reader
        # who runs one scenario never opens the comparison, and this is the
        # sentence that stops "demand +30%" reading as a saving.
        "attribution": dict(attribution or {}),
        "scope": briefing.scope.value,
        "entity_id": briefing.entity_id,
        "opening": briefing.opening,
        "context": briefing.context,
        "insights": [
            {
                "theme": item.theme,
                "headline": item.headline,
                "narrative": item.narrative,
                "severity": item.severity.value,
            }
            for item in briefing.kpi_insights
        ],
        "key_drivers": list(briefing.key_drivers),
        "recommendation": briefing.recommendation,
        "limitation": briefing.limitation,
        "evidence_completeness": briefing.evidence_completeness.value,
        "suggested_questions": list(briefing.suggested_questions),
        "missing_information": [m.model_dump(mode="json")
                                for m in briefing.missing_information],
        "source": getattr(reasoning, "source", "template"),
        "grounding": {"warnings": list(getattr(reasoning, "validation_warnings", []))},
    }


def _scenario_figures(kpis: Dict[str, Any]) -> List[Any]:
    """
    The three numbers for one scenario, supplied by code.

    Cost, demand served and sites open — the same quantities the comparison
    shows, so a reader moving between them is reading the same things. Money
    travels as an amount; the screen applies the project's currency.

    Read through `_valid`, so a metric whose status is not VALID becomes
    "Not available" rather than a number nobody stands behind. Reaching into
    the execution's raw network states instead would take the figure past the
    layer that decides whether it may be shown.
    """
    from netgravity.orchestrator.reasoning.card import Figure

    fill = _valid(kpis, "demand_fill_rate")
    open_sites = _valid(kpis, "n_facilities_open")
    return [
        Figure.money("Cost", _valid(kpis, "business_network_cost")),
        Figure(label="Demand served",
               value=(f"{fill * 100:,.1f}%" if fill is not None
                      else "Not available")),
        Figure(label="Sites open",
               value=(f"{open_sites:,.0f}" if open_sites is not None
                      else "Not available")),
    ]


#: How much demand a plan may serve below the baseline before the saving is
#: called what it is: a smaller promise, not a cheaper way of keeping the
#: same one. Percentage points.
_MATERIAL_FILL_DROP_PTS = 0.05


#: Below this, demand coverage is a problem in its own right and the cheapest
#: option cannot be presented as simply "the answer". Read from the policy
#: module rather than written here, so the screen and the engine draw the line
#: in the same place.
def _service_floor() -> float:
    try:
        from netgravity.config.defaults import SERVICE_THRESHOLDS

        return float(SERVICE_THRESHOLDS.get("fill_rate_floor", 0.95))
    except Exception:  # noqa: BLE001
        return 0.95


def _service_warning(best: Dict[str, Any], record: Optional[Dict[str, Any]]) -> str:
    """
    The thing a cost ranking must not be allowed to bury.

    "Cheapest" is a fact about cost and nothing else. A plan that costs less
    while stranding a third of demand, or while leaving a site at its limit,
    is cheaper and not therefore better — and a card headed with the cost
    alone invites exactly that reading.

    Returns "" only when there is genuinely nothing to warn about.
    """
    problems: List[str] = []

    fill = best.get("fill_rate")
    if isinstance(fill, (int, float)) and fill < _service_floor():
        problems.append(f"it still serves only {fill * 100:,.1f}% of demand")

    risk = str((record or {}).get("capacity_risk") or "").strip()
    if risk.lower() in ("high", "critical"):
        problems.append(f"capacity risk remains {risk.lower()}")

    if not problems:
        return ""
    joined = problems[0] if len(problems) == 1 else " and ".join(problems)
    return (f"This is the lower-cost option, but {joined}. The cheapest "
            f"scenario is not necessarily an acceptable one.")


def _attribution(best: Dict[str, Any]) -> Dict[str, Any]:
    """
    Where a headline saving actually comes from.

    Measured on a real upload: a +30% demand scenario reported 11.2% BELOW the
    network as it runs. Nothing was wrong with the arithmetic — the baseline
    pins twenty sites open and the scenario was free to shut four of them, so
    the gap was the redesign's saving minus the growth's cost. Read off the
    headline alone, "demand up 30%" was a cost reduction.

    Both halves are already in the row. This says which is which.

    IT RETURNS AMOUNTS, NOT A FORMATTED SENTENCE. The two figures are money,
    and the currency belongs to the upload — decided in `data.js::formatCurrency`
    and nowhere else. Composing the sentence here printed "167,846,924.60" on
    a card whose every other amount read "C$167.85M". `text` is the same
    statement with no figures in it, for a consumer that is not the browser.

    Empty when there is nothing to attribute: no reference solve, or a
    redesign worth nothing.
    """
    reopt = best.get("reoptimisation_effect")
    change = best.get("change_effect")
    if reopt is None or change is None or abs(reopt) < 1:
        return {}
    if abs(change) < 1:
        return {
            "reoptimisation_amount": reopt,
            "change_amount": change,
            "change_direction": "none",
            "text": ("None of this difference is the change itself: the solver "
                     "reaches the same plan with or without it. All of it comes "
                     "from re-optimising the footprint you already have, which "
                     "is available without this scenario."),
        }
    return {
        "reoptimisation_amount": reopt,
        "change_amount": change,
        "change_direction": "adds" if change > 0 else "saves",
        "text": ("Part of the difference against the network you run today "
                 "comes from re-optimising the footprint you already have — "
                 "available without this scenario — and part from the change "
                 "itself."),
    }


def _comparison_verdict(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    What the numbers say, in one sentence, and what it rests on.

    No branch states a figure the KPI layer did not report, and none of them
    recommends acting — this says which scenario the comparison ranks first
    and why, which is a finding, not a decision.
    """
    if not rows:
        return {"recommended_scenario_id": None,
                "verdict": "No scenario was compared.", "caveats": []}

    best = rows[0]
    caveats: List[str] = []
    incomparable = [r for r in rows if not r["comparable"]]
    if incomparable:
        caveats.append(
            f"{len(incomparable)} scenario(s) produced no cost the engine could "
            f"compare, so they are listed but not ranked.")

    if not best["comparable"]:
        return {
            "recommended_scenario_id": None,
            "verdict": ("None of the compared scenarios produced a cost that can "
                        "be measured against the current network."),
            "caveats": caveats,
        }

    delta = best["cost_delta"]
    # Plain business English, and no engine vocabulary. It read "below the
    # current network on solved business network cost", which is a sentence
    # about a solver rather than about a decision.
    if delta < 0:
        others = len(rows) - 1
        verdict = (f"{best['name']} costs less than the network you run today, "
                   f"and less than the {others} other "
                   f"{'option' if others == 1 else 'options'} compared."
                   if others else
                   f"{best['name']} costs less than the network you run today.")
    else:
        verdict = (f"Nothing compared costs less than the network you run "
                   f"today. {best['name']} comes closest.")

    # WHERE THE DIFFERENCE COMES FROM, immediately after the verdict that
    # states it. See `_attribution`. The figure-free sentence goes in the
    # caveats; the amounts travel separately so the screen can render them.
    attribution = _attribution(best)
    if attribution:
        caveats.insert(0, attribution["text"])

    if best["fill_delta"] is not None and best["fill_delta"] < -_MATERIAL_FILL_DROP_PTS:
        caveats.append(
            f"{best['name']} serves less demand than the network does today — "
            f"part of any saving is a smaller promise, not a cheaper way of "
            f"keeping the same one.")

    return {"recommended_scenario_id": best["scenario_id"],
            "verdict": verdict, "caveats": caveats, "best_row": best,
            "attribution": attribution}


def _comparison_figures(best: Dict[str, Any],
                        record: Optional[Dict[str, Any]]) -> List[Any]:
    """
    Three numbers, and they are the three that decide this: what it costs,
    how much demand it serves, and whether capacity is at risk.

    Cost alone was the whole card, which is how "cheapest" came to read as
    "best" on a plan serving 68.5% of demand.
    """
    from netgravity.orchestrator.reasoning.card import Figure

    fill = best.get("fill_rate")
    risk = str((record or {}).get("capacity_risk") or "").strip()
    return [
        Figure.money("Cost", best.get("cost")),
        Figure(label="Demand served",
               value=(f"{fill * 100:,.1f}%" if isinstance(fill, (int, float))
                      else "Not available")),
        Figure(label="Capacity risk", value=risk or "Not available"),
    ]


def _comparison_explanation(project_id: str, rows: List[Dict[str, Any]],
                            verdict: Dict[str, Any],
                            baseline: Dict[str, Any],
                            figures: Optional[List[Any]] = None) -> Dict[str, Any]:
    """
    The comparison's own grounded briefing: why THIS one rather than those.

    One model request per SET of scenarios, keyed on the set — so comparing
    A and B twice, or reopening the page, spends nothing. See
    orchestrator/explanation_service.py.

    NOT PRODUCED FOR A SET OF ONE. There are no alternatives to weigh, so the
    briefing would have nothing to compare and the scenario's own
    SCENARIO-scoped briefing — already produced by its run, at no further cost
    — answers that case properly. Spending a request to say "one scenario was
    compared" is the kind of waste a shared budget cannot absorb.

    Never raises: an explanation is advisory, and the ranking beside it is
    perfectly good without one.
    """
    if len(rows) < 2:
        return {}
    try:
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.storage import get_storage
        from netgravity.orchestrator.explanation_service import ExplanationService
        from netgravity.orchestrator.explanations import (
            KIND_COMPARISON,
            ExplanationStore,
        )
        from netgravity.orchestrator.reasoning.comparison_evidence import (
            comparison_reasoning_payload,
        )
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        scenario_ids = [r.get("scenario_id") for r in rows]
        service = ExplanationService(
            # The SHARED connection, not a bare agent. A bare
            # `ReasoningAgent()` has no gateway, so it produced templates
            # however the credential was set.
            explanation_reasoning_agent(),
            ExplanationStore(get_storage(IngestionConfig())))
        return service.explain(
            subject_id=project_id,
            kind=KIND_COMPARISON,
            scope=ReasoningScope.COMPARISON,
            # The SET identifies the analysis. Same set, any order, one call.
            result_parts=[scenario_ids, verdict.get("recommended_scenario_id")],
            build_payload=lambda: comparison_reasoning_payload(
                ranked=rows,
                recommended_scenario_id=verdict.get("recommended_scenario_id"),
                verdict=verdict.get("verdict", ""),
                baseline_cost=_valid(baseline, "business_network_cost"),
            ),
            # The credential is the switch; see explanation_llm.py.
            allow_llm=explanations_llm_enabled(),
            figures=figures,
            details=list(verdict.get("caveats") or []),
        )
    except Exception as exc:  # noqa: BLE001 — the ranking still stands
        logger.warning("scenario.comparison_explanation_failed: %s", exc)
        return {}


#: Solved-topology changes that permanently alter the physical network.
#: Mirrors STRUCTURAL_ACTIONS in orchestrator/governance/action_classifier.py.
_STRUCTURAL_ACTIONS = {"CLOSE_FACILITY", "OPEN_FACILITY", "ADD_FACILITY"}


def _is_structural(record: Dict[str, Any]) -> bool:
    """
    Whether this scenario opens or closes a site.

    Read from the SOLVED topology as well as the request, because a scenario
    that merely offered a site to the solver has not opened one, and a
    capacity change that made a site unviable has closed one.
    """
    if str((record.get("request") or {}).get("action") or "") in _STRUCTURAL_ACTIONS:
        return True
    before = record.get("baseline_facilities") or {}
    after = record.get("scenario_facilities") or {}
    for fid, state in after.items():
        was_open = bool((before.get(fid) or {}).get("isOpen"))
        is_open = bool((state or {}).get("isOpen"))
        if was_open != is_open:
            return True
    return False


def create_scenario_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/scenarios"):
    bp = Blueprint("scenarios", __name__, url_prefix=url_prefix)
    registry = KPIRegistry()

    # Scenarios are stored per project, never in one shared list, and written
    # through to the database so a restart does not discard an afternoon's
    # work. The dictionary stays the read path; the database is what it is
    # rebuilt from.
    _store: Dict[str, List[Dict[str, Any]]] = {}
    _lock = threading.RLock()
    _restored = {"done": False}

    def _load_scenarios() -> None:
        """Rebuild `_store` from the database, once, on first use."""
        with _lock:
            if _restored["done"]:
                return
            _restored["done"] = True
            from app.backend.services import persistence
            count = 0
            for project_id, record in persistence.load_scenarios():
                if not record.get("id"):
                    continue
                _store.setdefault(project_id, []).append(record)
                count += 1
            if count:
                logger.info("scenario.store.restored scenarios=%d", count)

    # One re-optimised reference per snapshot; see `_optimised_reference`.
    _reference: Dict[str, Dict[str, Any]] = {}
    _reference_lock = threading.RLock()

    def _optimised_reference(snapshot_id: str, user_id: str) -> Dict[str, Any]:
        """
        The same network, unchanged, solved the way every SCENARIO is solved.

        Without this, every scenario appears to save about the same 47%.

        The project baseline is deliberately an `ACTUAL_AS_IS_EVALUATION`: the
        client's footprint pinned open, because that is the network they
        actually run and the figure they recognise. A scenario is solved as a
        `BROWNFIELD_SCENARIO_OPTIMIZATION`, which is free to close sites. So the
        difference between the two columns is the change PLUS the whole value of
        redesigning the footprint — and on this network the redesign dominates.
        Three unrelated scenarios came back at −47.1%, −46.8% and −47.1%, which
        reads exactly as a screen showing the same number whatever you ask it.

        The reference is a no-change scenario: a capacity delta of zero, run
        through the identical code path, so what it isolates is guaranteed to be
        comparable rather than approximately so. Cached per snapshot because it
        does not depend on the scenario.
        """
        with _reference_lock:
            cached = _reference.get(snapshot_id)
        if cached is not None:
            return cached

        engine = _require_engine()
        snapshot = engine.snapshots.get(snapshot_id)
        anchor = next((f.id for f in snapshot.network.facilities
                       if getattr(f.role, "value", str(f.role)) not in
                       ("MARKET", "CUSTOMER")), None)
        if anchor is None:
            return {}

        req = OrchestratorRequest(
            input="Re-optimised reference: the network unchanged",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=[anchor],
                capacity_delta_units=0.0,
                label="Re-optimised, no change",
            )],
            actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            disable_llm=True,
            request_id=orchestrator_request_id("scenario-reference"),
        )
        try:
            response = engine.run_sync(req)
            ctx = engine.get_execution_state(response.execution_id)
            key = _scenario_state_key(ctx) if ctx else None
            result = _serialise_kpis(registry.network_kpis(ctx, key=key)) if key else {}
        except Exception:  # noqa: BLE001 — a missing reference is not fatal
            logger.warning("scenario.reference.failed snapshot_id=%s", snapshot_id)
            result = {}

        with _reference_lock:
            _reference[snapshot_id] = result
        return result

    def _require_engine() -> Orchestrator:
        if orchestrator is None:
            raise EngineUnavailableError(
                "The analysis engine is not mounted, so scenarios cannot be solved."
            )
        return orchestrator

    def _project_scope() -> tuple[str, str]:
        """(project_id, snapshot_id) for this request, access-checked."""
        project_id = str(request.args.get("project_id")
                         or (request.get_json(silent=True) or {}).get("project_id")
                         or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")
        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id
        )
        return project_id, snapshot_id

    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    @require_auth
    def list_scenarios():
        """Scenarios previously solved for this project."""
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            records = list(_store.get(project_id, []))
        return jsonify({
            "project_id": project_id,
            "scenarios": records,
            "total": len(records),
        }), 200

    @bp.route("/compare", methods=["POST"])
    @require_auth
    def compare_scenarios():
        """
        Rank a set of solved scenarios and say which one the numbers favour.

        The ranking, the verdict and the caveats are decided HERE, from the
        authoritative KPI values and their statuses — not in the browser,
        where the reasoning would be invisible to the audit trail and free to
        disagree with the same numbers elsewhere on screen.

        It RANKS. It does not approve: a structural change is flagged as a
        human decision whatever the economics say, matching
        orchestrator/governance/action_classifier.py.
        """
        project_id, _ = _project_scope()
        body = request.get_json(silent=True) or {}
        wanted = [str(x) for x in (body.get("scenario_ids") or [])]

        _load_scenarios()
        with _lock:
            records = list(_store.get(project_id, []))
        by_id = {r.get("id"): r for r in records}
        if wanted:
            # A requested comparison that cannot be resolved is REFUSED, not
            # quietly widened. Falling back to every saved scenario answered a
            # different question than the one asked, under the heading of the
            # one asked — and the user had no way to see the substitution.
            unknown = [i for i in wanted if i not in by_id]
            if unknown:
                raise ValidationError(
                    "Some of the scenarios you asked to compare are not "
                    "available for this project, so the comparison was not "
                    "run.",
                    context={"unknown_scenario_ids": unknown,
                             "requested": wanted})
            selected = [by_id[i] for i in wanted]
        else:
            selected = records
        if not selected:
            raise ValidationError(
                "There is no solved scenario for this project to compare.")

        # One baseline for every row, from a scenario's own baseline_kpis —
        # the same snapshot solve each was measured against.
        baseline = selected[0].get("baseline_kpis") or {}
        rows = _rank_scenarios(baseline, selected)
        verdict = _comparison_verdict(rows)

        recommended = by_id.get(verdict["recommended_scenario_id"])
        caveats = list(verdict["caveats"])
        if recommended and recommended.get("reference_note"):
            caveats.append(recommended["reference_note"])

        # Cost NEXT TO service and risk, never cost alone. Supplied by code,
        # so the model states no figure and cannot state one in the wrong
        # currency. Money travels as an amount; the screen applies the
        # project's own currency to it.
        best_row = verdict.get("best_row") or {}
        warning = _service_warning(best_row, recommended)
        figures = _comparison_figures(best_row, recommended)

        return jsonify({
            "project_id": project_id,
            "baseline_kpis": baseline,
            "ranked": rows,
            "recommended_scenario_id": verdict["recommended_scenario_id"],
            "verdict": verdict["verdict"],
            "caveats": caveats,
            # The split behind the headline, as amounts. The screen renders it
            # in the project's own currency; see `_attribution`.
            "attribution": verdict.get("attribution") or {},
            # Why the recommended one is preferable to the others — a
            # COMPARISON-scope briefing about the set, not about the winner
            # alone. Produced once per set of scenarios and saved against it,
            # so re-opening the comparison spends nothing.
            "explanation": _comparison_explanation(
                project_id, rows, verdict, baseline, figures=figures),
            # The one thing a cost ranking must not bury. Empty when there is
            # genuinely nothing to warn about.
            "warning": warning,
            "structural": bool(recommended and _is_structural(recommended)),
            "governance": {
                "classification": ("HUMAN_ONLY" if recommended
                                   and _is_structural(recommended) else "ANALYSIS"),
                "note": ("Opening or closing a site is a structural change and is "
                         "always a human decision, whatever the economics say."
                         if recommended and _is_structural(recommended)
                         else "This is an analysis of what the solver found."),
                "actioned": False,
            },
        }), 200

    @bp.route("/baseline", methods=["GET"])
    @require_auth
    @rate_limit("scenario.baseline", limit=60, window_seconds=300)
    def get_baseline():
        """
        The project's immutable baseline: a solve of the bound snapshot with no
        scenario applied. Recomputed on demand from the snapshot, so no scenario
        run can ever mutate it (brief §13).
        """
        engine = _require_engine()
        project_id, snapshot_id = _project_scope()

        req = OrchestratorRequest(
            input="Baseline network solve",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
            actor=Actor(actor_id=g.current_user.user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            disable_llm=True,
            request_id=orchestrator_request_id("scenario-baseline"),
        )
        response = engine.run_sync(req)
        ctx = engine.get_execution_state(response.execution_id)
        if ctx is None:
            raise EngineUnavailableError("Baseline execution produced no context.")

        kpis = registry.network_kpis(ctx)
        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "execution_id": response.execution_id,
            "type": "BASELINE",
            "kpis": _serialise_kpis(kpis),
            "triggered_thresholds": [
                t.model_dump(mode="json")
                for t in registry.evaluate_thresholds(list(kpis.values()))
            ],
        }), 200

    @bp.route("/simulate", methods=["POST"])
    @require_auth
    # Two MILP solves per call — the scenario and the re-optimised reference.
    # One caller can otherwise occupy every worker and the platform stops
    # answering for everyone else, with no malice required.
    @rate_limit("scenario.simulate", limit=30, window_seconds=300)
    def simulate_scenario():
        """
        Solve a what-if scenario against the project's bound snapshot.

        Returns authoritative baseline KPIs, scenario KPIs and deterministic
        deltas. An infeasible scenario is reported as infeasible; it is not
        rendered as a cheaper network.
        """
        engine = _require_engine()
        project_id, snapshot_id = _project_scope()
        _load_scenarios()
        body: Dict[str, Any] = request.get_json(silent=True) or {}

        name = str(body.get("name") or "").strip() or "Custom what-if scenario"
        action_str = str(body.get("action") or "CHANGE_CAPACITY").upper()
        if action_str not in _ACTION_MAP:
            raise ValidationError(
                f"Unsupported scenario action '{action_str}'.",
                context={"supported": sorted(_ACTION_MAP)},
            )
        action = _ACTION_MAP[action_str]

        facility_ids = body.get("facility_ids") or []
        if not isinstance(facility_ids, list):
            raise ValidationError("facility_ids must be a list.")
        # Demand, freight rates and the delivery promise are properties of the
        # whole network; a greenfield site names no existing facility because it
        # is not one yet. Requiring a facility for all four made three of the
        # six scenario types in the builder impossible to run.
        needs_facility = (action not in NETWORK_WIDE_ACTIONS
                          and action != ScenarioActionType.ADD_FACILITY)
        if needs_facility and not facility_ids:
            raise ValidationError(
                f"At least one facility_id is required for {action_str}.")

        def number(key: str, *aliases: str) -> Optional[float]:
            raw = body.get(key)
            for alias in aliases:
                if raw is None:
                    raw = body.get(alias)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValidationError(f"{key} must be numeric, got {raw!r}.")

        cap_delta = number("capacity_delta_units")
        demand_scale = number("demand_multiplier", "demand_scale")
        transport_mult = number("transport_cost_multiplier")
        sla_delta = number("sla_days_delta")

        required = {
            ScenarioActionType.CHANGE_CAPACITY: (
                cap_delta, "capacity_delta_units"),
            ScenarioActionType.CHANGE_DEMAND: (
                demand_scale, "demand_multiplier"),
            ScenarioActionType.CHANGE_TRANSPORT_COST: (
                transport_mult, "transport_cost_multiplier"),
            ScenarioActionType.CHANGE_SLA: (sla_delta, "sla_days_delta"),
        }
        if action in required and required[action][0] is None:
            raise ValidationError(
                f"{required[action][1]} is required for {action.value}.")

        site: Optional[GreenfieldSiteSpec] = None
        if action == ScenarioActionType.ADD_FACILITY:
            raw_site = body.get("new_facility")
            if not isinstance(raw_site, dict):
                raise ValidationError(
                    "ADD_FACILITY requires a new_facility object with a name, "
                    "latitude, longitude and capacity_units_per_period.")
            try:
                site = GreenfieldSiteSpec(**raw_site)
            except Exception as exc:  # noqa: BLE001 — pydantic message is the useful part
                raise ValidationError(f"new_facility is not usable: {exc}")

        spec = ScenarioIntentSpec(
            action=action,
            facility_ids=list(facility_ids),
            capacity_delta_units=cap_delta if action == ScenarioActionType.CHANGE_CAPACITY else None,
            demand_multiplier=demand_scale if action == ScenarioActionType.CHANGE_DEMAND else None,
            # Growth the client states for one region and/or one product
            # category. Empty string and missing are the same thing — no scope,
            # i.e. the whole network, which is what this endpoint did before.
            demand_region=(
                (str(body.get("demand_region") or "").strip() or None)
                if action == ScenarioActionType.CHANGE_DEMAND else None),
            demand_product_category=(
                (str(body.get("demand_product_category") or "").strip() or None)
                if action == ScenarioActionType.CHANGE_DEMAND else None),
            transport_cost_multiplier=(
                transport_mult if action == ScenarioActionType.CHANGE_TRANSPORT_COST else None),
            sla_days_delta=sla_delta if action == ScenarioActionType.CHANGE_SLA else None,
            new_facility=site,
            label=name,
        )

        req = OrchestratorRequest(
            input=f"Simulate scenario: {name}",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[spec],
            actor=Actor(actor_id=g.current_user.user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            # This run produces the scenario's OWN explanation, so it honours
            # the explanation switch. The two solves above do not: the
            # baseline and the re-optimised reference are numbers, and nothing
            # narrates them.
            #
            # The reasoning step inside is `single_request=True`, so a live
            # scenario costs exactly one model request, once, saved against
            # the run.
            disable_llm=not explanations_llm_enabled(),
            request_id=orchestrator_request_id("scenario-simulate"),
        )

        try:
            response = engine.run_sync(req)
        except Exception as exc:  # noqa: BLE001 — surfaced, never substituted
            logger.exception("scenario.simulate.failed project_id=%s", project_id)
            return jsonify({
                "error": {
                    "code": "CAPABILITY_FAILURE",
                    "message": f"The scenario could not be solved: {exc}",
                }
            }), 502

        ctx = engine.get_execution_state(response.execution_id)
        if ctx is None:
            raise EngineUnavailableError("Scenario execution produced no context.")

        scenario_key = _scenario_state_key(ctx)

        # A scenario that never materialised is not a scenario.
        #
        # `run_sync` never raises — it captures every failure and returns it on
        # the response, which is right for a control plane and wrong to treat as
        # success here. A refused build (a site with no capacity, an SLA change
        # on a network that states none) came back 201 with a stored record
        # whose every figure was null, and the comparison table rendered it as a
        # scenario with no results rather than saying the run was rejected.
        if scenario_key is None:
            reasons = [str(e.get("message") or e.get("error") or e)
                       for e in (response.errors or [])]
            detail = reasons[0] if reasons else (
                response.summary or "the scenario engine produced no scenario state")
            logger.info(
                "scenario.simulate.rejected project_id=%s action=%s reason=%s",
                project_id, action_str, detail,
            )
            return jsonify({
                "error": {
                    "code": "SCENARIO_NOT_BUILT",
                    "message": f"This scenario could not be run: {detail}",
                    "context": {"action": action_str,
                                "execution_id": response.execution_id,
                                "orchestrator_status": str(response.status)},
                }
            }), 422

        baseline_kpis = registry.network_kpis(ctx, key="optimization.solve")
        scenario_kpis = (registry.network_kpis(ctx, key=scenario_key)
                         if scenario_key else {})
        deltas = registry.scenario_comparison(ctx)

        # Headline projection for the comparison cards. Values appear ONLY when
        # the authoritative result is VALID; otherwise the status travels to the
        # client and the card renders an explicit unavailable state.
        headline: Dict[str, Any] = {}
        for metric_id in _HEADLINE_METRICS:
            result = scenario_kpis.get(metric_id)
            if result is None:
                headline[metric_id] = {"value": None, "status": "NOT_COMPUTABLE", "unit": ""}
            else:
                headline[metric_id] = {
                    "value": result.value if result.status.value == "VALID" else None,
                    "status": result.status.value,
                    "unit": result.unit,
                }

        record_baseline_kpis = _serialise_kpis(baseline_kpis)
        record_scenario_kpis = _serialise_kpis(scenario_kpis)
        # The network unchanged but solved the way scenarios are, so a
        # scenario's own effect can be separated from the value of
        # re-optimising the footprint. See `_optimised_reference`. Resolved
        # here rather than inline in the record because the explanation below
        # needs it too, and solving it twice is not free.
        reference_kpis = _optimised_reference(snapshot_id, g.current_user.user_id)

        # Both solved topologies, resolved once. They go on the record for the
        # Digital Twin, and the capacity account below reads the same two dicts
        # rather than asking the registry to flatten them a second time.
        baseline_facility_states = _facility_states(
            registry, ctx, "optimization.solve")
        scenario_facility_states = _facility_states(registry, ctx, scenario_key)

        record = {
            "id": f"SCN_{uuid.uuid4().hex[:8]}",
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "name": name,
            "type": "USER_CREATED",
            "source": "user",
            "created_at": time.time(),
            "execution_id": response.execution_id,
            "orchestrator_status": str(getattr(response, "status", "")),
            "feasible": scenario_key is not None and bool(scenario_kpis),
            "request": {
                "action": action_str,
                "facility_ids": list(facility_ids),
                "capacity_delta_units": cap_delta,
                "demand_multiplier": demand_scale,
                # WHERE the growth was applied. These reach the solver through
                # `ScenarioIntentSpec` and were dropped from the record, so a
                # run scoped to one product category read back — on the
                # drawer, and on the card's summary of what was asked — as a
                # change applied to every demand row in the network. The
                # figures were always right; the description of them was not.
                "demand_region": spec.demand_region,
                "demand_product_category": spec.demand_product_category,
                "transport_cost_multiplier": transport_mult,
                "sla_days_delta": sla_delta,
                "new_facility": site.model_dump(mode="json") if site else None,
            },
            # What the builder actually did to the network, in its own words.
            # The drawer used to describe changes from a hand-written list that
            # no builder produced.
            "overrides": _overrides_of(engine, scenario_key),
            # Sites this scenario introduces. Empty for every scenario that
            # only rearranges the existing footprint.
            "new_sites": _new_sites(engine, scenario_key, snapshot_id),
            "baseline_kpis": record_baseline_kpis,
            "scenario_kpis": record_scenario_kpis,
            "reference_kpis": reference_kpis,
            "reference_note": (
                "The network as uploaded, re-solved with the same freedom a "
                "scenario has to open and close sites. The difference between "
                "the baseline and this reference is the value of re-optimising "
                "your existing footprint; the difference between this reference "
                "and the scenario is what the change itself does."
            ),
            # The topology BOTH states produced. Without these the Digital Twin
            # cannot show what a scenario changed: it had only network totals,
            # so its map fell back to a hardcoded table of prototype facilities
            # and rendered the baseline for every scenario ever created.
            "baseline_facilities": baseline_facility_states,
            "scenario_facilities": scenario_facility_states,
            "baseline_flows": _lane_flows(registry, ctx, "optimization.solve"),
            "scenario_flows": _lane_flows(registry, ctx, scenario_key),
            # What the change asks of the sites that have to absorb it.
            #
            # A demand scenario was answered with the network's cost narration
            # — the same answer every scenario got. Which sites are full, how
            # much more each has to carry, and where the network has no room
            # left is the part that differs between +5% and +50%, and it was
            # not computed anywhere.
            "capacity_response": _capacity_response(
                engine, snapshot_id, scenario_key,
                baseline_facility_states, scenario_facility_states,
                record_scenario_kpis),
            "headline": headline,
            # The band the ranking's own service warning reads. Stored beside
            # the KPIs it is derived from, so the figure and its band travel
            # together and cannot drift.
            "capacity_risk": _capacity_risk(record_scenario_kpis),
            "baseline_capacity_risk": _capacity_risk(record_baseline_kpis),
            "deltas": {d.metric_id: d.model_dump(mode="json") for d in deltas},
            "triggered_thresholds": [
                t.model_dump(mode="json")
                for t in registry.evaluate_thresholds(list(scenario_kpis.values()))
            ],
            # THIS scenario's own explanation, from the reasoning step the
            # scenario workflow already runs (`_reason_and_govern`). It was
            # computed on every simulate and returned on none of them, so a
            # screen that wanted to explain a what-if had only the network's
            # general briefing to show — an explanation of something else,
            # next to this scenario's numbers.
            "explanation": _scenario_explanation(
                ctx, record_scenario_kpis,
                attribution=_attribution(_rank_scenarios(
                    record_baseline_kpis, [{
                        "id": "self", "name": name,
                        "scenario_kpis": record_scenario_kpis,
                        "reference_kpis": reference_kpis,
                    }])[0])),
            "provenance": {
                "engine": "netgravity MILP (PuLP/HiGHS)",
                "authoritative_source": "KPIRegistry (Phase 9.1)",
                "llm_used": explanations_llm_enabled(),
                "computed_by": "orchestrator.run_sync",
            },
        }

        with _lock:
            _store.setdefault(project_id, []).append(record)

        from app.backend.services import persistence
        persistence.guarded(persistence.save_scenario)(
            record["id"], project_id, record, record["created_at"],
        )

        logger.info(
            "scenario.simulated project_id=%s scenario_id=%s execution_id=%s feasible=%s",
            project_id, record["id"], response.execution_id, record["feasible"],
        )
        return jsonify(record), 201

    @bp.route("/<scenario_id>", methods=["GET"])
    @require_auth
    def get_scenario(scenario_id: str):
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            for rec in _store.get(project_id, []):
                if rec["id"] == scenario_id:
                    return jsonify(rec), 200
        raise NotFoundError(f"Scenario '{scenario_id}' not found in this project.")

    @bp.route("/<scenario_id>", methods=["DELETE"])
    @require_auth
    def delete_scenario(scenario_id: str):
        """
        Discard a solved scenario.

        The comparison holds three scenarios at a time, so removing one is part
        of ordinary use. It was a client-side splice only, which meant a
        scenario the user had deleted came back on the next page load.
        """
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            records = _store.get(project_id, [])
            remaining = [r for r in records if r["id"] != scenario_id]
            if len(remaining) == len(records):
                raise NotFoundError(
                    f"Scenario '{scenario_id}' not found in this project.")
            _store[project_id] = remaining

        from app.backend.services import persistence
        persistence.guarded(persistence.delete_scenario)(scenario_id)
        logger.info("scenario.deleted project_id=%s scenario_id=%s",
                    project_id, scenario_id)
        return jsonify({"deleted": scenario_id, "remaining": len(remaining)}), 200

    @bp.errorhandler(ApplicationError)
    def _scenario_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
