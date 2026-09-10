"""
NetGravity — Insights API Blueprint
===================================
Project-scoped insights and recommendations for the network a project is bound
to, produced by the Orchestrator's Reasoning Agent from the solved Digital Twin
state.

Why this endpoint exists
------------------------
The insight machinery was complete and unreachable. `POST
/orchestrator/insights` existed and worked; `reasoning-service.js` existed and
wrapped it; the Reasoning Agent existed and produced grounded briefings. But
nothing on any screen called any of it, and the two structures the dashboard
reads its insight feed from — `HOME_INSIGHTS` and `HOME_ACTION_ITEMS` — were
initialised empty and never written by anything. So every user who uploaded
their own data saw "No insights have been generated for this network yet",
permanently, on a network that had been fully solved.

The orchestrator endpoint could not close that on its own, because it is keyed
by Digital Twin `state_id`. A dashboard holds a `project_id`. Resolving one to
the other means knowing that a project has a snapshot, that a snapshot has a
twin state, and which of several states is the one the KPIs came from — control
plane knowledge that has no business being in a browser.

So this blueprint answers the question the dashboard actually has ("what should
I know about this project's network?") and follows the same shape as
`api/kpis.py`: authenticated, project-scoped, rate-limited, and cached in the
same durable analysis store — a briefing is derived data about one version of
one network, so it is computed once per version rather than per request.

What it does NOT do
-------------------
It does not compute a KPI, run a solve of its own, or invent a figure. Every
number in every insight comes from the Reasoning Agent's evidence pack, which
is built from the twin state, which is built from the MILP result. The numeric
grounding verdict travels with the response, so a consumer can see whether the
narrative was checked against the deterministic facts rather than assuming it.
"""

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, g, jsonify, make_response, request

