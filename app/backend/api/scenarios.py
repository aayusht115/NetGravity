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

from flask import Blueprint, g, jsonify, make_response, request

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
    state = (getattr(context, "network_states", {}) or {}).get(key)
    summaries = {getattr(f, "facility_id", None): f
                 for f in (getattr(state, "facilities", None) or [])}
    for facility_id, metrics in registry.facility_kpis(context, key=key).items():
        def value(metric_id: str) -> Any:
            result = metrics.get(metric_id)
            return result.value if result and result.status.value == "VALID" else None

        out[facility_id] = {
            "utilPct": value("utilization_pct"),
            "throughput": value("throughput_units"),
            "capacity": value("capacity_units"),
            "isOpen": value("is_open"),
            # `capacity` is the capacity that BOUND the site: rated, the
            # month's availability, or a plant's production limit. These say
            # which, and what was uploaded and recorded beside it.
            "ratedCapacity": value("rated_capacity_units"),
            "capacityLimit": getattr(summaries.get(facility_id), "capacity_limit", None),
            "observedUtilPct": getattr(summaries.get(facility_id),
                                       "observed_utilization_pct", None),
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

#: Below this, an OPEN site is carrying its full fixed cost for a fraction of
#: its capacity. The opposite finding to the two thresholds above, and the one
#: this file had no name for — so every plan that was not short of room
#: produced "No network change is indicated" whatever it was wasting.
#:
#: Mirrors `strategic_actions.IDLE_PCT`, which is what the Insights ladder uses
#: for the same finding. One number, so a site the Insights page calls
#: under-used is not called healthy here.
_UNDER_USED_PCT = 30.0

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
        # What was uploaded, which limit `capacity` is, and what was recorded.
        "rated_capacity": scenario.get("ratedCapacity"),
        "capacity_limit": scenario.get("capacityLimit"),
        "observed_util_pct": scenario.get("observedUtilPct"),
        "headroom_units": (round(scenario["capacity"] - throughput, 2)
                           if isinstance(throughput, (int, float))
                           and isinstance(scenario.get("capacity"), (int, float))
                           else None),
    }


# ---------------------------------------------------------------------------
# What to DO about a scenario
# ---------------------------------------------------------------------------
#: Every action this application can recommend, IMPORTED rather than restated.
#:
#: This was a second tuple of the same strings, and a second vocabulary is a
#: vocabulary that drifts: the Insights feed now derives its recommendations
#: from `strategic_actions.build_actions`, and a key added there and not here
#: would produce a card the scenario screen could not map to a form.
#:
#: The LADDER is not shared, and deliberately. This function has something the
#: generic one does not: a full capacity account, including which regions have
#: run out of room — computed by `_capacity_response` from every solved site.
#: `build_actions` sees only the rows it is handed, so asking it to decide
#: "every site in this region is full" from a subset would have it conclude
#: that from whatever it was given. The two agree on WHAT can be recommended
#: and on the words; this one knows more about where.
#:
#: NO_ACTION is not an intervention — it is the STATEMENT that none is
#: indicated, with the finding behind it. Carried in the same list because
#: "nothing needs doing" is an answer to "what should I do", and a screen that
#: renders an empty space there has answered nothing. A consumer draws it as a
#: sentence rather than a control.
from netgravity.orchestrator.reasoning.strategic_actions import (  # noqa: E402
    ACTION_KEYS as _ACTION_KEYS,
    CTA_BY_ACTION as _CTA_BY_ACTION,
)


#: The scenario each recommendation is PROVED by, keyed by action. Pressing the
#: button opens the builder already filled in with the change being
#: recommended — which is what makes it a recommendation a leader can price
#: rather than an opinion. Mirrors `StrategicAction.scenario`, so the Insights
#: feed and this screen hand the builder the same shape.
def _scenario_for(key: str, target: Dict[str, Any]) -> Dict[str, Any]:
    facility_id = target.get("facility_id") or ""
    name = target.get("name") or facility_id or "site"
    region = target.get("region") or ""
    if key == "REOPEN_FACILITY":
        return {"action": "OPEN_FACILITY", "open_mode": "EXISTING",
                "facility_id": facility_id, "name": f"Reopen {name}"}
    if key == "ADD_CAPACITY":
        return {"action": "CHANGE_CAPACITY", "facility_id": facility_id,
                "name": f"More capacity at {name}" if facility_id
                        else "Relieve the network shortfall"}
    if key == "OPEN_NEW_FACILITY":
        return {"action": "OPEN_FACILITY", "open_mode": "NEW",
                "region": region,
                "name": f"New site in {region}".strip() if region else "New site"}
    if key == "CONSOLIDATE":
        return {"action": "CLOSE_FACILITY", "facility_id": facility_id,
                "name": f"Consolidate {name}"}
    if key == "SCOPE_DEMAND_GROWTH":
        return {"action": "CHANGE_DEMAND", "name": "Growth, scoped to its region"}
    return {}


def _fmt_units(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "an unrecorded quantity"
    return f"{value:,.0f} units"


def _capacity_account(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    Which sites this plan fills, empties and leaves closed — for a record of
    any age.

    `_capacity_response` computes this at simulate time and writes it onto the
    record, and every rung in `_recommended_actions` is gated on it. A scenario
    stored before it existed carries no such block, so `cap` was `{}` and the
    ladder could see nothing: measured on the demo project, all three saved
    scenarios return `capacity_response: ABSENT`, one of them a +30% demand run
    with a site sitting at 100% of capacity — and the card recommended nothing
    about capacity, because the only rung that could still fire reads the
    scenario REQUEST rather than the account.

    `/compare` recomputes the recommendations against the stored record, which
    was meant to cover exactly this. Recomputing a ladder over a block that is
    not there recovers nothing.

    So the account is rebuilt from what the record does carry:
    `scenario_facilities` and `baseline_facilities` are the same authoritative
    per-site figures `_capacity_response` reads, and the thresholds are the same
    three constants.

    TWO THINGS ARE NOT RECOVERED, deliberately:

      * site NAMES live on the engine's facility metadata rather than on the
        record, so the id is used. "Test consolidating DC_EAST" is worse than
        the same sentence with the site's real name and far better than no
        recommendation at all;
      * `regions_without_room` stays empty. A region cannot be declared full
        without knowing which sites are in it, and the record does not say. A
        record of this age can therefore be recommended an expansion, a
        reopening or a consolidation — never a new site somewhere the data
        cannot place.
    """
    stored = record.get("capacity_response")
    if isinstance(stored, dict) and stored:
        return stored

    scenario = record.get("scenario_facilities") or {}
    baseline = record.get("baseline_facilities") or {}
    at_ceiling: List[Dict[str, Any]] = []
    working_harder: List[Dict[str, Any]] = []
    under_used: List[Dict[str, Any]] = []
    idle: List[Dict[str, Any]] = []
    headroom = 0.0
    headroom_known = False

    for facility_id, state in scenario.items():
        if not isinstance(state, dict):
            continue
        capacity = state.get("capacity")
        if state.get("isOpen") is False:
            if isinstance(capacity, (int, float)) and capacity > 0:
                idle.append({"id": facility_id, "name": facility_id,
                             "util_pct": None, "region": None,
                             "capacity": capacity})
            continue

        util = state.get("utilPct")
        if not isinstance(util, (int, float)) or isinstance(util, bool):
            continue
        was = (baseline.get(facility_id) or {}).get("throughput")
        now = state.get("throughput")
        added = (round(now - was, 2)
                 if isinstance(now, (int, float)) and isinstance(was, (int, float))
                 else None)
        row = {"id": facility_id, "name": facility_id, "util_pct": float(util),
               "baseline_util_pct": (baseline.get(facility_id) or {}).get("utilPct"),
               "region": None, "capacity": capacity, "added_units": added}
        if isinstance(capacity, (int, float)) and isinstance(now, (int, float)):
            headroom += max(capacity - now, 0.0)
            headroom_known = True

        if util >= _SATURATED_PCT:
            at_ceiling.append(row)
        elif util >= _LOADED_PCT and (added or 0) > 0:
            working_harder.append(row)
        elif util < _UNDER_USED_PCT:
            under_used.append(row)

    at_ceiling.sort(key=lambda r: -(r["added_units"] or 0))
    working_harder.sort(key=lambda r: -(r["util_pct"] or 0))
    under_used.sort(key=lambda r: (r["util_pct"] or 0))
    idle.sort(key=lambda r: -(r["capacity"] or 0))

    return {
        "at_ceiling": at_ceiling, "at_ceiling_count": len(at_ceiling),
        "working_harder": working_harder,
        "working_harder_count": len(working_harder),
        "under_used": under_used, "under_used_count": len(under_used),
        "idle": idle, "idle_count": len(idle),
        "open_headroom_units": round(headroom, 2) if headroom_known else None,
        # Not recoverable from the record — see the note above.
        "regions_without_room": [],
        # Says this account was rebuilt rather than solved, so a consumer that
        # cares can tell the difference.
        "reconstructed": True,
    }


def _recommended_actions(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    What a reader should DO about this scenario, in priority order.

    DERIVED HERE, NOT ON THE SCREEN. This list used to be built in the browser
    from the same block, and the drawer, the card and anything else that wanted
    it would each have had their own copy. One definition means the sentence a
    leader reads on the card is the sentence in the document they forward.

    Every entry is a NETWORK INTERVENTION — add capacity, reopen a site, build
    one, scope the growth, get the missing input. Reading the result is not an
    action: "review the proposed changes" told a reader to look at the screen
    they were already looking at, and it was the first thing offered on every
    scenario whatever the solve found. It is a way into the detail, which the
    screen offers separately, and it is not a recommendation.

    Each is gated on a FINDING in this scenario's own solved result, so a plan
    that stranded no demand is never told to add capacity and a plan with room
    everywhere is never told to build. An empty list is a real answer and the
    caller must say so rather than filling the space.

    ABOUT THIS CHANGE, NOT ABOUT THE NETWORK. Every rung reads what the
    scenario did relative to today: a site it pushed to its ceiling, demand it
    stranded, a site whose load it took away, capacity it added that nothing
    used. A site that was already full, or already closed, before the change
    is a property of today's network. The Insights feed recommends on those,
    and repeating them here put the same "reopen X, expand Y" on every
    scenario of a project whatever was asked. Measured on one upload: a
    closure and two demand runs all opened with the same two recommendations,
    and the closure's first one was to reopen the site it had just closed.

    A FINDING WITH NOTHING TO PRESS is marked `statement`, and carries no
    scenario and no button verb. "Keep this site open" is an answer; a button
    under it that opens a form re-running today's network is not.
    """
    cap = _capacity_account(record)
    kpis = record.get("scenario_kpis") or {}
    base_kpis = record.get("baseline_kpis") or {}
    request = record.get("request") or {}
    action = str(request.get("action") or "").upper()
    named = [str(f) for f in (request.get("facility_ids") or [])]

    def kpi(block: Dict[str, Any], name: str) -> Optional[float]:
        row = block.get(name)
        if isinstance(row, dict):
            if row.get("status") not in (None, "VALID"):
                return None
            row = row.get("value")
        if isinstance(row, bool) or not isinstance(row, (int, float)):
            return None
        return float(row)

    unserved = kpi(kpis, "unserved_demand")
    was_unserved = kpi(base_kpis, "unserved_demand")
    # Demand THIS change strands. A record with no baseline figure counts the
    # whole shortfall, which is how every record was read before.
    if unserved is None:
        stranded: Optional[float] = None
    elif was_unserved is None:
        stranded = unserved
    else:
        stranded = max(unserved - was_unserved, 0.0)
    total = kpi(kpis, "total_demand")
    # A rounding tail on a network of millions of units is not a shortfall.
    tolerance = max(1.0, (total or 0.0) * 1e-6)
    strands = stranded is not None and stranded > tolerance

    headroom = cap.get("open_headroom_units")
    # Out of REACH rather than short of ROOM: the open sites have more spare
    # capacity between them than the whole shortfall, so capacity is not what
    # binds. The same test `_capacity_verdict` states in words.
    out_of_reach = bool(strands and isinstance(headroom, (int, float))
                        and unserved is not None and headroom > unserved)

    def caused(row: Dict[str, Any]) -> bool:
        """Whether this change put the site at its ceiling."""
        added = row.get("added_units")
        was = row.get("baseline_util_pct")
        # Full before the change is a standing constraint, even when the plan
        # squeezes a few more units through it in slacker periods (measured:
        # +501 units at a plant that was already at 100%).
        if isinstance(was, (int, float)):
            return was < _SATURATED_PCT
        if isinstance(added, (int, float)):
            return added > 0
        # Too old a record to tell either way: counted, as it always was.
        return True

    closed_by_change = set(named) if action == "CLOSE_FACILITY" else set()
    at_ceiling = [r for r in (cap.get("at_ceiling") or []) if caused(r)]

    def warmed(row: Dict[str, Any]) -> bool:
        """Whether this change took the site into the loaded band."""
        was = row.get("baseline_util_pct")
        return not isinstance(was, (int, float)) or was < _LOADED_PCT

    warming = [r for r in (cap.get("working_harder") or []) if warmed(r)]
    idle = [r for r in (cap.get("idle") or [])
            if str(r.get("id")) not in closed_by_change]
    regions = list(cap.get("regions_without_room") or [])

    facility_cost = kpi(base_kpis, "facility_cost")
    if facility_cost is None:
        facility_cost = kpi(kpis, "facility_cost")
    # The upload states no fixed cost anywhere. Capacity, closures and
    # openings then carry no price, and a cost comparison of them is empty.
    unpriced = facility_cost is not None and facility_cost <= 0.0
    # Some sites unpriced rather than all of them. Said, and never offered as
    # a consolidation: closing a site whose fixed cost is missing saves
    # nothing the model can see.
    completeness = record.get("cost_completeness") or {}
    unpriced_sites = {str(f) for f in (completeness.get("sites_without_fixed_cost") or [])}
    partly_unpriced = completeness.get("complete") is False and not unpriced

    def site_name(facility_id: str) -> str:
        for block in ("at_ceiling", "working_harder", "under_used", "idle"):
            for row in cap.get(block) or []:
                if str(row.get("id")) == facility_id and row.get("name"):
                    return str(row["name"])
        return facility_id

    def name_of(site: Dict[str, Any]) -> str:
        return str(site.get("name") or site.get("id") or "")

    actions: List[Dict[str, Any]] = []
    seen: set = set()

    def add(entry: Dict[str, Any]) -> None:
        target = entry.get("target") or {}
        identity = (entry["key"], target.get("facility_id") or target.get("region"))
        if identity in seen:
            return
        seen.add(identity)
        actions.append(entry)

    # 1. A CLOSURE THAT STRANDS DEMAND. The first thing to know about it, and
    #    not an intervention: the closed site was being offered back as a
    #    "reopen" button, which re-runs the network as it is today.
    if action == "CLOSE_FACILITY" and strands and named:
        who = ", ".join(site_name(f) for f in named)
        add({
            "key": "REOPEN_FACILITY",
            "statement": True,
            "label": f"Keep {who} open",
            "reason": (
                f"Closing it leaves {_fmt_units(stranded)} of demand unserved "
                f"that today's network serves. "
                + ("The sites that stay open have room between them, but none "
                   "can reach those markets, so no capacity added elsewhere "
                   "would serve them. Those markets need another lane before "
                   "this closure goes further."
                   if out_of_reach else
                   "The sites that stay open do not have the room to take it, "
                   "and that room is what the closure has to find first.")),
            "target": {"facility_id": named[0], "name": who},
        })

    # 2. CAPACITY ADDED THAT NOTHING USED. The finding a capacity scenario is
    #    run to get, and it was never stated: the card went on to recommend
    #    whatever the network needed elsewhere.
    delta = request.get("capacity_delta_units")
    if action == "CHANGE_CAPACITY" and isinstance(delta, (int, float)) and delta > 0:
        now_states = record.get("scenario_facilities") or {}
        was_states = record.get("baseline_facilities") or {}
        pricing = record.get("capacity_pricing") or {}
        for facility_id in named:
            now = (now_states.get(facility_id) or {}).get("throughput")
            was = (was_states.get(facility_id) or {}).get("throughput")
            if not (isinstance(now, (int, float)) and isinstance(was, (int, float))):
                continue
            if now > was + tolerance:
                continue
            util = (now_states.get(facility_id) or {}).get("utilPct")
            running = (f" and runs at {util:,.0f}% of its new capacity"
                       if isinstance(util, (int, float)) else "")
            if pricing.get("basis") == "LIMIT_NOT_RAISED":
                cost = (" The change did not raise the limit that binds this "
                        "site, so none of the added room could be used.")
            elif pricing.get("basis") in ("PRO_RATA", "STATED"):
                cost = " The change adds fixed cost for room that goes unused."
            elif pricing.get("basis") == "UNPRICED" or unpriced:
                cost = (" The upload states no fixed cost for this site, so "
                        "the plan shows no cost for the added room either.")
            else:
                cost = ""
            add({
                "key": "NO_ACTION",
                "statement": True,
                "label": f"The added capacity at {site_name(facility_id)} is not used",
                "reason": (
                    f"{site_name(facility_id)} carries no more in this plan "
                    f"than it does today{running}. Capacity there is not what "
                    f"limits this network." + cost),
                "target": {"facility_id": facility_id,
                           "name": site_name(facility_id)},
            })

    # 3. Reopening beats building: the capacity exists and is already paid
    #    for. Only for a shortage THIS change creates, and never the site the
    #    change itself closed.
    if idle and (at_ceiling or (strands and not out_of_reach)):
        region = at_ceiling[0].get("region") if at_ceiling else None
        nearby = [r for r in idle if region and r.get("region") == region]
        site = (nearby or idle)[0]
        add({
            "key": "REOPEN_FACILITY",
            "label": f"Reopen {name_of(site)}",
            "reason": (
                f"This plan leaves {_fmt_units(site.get('capacity'))} of capacity "
                f"closed at {name_of(site)}"
                + (f" in {site['region']}" if site.get("region") else "")
                + ", while the change "
                + ("fills " + name_of(at_ceiling[0]) if at_ceiling
                   else "leaves demand unserved")
                + ". Capacity that already exists is cheaper to use than "
                  "capacity that has to be built."),
            "target": {"facility_id": site.get("id"), "name": site.get("name"),
                       "region": site.get("region")},
        })

    # 4. Relief where THIS change runs a site out of room.
    if at_ceiling:
        site = at_ceiling[0]
        util = site.get("util_pct")
        at = (f"{util:,.0f}% of its capacity" if isinstance(util, (int, float))
              else "its ceiling")
        carrying = ""
        if isinstance(site.get("added_units"), (int, float)) and site["added_units"] > 0:
            carrying = (f", carrying {_fmt_units(site['added_units'])} more than "
                        f"it does today")
        # "Nothing more can move through this network" was said of a full site
        # on a network with millions of units of room elsewhere. The sentence
        # now claims only what this site's own figures show.
        tail = (" Demand is going unserved for want of room, and this is where "
                "the room runs out first."
                if strands and not out_of_reach else
                " It has no room left for anything more this change asks of it.")
        add({
            "key": "ADD_CAPACITY",
            "label": f"Increase capacity at {name_of(site)}",
            "reason": f"{name_of(site)} runs at {at} in this plan{carrying}.{tail}",
            "target": {"facility_id": site.get("id"), "name": site.get("name"),
                       "region": site.get("region")},
        })
    elif strands and not out_of_reach:
        add({
            "key": "ADD_CAPACITY",
            "label": "Increase capacity where the plan runs out",
            "reason": (
                f"This change leaves {_fmt_units(stranded)} of demand unserved "
                f"that today's network serves, while no site it fills reaches "
                f"its ceiling, so the shortfall is spread across the network "
                f"rather than sitting at one site."),
            "target": {},
        })
    elif str(record.get("capacity_risk") or "").upper() == "HIGH" and warming:
        # HIGH RISK WITH NOTHING THIS CHANGE HAS FILLED. Only a site the change
        # itself took into the loaded band; one running hot before it is
        # today's network, and the risk band alone cannot tell them apart.
        site = warming[0]
        util = site.get("util_pct")
        at = (f"{util:,.0f}% of its capacity" if isinstance(util, (int, float))
              else "close to its ceiling")
        add({
            "key": "ADD_CAPACITY",
            "label": f"Increase capacity at {name_of(site)}",
            "reason": (
                f"Capacity risk is high in this plan. This change fills no site "
                f"to its ceiling, but it takes {name_of(site)} to {at}, and "
                f"that is the first site it will fill. Adding capacity there "
                f"is what buys the network room before it starts stranding "
                f"demand."),
            "target": {"facility_id": site.get("id"), "name": site.get("name"),
                       "region": site.get("region")},
        })

    # 5. A new site only where a region this change fills has nothing closed
    #    to reopen and no room left.
    if regions and at_ceiling:
        region = regions[0].get("region")
        add({
            "key": "OPEN_NEW_FACILITY",
            "label": f"Set up a new facility in {region}",
            "reason": (
                f"Every site in {region} is at its ceiling in this plan and "
                f"none is closed, so demand growing there has nowhere to go. "
                f"This is the only condition under which building is the "
                f"cheapest answer rather than the first one."),
            "target": {"region": region},
        })

    # 6. THE OPPOSITE FINDING: a site this change empties. A site that was
    #    near-empty before it is today's network, not this scenario. Never on
    #    an upload with no fixed cost, where consolidating saves nothing the
    #    model can see, and never beside a shortage.
    def emptied(row: Dict[str, Any]) -> bool:
        added = row.get("added_units")
        return not isinstance(added, (int, float)) or added < 0

    under_used = [r for r in (cap.get("under_used") or [])
                  if emptied(r) and str(r.get("id")) not in named
                  and str(r.get("id")) not in unpriced_sites]
    if under_used and not at_ceiling and not strands and not unpriced:
        site = under_used[0]
        util = site.get("util_pct")
        at = (f"{util:,.0f}% of its capacity" if isinstance(util, (int, float))
              else "a fraction of its capacity")
        lighter = (", with less going through it than today"
                   if isinstance(site.get("added_units"), (int, float)) else "")
        add({
            "key": "CONSOLIDATE",
            "label": f"Test consolidating {name_of(site)}",
            "reason": (
                f"{name_of(site)} stays open in this plan and runs at {at}"
                f"{lighter}. Moving its volume onto the sites with room is "
                f"worth pricing before any capacity is added anywhere."),
            "target": {"facility_id": site.get("id"), "name": site.get("name"),
                       "region": site.get("region")},
        })

    # 7. Growth stated for the whole network — worth scoping only when that
    #    growth actually runs into something. On a run that fills nothing the
    #    advice changes no decision, and it was on every demand scenario.
    scoped = request.get("demand_region") or request.get("demand_product_category")
    #    Its whole point is that unscoped growth overstates the case for
    #    EXPANDING, so it accompanies an expansion recommendation or nothing.
    expands = any(a["key"] in ("ADD_CAPACITY", "REOPEN_FACILITY",
                               "OPEN_NEW_FACILITY") and not a.get("statement")
                  for a in actions)
    if action == "CHANGE_DEMAND" and not scoped and expands:
        add({
            "key": "SCOPE_DEMAND_GROWTH",
            "label": "Re-run this growth for the region it is happening in",
            "reason": (
                "This scenario grew every demand row in the network, and the "
                "capacity recommended above is sized to that. Loading every warehouse "
                "with growth that is happening in one region overstates the "
                "case for expanding the ones that are not."),
            "target": {},
        })

    # 8. The input without which this change cannot be judged on cost.
    explanation = record.get("explanation") or {}
    missing = list(explanation.get("missing_information") or [])
    if (unpriced or partly_unpriced) and action in (
            "CHANGE_CAPACITY", "CLOSE_FACILITY", "OPEN_FACILITY", "ADD_FACILITY"):
        count = completeness.get("count")
        sites = completeness.get("sites")
        where = ("any site" if unpriced or not count or not sites
                 else f"{count} of its {sites} sites")
        add({
            "key": "REQUEST_DATA",
            "label": "Obtain each site's annual fixed cost",
            "reason": (
                f"This upload states no fixed cost for {where}, so rent, lease "
                f"and overhead there are missing from this plan's cost, and "
                f"capacity, closures and openings there carry no price. A change "
                f"like this one cannot be judged on cost until that input is in."),
            "target": {},
        })
    elif missing:
        add({
            "key": "REQUEST_DATA",
            "label": "Obtain the inputs this analysis did not have",
            "reason": (
                f"{len(missing)} input this scenario needed was not in the "
                f"upload, so part of the answer rests on less evidence than "
                f"the rest of it."),
            "target": {},
        })

    if not actions:
        # WHY nothing is recommended, from the same figures the actions are
        # gated on. "No recommended actions" is a blank; this is a finding.
        standing = [r for r in (cap.get("at_ceiling") or []) if not caused(r)]
        label = "No network change is indicated"
        if out_of_reach:
            label = "No capacity change will serve the missed demand"
            reason = (
                f"This plan leaves {_fmt_units(unserved)} of demand unserved "
                f"while the open sites have {_fmt_units(headroom)} of room "
                f"between them. The shortfall is out of reach rather than short "
                f"of capacity: it is the lanes and the delivery promise that "
                f"bind, not the size of any site.")
        elif standing and not strands:
            count = len(standing)
            reason = (
                f"This change strands no demand and fills no site that was not "
                f"already full. {count} {'site was' if count == 1 else 'sites were'} "
                f"at {'its' if count == 1 else 'their'} ceiling before it and "
                f"{'still is' if count == 1 else 'still are'}: that is a "
                f"constraint of today's network, not of this change.")
        elif unserved is not None and unserved <= 0 and not cap.get("at_ceiling"):
            reason = (
                "This plan serves all of the demand and no site reaches its "
                "capacity ceiling, so nothing in the network is constraining "
                "it. There is no capacity change to recommend.")
        elif not cap:
            reason = (
                "This scenario was solved before the per-site capacity "
                "account was recorded, so which sites it fills is not known "
                "for it. Re-run the scenario to see what it asks of each "
                "site.")
        else:
            reason = (
                "Nothing in this plan meets the threshold for a recommended "
                "change: this change fills no site, strands no demand, and "
                "leaves no region without room.")
        add({"key": "NO_ACTION", "label": label, "reason": reason, "target": {}})

    for index, entry in enumerate(actions, start=1):
        entry["priority"] = index
        statement = bool(entry.get("statement")) or entry["key"] == "NO_ACTION"
        entry["statement"] = statement
        # A statement opens no form and names no verb — see the docstring.
        entry["scenario"] = ({} if statement else
                             _scenario_for(entry["key"], entry.get("target") or {}))
        # The phrase under the button, naming THIS change — from the same map
        # the Insights feed reads, so one decision reads the same on both
        # screens. No destination: this card is already in the planner.
        entry["cta"] = "" if statement else _CTA_BY_ACTION.get(entry["key"], "")
        assert entry["key"] in _ACTION_KEYS, entry["key"]
    return actions


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
    under_used: List[Dict[str, Any]] = []
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
        elif util < _UNDER_USED_PCT:
            # OPEN, PAID FOR, AND NEARLY EMPTY. Everything that was neither at
            # its ceiling nor working harder used to fall off the end of this
            # loop, so the one plan shape this card could say nothing about was
            # the one with capacity going to waste in it.
            under_used.append(row)

    # Busiest first: the site a planner has to deal with is the fullest one.
    at_ceiling.sort(key=lambda r: -(r["added_units"] or 0))
    working_harder.sort(key=lambda r: -(r["util_pct"] or 0))
    # Emptiest first: the site with the least going through it is the one worth
    # pricing a consolidation against.
    under_used.sort(key=lambda r: (r["util_pct"] or 0))
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
        # Open sites running below `_UNDER_USED_PCT`. The finding this card
        # could not make.
        "under_used": under_used[:_CAPACITY_SITE_LIMIT],
        "under_used_count": len(under_used),
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


def _capacity_pricing(engine: Any, snapshot_id: str, action: str,
                      facility_ids: List[str],
                      delta_units: Optional[float],
                      *,
                      limit: Optional[str] = None,
                      recurring_per_year: Optional[float] = None,
                      ) -> Optional[Dict[str, Any]]:
    """
    The fixed cost a capacity change was charged, site by site, and on what
    basis — from the same functions the builder applies.

    THE ANSWER TO "WHY DID THE COST MOVE, OR NOT?" The basis is one of:

      * STATED               — the caller stated the recurring cost, and it is
                               charged as stated;
      * PRO_RATA             — priced at the site's own fixed cost per unit of
                               the capacity it can actually use;
      * LIMIT_NOT_RAISED     — the change raised a limit that does not bind
                               (a plant's handling capacity above its
                               production limit), so nothing usable was added
                               and nothing is charged;
      * UNPRICED             — the upload states no fixed cost for the site,
                               so the added room carries no cost in this plan;
      * REDUCTION_KEEPS_COST — capacity taken away keeps its fixed cost.

    None for any other action, or when the snapshot cannot be read. Never
    raises: the solve beside it is authoritative either way.
    """
    if (action != "CHANGE_CAPACITY" or not isinstance(delta_units, (int, float))
            or not facility_ids):
        return None
    try:
        from netgravity.orchestrator.engines.scenario_builder import (
            _UNSTATED_CAPACITY,
            capacity_fixed_cost,
            planned_capacity,
            usable_capacity,
        )

        facilities = {f.id: f for f in
                      engine.snapshots.get(snapshot_id).network.facilities}
    except Exception:  # noqa: BLE001 — an unreadable snapshot prices nothing
        return None

    sites: List[Dict[str, Any]] = []
    for facility_id in facility_ids:
        fac = facilities.get(facility_id)
        if fac is None:
            continue
        handling = float(fac.capacity_units_per_period)
        raw = getattr(fac, "production_capacity_units_per_period", None)
        production = float(raw) if raw is not None else 1e12
        new_handling, new_production = planned_capacity(
            fac, delta_units=float(delta_units), limit=limit)
        usable_before = usable_capacity(fac, handling, production)
        usable_after = usable_capacity(fac, new_handling, new_production)
        before = float(fac.fixed_cost_per_year or 0.0)
        if recurring_per_year is not None:
            after = before + float(recurring_per_year)
            basis = "STATED"
        else:
            after = capacity_fixed_cost(before, usable_before, usable_after)
            if delta_units <= 0:
                basis = "REDUCTION_KEEPS_COST"
            elif usable_after <= usable_before:
                basis = "LIMIT_NOT_RAISED"
            elif (before <= 0 or usable_before <= 0
                  or usable_before >= _UNSTATED_CAPACITY):
                basis = "UNPRICED"
            else:
                basis = "PRO_RATA"
        sites.append({
            "facility_id": facility_id,
            "name": getattr(fac, "name", None) or facility_id,
            "limit": (limit or "ORDINARY"),
            "capacity_before": handling,
            "capacity_after": new_handling,
            "production_capacity_before": (production
                                           if production < _UNSTATED_CAPACITY else None),
            "production_capacity_after": (new_production
                                          if new_production < _UNSTATED_CAPACITY else None),
            "usable_capacity_before": usable_before,
            "usable_capacity_after": usable_after,
            "fixed_cost_per_year_before": round(before, 2),
            "fixed_cost_per_year_after": round(after, 2),
            "added_fixed_cost_per_year": round(after - before, 2),
            "basis": basis,
        })
    if not sites:
        return None
    bases = {site["basis"] for site in sites}
    return {
        "sites": sites,
        "added_fixed_cost_per_year": round(
            sum(site["added_fixed_cost_per_year"] for site in sites), 2),
        "basis": bases.pop() if len(bases) == 1 else "MIXED",
    }


def _cost_completeness(engine: Any, snapshot_id: str) -> Optional[Dict[str, Any]]:
    """
    Whether this network's cost is fully priced — said, not implied.

    A site with no fixed cost is a site whose rent, lease and overhead the
    upload did not give. Every figure is short by that amount, and a scenario
    that closes, consolidates or expands such a site is priced on freight and
    handling alone. Measured: an upload with its fixed-cost column removed was
    solved, compared and ranked as a complete network, and closing a DC read as
    a C$5.72M saving.

    None when the snapshot cannot be read, which is not the same as complete.
    """
    try:
        facilities = engine.snapshots.get(snapshot_id).network.facilities
    except Exception:  # noqa: BLE001
        return None
    scope = [f for f in facilities
             if getattr(f.role, "value", str(f.role)) not in ("MARKET", "CUSTOMER")
             and getattr(f.status, "value", str(f.status)) != "CLOSED"]
    if not scope:
        return None
    missing = [f.id for f in scope if float(f.fixed_cost_per_year or 0.0) <= 0.0]
    return {
        "complete": not missing,
        "sites": len(scope),
        "count": len(missing),
        "sites_without_fixed_cost": missing[:50],
        "missing_input": "fixed_cost_per_year" if missing else None,
    }


def _horizon(engine: Any, snapshot_id: str) -> Optional[Dict[str, Any]]:
    """How many periods a plan's costs cover, and what a period is."""
    try:
        network = engine.snapshots.get(snapshot_id).network
    except Exception:  # noqa: BLE001
        return None
    periods = len({d.period for d in network.demands}) or 1
    cost_period = network.config.cost_period
    return {"periods": periods,
            "cost_period": getattr(cost_period, "value", str(cost_period))}


def _investment(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    The one-time cost of a change, beside — never inside — its operating cost.

    Expansion and new sites used to carry no investment at all: capacity was a
    constraint with nothing to build, and a new DC cost its fixed and handling
    rates and nothing up front. Putting a one-time figure into a twelve-month
    operating solve would be the opposite error — a building that lasts decades
    charged against one year of savings, so nothing would ever be built.

    So the solve stays an operating plan, and this block states the rest from
    the record's own figures:

      * `cost_change`                — the plan's cost against today;
      * `capacity_fixed_cost_change` — the part of that which is the new
                                       capacity's own recurring fixed cost;
      * `operating_cost_change`      — the rest: freight, handling and stock;
      * `payback_periods`            — the one-time cost over the plan's saving
                                       per period, where it saves anything.

    `one_time_cost_stated` is False when the caller gave none, which the screen
    says rather than treating as free. None for any other action.
    """
    request = record.get("request") or {}
    action = str(request.get("action") or "").upper()
    if action == "CHANGE_CAPACITY":
        one_time = request.get("expansion_one_time_cost")
        # Taking capacity away builds nothing. Without this a reduction read
        # "no one-time cost stated" and was compared "as though it cost
        # nothing up front", which is a caveat about a decision it is not.
        delta = request.get("capacity_delta_units")
        if (one_time is None and isinstance(delta, (int, float))
                and not isinstance(delta, bool) and delta <= 0):
            return None
    elif action == "ADD_FACILITY":
        one_time = (request.get("new_facility") or {}).get("opening_cost")
    else:
        return None
    stated = isinstance(one_time, (int, float)) and not isinstance(one_time, bool)

    baseline = record.get("baseline_kpis") or {}
    scenario = record.get("scenario_kpis") or {}
    base_cost = _valid(baseline, "business_network_cost")
    cost = _valid(scenario, "business_network_cost")
    base_fixed = _valid(baseline, "facility_cost")
    fixed = _valid(scenario, "facility_cost")
    horizon = record.get("horizon") or {}
    periods = horizon.get("periods")
    periods = periods if isinstance(periods, int) and periods > 0 else 1

    change = None if cost is None or base_cost is None else round(cost - base_cost, 4)
    own = None if fixed is None or base_fixed is None else round(fixed - base_fixed, 4)
    operating = None if change is None or own is None else round(change - own, 4)
    per_period = None if change is None else change / periods
    payback = (round(float(one_time) / -per_period, 2)
               if stated and one_time > 0 and per_period is not None and per_period < 0
               else None)
    return {
        "one_time_cost": float(one_time) if stated else None,
        "one_time_cost_stated": stated,
        "cost_change": change,
        "capacity_fixed_cost_change": own,
        "operating_cost_change": operating,
        "periods": periods,
        "cost_period": horizon.get("cost_period") or "MONTH",
        "payback_periods": payback,
        "note": ("The one-time cost is in none of the cost figures on this "
                 "scenario, which are operating costs over the modelled "
                 "periods. The recurring cost of the new capacity is in them."),
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

    def serves_less(fill: Optional[float], than: Optional[float]) -> bool:
        return (fill is not None and than is not None
                and (fill - than) * 100.0 < -_MATERIAL_FILL_DROP_PTS)

    def saving_is_shrinkage(cost_delta: Optional[float], fill: Optional[float],
                            than_fill: Optional[float],
                            than_cost: Optional[float]) -> bool:
        """
        Whether a saving is made of the demand the plan stops serving.

        The demand dropped is valued at the other side's own average cost per
        unit served: (fill lost / fill) x that side's cost. When that is at
        least half the saving, the saving is mostly shrinkage. "Serves less"
        alone is too blunt: a plan 161M cheaper that left 0.05% of demand
        unserved was told its saving came from that demand, which on its own
        figures was worth about 370K of it.
        """
        if (cost_delta is None or cost_delta >= 0 or than_cost is None
                or not than_fill or not serves_less(fill, than_fill)):
            return False
        dropped = than_cost * (than_fill - fill) / than_fill
        return dropped >= 0.5 * abs(cost_delta)

    rows: List[Dict[str, Any]] = []
    for record in records:
        kpis = record.get("scenario_kpis") or {}
        cost = _valid(kpis, "business_network_cost")
        fill = _valid(kpis, "demand_fill_rate")
        reference_cost = _valid(record.get("reference_kpis") or {},
                                "business_network_cost")
        reference_fill = _valid(record.get("reference_kpis") or {},
                                "demand_fill_rate")
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
            # THE SAVING THAT IS NOT ONE. A plan serving materially less demand
            # than today spends less because it ships less. Measured: closing
            # one DC stranded 316,754 units and read as C$5.72M cheaper, in
            # green. Flagged here so the ranking and the screen stop presenting
            # a smaller promise as a cheaper network.
            # Ranks the plan behind every plan that keeps today's service.
            "sheds_demand": serves_less(fill, baseline_fill),
            # And whether what it saves is mostly that demand — the claim the
            # verdict and the screen are allowed to make only when it holds.
            "saving_is_shrinkage": saving_is_shrinkage(
                None if cost is None or baseline_cost is None
                else cost - baseline_cost, fill, baseline_fill, baseline_cost),
            # The same test against the reference, for the half of the
            # attribution that is the change itself.
            "change_sheds_demand": saving_is_shrinkage(
                None if cost is None or reference_cost is None
                else cost - reference_cost, fill, reference_fill, reference_cost),
            # Below this, two solves of the same network differ by the solver's
            # own optimality tolerance, not by anything that happened.
            "noise_floor": _noise_floor(baseline_cost),
            "comparable": cost is not None and baseline_cost is not None,
        })
    # Deterministic regardless of the order the ids arrived in.
    #
    # Sorting on cost alone left ties — two scenarios with equal cost, or two
    # with no comparable cost at all — resolved by input order. So comparing
    # A and B named a different winner than comparing B and A, which is the
    # same analysis asked twice. The id is the tiebreak: arbitrary, but
    # stable, which is the property that matters.
    #
    # A plan that is cheaper only by serving less ranks BEHIND every plan that
    # keeps today's service, whatever the two cost. Ranked on cost alone, the
    # closure that strands a tenth of demand came first on every comparison it
    # was in.
    rows.sort(key=lambda r: (r["cost_delta"] is None,
                             bool(r["sheds_demand"]),
                             r["cost_delta"] if r["cost_delta"] is not None else 0.0,
                             str(r["scenario_id"] or "")))
    return rows


def _noise_floor(baseline_cost: Optional[float]) -> float:
    """
    The smallest cost difference between two solves that is a finding.

    Every solve stops inside a relative optimality gap
    (`OptimizationConfig.mip_gap`, 0.1%), so two solves of the SAME network can
    land that far apart — and did: the unchanged 56,081,045 network re-solved
    as the reference came back at 56,109,836, and the card attributed the
    28,791 between them to "re-optimising today's footprint" on a plan whose
    footprint is held open and cannot be re-optimised at all.
    """
    from netgravity.schemas.network import OptimizationConfig

    gap = float(OptimizationConfig.model_fields["mip_gap"].default or 0.0)
    if not isinstance(baseline_cost, (int, float)):
        return 1.0
    return max(1.0, abs(float(baseline_cost)) * gap)


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
    floor = float(best.get("noise_floor") or 1.0)
    if reopt is None or change is None or abs(reopt) < floor:
        return {}
    if abs(change) < floor:
        return {
            "reoptimisation_amount": reopt,
            "change_amount": change,
            "change_direction": "none",
            "text": ("None of this difference is the change itself: the solver "
                     "reaches the same plan with or without it. All of it comes "
                     "from re-optimising the footprint you already have, which "
                     "is available without this scenario."),
        }
    sheds = change < 0 and bool(best.get("change_sheds_demand"))
    return {
        "reoptimisation_amount": reopt,
        "change_amount": change,
        "change_direction": "adds" if change > 0 else "saves",
        # A change that "saves" by serving less demand than the same network
        # re-optimised has not found a cheaper way to do the same job.
        "change_sheds_demand": sheds,
        "text": ("Part of the difference against the network you run today "
                 "comes from re-optimising the footprint you already have — "
                 "available without this scenario — and part from the change "
                 "itself."
                 + (" What the change itself saves, it saves by serving less "
                    "demand." if sheds else "")),
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
    # Options that cost less than the winner, which after the ranking can only
    # be ones that serve less demand to do it.
    undercut = [r for r in rows[1:] if r["comparable"]
                and r["cost_delta"] is not None and r["cost_delta"] < delta]
    # Plain business English, and no engine vocabulary. It read "below the
    # current network on solved business network cost", which is a sentence
    # about a solver rather than about a decision.
    if delta < 0 and best.get("saving_is_shrinkage"):
        verdict = (f"{best['name']} costs less than the network you run today, "
                   f"but only because it leaves demand unserved that today's "
                   f"network serves.")
    elif delta < 0 and undercut:
        verdict = (f"{best['name']} costs less than the network you run today "
                   f"while serving the same demand. "
                   f"{len(undercut)} other "
                   f"{'option costs' if len(undercut) == 1 else 'options cost'} "
                   f"less but {'serves' if len(undercut) == 1 else 'serve'} "
                   f"less demand.")
    elif delta < 0:
        others = len(rows) - 1
        verdict = (f"{best['name']} costs less than the network you run today, "
                   f"and less than the {others} other "
                   f"{'option' if others == 1 else 'options'} compared."
                   if others else
                   f"{best['name']} costs less than the network you run today.")
    elif any(r["comparable"] and r.get("saving_is_shrinkage") for r in rows):
        verdict = (f"Nothing compared costs less than the network you run "
                   f"today without serving less demand. {best['name']} comes "
                   f"closest.")
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


# ---------------------------------------------------------------------------
# "What did we change, what did it do, and how do you know?" — as a document
# ---------------------------------------------------------------------------
#: The cost lines a plan is made of, in the order a reader adds them up.
_COST_COMPONENTS = (
    ("transport_cost", "Transport"),
    # `facility_cost`, which is what the KPI registry calls it. Asking for
    # `fixed_cost` matched nothing, so the largest single line in this
    # network's cost — ₹95,000 a period, more than half the total — was
    # missing from the table while the components below it were listed.
    ("facility_cost", "Fixed facility"),
    ("handling_cost", "Handling"),
    ("inventory_cost", "Inventory"),
    ("opening_cost", "Opening"),
    ("closure_cost", "Closure"),
    ("business_network_cost", "Total network cost"),
)

#: The service and utilisation figures, with the label a reader recognises.
_OUTCOME_METRICS = (
    ("demand_fill_rate", "Demand met"),
    ("unserved_demand", "Demand left unserved"),
    ("pct_demand_in_sla", "Demand within its lead time"),
    ("avg_utilization_pct", "Average site utilisation"),
    ("max_utilization_pct", "Busiest site"),
    ("n_facilities_open", "Sites open"),
    ("total_carbon_kg", "Transport emissions"),
)


def _kpi_display(block: Dict[str, Any], key: str) -> str:
    """
    One KPI, formatted the way the screens format it.

    Reads `display_value` when the KPI layer supplied one — which is the rule
    everywhere else in this product: the engine that computed a figure decided
    how it reads, and a second opinion about that here is how a document and
    the screen it came from disagree about one number.

    THE ROW'S OWN `unit` DECIDES THE REST, not the metric name. A stored KPI
    carries `unit: "INR"` or `unit: "fraction"` beside its value, and that is
    the only place the currency of THIS network is recorded on the record —
    the reasoning payload's currency is not in scope here. Formatting money
    without it printed "167,050.33 per period" in a document that is
    forwarded to people who cannot know from context whether that is rupees
    or dollars.
    """
    row = block.get(key)
    unit = ""
    if isinstance(row, dict):
        shown = row.get("display_value")
        if shown:
            return str(shown)
        value = row.get("value")
        unit = str(row.get("unit") or "")
    else:
        value = row
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "Not available"

    from netgravity.orchestrator.reasoning.evidence import (
        _CURRENCY_SYMBOLS, _display, format_money)

    code = unit.strip().upper()
    # A three-letter alphabetic unit that is not one of the measures this
    # product records IS an ISO currency code — `format_money` renders an
    # unlisted one as "AED 1,234.00" rather than dropping it, so a network in
    # a currency this build has no symbol for still says which one it is.
    _NOT_CURRENCY = {"PCT", "KGS", "DAY", "QTY", "PPM", "KMS", "TON"}
    if code in _CURRENCY_SYMBOLS or (
            len(code) == 3 and code.isalpha() and code not in _NOT_CURRENCY):
        return format_money(value, code)
    if unit.strip().lower() in ("fraction", "ratio"):
        # A fill rate stored as 1.0 is "100.0%" to a reader. "1.000" is the
        # storage format and reads as a scale nobody defined.
        return f"{value * 100:,.1f}%"
    return _display(value, key)[0]


def _delta_display(scenario: Dict[str, Any], baseline: Dict[str, Any],
                   key: str) -> str:
    """The change between the two plans, as a percentage of the baseline."""
    def raw(block):
        row = block.get(key)
        value = row.get("value") if isinstance(row, dict) else row
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    after, before = raw(scenario), raw(baseline)
    if after is None or before is None:
        return "not comparable"
    if after == before:
        # Reached before the zero check, so a line that is zero on both sides
        # reads "unchanged" rather than "no baseline to compare against" — a
        # component neither plan incurs is not a missing comparison.
        return "unchanged"
    if before == 0:
        return "nothing in the baseline to compare against"
    return f"{(after - before) / abs(before) * 100.0:+,.1f}%"


def _scenario_derivation(record: Dict[str, Any], project_name: str,
                         actions: List[Dict[str, Any]]) -> Any:
    """
    One scenario, as a `DerivationReport`.

    The third caller of `netgravity.reporting`, and deliberately the same
    shape as the insight and the forecast: a reader asking "how was this
    reached?" is asking one question, and three documents answering it
    differently would make the answer look like a property of which screen it
    was pressed from.

    NOTHING IS SOLVED HERE. Every figure is one the MILP already produced and
    this project already stored, printed through the same display rule the
    screens use.
    """
    from datetime import datetime, timezone

    from netgravity.reporting import DerivationReport, DerivationStep, Figure

    scenario_kpis = record.get("scenario_kpis") or {}
    baseline_kpis = record.get("baseline_kpis") or {}
    request_block = record.get("request") or {}
    explanation = record.get("explanation") or {}
    card = explanation.get("card") or {}
    cap = record.get("capacity_response") or {}
    name = record.get("name") or record.get("id") or "Scenario"

    steps: List[Any] = []

    # ── 1. what was changed ──────────────────────────────────────────
    change_figures = []
    action = str(request_block.get("action") or "").replace("_", " ").title()
    if action:
        change_figures.append(Figure("Change requested", action, "Input",
                                     "scenario builder"))
    if request_block.get("facility_ids"):
        change_figures.append(Figure(
            "Sites named", ", ".join(str(f) for f in request_block["facility_ids"]),
            "Input", "scenario builder"))
    if request_block.get("capacity_delta_units") is not None:
        delta = request_block["capacity_delta_units"]
        change_figures.append(Figure(
            "Capacity adjustment",
            f"{delta:+,.0f} units per period", "Input", "scenario builder"))
    if request_block.get("demand_multiplier") is not None:
        change_figures.append(Figure(
            "Demand", f"x{request_block['demand_multiplier']} on every demand row",
            "Input", "scenario builder"))
    if request_block.get("transport_cost_multiplier") is not None:
        change_figures.append(Figure(
            "Freight rates", f"x{request_block['transport_cost_multiplier']}",
            "Input", "scenario builder"))
    if request_block.get("sla_days_delta") is not None:
        change_figures.append(Figure(
            "Delivery promise", f"{request_block['sla_days_delta']:+} days",
            "Input", "scenario builder"))
    for override in (record.get("overrides") or []):
        change_figures.append(Figure("Applied to the network as", str(override),
                                     "Input", "scenario builder"))
    steps.append(DerivationStep(
        title="What was changed",
        detail=("The intervention exactly as it was submitted, and how the "
                "builder applied it to the network. Everything below follows "
                "from re-solving the network with these changes in place and "
                "nothing else altered."),
        figures=tuple(change_figures) or (
            Figure("Change requested", "Not recorded", "Input", ""),)))

    # ── 2. what it cost ──────────────────────────────────────────────
    cost_figures = []
    for key, label in _COST_COMPONENTS:
        after = _kpi_display(scenario_kpis, key)
        if after == "Not available":
            continue
        before = _kpi_display(baseline_kpis, key)
        cost_figures.append(Figure(
            label, f"{after}   (was {before}, {_delta_display(scenario_kpis, baseline_kpis, key)})",
            "Measured", "MILP"))
    if cost_figures:
        steps.append(DerivationStep(
            title="What the plan costs, component by component",
            detail=("Each line is the solved plan's own cost for this "
                    "scenario, with the same line from the baseline solve "
                    "beside it. The shortage penalty the solver uses to decide "
                    "which demand to strand is excluded — nobody pays it — so "
                    "unserved demand is reported below as a quantity rather "
                    "than as money."),
            figures=tuple(cost_figures)))

    # ── 3. what it does to service ───────────────────────────────────
    outcome_figures = []
    for key, label in _OUTCOME_METRICS:
        after = _kpi_display(scenario_kpis, key)
        if after == "Not available":
            continue
        before = _kpi_display(baseline_kpis, key)
        outcome_figures.append(Figure(
            label, f"{after}   (was {before})", "Measured", "MILP"))
    if outcome_figures:
        steps.append(DerivationStep(
            title="What it does to service and utilisation",
            detail=("Cost is not the only thing a network change moves. These "
                    "are the figures a cost saving has to be weighed against, "
                    "each from the same solve."),
            figures=tuple(outcome_figures)))

    # ── 4. what it asks of the sites ─────────────────────────────────
    site_figures = []
    for row in (cap.get("at_ceiling") or [])[:8]:
        util = row.get("util_pct")
        site_figures.append(Figure(
            str(row.get("name") or row.get("id")),
            (f"{util:,.1f}% of capacity" if isinstance(util, (int, float))
             else "at its ceiling"),
            "Full in this plan", str(row.get("region") or "")))
    for row in (cap.get("working_harder") or [])[:8]:
        util = row.get("util_pct")
        site_figures.append(Figure(
            str(row.get("name") or row.get("id")),
            (f"{util:,.1f}% of capacity" if isinstance(util, (int, float))
             else "carrying more"),
            "Working harder", str(row.get("region") or "")))
    for row in (cap.get("idle") or [])[:8]:
        site_figures.append(Figure(
            str(row.get("name") or row.get("id")),
            _fmt_units(row.get("capacity")) + " left closed",
            "Not used by this plan", str(row.get("region") or "")))
    if site_figures:
        steps.append(DerivationStep(
            title="What this asks of each site",
            detail=("Which sites the plan fills, which are carrying more than "
                    "they do today, and what capacity it chose to leave "
                    "closed. This is the part that differs between raising "
                    "demand by 5% and raising it by 50%, and it is where every "
                    "recommendation below comes from."),
            figures=tuple(site_figures)))

    # ── 5. what to do about it ───────────────────────────────────────
    if actions:
        steps.append(DerivationStep(
            title="What is recommended, and why",
            detail=("Each recommendation is gated on a finding in this "
                    "scenario's own solved result — not on a general rule "
                    "about networks. Where nothing meets the threshold, that "
                    "is stated rather than filled in."),
            figures=tuple(
                Figure(str(a.get("label") or ""), str(a.get("reason") or ""),
                       ("Statement" if a.get("key") == "NO_ACTION"
                        else "Recommendation"),
                       "solved result")
                for a in actions)))

    # ── the conclusion ───────────────────────────────────────────────
    cost_now = _kpi_display(scenario_kpis, "business_network_cost")
    cost_change = _delta_display(scenario_kpis, baseline_kpis,
                                 "business_network_cost")
    feasible = record.get("feasible")
    if feasible is False:
        conclusion = (f"{name} has no feasible plan: the network cannot meet "
                      f"the constraints this scenario imposes")
    else:
        conclusion = (f"{name} costs {cost_now} per period, {cost_change} "
                      f"against the network as it runs today")

    limitations = []
    for item in (explanation.get("missing_information") or []):
        text = item.get("reason") if isinstance(item, dict) else str(item)
        if text:
            limitations.append(str(text))
    if record.get("reference_note"):
        limitations.append(str(record["reference_note"]))
    if not cap:
        limitations.append(
            "This scenario carries no per-site capacity account, so which "
            "sites it fills is not established here.")
    limitations.append(
        "A scenario is an evaluation, not a decision. Opening or closing a "
        "site is classified as a human decision by governance whatever the "
        "economics say, and nothing in this document approves anything.")

    provenance_block = record.get("provenance") or {}
    provenance = (
        f"Source: {provenance_block.get('engine') or 'netgravity MILP'}, "
        f"read through {provenance_block.get('authoritative_source') or 'the KPI layer'}. "
        f"Snapshot {record.get('snapshot_id') or 'unknown'}; "
        f"execution {record.get('execution_id') or 'unknown'}.")
    grounding = (explanation.get("grounding") or {}).get("warnings") or []
    if grounding:
        provenance += " Validation warnings: " + "; ".join(str(g) for g in grounding) + "."

    return DerivationReport(
        kind="Scenario analysis",
        subject=f"{name}{f' — {project_name}' if project_name else ''}",
        conclusion=conclusion,
        summary=(str(card.get("headline") or "").strip()
                 or "This document states what this scenario changed, what "
                    "the solver did with it, and what follows from the result."),
        method=(
            "A scenario is evaluated by solving the network twice. The "
            "baseline solve optimises the network exactly as uploaded. The "
            "scenario solve applies the change listed below and re-optimises "
            "with the same freedom — the same objective, the same "
            "constraints, the same sites available to open or close. Every "
            "figure in this document is the difference between those two "
            "solved plans, read through the authoritative KPI layer. No "
            "language model takes part in producing any figure here."),
        steps=steps,
        recommended_action=(
            "; ".join(str(a.get("label")) for a in actions
                      if a.get("key") != "NO_ACTION")
            or (actions[0].get("reason") if actions else "")),
        assumptions=[str(d) for d in (card.get("details") or [])],
        limitations=limitations,
        provenance=provenance,
        generated_at="Generated "
                     + datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC"),
    )


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
    def _gateway() -> Any:
        """
        The gateway every explanation on this blueprint shares.

        `explanation_gateway()` rather than a fresh `LLMGateway()`: the budget
        is cumulative and SHARED across every holder of the token — 100
        requests a day for the whole product — so two clients each believing
        they have the full allowance is how a shared limit gets exceeded
        rather than respected.
        """
        from netgravity.orchestrator.explanation_llm import explanation_gateway

        return explanation_gateway()

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
        # WHAT THE FIGURES DO NOT CONTAIN, before anything about what they say.
        incomplete = [r for r in selected
                      if (r.get("cost_completeness") or {}).get("complete") is False]
        if incomplete:
            caveats.insert(0, (
                "These costs are incomplete: the upload states no fixed cost for "
                "some sites, so their rent, lease and overhead are missing from "
                "every figure compared, and closing, consolidating or expanding "
                "them is priced on freight and handling alone."))
        investing = [r for r in selected
                     if ((r.get("investment") or {}).get("one_time_cost") or 0) > 0]
        if investing:
            caveats.append(
                f"{len(investing)} of the scenarios compared "
                f"{'requires' if len(investing) == 1 else 'require'} a one-time "
                f"investment that is not in the cost ranking; it is reported "
                f"beside each scenario.")
        unstated = [r for r in selected
                    if (r.get("investment") or {}).get("one_time_cost_stated") is False]
        if unstated:
            caveats.append(
                f"{len(unstated)} of the scenarios compared "
                f"{'states' if len(unstated) == 1 else 'state'} no one-time "
                f"cost, so building or expanding is compared as though it cost "
                f"nothing up front.")
        if recommended and recommended.get("reference_note"):
            caveats.append(recommended["reference_note"])

        # Cost NEXT TO service and risk, never cost alone. Supplied by code,
        # so the model states no figure and cannot state one in the wrong
        # currency. Money travels as an amount; the screen applies the
        # project's own currency to it.
        best_row = verdict.get("best_row") or {}
        warning = _service_warning(best_row, recommended)
        figures = _comparison_figures(best_row, recommended)

        # WHAT TO DO, per scenario, derived here from each solved record.
        #
        # Sent on the comparison rather than only on `/simulate` so a scenario
        # solved before this existed still gets its actions — and so the list
        # is recomputed against the record as it now stands, rather than
        # replayed from whatever was true when it was first saved.
        actions = {r.get("id"): _recommended_actions(r) for r in selected}

        return jsonify({
            "project_id": project_id,
            "baseline_kpis": baseline,
            "ranked": rows,
            "recommended_actions": actions,
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
        # What a capacity change moves and what it costs. See
        # `ScenarioIntentSpec.capacity_limit` and the two expansion fields.
        one_time = number("expansion_one_time_cost")
        recurring = number("expansion_fixed_cost_per_year")
        capacity_limit = str(body.get("capacity_limit") or "").strip().upper() or None
        if capacity_limit not in (None, "BOTH", "HANDLING", "PRODUCTION"):
            raise ValidationError(
                "capacity_limit must be BOTH, HANDLING or PRODUCTION.",
                context={"capacity_limit": capacity_limit})

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
            capacity_limit=(capacity_limit
                            if action == ScenarioActionType.CHANGE_CAPACITY else None),
            expansion_one_time_cost=(
                one_time if action == ScenarioActionType.CHANGE_CAPACITY else None),
            expansion_fixed_cost_per_year=(
                recurring if action == ScenarioActionType.CHANGE_CAPACITY else None),
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
            # WHY it could not be run, in units, when the reason was that the
            # network cannot serve the demand this scenario asks of it.
            #
            # An infeasible scenario used to come back as "no feasible solution
            # exists" and nothing else — true, and unactionable. The solver now
            # attaches a diagnosis (`milp._diagnose_infeasible`) and it travels
            # here on the failing step's own context.
            diagnosis = next(
                (dict((e.get("context") or {}).get("infeasibility") or {})
                 for e in (response.errors or [])
                 if (e.get("context") or {}).get("infeasibility")), None)
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
                                "orchestrator_status": str(response.status),
                                # Structured, so the screen can state the
                                # shortfall rather than print a paragraph. None
                                # when the failure was not an infeasible solve.
                                "infeasibility": diagnosis},
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
                "capacity_limit": (capacity_limit
                                   if action_str == "CHANGE_CAPACITY" else None),
                "expansion_one_time_cost": (one_time
                                            if action_str == "CHANGE_CAPACITY" else None),
                "expansion_fixed_cost_per_year": (recurring
                                                  if action_str == "CHANGE_CAPACITY" else None),
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
            # What the capacity change was charged, and on what basis. See
            # `_capacity_pricing`.
            "capacity_pricing": _capacity_pricing(
                engine, snapshot_id, action_str, list(facility_ids), cap_delta,
                limit=capacity_limit, recurring_per_year=recurring),
            # Whether the costs on this record are a fully priced network.
            "cost_completeness": _cost_completeness(engine, snapshot_id),
            # How many periods the costs cover, and what a period is.
            "horizon": _horizon(engine, snapshot_id),
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

        # What to DO about it, from the solved result. Written onto the record
        # AFTER it is complete, because it reads the capacity response and the
        # explanation that were just built.
        # One-time investment, reported BESIDE the operating cost. Written
        # before the recommendations, which do not read it, and after the
        # KPIs, which it does.
        record["investment"] = _investment(record)
        record["recommended_actions"] = _recommended_actions(record)

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

    @bp.route("/<scenario_id>/document", methods=["GET"])
    @require_auth
    @rate_limit("scenario.document", limit=30, window_seconds=60)
    def scenario_document(scenario_id: str):
        """
        One scenario, as the document a decision gets taken from.

        WHY THIS EXISTS. The recommendation card answers "what should I do?"
        in a paragraph. The question that follows it, in the room where the
        decision is actually made, is "what exactly did you change, what did
        it move, and how do you know?" — and that is four tables and a method
        note. It has to survive being forwarded to somebody who will never
        open this application.

        NOTHING IS SOLVED HERE. Every figure is one the MILP already produced
        and this project already stored, printed through the same display rule
        the screens use, so the file and the screen cannot disagree.

        The writer is `netgravity.reporting`, the same one the insight and the
        forecast documents use.
        """
        from netgravity.reporting import build_derivation_docx, narrate

        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            record = next((r for r in _store.get(project_id, [])
                           if r.get("id") == scenario_id), None)
        if record is None:
            raise NotFoundError(
                f"Scenario '{scenario_id}' is not in this project, so there "
                f"is nothing to document.")

        # Recomputed from the record as it now stands rather than replayed
        # from whatever was saved with it, so the document and the card state
        # the same recommendations.
        actions = _recommended_actions(record)

        project_name = ""
        try:
            project = project_registry.get(project_id,
                                           user_id=g.current_user.user_id)
            project_name = str(getattr(project, "name", "") or "")
        except Exception:  # noqa: BLE001 — the title reads fine without it
            project_name = ""

        report = _scenario_derivation(record, project_name, actions)

        # The model writes the joining-up and cannot add a figure: every
        # number it quotes is checked against the figures already in the
        # report, and any sentence quoting one that is not there is dropped.
        narration = narrate(report, _gateway(), purpose="scenario_document")
        report.narrative = list(narration.paragraphs)
        report.narrative_note = (
            narration.note
            if (narration.paragraphs or narration.source == "rejected") else "")

        document = build_derivation_docx(report)
        out = make_response(document)
        out.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document")
        out.headers["Content-Disposition"] = (
            f'attachment; filename="{report.filename()}"')
        out.headers["Cache-Control"] = "no-store"
        return out

    @bp.errorhandler(ApplicationError)
    def _scenario_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