from app.backend.services.errors import (
    ApplicationError,
    ConflictError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.correlation import orchestrator_request_id
from app.backend.services.analysis_store import analysis_service
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.schemas.reasoning import ReasoningScope
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

#: Scopes a caller may ask for. `COMPARISON` is deliberately absent: it needs a
#: second state, which is the scenario comparison endpoint's job.
_ALLOWED_SCOPES = {"NETWORK", "FACILITY", "LANE"}

#: Version of this endpoint's CONTENT — its shape and its wording alike. Part
#: of the cache key; see the note at the `variant` assignment.
#:
#: Bump on any field added to or removed from the body, AND on any change to
#: the prose the Reasoning Agent generates into it. The second half is not a
#: nicety: cached entries are keyed on the network's `data_version`, which does
#: not move when the code that writes the narrative changes. So a deploy that
#: corrects a materially-false sentence goes on serving the false one to every
#: project whose network has not been re-uploaded since — which is precisely
#: what happened to the SLA insight below.
#:
#:   1  the original briefing payload
#:   2  evidence gained `value` and `role`; insights gained `entities`;
#:      the body gained `thresholds` and `series`; ids gained a headline digest
#:   3  the SLA insight no longer describes unserved demand as "served late",
#:      and a cost COMPONENT names the horizon it covers rather than being
#:      labelled "per period" on a multi-period solve
#:   4  every insight carries `recommended_action` — what to DO about the
#:      finding — and `headline`/`narrative` are emitted in plain voice
#:      ("Demand fill rate is 0.68") rather than the agent's first person
#:      ("I see a demand fill rate of 0.68"), which is what the Overview's
#:      insight tiles read
#:   5  the deterministic template's headlines are conclusions rather than
#:      labels — "Facility cost is the largest single component of what this
#:      network costs", not "Facility cost as the largest cost line". A cached
#:      entry is keyed on the network's `data_version`, which does not move
#:      when the wording does, so without this every project that had already
#:      loaded its insights would keep the old labels for ever.
#:   6-8 further wording and shape changes: the structured `action` behind
#:      each recommendation, and the strategic phrasing that replaced
#:      "open the KPI page" throughout
#:   9  a recommendation is derived per FINDING rather than per network.
#:      Every capacity-family card used to call the ladder for its top rung
#:      and print the same sentence — seven identical recommendations on a
#:      loaded network, several of them contradicting the card above them.
#:      A cached briefing carries those sentences in its body, and nothing
#:      about the network changed, so without this bump every project that
#:      had loaded its insights would keep them.
#:  10  percentages print whole ("54%", not "54.00%") and a FACILITY-scoped
#:      card answers about the facility rather than repeating the network
#:      card's sentence word for word
#:  11  every insight says whether its recommendation is a DECISION
#:      (`actionable`), the cost-structure finding names the way to cut the
#:      largest cost line and the scenario that prices it, an unused-candidate
#:      finding names the site to reopen, and a capacity test carries a
#:      suggested amount. The Executive view ranks its tiles on the first.
#:  12  a fully-served plan's service finding recommends a demand-growth
#:      stress test instead of holding the run as the baseline
_PAYLOAD_VERSION = 12


#: Theme -> the per-facility field that theme is ABOUT. A chart for a finding
#: about utilisation plots utilisation; one about footprint plots throughput.
#: Absent from this map means the theme is not a per-facility statement, and no
#: entity rows are sent — an empty list is the honest answer, not a fallback to
#: whichever field happens to be present.
_FACILITY_THEME_FIELD = {
    "Capacity": "utilization_pct",
    "Utilisation": "utilization_pct",
    "Footprint": "throughput_units",
    "Resilience": "rei",
}

#: Themes whose subject is a lane rather than a site.
_LANE_THEME_FIELD = {
    "Carbon": "carbon_kg",
}

#: Ceiling on entity rows in one insight. A 400-facility network would
#: otherwise put 400 rows on the wire for a chart that can show perhaps 30.
_MAX_ENTITY_ROWS = 30


def _headline_digest(insight: Any) -> str:
    """
    A short, stable discriminator for two findings sharing a theme.

    Derived from the headline rather than from the position in the list: rank
    shifts when a different insight outranks it, and an id that moves is an id
    a dismissal cannot follow.
    """
    import hashlib

    headline = str(getattr(insight, "headline", "") or "")
    return hashlib.sha1(headline.encode("utf-8")).hexdigest()[:8].upper()


def _finite(value: Any) -> Optional[float]:
    """The value as a plottable float, or None. Bools are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _resolve_entities(insight: Any, pack: Any) -> List[Dict[str, Any]]:
    """
    The facilities or lanes a finding was computed OVER.

    `_utilization_insights` sorts every site by utilisation, names the worst
    three in prose, and discards the list. The sentence "3 sites are above the
    90% threshold" therefore reached the browser citing one scalar, so a screen
    could draw the maximum and nothing else. These are those rows — already
    built, already authoritative, previously thrown away.

    Sorted by the field the theme is about, descending, so a bar chart drawn in
    array order is ranked without the client sorting formatted strings.
    """
    if pack is None:
        return []
    payload = getattr(pack, "payload", {}) or {}
    theme = str(getattr(insight, "theme", "") or "")

    field = _FACILITY_THEME_FIELD.get(theme)
    if field:
        rows = []
        for item in payload.get("facilities", []) or []:
            value = _finite(item.get(field))
            if value is None:
                continue
            rows.append({
                "kind": "FACILITY",
                "entity_id": item.get("facility_id"),
                "label": item.get("facility_name") or item.get("facility_id"),
                "metric": field,
                "value": value,
                # Second-order facts a chart legend or tooltip needs, and which
                # a client must never recompute: a utilisation bar means one
                # thing for an open site and another for a closed one.
                "is_open": bool(item.get("is_open")),
                "role": item.get("role"),
                "capacity_units": _finite(item.get("capacity_units")),
                "throughput_units": _finite(item.get("throughput_units")),
            })
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows[:_MAX_ENTITY_ROWS]

    lane_field = _LANE_THEME_FIELD.get(theme)
    if lane_field:
        rows = []
        for item in payload.get("flows", []) or []:
            value = _finite(item.get(lane_field))
            if value is None:
                continue
            origin, dest = item.get("origin_id"), item.get("destination_id")
            rows.append({
                "kind": "LANE",
                "entity_id": f"{origin}->{dest}",
                "label": f"{origin} → {dest}",
                "metric": lane_field,
                "value": value,
                "flow_units": _finite(item.get("flow_units")),
                "distance_km": _finite(item.get("distance_km")),
            })
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows[:_MAX_ENTITY_ROWS]

    return []


def _thresholds_from(pack: Any) -> Dict[str, Any]:
    """The policy thresholds indexed into the pack, or an empty block."""
    payload = getattr(pack, "payload", {}) or {}
    block = payload.get("thresholds") or {}
    return {k: v for k, v in block.items() if _finite(v) is not None}


def _network_series(pack: Any) -> Dict[str, Any]:
    """
    Whole-network breakdowns a chart can draw, with raw values.

    Only what the solve actually produced. There is deliberately no utilisation
    time series here: see `_period_series`, which returns one when — and only
    when — the network states more than one demand period.
    """
    if pack is None:
        return {}
    payload = getattr(pack, "payload", {}) or {}
    network = payload.get("network_state") or {}
    out: Dict[str, Any] = {}

    components = network.get("cost_components") or {}
    if isinstance(components, dict):
        rows = [{"label": str(k).replace("_", " ").title(), "key": k,
                 "value": _finite(v)}
                for k, v in components.items() if _finite(v) is not None]
        # Ranked, and zero-valued components dropped: a cost breakdown listing
        # six components of which four are 0.00 reads as a solver failure.
        rows = [r for r in rows if r["value"] != 0.0]
        rows.sort(key=lambda r: r["value"], reverse=True)
        if rows:
            out["cost_components"] = rows

    return out


def _resolve_evidence(refs: List[str], pack: Any,
                      role: str = "metric") -> List[Dict[str, Any]]:
    """
    The metrics an insight cites, with their authoritative values.

    A deep-dive screen has to show what a finding is BASED on, and a list of
    opaque refs (`network_state.avg_utilization_pct`) is not that. Resolved here
    from the same evidence pack the narrative was written against, so the figure
    on the screen and the figure in the sentence cannot disagree.

    `value` carries the RAW number beside the formatted `display_value`.
    Without it a chart had two options, both bad: parse `"₹1,234,567.00"` back
    into a float in the browser — locale grouping, a currency glyph, `"12,000
    units"`, `"92.41%"` and the literal `"Not available"` all in the same field
    — or invent its own series. `display_value` stays authoritative for
    anything a user READS, so a figure in prose and the same figure on an axis
    cannot drift apart; `value` exists only to be plotted.

    `role` says WHY the figure is cited — the measurement, the thing it was
    compared against, or the driver behind it. The three ref lists used to be
    concatenated into one flat array, which threw that distinction away and
    left a table unable to label its own rows.
    """
    out: List[Dict[str, Any]] = []
    metrics = getattr(pack, "metrics", {}) or {}
    for ref in refs:
        metric = metrics.get(ref)
        if metric is None:
            continue
        raw = getattr(metric, "value", None)
        out.append({
            "ref": ref,
            "label": metric.label,
            "display_value": metric.display_value,
            # Only real, finite numbers. A bool is an int in Python and would
            # plot as 0/1; None means the engine could not compute it, and a
            # chart must render that as a gap rather than as zero.
            "value": (raw if isinstance(raw, (int, float))
                      and not isinstance(raw, bool)
                      and math.isfinite(raw) else None),
            "unit": metric.unit,
            "source": metric.source,
            "entity_id": metric.entity_id,
            "role": role,
        })
    return out


#: Themes whose finding is ABOUT the network's capacity, and which therefore
#: get a real intervention derived from the solved per-site load rather than a
#: sentence chosen by theme.
#:
#: These are the findings where "what should I do" has a defensible answer in
#: the data: a site is full, a site is empty, demand is not being reached. The
#: ladder in `strategic_actions` decides which rung applies.
_CAPACITY_THEMES = frozenset({
    "Capacity", "Utilisation", "Service", "Footprint", "Resilience",
})

#: What to DO about the findings the capacity ladder does not speak to, by
#: theme, when the narrative layer wrote no action of its own.
#:
#: The Reasoning Agent writes `recommended_action` per insight when the LLM
#: path is live (see `reasoning/prompts.py`). The deterministic template path
#: — which is what a cached, un-prompted briefing uses, and therefore what
#: most page loads see — writes prose and no action, and a tile headed
#: "Recommended action" with nothing under it is worse than no tile.
#:
#: EVERY ONE OF THESE NAMES A CHANGE, NOT A SCREEN. They used to read "Open the
#: KPI page to see which sites are over the threshold, then test a scenario
#: that relieves them" — which tells a reader to go and do the analysis
#: themselves, is identical on every network ever uploaded, and is the single
#: thing a senior audience has no use for. What replaced them states the
#: decision and leaves the evidence to the finding above it.
#:
#: None claims a saving, a magnitude or an outcome: nothing here has been
#: solved. The scenario each one names is what produces those.
_ACTION_BY_THEME = {
    # ---- the capacity family --------------------------------------------
    # These are reached only when the ladder produced nothing to say: a
    # healthy network, or a briefing with no solved rows behind it. They are
    # still DECISIONS — what to do about the finding — because "there is no
    # site to name" is not a reason to fall back to telling a reader which tab
    # to click.
    ("Service", "RISK"):
        "Put capacity near the demand this plan cannot reach, and price it "
        "against this baseline before the next planning round.",
    ("Service", "INFORMATION"):
        "Hold this run as the service baseline every scenario is measured "
        "against.",
    ("Capacity", "RISK"):
        "Relieve the sites that are over the threshold before demand grows "
        "into them — the lead time on capacity is longer than the warning.",
    ("Capacity", "OPPORTUNITY"):
        "Move volume onto the sites with headroom before adding capacity "
        "anywhere.",
    ("Capacity", "INFORMATION"):
        "Headroom is not a decision on its own. Put more volume through the "
        "sites with room before adding any.",
    ("Utilisation", "RISK"):
        "Relieve the sites that are over the threshold before demand grows "
        "into them.",
    ("Utilisation", "OPPORTUNITY"):
        "Consolidate the idle sites and price the saving against this "
        "baseline.",
    ("Utilisation", "INFORMATION"):
        "Headroom is not a decision on its own. Put more volume through the "
        "sites with room before adding any.",
    ("Footprint", "RISK"):
        "Decide on the sites this plan leaves unused before the footprint is "
        "committed.",
    ("Footprint", "OPPORTUNITY"):
        "Open the unused candidate sites and price the cost and service "
        "against this baseline.",
    ("Footprint", "INFORMATION"):
        "Treat this footprint as the baseline any change to it is measured "
        "against.",
    ("Resilience", "RISK"):
        "Spread the exposure off the sites carrying it, and price the "
        "alternative before it concentrates further.",
    ("Resilience", "INFORMATION"):
        "Keep the exposure this figure summarises inside its tolerance as the "
        "footprint changes.",
    # ---- everything the ladder does not speak to -------------------------
    ("Cost", "OPPORTUNITY"):
        "Price this against a changed footprint before committing to the "
        "current one.",
    ("Cost", "INFORMATION"):
        "Hold this as the baseline every proposed change is measured against.",
    ("Cost structure", "INFORMATION"):
        "Target the largest component of this cost first — the smaller ones "
        "cannot move the total far enough to matter.",
    ("Cost structure", "OPPORTUNITY"):
        "Take the largest cost component into a scenario and price the "
        "alternative before committing.",
    ("Carbon", "RISK"):
        "Shorten the longest lanes, or move them to a lower-emitting mode, "
        "and price the trade-off against cost.",
    ("Carbon", "OPPORTUNITY"):
        "Shorten the longest lanes and price the emissions saved against what "
        "the re-route costs.",
    ("Carbon", "INFORMATION"):
        "Treat this as the emissions baseline any re-routing is measured "
        "against.",
    ("Scenario impact", "OPPORTUNITY"):
        "Take this scenario to a decision, or park it — it has been priced "
        "against the baseline and is waiting on a call.",
    # The severity this one actually carries. It was emitted with the default
    # (INFORMATION), missed both lookups above, and fell through to the
    # severity fallback — so a card reading "this change raises what the
    # network costs" carried the line "No decision is needed on this one."
    ("Scenario impact", "INFORMATION"):
        "Weigh this price against the operational benefit and take the "
        "scenario to a decision, or park it.",
    ("Scenario impact", "RISK"):
        "Decide whether this price is worth paying before the change is "
        "committed.",
    # ---- comparisons -----------------------------------------------------
    ("Trade-off", "OPPORTUNITY"):
        "Commit to the option that wins on the terms that matter here, or "
        "state which of them the business is willing to trade.",
    ("Trade-off", "INFORMATION"):
        "Choose between these on the basis they differ on — the cost gap "
        "alone does not settle it.",
    ("Not compared", "RISK"):
        "Re-run the scenarios that returned no usable cost before the "
        "ranking is relied on — they are absent from it, not behind in it.",
    # ---- costs -----------------------------------------------------------
    ("Cost", "RISK"):
        "Decide which cost line is going to move, and price the change "
        "against this baseline before the next planning round.",
    ("Demand outlook", "INFORMATION"):
        "Size the network against this outlook rather than against last "
        "year's volume.",
    ("Where the growth is", "INFORMATION"):
        "Put capacity where this growth is landing, not where the current "
        "footprint already sits.",
    ("External signals", "INFORMATION"):
        "Decide whether these signals belong in the planning assumption "
        "before the next capacity round.",
    ("History that changed", "INFORMATION"):
        "Re-baseline the plan on the corrected history before acting on any "
        "figure derived from it.",
}

#: The same theme, said to a reader looking at ONE SITE.
#:
#: The Insights feed merges two briefings — the network's, and one per facility
#: — and each is computed in its own request, so neither can see what the other
#: recommended. On the demo network that produced two cards a few rows apart
#: carrying the same sentence word for word: "No open site reaches the 90%
#: threshold" and "Central Distribution Centre is running at 54% of its stated
#: capacity", both answered with "Headroom is not a decision on its own. Put
#: more volume through the sites with room before adding any."
#:
#: De-duplicating that after the fact would be papering over it. The two
#: findings are genuinely different — one is about the footprint, one is about
#: a site — and the answers should differ because the findings do. A
#: facility-scoped card speaks about the facility.
_ACTION_BY_THEME_FACILITY = {
    ("Capacity", "INFORMATION"):
        "Put more volume through this site before any capacity is added "
        "elsewhere in the network.",
    ("Capacity", "RISK"):
        "Relieve this site before demand grows into it — the lead time on "
        "capacity is longer than the warning.",
    ("Capacity", "OPPORTUNITY"):
        "Move volume onto this site before adding capacity anywhere.",
    ("Utilisation", "INFORMATION"):
        "Judge this site on what it is asked to carry, not on the reading "
        "alone — headroom is only worth having where demand can reach it.",
    ("Utilisation", "RISK"):
        "Relieve this site before demand grows into it.",
    ("Utilisation", "OPPORTUNITY"):
        "Price moving this site's volume onto sites with room.",
    ("Service", "RISK"):
        "Put capacity within reach of the demand this site cannot serve, and "
        "price it against this baseline.",
    ("Footprint", "OPPORTUNITY"):
        "Price this site's cost against the routing it saves before the "
        "footprint is committed.",
    ("Resilience", "RISK"):
        "Decide what covers this site's volume if it is lost, before the "
        "exposure concentrates further.",
    ("Cost", "INFORMATION"):
        "Hold this site's cost as the baseline any change to it is measured "
        "against.",
}

#: Findings that are not decisions and must not be given one.
#:
#: "Summary" is the briefing's own lead — the sentence the whole page is about.
#: Any action under it is either the page's headline recommendation printed
#: twice or a second, weaker one competing with it.
_NO_ACTION_THEMES = frozenset({"Summary"})

#: Last resort, by severity alone — a theme this map does not name yet. Still
#: a decision, still not a place to look.
_ACTION_BY_SEVERITY = {
    "RISK": "Decide whether this is accepted or acted on, and price the "
            "change before the next planning round.",
    "OPPORTUNITY": "Price this change against the current baseline before "
                   "committing either way.",
    "INFORMATION": "No decision is needed on this one.",
}


#: WHAT IS NOT A DECISION.
#:
#: Several sentences above hold a figure in place rather than change anything
#: — "Hold this as the baseline every proposed change is measured against".
#: They are true, and on the Insights page, under a finding that asks for
#: nothing, they are the right thing to say. They are the wrong thing to lead a
#: screen with: the Executive view has three tiles, and a tile spent on "keep
#: measuring against this" is a tile not spent on a change a leader can make.
#:
#: So every serialised insight says whether its recommendation is a decision —
#: `actionable` — and the tiles rank on it. Keyed by the sentence rather than
#: the theme, because one theme carries a real change at one severity and a
#: hold at another.
_HOLD_SENTENCES = frozenset(
    [_ACTION_BY_THEME[key] for key in (
        ("Service", "INFORMATION"), ("Capacity", "INFORMATION"),
        ("Utilisation", "INFORMATION"), ("Footprint", "INFORMATION"),
        ("Resilience", "INFORMATION"), ("Cost", "INFORMATION"),
        ("Carbon", "INFORMATION"))]
    + [_ACTION_BY_THEME_FACILITY[key] for key in (
        ("Cost", "INFORMATION"), ("Capacity", "INFORMATION"),
        ("Utilisation", "INFORMATION"))]
    + [_ACTION_BY_SEVERITY["INFORMATION"]]
)

#: The same test, for a sentence the narrative layer wrote itself.
_HOLD_PATTERN = re.compile(
    r"^\s*(hold|treat|keep|use)\b[^.]*\bbaseline\b|\bno decision is needed\b",
    re.IGNORECASE)


def is_decision(sentence: str, action: Optional[Dict[str, Any]] = None) -> bool:
    """
    Whether a recommendation CHANGES something.

    A recommendation with a scenario behind it always does. One without is a
    decision unless it only holds a figure as the baseline — see
    `_HOLD_SENTENCES`.
    """
    if (action or {}).get("scenario"):
        return True
    text = str(sentence or "").strip()
    if not text or text in _HOLD_SENTENCES:
        return False
    return not _HOLD_PATTERN.search(text)


# ---------------------------------------------------------------------------
# The largest cost line, and how to cut it
# ---------------------------------------------------------------------------
#
# "Target the largest component of this cost first" was the recommendation
# under the cost-structure finding on every network: it named no site, no
# lever and no test, so the one finding about where the money goes told a
# reader to go and work out what to do about it. What replaces it reads the
# same solved rows the capacity ladder does, plus the uploaded cost rates, and
# names the lever that actually moves THAT line:
#
#   transport   renegotiate freight where the spend is concentrated, and
#               re-source the longest lanes
#   facility    consolidate a site carrying full fixed cost for part of its
#               capacity, and renegotiate the leases that stay
#   handling    expand a cheap handler that is full, so volume leaves the dear
#               ones — the solve already uses cheap sites that have room, so
#               their ceiling is what keeps volume at the expensive ones
#   inventory   hold stock in fewer places, so safety stock pools
#
# Each names a scenario the planner can price. None states a saving: nothing
# has been solved at this point, and the scenario is what produces the figure.

#: A capacity test is sized to bring the site back to this share of its limit.
_SUGGESTED_UTILISATION_PCT = 85.0
#: Below this a site is a consolidation candidate when facility or inventory
#: cost is the largest line. Looser than `IDLE_PCT` on purpose: the question is
#: where the largest line can be cut, not whether a site is idle.
_CONSOLIDATE_BELOW_PCT = 60.0
#: The freight-rate change a transport test opens at — an assumption the
#: planner edits, never a negotiated rate.
_FREIGHT_TEST_PCT = -10

_PLANT_ROLES = frozenset({"PLANT", "SUPPLIER"})
_DEMAND_ROLES = frozenset({"MARKET", "CUSTOMER"})


def _role_of(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "").strip().upper()
    return raw.split(".")[-1]


def _cost_sites(pack: Any, network: Any) -> List[Dict[str, Any]]:
    """
    The open supply sites, each with the solve's load and the upload's rates.

    Utilisation and throughput come from the twin state this briefing is
    about; fixed cost and handling rate are the inputs on the snapshot. Nothing
    is derived from either beyond ranking them.
    """
    records = {str(getattr(r, "id", "")): r
               for r in (getattr(network, "facilities", None) or [])}
    out: List[Dict[str, Any]] = []
    for row in _facility_rows(pack):
        fid = str(row.get("facility_id") or "")
        record = records.get(fid)
        role = _role_of(row.get("role")) or _role_of(getattr(record, "role", ""))
        if not fid or role in _DEMAND_ROLES or not row.get("is_open"):
            continue
        out.append({
            "facility_id": fid,
            "name": str(row.get("facility_name") or row.get("name") or fid),
            "role": role,
            "util": _finite(row.get("utilization_pct")),
            "throughput": _finite(row.get("throughput_units")),
            "capacity": _finite(row.get("capacity_units")),
            "fixed": _finite(getattr(record, "fixed_cost_per_year", None)),
            "rate": _finite(getattr(record, "handling_cost_per_unit", None)),
        })
    return out


def _lever(key: str, sentence: str, reason: str, cta: str,
           scenario: Optional[Dict[str, Any]] = None,
           site: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """A recommendation in the shape `StrategicAction.to_dict` produces."""
    return sentence, {
        "key": key,
        "label": sentence,
        "reason": reason,
        "scenario": dict(scenario or {}),
        "target": ({"facility_id": site["facility_id"], "name": site["name"]}
                   if site else {}),
        "priority": 1,
        # No scenario, no button: a form opened from advice would be empty.
        "cta": cta if scenario else "",
    }


def _unclaimed(key: str, fid: str, claimed: Optional[set]) -> bool:
    return (key, fid) not in (claimed or set())


def _consolidation_pick(sites: List[Dict[str, Any]], eligible, claimed):
    """
    The least-used site worth consolidating, or None.

    Never the last open site of its kind — closing that is not a
    consolidation — and never a site another card already recommends
    consolidating.
    """
    peers: Dict[str, int] = {}
    for site in sites:
        peers[site["role"]] = peers.get(site["role"], 0) + 1
    pool = [s for s in sites
            if s["util"] is not None and s["util"] < _CONSOLIDATE_BELOW_PCT
            and peers.get(s["role"], 0) > 1 and eligible(s)
            and _unclaimed("CONSOLIDATE", s["facility_id"], claimed)
            # A site another card EXPANDS is not one to take out.
            and _unclaimed("ADD_CAPACITY", s["facility_id"], claimed)]
    if not pool:
        return None
    return sorted(pool, key=lambda s: (s["util"], -(s["fixed"] or 0),
                                       s["facility_id"]))[0]


def _consolidate(site: Dict[str, Any], sentence: str, reason: str):
    return _lever("CONSOLIDATE", sentence, reason, "Test the consolidation",
                  scenario={"action": "CLOSE_FACILITY",
                            "facility_id": site["facility_id"],
                            "name": f"Consolidate {site['name']}"},
                  site=site)


def _cut_transport(sites, payload, claimed):
    from netgravity.orchestrator.reasoning.strategic_actions import format_pct  # noqa: F401

    by_id = {s["facility_id"]: s for s in sites}
    spend: Dict[str, float] = {}
    for flow in payload.get("flows") or []:
        origin = str((flow or {}).get("origin_id") or "")
        cost = _finite((flow or {}).get("transport_cost"))
        if origin in by_id and cost is not None and cost > 0:
            spend[origin] = spend.get(origin, 0.0) + cost
    reason = ("Transport is the largest line in what this network costs. A "
              "freight-rate change is the quickest lever on it to price; the "
              "scenario opens at an assumed rate cut for you to edit, not a "
              "negotiated one.")
    if not spend:
        sentence = ("Cut transport, the largest cost line: renegotiate freight "
                    "on the busiest lanes, and re-source the longest ones from "
                    "a site nearer the demand they serve.")
        return _lever("REDUCE_TRANSPORT_COST", sentence, reason,
                      "Test a freight-rate cut",
                      scenario={"action": "CHANGE_TRANSPORT_COST",
                                "amount": _FREIGHT_TEST_PCT,
                                "name": "Renegotiated freight"})
    top = by_id[max(spend, key=lambda k: (spend[k], k))]
    sentence = (f"Cut transport, the largest cost line: renegotiate freight on "
                f"the lanes out of {top['name']}, which carry more transport "
                f"spend than any other site's, and re-source the longest lanes "
                f"from a site nearer the demand they serve.")
    return _lever("REDUCE_TRANSPORT_COST", sentence, reason,
                  "Test a freight-rate cut",
                  scenario={"action": "CHANGE_TRANSPORT_COST",
                            "facility_id": top["facility_id"],
                            "amount": _FREIGHT_TEST_PCT,
                            "name": f"Renegotiated freight out of {top['name']}"},
                  site=top)


def _cut_facility(sites, payload, claimed):
    from netgravity.orchestrator.reasoning.strategic_actions import format_pct

    site = _consolidation_pick(sites, lambda s: (s["fixed"] or 0) > 0, claimed)
    if site is not None:
        sentence = (f"Cut facility cost, the largest cost line: consolidate "
                    f"{site['name']}, which carries a full fixed cost at "
                    f"{format_pct(site['util'])} of its capacity, onto sites "
                    f"with room, and renegotiate the leases on the sites that "
                    f"stay.")
        reason = ("Facility cost is the largest line, and it is fixed: it is "
                  "cut by carrying fewer sites, not by moving volume between "
                  "them.")
        return _consolidate(site, sentence, reason)
    sentence = ("Cut facility cost, the largest cost line: renegotiate the "
                "leases and service contracts on the sites with the largest "
                "fixed cost, and give up space the plan does not use.")
    return _lever("REDUCE_FACILITY_COST", sentence,
                  "No open site is lightly enough used to consolidate, so the "
                  "fixed cost is cut by its terms rather than by its sites.", "")


def _cut_handling(sites, payload, claimed):
    from netgravity.orchestrator.reasoning.strategic_actions import (
        LOADED_PCT, format_pct,
    )

    rated = [s for s in sites if s["rate"] is not None and s["rate"] > 0]
    rates = sorted(s["rate"] for s in rated)

    # 1. A CHEAP HANDLER THAT IS FULL. The solve already routes volume to cheap
    #    handlers that have room, so a full one is what keeps volume at the
    #    dear ones. "One of the lowest rates" means the cheapest QUARTER: the
    #    first cut used the lower median, and on the Canada upload it called a
    #    6.90 handler one of the lowest rates in a network whose cheapest sites
    #    handle at 1.18 — true of the rank, false of the sentence.
    if len(rates) >= 2:
        cheap_ceiling = rates[(len(rates) - 1) // 4]
        full = [s for s in rated
                if s["rate"] <= cheap_ceiling and s["rate"] < rates[-1]
                and (s["util"] or 0) >= LOADED_PCT
                and _unclaimed("ADD_CAPACITY", s["facility_id"], claimed)
                and _unclaimed("CONSOLIDATE", s["facility_id"], claimed)]
        if full:
            site = sorted(full, key=lambda s: (s["rate"], -(s["util"] or 0),
                                               s["facility_id"]))[0]
            sentence = (f"Cut handling, the largest cost line: expand "
                        f"{site['name']}, which handles at one of the lowest "
                        f"rates in the network and is at "
                        f"{format_pct(site['util'])} of its capacity, so volume "
                        f"can move off dearer sites; then automate or "
                        f"renegotiate handling where the rates are highest.")
            reason = ("The solve already routes volume to cheap handlers that "
                      "have room. A cheap handler that is full is what keeps "
                      "volume at the dear ones.")
            return _lever("ADD_CAPACITY", sentence, reason,
                          "Test the capacity increase",
                          scenario={"action": "CHANGE_CAPACITY",
                                    "facility_id": site["facility_id"],
                                    "name": f"More capacity at {site['name']}"},
                          site=site)

    spenders = [s for s in rated if s["throughput"] is not None]
    if not spenders:
        sentence = ("Cut handling, the largest cost line: automate or "
                    "renegotiate handling at the busiest sites, and move volume "
                    "toward the sites that handle it more cheaply.")
        return _lever("REDUCE_HANDLING_COST", sentence,
                      "The upload states no handling rate per site to rank.", "")

    dear = max(spenders, key=lambda s: (s["rate"] * s["throughput"],
                                        s["facility_id"]))

    # 2. THE DEAREST SPEND, WHERE IT HAS SOMEWHERE TO GO. Only onto sites of
    #    the same kind — volume does not move from a distribution centre to a
    #    plant — that handle more cheaply and have room for ALL of it between
    #    them. Short of that, closing it is not a handling lever; it is a
    #    shortfall the planner would price as one.
    median = rates[(len(rates) - 1) // 2]
    kin = [s for s in sites if s["role"] == dear["role"]]
    room = sum(max((s["capacity"] or 0.0) - (s["throughput"] or 0.0), 0.0)
               for s in kin
               if s is not dear and s["rate"] is not None
               and s["rate"] < dear["rate"]
               # NOT ROOM ANOTHER CARD IS TAKING AWAY. On the Canada upload the
               # idle-sites card recommended consolidating Montreal while this
               # one counted Montreal's headroom as where Mississauga's volume
               # would go — two tiles, side by side, giving opposite advice.
               and _unclaimed("CONSOLIDATE", s["facility_id"], claimed))
    if (dear["rate"] > median and len(kin) > 1
            and room >= (dear["throughput"] or 0.0) > 0
            and _unclaimed("CONSOLIDATE", dear["facility_id"], claimed)
            and _unclaimed("ADD_CAPACITY", dear["facility_id"], claimed)):
        sentence = (f"Cut handling, the largest cost line: move the volume at "
                    f"{dear['name']}, which carries more handling spend than any "
                    f"other site, onto the cheaper handlers of its kind that "
                    f"have room for it — price consolidating it — and automate "
                    f"or renegotiate handling where rates stay high.")
        return _consolidate(dear, sentence,
                            "Its handling rate is above the network's median, "
                            "and sites of the same kind that handle more "
                            "cheaply have room for all of its volume.")

    # 3. The rate itself is the lever.
    sentence = (f"Cut handling, the largest cost line: automate or renegotiate "
                f"handling at {dear['name']}, which carries more handling spend "
                f"than any other site, and move volume toward the sites that "
                f"handle it more cheaply as they gain room.")
    return _lever("REDUCE_HANDLING_COST", sentence,
                  "No cheap handler is full, and the cheaper sites cannot take "
                  "all of the dearest site's volume, so the rate is the lever.",
                  "", site=dear)

def _cut_inventory(sites, payload, claimed):
    from netgravity.orchestrator.reasoning.strategic_actions import format_pct

    site = _consolidation_pick(sites, lambda s: s["role"] not in _PLANT_ROLES,
                               claimed)
    if site is not None:
        sentence = (f"Cut inventory, the largest cost line: hold stock in fewer "
                    f"places — consolidate {site['name']}, the least-used "
                    f"stocking point at {format_pct(site['util'])} of its "
                    f"capacity, so its safety stock pools with the sites that "
                    f"take its volume — and shorten cover on the slowest-moving "
                    f"lines.")
        return _consolidate(site, sentence,
                            "Safety stock grows with the number of places it is "
                            "held, so fewer stocking points hold less of it.")
    sentence = ("Cut inventory, the largest cost line: shorten safety-stock "
                "cover and reorder quantities at the sites holding the most "
                "stock, and hold slow-moving lines centrally.")
    return _lever("REDUCE_INVENTORY_COST", sentence,
                  "No stocking point is lightly enough used to consolidate.", "")


def _advice(sentence: str, key: str):
    return lambda sites, payload, claimed: _lever(key, sentence, "", "")


_COST_LEVERS = {
    "transport_cost": _cut_transport,
    "facility_cost": _cut_facility,
    "handling_cost": _cut_handling,
    "inventory_cost": _cut_inventory,
    "opening_cost": _advice(
        "Cut opening cost, the largest cost line: phase or defer the openings "
        "this plan commits to, and price expanding an existing site as the "
        "alternative.", "REDUCE_OPENING_COST"),
    "closure_cost": _advice(
        "Cut closure cost, the largest cost line: revisit the closures this "
        "plan pays for, and price keeping the cheapest of them open.",
        "REDUCE_CLOSURE_COST"),
    "carbon_cost": _advice(
        "Cut carbon cost, the largest cost line: shorten the longest lanes, "
        "move them to lower-emitting modes, and price the re-route against "
        "what it saves.", "REDUCE_CARBON_COST"),
}


def _cost_structure_action(pack: Any, network: Any = None,
                           claimed: Optional[set] = None
                           ) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    How to cut the network's largest cost line, and the scenario that prices it.

    The same component the finding names: the largest positive entry in the
    solve's own `cost_components`, and only when there are at least two — the
    emitter's rule, so the recommendation never answers a finding that was not
    made.
    """
    payload = getattr(pack, "payload", None)
    if not isinstance(payload, dict):
        return None
    components = (payload.get("network_state") or {}).get("cost_components") or {}
    priced = {k: v for k, v in ((k, _finite(v)) for k, v in components.items())
              if v is not None and v > 0}
    if len(priced) < 2:
        return None
    largest = max(priced, key=lambda k: (priced[k], k))
    lever = _COST_LEVERS.get(largest)
    if lever is None:
        return None
    return lever(_cost_sites(pack, network), payload, claimed)


def _reopen_action(pack: Any, claimed: Optional[set] = None
                   ) -> Optional[Dict[str, Any]]:
    """
    The candidate site to reopen, for a finding that the plan leaves some unused.

    "Open the unused candidate sites" named none of them and carried no
    scenario. The largest closed candidate is the one worth pricing first:
    it is the most capacity the network already has and does not use.
    """
    from netgravity.orchestrator.reasoning.strategic_actions import CTA_BY_ACTION

    closed = [r for r in _facility_rows(pack)
              if not r.get("is_open") and r.get("facility_id")
              and _role_of(r.get("role")) not in _DEMAND_ROLES
              and _unclaimed("REOPEN_FACILITY", str(r.get("facility_id")), claimed)]
    if not closed:
        return None
    row = sorted(closed, key=lambda r: (-(_finite(r.get("capacity_units")) or 0),
                                        str(r.get("facility_id"))))[0]
    fid = str(row.get("facility_id"))
    name = str(row.get("facility_name") or row.get("name") or fid)
    sentence = (f"Price reopening {name}, the largest of the candidate sites "
                f"this plan leaves unused, against today's cost and service.")
    return {
        "key": "REOPEN_FACILITY",
        "label": sentence,
        "reason": ("Capacity that already exists costs less to bring into use "
                   "than capacity that has to be built."),
        "scenario": {"action": "OPEN_FACILITY", "open_mode": "EXISTING",
                     "facility_id": fid, "name": f"Reopen {name}"},
        "target": {"facility_id": fid, "name": name},
        "priority": 1,
        "cta": CTA_BY_ACTION["REOPEN_FACILITY"],
    }


#: The demand growth a stress test opens at — an input to edit, not a forecast.
_STRESS_TEST_GROWTH_PCT = 10


def _stress_test_action(pack: Any, claimed: Optional[set] = None
                        ) -> Optional[Dict[str, Any]]:
    """
    The test a fully-served plan is owed: how much growth it absorbs.

    Only when the solve left no demand unserved — a shortfall is a RISK
    finding with its own rungs, and growth on top of it answers nothing.
    """
    unserved = _unserved(pack)
    if unserved is None or unserved > 0:
        return None
    if ("STRESS_TEST_DEMAND", "") in (claimed or set()):
        return None
    sentence = ("Stress-test this plan: grow demand across the network and find "
                "the first site that runs out of room, before the next planning "
                "round commits the footprint.")
    return {
        "key": "STRESS_TEST_DEMAND",
        "label": sentence,
        "reason": ("Every unit is served today, so the open question is how much "
                   "growth the footprint absorbs and where it breaks first. The "
                   "scenario opens at an assumed uplift for you to edit."),
        "scenario": {"action": "CHANGE_DEMAND",
                     "amount": _STRESS_TEST_GROWTH_PCT,
                     "name": "Demand growth stress test"},
        "target": {},
        "priority": 1,
        "cta": "Test the demand growth",
    }


def _suggested_capacity_units(record: Any, util_pct: Any) -> Optional[int]:
    """
    How much capacity a test of an expansion should open at, or None.

    Enough to bring the site back to `_SUGGESTED_UTILISATION_PCT` of its
    stated capacity, and never less than a tenth of it, rounded up to two
    significant figures. The form opened at 2,000 units whatever the site —
    a rounding error on a 150,000-unit plant, and a doubling of a small depot.
    It is an INPUT to the test, stated as editable, not a finding.
    """
    capacity = _finite(getattr(record, "capacity_units_per_period", None))
    if capacity is None or capacity <= 0 or capacity >= 1e11:
        return None
    util = _finite(util_pct) or 0.0
    raw = capacity * max(util / _SUGGESTED_UTILISATION_PCT - 1.0, 0.10)
    if raw <= 0:
        return None
    step = 10 ** max(0, int(math.floor(math.log10(raw))) - 1)
    return int(math.ceil(raw / step) * step)


def _with_suggested_amount(action: Dict[str, Any], pack: Any,
                           network: Any) -> Dict[str, Any]:
    """The action, with a sized amount on a capacity test that names a site."""
    scenario = (action or {}).get("scenario") or {}
    if (scenario.get("action") != "CHANGE_CAPACITY"
            or not scenario.get("facility_id") or "amount" in scenario):
        return action
    fid = str(scenario["facility_id"])
    record = next((r for r in (getattr(network, "facilities", None) or [])
                   if str(getattr(r, "id", "")) == fid), None)
    row = next((r for r in _facility_rows(pack)
                if str(r.get("facility_id")) == fid), {})
    units = _suggested_capacity_units(record, row.get("utilization_pct"))
    if units is None:
        return action
    return {**action, "scenario": {**scenario, "amount": units}}


#: WHICH RUNGS ANSWER WHICH FINDING, best first.
#:
#: This is the fix for the thing that made the recommendations useless: every
#: capacity-family card called the ladder for its TOP rung and got the same
#: sentence. Measured on a network with two sites over 90%, one idle site and
#: one closed candidate, seven findings printed one recommendation between them
#: — and it contradicted several of the cards it sat under. A card reporting
#: idle sites recommended bringing more capacity online.
#:
#: A finding is not a network. "3 sites are above the threshold" is answered by
#: relieving them; "3 sites run at 22%" is answered by taking one out; "demand
#: is going unserved" is answered by whatever adds reach soonest. The rungs are
#: the same ladder — this says which of them speak to which question.
#:
#: Order inside each tuple is preference, not alternatives: the first rung the
#: data supports wins, the rest are what it falls to when it does not.
#: `strategic_actions.build_actions` still decides whether a rung applies at
#: all, so nothing here can recommend expanding a site that is not tight.
_ACTIONS_BY_FINDING = {
    # Sites at their ceiling. Expand the constraint first; build only where
    # there is nothing to expand into and nothing closed to bring back.
    ("Capacity", "RISK"):
        ("ADD_CAPACITY", "REOPEN_FACILITY", "OPEN_NEW_FACILITY"),
    ("Utilisation", "RISK"):
        ("ADD_CAPACITY", "REOPEN_FACILITY", "OPEN_NEW_FACILITY"),
    # Sites carrying full fixed cost for a fraction of their capacity. The one
    # rung that answers this, and the one the network-wide call withheld
    # whenever anything else was tight.
    ("Capacity", "OPPORTUNITY"): ("CONSOLIDATE",),
    ("Utilisation", "OPPORTUNITY"): ("CONSOLIDATE",),
    # Demand the footprint cannot reach. Reopening beats expanding here
    # because reach, not throughput, is what is short.
    ("Service", "RISK"):
        ("REOPEN_FACILITY", "OPEN_NEW_FACILITY", "ADD_CAPACITY"),
    # Both directions are live under this theme, which is why the emit site
    # sets `action_hint` — see `KPIInsight`. This is the fallback for a
    # footprint finding that sets none.
    ("Footprint", "OPPORTUNITY"): ("CONSOLIDATE", "REOPEN_FACILITY"),
    ("Footprint", "RISK"): ("CONSOLIDATE", "REOPEN_FACILITY"),
    # Exposure concentrated on one site. An alternative is what reduces it;
    # making the exposed site bigger concentrates it further, so ADD_CAPACITY
    # is deliberately absent.
    ("Resilience", "RISK"): ("REOPEN_FACILITY", "OPEN_NEW_FACILITY"),
}


def _facility_rows(pack: Any) -> List[Dict[str, Any]]:
    """
    The solved per-site rows a recommendation is derived from.

    Read off the evidence pack's own payload rather than re-fetched, because
    the pack is built from exactly the state this briefing describes — a second
    read could return a different solve and recommend a change for a network
    the reader is not looking at.
    """
    payload = getattr(pack, "payload", None)
    if not isinstance(payload, dict):
        return []
    rows = payload.get("facilities")
    return list(rows) if isinstance(rows, list) else []


def _unserved(pack: Any) -> Optional[float]:
    payload = getattr(pack, "payload", None)
    if not isinstance(payload, dict):
        return None
    return _finite((payload.get("network_state") or {}).get("unserved_demand"))


def _strategic_action(pack: Any) -> Optional[Dict[str, Any]]:
    """
    The top rung of the ladder for this NETWORK, as a serialisable action.

    This is the page's own headline recommendation — the single change that
    ranks above the others — and it is the one place the top rung is still the
    right answer. Per-finding recommendations go through
    `_finding_action` instead; see `_ACTIONS_BY_FINDING` for why.

    None when there are no solved rows to reason over — in which case the
    caller falls back to the theme sentence, which claims nothing about sites
    it cannot see.
    """
    from netgravity.orchestrator.reasoning.strategic_actions import build_actions

    rows = _facility_rows(pack)
    if not rows:
        return None
    actions = build_actions(rows, unserved_demand=_unserved(pack), limit=1)
    if not actions or actions[0].key == "NO_ACTION":
        return None
    return actions[0].to_dict()


def _subject_ids(insight: Any, rows: List[Dict[str, Any]]) -> List[str]:
    """
    The facilities THIS finding is about, by id.

    Several findings name their site in the headline — "Nagpur DC is at 97.4%
    in its busiest period", "Kochi DC is where losing a single site would cost
    the most" — and the recommendation under them named whichever site was
    busiest network-wide instead, which on a network with three tight sites is
    the wrong one twice.

    Matched against the AUTHORITATIVE name on the solved row, not parsed out of
    the prose: the names in the sentence were interpolated from these same rows
    by the layer that wrote it, so an exact match is a lookup rather than a
    guess. Bounded by word edges, so "Central DC" does not claim "Central DC
    North", and the result is empty for a network-wide finding — which is the
    honest answer for one.
    """
    text = " ".join((
        str(getattr(insight, "headline", "") or ""),
        str(getattr(insight, "narrative", "") or ""),
    ))
    if not text.strip():
        return []
    # LONGEST NAME FIRST, and each match consumes the characters it used.
    #
    # Word edges alone are not enough: "Central DC" sits at both edges of its
    # own mention inside "Central DC North", so a bounded match still let the
    # shorter name claim the longer one's site — and on a network that names
    # its sites that way the recommendation went to the wrong facility. A
    # matched name is removed from the text before the shorter ones are
    # offered it, which also leaves a genuine second mention still matchable.
    candidates = []
    for row in rows:
        fid = str(row.get("facility_id") or "").strip()
        for token in (str(row.get("facility_name") or "").strip(), fid):
            # Two characters is not an identifier, it is a coincidence.
            if len(token) >= 3:
                candidates.append((fid, token))
    candidates.sort(key=lambda pair: -len(pair[1]))

    residue = text
    hit = set()
    for fid, token in candidates:
        if not fid or fid in hit:
            continue
        pattern = (r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])")
        if re.search(pattern, residue):
            hit.add(fid)
            residue = re.sub(pattern, " ", residue)

    # Row order, so the same finding produces the same list every time.
    out: List[str] = []
    for row in rows:
        fid = str(row.get("facility_id") or "").strip()
        if fid in hit and fid not in out:
            out.append(fid)
    return out


def action_identity(action: Dict[str, Any]) -> Tuple[str, str]:
    """
    What makes two recommendations THE SAME recommendation.

    Not the rung on its own. "Expand capacity at Western DC" and "Expand
    capacity at Nagpur DC" are both ADD_CAPACITY and they are two different
    decisions about two different sites — suppressing the second because the
    first spent the rung would hide a real finding. What must not appear twice
    is the same change at the same place.
    """
    target = action.get("target") or {}
    where = str(target.get("facility_id") or target.get("region") or "")
    return (str(action.get("key") or ""), where)


def _finding_action(insight: Any, theme: str, severity: str, pack: Any,
                    claimed: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """
    The intervention that answers THIS finding, not this network.

    Three things make it specific where `_strategic_action` is general:

      * the rungs are chosen by what the finding says (`_ACTIONS_BY_FINDING`),
        with the emit site's own `action_hint` ahead of them when it set one;
      * the sites the finding NAMES rank first inside each rung, so the
        recommendation is about the site the reader has just read about;
      * a rung another card in this briefing has already used is excluded, so
        one briefing cannot print one sentence six times.

    None when no rung answers it — a legitimate outcome, and the caller falls
    back to a decision sentence chosen by theme rather than inventing a change
    the rows do not support.
    """
    from netgravity.orchestrator.reasoning.strategic_actions import (
        ACTION_KEYS, build_actions,
    )

    rows = _facility_rows(pack)
    if not rows:
        return None

    prefer: List[str] = []
    hint = str(getattr(insight, "action_hint", "") or "").strip()
    # The emit site wins, because it is the only thing that can separate two
    # findings that share a theme and a severity and point opposite ways.
    if hint in ACTION_KEYS:
        prefer.append(hint)
    for key in _ACTIONS_BY_FINDING.get((theme, severity), ()):
        if key not in prefer:
            prefer.append(key)
    if not prefer:
        return None

    # Every rung that answers this finding, in preference order — not just the
    # first. The pick below needs somewhere to fall to when the best answer is
    # one another card has already given about the same site.
    actions = build_actions(
        rows,
        unserved_demand=_unserved(pack),
        limit=len(prefer),
        focus_ids=_subject_ids(insight, rows),
        prefer=prefer,
    )
    spent = claimed if claimed is not None else set()
    for action in actions:
        body = action.to_dict()
        if action_identity(body) not in spent:
            return body
    return None


def _recommended_action(insight: Any, theme: str, severity: str,
                        pack: Any = None,
                        claimed: Optional[set] = None,
                        scope: str = "NETWORK",
                        network: Any = None,
                        ) -> Tuple[str, Dict[str, Any]]:
    """
    The one decision to take about this finding, and the test that proves it.

    Returns `(sentence, action)`. The sentence is what a card prints; `action`
    is the structured intervention behind it — its key, the site or region it
    is about, and the pre-filled scenario a reader presses to price it. `{}`
    when the recommendation is advisory and has no scenario to open.

    Order of preference:

      1. THE NARRATIVE LAYER'S OWN LINE. It saw the evidence, so it wins
         whenever it wrote one.
      2. THE LADDER, for a capacity-family finding on a network with solved
         rows. This is the one that can name a site.
      3. THE THEME SENTENCE. A decision, not a destination.

    `claimed` is the set of rungs the cards above this one have already used.
    A briefing that recommends the same change five times has recommended it
    once and wasted four cards — and the rungs it falls through to are the
    other real answers to the same finding, not weaker phrasings of the first.

    A finding a reader has to translate into a decision on their own is half a
    finding — and on a screen read by people who do not run the model
    themselves, half a finding is none.
    """
    written = str(getattr(insight, "recommended_action", "") or "").strip()
    if written:
        return written, {}

    # The briefing's own lead card. It restates the whole finding set rather
    # than making one, and a "Recommended action" tile under it either repeats
    # the recommendation the page already carries at the top or invents a
    # second one. Empty, and the card omits the tile.
    if theme in _NO_ACTION_THEMES:
        return "", {}

    if theme in _CAPACITY_THEMES and severity != "INFORMATION":
        action = _finding_action(insight, theme, severity, pack, claimed)
        if action:
            # The LABEL is the sentence. It is an imperative naming the
            # intervention — "Expand capacity at Pune DC" — and the evidence
            # for it is the finding the reader has just read, so repeating the
            # reason underneath would say the same thing twice.
            return action["label"], action

    # THE LARGEST COST LINE, AND HOW TO CUT IT — see `_COST_LEVERS`. The theme
    # sentence below it stays as the answer when there are no solved rows.
    if theme == "Cost structure" and pack is not None:
        lever = _cost_structure_action(pack, network, claimed)
        if lever:
            return lever

    # Unused candidate sites, with the one to reopen named. The ladder's own
    # REOPEN rung needs a tight site or unserved demand, so on a healthy
    # network this finding used to fall to a sentence naming no site.
    if (theme, severity) == ("Footprint", "OPPORTUNITY") and pack is not None:
        reopen = _reopen_action(pack, claimed)
        if reopen:
            return reopen["label"], reopen

    # Every unit served. "Hold this run as the service baseline" asked a leader
    # to do nothing, and on a network with few other decisions it still took
    # an Executive view tile. What a fully-served plan is owed is the test of
    # how far it goes: grow demand and find the first site that runs out.
    if (theme, severity) == ("Service", "INFORMATION") and pack is not None:
        stress = _stress_test_action(pack, claimed)
        if stress:
            return stress["label"], stress

    table = _ACTION_BY_THEME_FACILITY if scope == "FACILITY" else {}
    sentence = (table.get((theme, severity))
                or table.get((theme, "INFORMATION"))
                or _ACTION_BY_THEME.get((theme, severity))
                or _ACTION_BY_THEME.get((theme, "INFORMATION"))
                or _ACTION_BY_SEVERITY.get(severity)
                or _ACTION_BY_SEVERITY["INFORMATION"])
    return sentence, {}


def _serialise_insight(insight: Any, index: int, *, scope: str,
                       entity_id: Optional[str],
                       pack: Any = None,
                       claimed: Optional[set] = None,
                       network: Any = None) -> Dict[str, Any]:
    """
    One KPI insight, in the shape a feed can render.

    `id` is derived from the scope, the entity and the theme rather than being
    random, so the same finding keeps the same identity across refreshes — a
    feed that lets a user dismiss an item needs an id that survives a re-fetch,
    and a UUID per request would resurrect everything they had dismissed.
    """
    from netgravity.orchestrator.reasoning.card import plain_voice

    theme = str(getattr(insight, "theme", "") or "GENERAL")
    slug = theme.upper().replace(" ", "_")
    entity = (entity_id or "NETWORK").replace(" ", "_")
    severity = getattr(insight, "severity", None)
    severity_name = (severity.value if hasattr(severity, "value")
                     else str(severity or "INFORMATION"))
    metric_refs = list(getattr(insight, "metric_refs", []) or [])
    comparison_refs = list(getattr(insight, "comparison_refs", []) or [])
    driver_refs = list(getattr(insight, "driver_refs", []) or [])
    _action_pair = _recommended_action(insight, theme, severity_name, pack,
                                       claimed, scope, network)
    if _action_pair[1] and pack is not None:
        _action_pair = (_action_pair[0],
                        _with_suggested_amount(_action_pair[1], pack, network))
    # Spend the rung, so the next card in this briefing reaches for a different
    # one. `claimed` is per-briefing and is passed in by the loop below; a
    # caller serialising one insight on its own passes None and nothing is
    # spent.
    if claimed is not None and _action_pair[1].get("key"):
        claimed.add(action_identity(_action_pair[1]))
    return {
        # The theme alone is not unique within a scope: `_service_insights` can
        # emit two `theme="Service"` findings (unserved demand, and SLA), and
        # both used to serialise to INS_NETWORK_NETWORK_SERVICE. The deep dive
        # looks a record up BY id, so the second insight's card opened the
        # first insight's page. The headline discriminates them, and a short
        # digest of it keeps the id stable across refreshes — which is what a
        # dismissable feed needs, and what a UUID per request would destroy.
        "id": f"INS_{scope}_{entity}_{slug}_{_headline_digest(insight)}",
        "theme": theme,
        # PLAIN VOICE, not the agent's own.
        #
        # The Reasoning Agent writes in the first person by contract — "I see
        # 452,610 units of 1,435,985 units of demand left unserved" — because
        # that is the voice its validator enforces and its grounding checks.
        # A reader of the Overview is not having a conversation with the
        # engine; they are reading a report about their network, and the extra
        # actor in every sentence is what made the tiles read as machine
        # output. `plain_voice` is the rule the explanation card already owns
        # (netgravity/orchestrator/reasoning/card.py) — applied here, at the
        # presentation boundary, so there is one definition of it and the
        # briefing itself is untouched.
        "headline": plain_voice(getattr(insight, "headline", "") or ""),
        "narrative": plain_voice(getattr(insight, "narrative", "") or ""),
        # What to DO about it. The narrative layer's own line when it wrote
        # one; a theme-appropriate, figure-free default when it did not. A
        # finding a reader has to translate into a decision on their own is
        # half a finding.
        "recommended_action": _action_pair[0],
        # The intervention BEHIND the sentence: which rung of the ladder
        # it is, the site or region it is about, and the pre-filled
        # scenario that prices it. `{}` when the recommendation is
        # advisory and has no scenario to open — a button that opens an
        # empty form is worse than no button.
        "action": _action_pair[1],
        # Whether that recommendation CHANGES something. False for the
        # sentences that hold a figure as the baseline; the Executive view
        # ranks its three tiles on this. See `is_decision`.
        "actionable": is_decision(_action_pair[0], _action_pair[1]),
        # Stated by the engine, not inferred from the wording by the client.
        # The Home feed used to decide a card's colour, icon and priority by
        # searching its prose for "high impact" / "opportunity" / "positive",
        # so an insight phrased differently was rendered neutral whatever it
        # had found.
        "severity": severity_name,
        "metric_refs": metric_refs,
        "comparison_refs": comparison_refs,
        "driver_refs": driver_refs,
        # The figures this finding rests on, with their authoritative values.
        # A deep dive needs to show its basis, and the alternative — a screen
        # inventing plausible before/after numbers to fill the space — is what
        # this replaces.
        #
        # Still one flat list, because that is what the table renders, but each
        # row now says which role it played.
        "evidence": (
            _resolve_evidence(metric_refs, pack, role="metric")
            + _resolve_evidence(comparison_refs, pack, role="comparison")
            + _resolve_evidence(driver_refs, pack, role="driver")
        ) if pack is not None else [],
        # The facilities or lanes this finding was computed OVER, not merely the
        # one scalar it cites. "3 sites are above the threshold" cited only
        # `max_utilization_pct`, so a screen could name the worst site and
        # nothing else — the three rows behind the sentence were built, used to
        # write the prose, and dropped. A chart needs the rows.
        "entities": _resolve_entities(insight, pack),
        "rank": index + 1,
        # THE SCOPE THIS FINDING WAS COMPUTED IN, carried so a client can ask
        # for the same briefing back.
        #
        # Without it the deep dive's document download assumed NETWORK, and a
        # facility-scoped finding — which the deep dive opens exactly as
        # readily — answered 404 for an id the reader was looking at. A record
        # that cannot say where it came from makes every consumer guess.
        "scope": scope,
        "entity_id": entity_id,
    }


#: How each step of the working is introduced, keyed by the role the engine
#: tagged its figures with. The screen prints the same three headings; this is
#: the same distinction written out for a reader who is not looking at it.
_STEP_ROLES = (
    ("metric", "What was measured",
     "The figures this finding reads, exactly as the solve computed them. "
     "Nothing here is re-derived: each value is the one the named engine "
     "produced for this run."),
    ("comparison", "What it was compared against",
     "The figures the measurement above was read against — a configured "
     "policy threshold, a baseline, or the counterpart quantity that makes "
     "the measurement mean something."),
    ("driver", "What is behind it",
     "The quantities moving the measurement. These are reported because the "
     "engine cited them in reaching the conclusion, not because a correlation "
     "was tested."),
)

_ROLE_LABEL = {"metric": "Measured", "comparison": "Compared against",
               "driver": "Driver"}


def _method_note(record: Dict[str, Any]) -> str:
    """
    How to read the working, in one paragraph.

    The deep dive shows the steps and the figures and says nothing about how
    they were arrived at, which is what makes a correct derivation still feel
    like a black box: a reader can see 97.2% and see the conclusion and has
    no account of the move between them.
    """
    theme = record.get("theme") or "this"
    return (
        f"This {theme.lower()} finding is produced in two stages. First the "
        "deterministic layer solves the network and computes every KPI from "
        "the solved plan — the optimiser's own flows, the facilities it "
        "opened and the demand it served — and publishes them as a digital "
        "twin state. No language model takes part in that stage, and no "
        "figure below is estimated. Second, the reasoning layer reads that "
        "state, selects the figures relevant to this theme, compares them "
        "against the configured policy thresholds, and states the conclusion "
        "in the section above. Every number it quotes is then checked back "
        "against the computed results before the finding is published; the "
        "outcome of that check is recorded under Provenance."
    )


def _derivation_for(record: Dict[str, Any], analysis: Dict[str, Any]) -> Any:
    """
    One serialised insight, as a `DerivationReport`.

    Reads the SAME record the browser renders, so the document and the screen
    cannot disagree: if the deep dive shows 97.20%, so does the table in the
    file, because both print the identical `display_value` string.

    `analysis` is the whole serialised briefing — the identical JSON object
    `GET /api/insights` returned — rather than the live briefing, result and
    twin state this used to take. That is not a tidying: the list response is
    CACHED per network version and this route recomputed, so the two could
    disagree about what findings exist. They did. A reader who opened a
    finding and pressed Download got 404 "not a finding on this network's
    current analysis" about the finding on their screen, because the fresh
    reasoning pass had produced a slightly different headline and the id is a
    digest of the headline. Reading both from one cached payload makes the
    disagreement unrepresentable rather than unlikely.
    """
    from datetime import datetime, timezone

    from netgravity.reporting import DerivationReport, DerivationStep, Figure

    evidence = [e for e in (record.get("evidence") or [])
                if e.get("display_value")
                and e["display_value"] != "Not available"]

    steps = []
    for role, title, detail in _STEP_ROLES:
        rows = [e for e in evidence if (e.get("role") or "metric") == role]
        if not rows:
            continue
        steps.append(DerivationStep(
            title=title,
            detail=detail,
            figures=tuple(
                Figure(label=e.get("label") or e.get("ref") or "",
                       value=e.get("display_value") or "",
                       role=_ROLE_LABEL.get(e.get("role") or "metric", "Measured"),
                       source=e.get("source") or "")
                for e in rows),
        ))

    # The entities the finding was computed OVER, where it has them. This is
    # the part a screen can only show as a chart and a reader most often wants
    # as a list they can sort — which site, at what figure.
    entities = record.get("entities") or []
    if entities:
        # The metric's own readable name, not its storage key: the document
        # said "Ranked by utilization pct" where the table beside it already
        # said "Average utilisation".
        from netgravity.orchestrator.reasoning.evidence import metric_label
        metric = metric_label(entities[0].get("metric") or "").lower()
        steps.append(DerivationStep(
            title="Every record this was computed over",
            detail=(f"Ranked by {metric or 'the metric this theme is about'}, "
                    "as the solve reported it for each one. The conclusion is "
                    "a statement about this population, not about the "
                    "single figure above."),
            figures=tuple(
                Figure(label=str(e.get("label") or e.get("entity_id") or ""),
                       value=_format_entity_value(e),
                       role=("Not used by this plan" if e.get("is_open") is False
                             else "In this plan"),
                       source=str(e.get("role") or e.get("kind") or ""))
                for e in entities),
        ))

    limitations = []
    limitation = str(analysis.get("limitation") or "").strip()
    if limitation:
        limitations.append(limitation)
    completeness = str(analysis.get("evidence_completeness") or "")
    if completeness and completeness != "COMPLETE":
        limitations.append(
            f"Evidence for this run is {completeness}: some analyses did not "
            "produce a value, so those quantities are unknown rather than "
            "zero.")

    grounding_block = analysis.get("grounding") or {}
    grounding = str(grounding_block.get("status") or "UNKNOWN")
    warnings = [str(w) for w in (grounding_block.get("warnings") or [])]
    provenance = (
        f"Source: NetGravity reasoning over the solved network state "
        f"{analysis.get('state_id') or 'unknown'}. "
        f"Numeric grounding: {grounding}."
    )
    if grounding not in ("GROUNDED", "NO_CLAIMS"):
        provenance += (" Not every figure quoted in the prose was verified "
                       "against the deterministic results.")
    if warnings:
        provenance += " Validation warnings: " + "; ".join(warnings) + "."

    return DerivationReport(
        kind="Insight",
        subject=f"{record.get('theme') or 'Network'} · "
                + ("whole network" if not record.get("entity_id")
                   else str(record.get("entity_id"))),
        conclusion=record.get("headline") or "",
        summary=record.get("narrative") or "",
        method=_method_note(record),
        steps=steps,
        recommended_action=record.get("recommended_action") or "",
        assumptions=[str(d) for d in (analysis.get("key_drivers") or [])],
        limitations=limitations,
        provenance=provenance,
        generated_at="Generated "
                     + datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC"),
    )


def _format_entity_value(entity: Dict[str, Any]) -> str:
    """
    One entity's figure, formatted the way the chart's axis formats it.

    The entity rows carry raw numbers (they exist to be plotted), so unlike
    every other figure in this document there is no `display_value` to copy.
    The rule is the axis's own: a `_pct` metric reads as a percentage to two
    places, anything else with thousands separators.
    """
    value = entity.get("value")
    if not isinstance(value, (int, float)):
        return "—"
    metric = str(entity.get("metric") or "")
    if metric.endswith("_pct"):
        # ONE decimal on a percentage. The second is below the precision of
        # every input this figure is derived from, and a column of "92.37%"
        # against "88.41%" invites a comparison at a resolution the model does
        # not have.
        return f"{value:,.1f}%"
    # Whole units above the rate threshold, decimals below it — the same rule
    # `format_money` applies, so a figure keeps its shape across the product.
    return f"{value:,.2f}" if abs(value) < 100 else f"{value:,.0f}"


def create_insights_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/insights"):
    bp = Blueprint("insights", __name__, url_prefix=url_prefix)

    def _gateway() -> Any:
        """
        The gateway the reasoning agent already holds.

        Not a second `LLMGateway()`. The budget is cumulative and SHARED
        across every holder of the token — 100 requests a day for the whole
        product — and a second client keeps its own counters, so two objects
        each believing they have the full allowance is how the limit gets
        exceeded rather than respected. It also carries the per-execution
        state `begin_execution` sets.
        """
        if orchestrator is None:
            return None
        agent = (orchestrator.services or {}).get("reasoning_agent")
        return getattr(agent, "gateway", None)

    def _resolve_state(project_id: str, user_id: str) -> Any:
        """
        The Digital Twin state the project's current figures came from, built if
        this process does not have one.

        Twin states live in a process-local store while the KPI analysis is
        durable, so the two do not survive a restart together. That asymmetry
        made this endpoint return 409 "not solved yet" on a network that had
        been solved, whose KPIs were on screen, and whose analysis had just been
        restored from the database — the report was about the process, not about
        the network.

        So when no state is present, the baseline execution is run to publish
        one. It costs one solve, once per process per snapshot, and the briefing
        it produces is then cached in the durable analysis store (see
        `_briefing_analysis`) so the next process pays nothing.

        Prefers an OPTIMIZED state: that is what the KPI endpoints report, and
        an insight describing a different state from the numbers beside it is
        worse than no insight.
        """
        snapshot_id = project_registry.snapshot_for(project_id, user_id=user_id)
        refs = orchestrator.twin.list_states(snapshot_id)
        if not refs:
            logger.info("insights.publishing_twin_state snapshot=%s", snapshot_id)
            orchestrator.run_sync(OrchestratorRequest(
                input="Baseline network state for insight generation",
                explicit_intent=Intent.NETWORK_STATE_QUERY,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
                request_id=orchestrator_request_id("insights-twin-publish"),
            ))
            refs = orchestrator.twin.list_states(snapshot_id)
        if not refs:
            raise ConflictError(
                "This network could not be solved, so there is nothing to "
                "explain. The KPI endpoints report why.",
                context={"project_id": project_id, "snapshot_id": snapshot_id},
            )
        # Prefer an OPTIMIZED state that HAS CONTENT.
        #
        # A run that produces no network state still publishes a twin state, and
        # publishes it as OPTIMIZED with zero facilities and zero flows — that
        # is deliberate (see `build_unavailable_state`): a viewer must see an
        # explicitly empty state rather than the previous, stale one.
        #
        # But this preference read only the label. Once any run had failed for a
        # snapshot, the empty OPTIMIZED state outranked the populated BASELINE
        # one for every subsequent request, and every facility-scoped briefing
        # answered 404 "Facility 'F005' is not present in state ..." for a
        # network whose twelve facilities were on screen beside it. The whole
        # per-facility insight surface was unreachable, on a solved network,
        # because of a state that describes a run that produced nothing.
        #
        # So: rank by whether the state can answer the question at all, and only
        # then by type. A populated baseline beats an empty optimized every time.
        def rank(ref):
            populated = 1 if getattr(ref, "n_facilities", 0) else 0
            optimized = 1 if str(getattr(ref, "state_type", "")).upper().endswith(
                "OPTIMIZED") else 0
            return (populated, optimized)

        chosen = sorted(refs, key=rank)[-1]
        return snapshot_id, orchestrator.twin.materialize(chosen.state_id)

    def _briefing_for(state: Any, scope: ReasoningScope,
                      entity_id: Optional[str], question: str,
                      allow_llm: bool) -> Any:
        """Returns `(ReasoningResult, ReasoningEvidencePack)`."""
        from netgravity.orchestrator.reasoning.evidence import (
            build_evidence_pack, twin_reasoning_payload, with_policy_thresholds,
        )

        # Wrapped, not called bare. `with_policy_thresholds` exists precisely so
        # a narrative may cite "the 90% threshold" without the numeric validator
        # adjudicating 90 against whatever unrelated percentage it finds nearest
        # — and this endpoint was calling the payload builder directly, so the
        # thresholds reached neither the pack nor the response. A chart drawing
        # a threshold line would otherwise have to hardcode 90/40, i.e. restate
        # a policy constant it does not own.
        payload = with_policy_thresholds(twin_reasoning_payload(
            state, scope=scope, entity_id=entity_id, comparison=None))
        unavailable = {
            item.field: {"status": item.status.value, "reason": item.reason}
            for item in state.unavailable
        }
        # Built here as well as inside the agent. It is a pure, cheap indexing
        # function over the same payload, so this is reuse rather than a second
        # implementation — and it is what lets the response carry the value
        # behind every ref the narrative cites.
        pack = build_evidence_pack(
            payload, scope=scope, entity_id=entity_id, user_question=question,
            unavailable=unavailable, provenance={"state_id": state.state_id},
        )
        agent = orchestrator.services["reasoning_agent"]
        result = agent.reason(
            payload,
            unavailable_evidence=unavailable,
            provenance={
                "state_id": state.state_id,
                "snapshot_id": state.snapshot_id,
                "scenario_id": state.scenario_id or "",
            },
            allow_llm=allow_llm,
            scope=scope,
            entity_id=entity_id,
            user_question=question,
        )
        return result, pack

    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    @require_auth
    @rate_limit("insights.read", limit=120, window_seconds=60)
    def get_insights():
        """
        Insights and a recommendation for a project's network.

        Query:
            ``project_id``  required
            ``scope``       NETWORK (default) | FACILITY | LANE
            ``entity_id``   required for FACILITY and LANE
            ``question``    an optional question to answer alongside
            ``use_llm``     ``1`` to allow a model to phrase the briefing.
                            Off by default: the deterministic template is
                            grounded by construction and costs nothing, and a
                            dashboard load should not spend a model call per
                            facility.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The reasoning engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")

        scope_arg = str(request.args.get("scope") or "NETWORK").strip().upper()
        if scope_arg not in _ALLOWED_SCOPES:
            raise ValidationError(
                f"scope must be one of {', '.join(sorted(_ALLOWED_SCOPES))}.")
        entity_id = str(request.args.get("entity_id") or "").strip() or None
        if scope_arg in {"FACILITY", "LANE"} and not entity_id:
            raise ValidationError(f"scope={scope_arg} requires an entity_id.")

        question = str(request.args.get("question") or "")[:1000]
        allow_llm = request.args.get("use_llm") == "1"
        scope = ReasoningScope(scope_arg)

        payload = _briefing_analysis(project_id, scope_arg, scope, entity_id,
                                     question, allow_llm)
        return jsonify(payload), 200

    def _briefing_analysis(project_id: str, scope_arg: str, scope: ReasoningScope,
                           entity_id: Optional[str], question: str,
                           allow_llm: bool) -> Dict[str, Any]:
        """
        The briefing for one scope, cached per network version.

        Cached in the same durable store the KPI analysis uses, under its own
        variant. A briefing is derived data about one version of one network,
        exactly as the KPIs are: it does not change until the network does, so
        recomputing it per request would pay for a reasoning pass — and, on a
        fresh process, a solve — on every dashboard load and every facility
        click.
        """
        user_id = g.current_user.user_id
        snapshot_id = project_registry.snapshot_for(project_id, user_id=user_id)
        snapshot = orchestrator.snapshots.get(snapshot_id)

        # A question or a model call makes the answer specific to this request,
        # so neither is cached: an ad-hoc question must not be served to the next
        # caller who asks a different one.
        cacheable = not question and not allow_llm
        # The payload SHAPE is part of the cache key, not just the network.
        #
        # A cached briefing is invalidated when the network changes
        # (`data_version`), which is right for the figures but says nothing
        # about the fields. Adding `value`, `role`, `entities`, `thresholds`
        # and `series` to this response changed the shape while leaving every
        # `data_version` untouched, so every project that had ever loaded its
        # insights kept being served the older, thinner payload — the browser
        # showed `thresholds: {}` and `value: null` while the endpoint itself
        # demonstrably returned both. A stale cache that outlives a deploy is
        # indistinguishable, from the client's side, from a broken serialiser.
        #
        # The same argument applies to the WORDING, and that case is worse: the
        # body carries generated prose stating what the figures mean, and a
        # correction to a materially-false sentence changes no `data_version`
        # at all. Bump `_PAYLOAD_VERSION` whenever a field or a narrative
        # changes.
        variant = f"insights:v{_PAYLOAD_VERSION}:{scope_arg}:{entity_id or ''}"

        def compute() -> Dict[str, Any]:
            from netgravity.orchestrator.reasoning.card import plain_voice

            _, state = _resolve_state(project_id, user_id)
            try:
                result, pack = _briefing_for(state, scope, entity_id, question,
                                            allow_llm)
            except ValueError as exc:
                # `twin_reasoning_payload` raises this when the entity is not in
                # the state — a client asking about a facility this network does
                # not have, which is a bad request rather than a server fault.
                raise NotFoundError(str(exc)) from exc

            briefing = result.briefing
            # The uploaded inputs behind the solve — fixed cost and handling
            # rate per site — which the cost levers rank on. The same snapshot
            # the state was solved from.
            network = getattr(snapshot, "network", None)
            # The rungs this briefing has already spent, filled in rank order
            # as the cards are serialised. Shared across the whole list on
            # purpose: it is what stops six cards printing one sentence.
            claimed: set = set()
            return {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "state_id": state.state_id,
                "scenario_id": state.scenario_id,
                "scope": scope_arg,
                "entity_id": entity_id,
                # A comprehension, and ORDER MATTERS inside it: each card
                # spends the rung it used, so the ones after it reach for a
                # different one. Python evaluates this left to right, and the
                # insights arrive already ranked, so the most important finding
                # gets first pick.
                "insights": [
                    _serialise_insight(item, i, scope=scope_arg,
                                       entity_id=entity_id, pack=pack,
                                       claimed=claimed, network=network)
                    for i, item in enumerate(briefing.kpi_insights)
                ],
                # The policy constants a threshold line may be drawn at, so the
                # chart and the sentence quote the same number and neither
                # hardcodes it. Sourced from `UTILIZATION_THRESHOLDS`.
                "thresholds": _thresholds_from(pack),
                # Whole-network series a chart can plot without inventing one.
                # `cost_components` was reaching the browser as a single ref —
                # the largest component only — so a breakdown chart had one
                # slice and no total.
                "series": _network_series(pack),
                # The recommendation is ONE string chosen by the evidence, not a
                # list of options. A list would imply the engine had ranked
                # alternatives it has not evaluated.
                #
                # In plain voice, like the insights above it. It reaches the
                # Insights page as the one recommendation that ranks the
                # findings rather than following from any single one, and a
                # page of reports with one paragraph of "I recommend" in the
                # middle of it reads as two different documents.
                "recommendation": plain_voice(briefing.recommendation or ""),
                # THE CHANGE BEHIND THAT SENTENCE, when the solved rows justify
                # one. The page's headline button read "Open scenario planner"
                # — hardcoded in the browser, identical on every network, and a
                # destination rather than a decision. With this it names the
                # intervention and opens the scenario already filled in.
                #
                # The SENTENCE is still the engine's; this only says what the
                # button under it does. `{}` on a network that needs no change,
                # in which case the button falls back to the planner.
                "action": _with_suggested_amount(
                    _strategic_action(pack) or {}, pack, network),
                "opening": plain_voice(briefing.opening or ""),
                "context": plain_voice(briefing.context or ""),
                "key_drivers": [plain_voice(d) for d in briefing.key_drivers],
                "limitation": plain_voice(briefing.limitation or ""),
                "suggested_questions": list(briefing.suggested_questions),
                "missing_information": [m.model_dump(mode="json")
                                        for m in briefing.missing_information],
                "evidence_completeness": briefing.evidence_completeness.value,
                # Whether the narrative's numbers were checked against the
                # deterministic results, and what failed if any did. A consumer
                # that renders prose must be able to see this.
                "grounding": {
                    "status": result.grounding_status,
                    "warnings": list(result.validation_warnings),
                    "source": result.source,
                },
                "provenance": {
                    "authoritative_source":
                        "netgravity.orchestrator.agents.reasoning_agent",
                    "evidence_from": "digital_twin_state",
                    "llm_used": result.source != "template",
                },
            }

        if not cacheable:
            body = compute()
            body["computed_at"] = time.time()
            return body
        return analysis_service.get(
            snapshot_id, snapshot.data_version, compute, variant=variant)

    # ------------------------------------------------------------------
    @bp.route("/<insight_id>/document", methods=["GET"])
    @require_auth
    @rate_limit("insights.document", limit=30, window_seconds=60)
    def insight_document(insight_id: str):
        """
        One finding, as a document somebody can take into a meeting.

        WHY THIS EXISTS. The deep-dive page shows the conclusion, the figures
        it cites and the role each played. That is the right amount for a
        screen and the wrong amount for the conversation that follows it: the
        first question asked of a capacity finding in a steering committee is
        which figures it rests on and what the model could not see, and the
        answer has to survive being forwarded to somebody who will never open
        this application.

        NOTHING IS COMPUTED HERE. Every figure is the `display_value` the
        evidence pack already carries, written out verbatim — the same rule
        the screens follow. The document restates the run; it does not
        re-derive it.

        The writer itself is `netgravity.reporting`, which knows nothing about
        insights: the demand forecast is asked the same question ("which
        series, which method, what history") and will build the same shape.
        """
        from netgravity.reporting import build_derivation_docx, narrate

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")

        scope_arg = str(request.args.get("scope") or "NETWORK").strip().upper()
        if scope_arg not in _ALLOWED_SCOPES:
            raise ValidationError(
                f"scope must be one of {', '.join(sorted(_ALLOWED_SCOPES))}.")
        entity_id = str(request.args.get("entity_id") or "").strip() or None
        if scope_arg in {"FACILITY", "LANE"} and not entity_id:
            raise ValidationError(f"scope={scope_arg} requires an entity_id.")
        scope = ReasoningScope(scope_arg)

        # THE SAME CALL THE LIST ROUTE MAKES, cache and all.
        #
        # This route used to resolve the twin state and run its own reasoning
        # pass. `GET /api/insights` does not: it is cached per network version,
        # because a briefing is derived data about one version of one network.
        # So the list a reader is looking at and the list this route searched
        # were two different computations, and they diverged the moment a
        # hydration published a fresher state — the reasoning pass wrote a
        # slightly different headline, the id is a digest of the headline, and
        # the download 404'd on the finding filling the screen.
        #
        # Going through `_briefing_analysis` means the record this document is
        # built from IS the record the browser rendered, byte for byte.
        analysis = _briefing_analysis(project_id, scope_arg, scope, entity_id,
                                      "", False)

        record = next((r for r in analysis.get("insights") or []
                       if r.get("id") == insight_id), None)
        if record is None:
            raise NotFoundError(
                f"'{insight_id}' is not a finding on this network's current "
                f"analysis. Reload the page to pick up the current findings.")

        report = _derivation_for(record, analysis)

        # THE PART A MODEL IS ALLOWED TO WRITE.
        #
        # The tables above are the engine's and are complete; what they do not
        # do is join up. A reader who was not in the room gets a correct
        # derivation and still has to work out why three figures add to one
        # conclusion, which is exactly the "black box" complaint a table of
        # numbers does not answer.
        #
        # `narrate` verifies every figure it writes against the figures in the
        # report and drops any sentence quoting one that is not there, so the
        # worst case is a shorter passage rather than a fabricated number under
        # a letterhead. It raises nothing: a gateway that is unconfigured, over
        # budget or unreachable produces a document without this section, never
        # a failed download.
        narration = narrate(report, _gateway(), purpose="insight_document")
        report.narrative = list(narration.paragraphs)
        # The note is printed only when there is something for it to explain:
        # a passage that was written, or one that was written and withheld. An
        # unconfigured gateway is not a fact about this analysis, and a line
        # about a missing service in a document about a network reads as a
        # caveat on the network.
        report.narrative_note = (
            narration.note
            if (narration.paragraphs or narration.source == "rejected") else "")

        payload = build_derivation_docx(report)

        response = make_response(payload)
        response.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document")
        # `attachment` because this is a file to keep, not a page to read. The
        # filename is what the reader will look for in a downloads folder a
        # week later, so it names the finding rather than the id.
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{report.filename()}"')
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.errorhandler(ApplicationError)
    def _insight_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
