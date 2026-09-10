"""
Orchestrator — What a leader should DO about a finding.

WHAT THIS IS FOR
================
Three screens used to answer "what should I do about this?" and only one of
them answered it. The scenario planner derived a real intervention from the
solved per-site load. The Insights feed looked the finding's THEME up in a
static table and returned "Open the KPI page to see which sites are over the
threshold" — which is not a recommendation, it is a navigation instruction,
and it is the same sentence for every network ever uploaded. The Forecast card
did the same thing in different words.

A senior reader does not need to be told which screen to open. They need to be
told what to change, where, and on what evidence — and then handed the means to
test it before committing. That is what this module produces, from the solved
facility rows, for every caller that has them.

WHERE THE SCENARIO PLANNER FITS
-------------------------------
`app/backend/api/scenarios.py` runs the same ladder over a RICHER input and
keeps its own copy of it. That is deliberate, and it is the one duplication
here worth having: it holds a full capacity account — every solved site, and
which regions have run out of room — where this module sees only the rows it
is handed. Asked to decide "every site in this region is full" from a subset,
`_region_without_room` would conclude it from whatever it was given, which is
how a recommendation to build somewhere gets made on three rows.

What the two DO share is the vocabulary: `ACTION_KEYS` is imported there
rather than restated, and both emit the same `scenario` prefill and the same
words under the button. A key added here and not there produces a card the
scenario screen cannot map to a form, which is the failure the shared tuple
prevents.

THE LADDER
----------
Cheapest defensible intervention first. The order is not a preference; each
rung is only reachable because the one above it does not apply:

    1. REOPEN_FACILITY     capacity that already exists and is already paid
                           for. Nothing built. Always beats building.
    2. ADD_CAPACITY        a site is at or near its ceiling and there is
                           nothing closed to bring back. Expanding a site that
                           already has land, labour and a licence beats a
                           greenfield.
    3. OPEN_NEW_FACILITY   every site in the region is full and none is
                           closed. This is the ONLY condition under which
                           building is the cheapest answer rather than the
                           first one somebody thought of.
    4. CONSOLIDATE         the opposite finding: sites carrying full fixed
                           cost for a fraction of their capacity.
    5. SCOPE_DEMAND_GROWTH growth stated for the whole network when the
                           upload knows which region it is happening in.
    6. REQUEST_DATA        the analysis ran without an input it needed.
    7. NO_ACTION           nothing meets the bar. A finding, not a blank.

EVERY RUNG ENDS IN A TEST, NEVER IN A COMMITMENT
------------------------------------------------
`scenario` on each action is the pre-filled scenario the recommendation would
be PROVED by. It is what turns "establish a distribution centre in the west"
from an opinion into something a planner can put a number against before it
reaches a capital committee — and it is why the button under a recommendation
reads "Test this as a scenario" rather than "Apply". Nothing here commits
anything; the governance layer still decides what may be executed.

WHAT THIS MODULE MAY NOT DO
---------------------------
It states no saving, no payback and no magnitude of benefit. None of those has
been solved at the point a recommendation is made — the scenario it hands over
is what produces them. A recommendation that quotes a saving nobody computed is
the exact failure the numeric grounding layer exists to catch, and it would be
caught here as UNSUPPORTED.

Figures that DO appear are read straight off the authoritative rows: a
utilisation the MILP reported, a capacity the upload stated. Nothing is
divided, summed or inferred.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Every action this product can recommend. The key is the contract: a screen
#: maps it to a form, a document prints its label, so a new intervention is
#: added here and nowhere else.
ACTION_KEYS = (
    "REOPEN_FACILITY",
    "ADD_CAPACITY",
    "OPEN_NEW_FACILITY",
    "CONSOLIDATE",
    "SCOPE_DEMAND_GROWTH",
    "REQUEST_DATA",
    "NO_ACTION",
)

#: At or above this, a site has no usable room left.
SATURATED_PCT = 99.0
#: Running hot. The band the facility panel and the mapper already call "high",
#: so one site is not "tight" on one screen and "healthy" on the next.
LOADED_PCT = 90.0
#: Below this, a site is carrying its full fixed cost for a fraction of its
#: capacity. Mirrors ``UTILIZATION_THRESHOLDS["under_threshold"]``.
IDLE_PCT = 30.0


#: What the button under each recommendation says, by rung.
#:
#: Every one of these used to read "Test this as a scenario" — one phrase for
#: four different decisions, under labels that were the only thing telling them
#: apart. A control that says the same thing whatever it is attached to stops
#: being read.
#:
#: The DESTINATION is not in these strings. A consumer that navigates away
#: appends it ("…in the scenario planner"); the scenario planner's own card
#: does not, because the reader is already there.
CTA_BY_ACTION = {
    "REOPEN_FACILITY":     "Test the reopening",
    "ADD_CAPACITY":        "Test the capacity increase",
    "OPEN_NEW_FACILITY":   "Test the new site",
    "CONSOLIDATE":         "Test the consolidation",
    "SCOPE_DEMAND_GROWTH": "Re-run it scoped to the region",
    # Neither of these is a change to solve, so neither gets a button. A form
    # opened from "no network change is indicated" would contradict the
    # sentence above it.
    "REQUEST_DATA":        "",
    "NO_ACTION":           "",
}


@dataclass(frozen=True)
class StrategicAction:
    """
    One decision, its evidence, and the scenario that would test it.

    `label` is an IMPERATIVE naming the intervention — "Establish a new
    distribution centre in the West" — never a place to look. `reason` is one
    sentence of grounded evidence. `scenario` is the pre-filled test.
    """
    key: str
    label: str
    reason: str
    #: The pre-filled scenario this recommendation is proved by. Empty for the
    #: rungs that are not an intervention (REQUEST_DATA, NO_ACTION).
    scenario: Dict[str, Any] = field(default_factory=dict)
    #: Which site or region the decision is about, for a screen that wants to
    #: highlight it. Empty when the finding is network-wide.
    target: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "reason": self.reason,
            "scenario": dict(self.scenario),
            "target": dict(self.target),
            "priority": self.priority,
            # The phrase under the button, NAMING THIS CHANGE. A screen that
            # navigates away appends where it goes; see `CTA_BY_ACTION`.
            "cta": CTA_BY_ACTION.get(self.key, ""),
        }


# ---------------------------------------------------------------------------
# Reading the rows
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """A finite number, or None. Never 0 for an absent reading."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def format_pct(value: Any) -> str:
    """
    A utilisation, at the precision a leader reads — with one exception.

    Whole percentages everywhere, because "92.37%" is noise on a card. But a
    site at 99.6% must never print as "100%": full and nearly-full are
    different findings, and the second decimal is the only thing separating
    "this is the constraint" from "this has stopped". So the one case that
    would round ACROSS 100 keeps a decimal place.
    """
    number = _num(value)
    if number is None:
        return "an unrecorded share"
    if number < 100 and round(number) >= 100:
        return f"{number:,.1f}%"
    return f"{number:,.0f}%"


#: PUBLIC because the narrative layer needs the same rule.
#:
#: The Reasoning Agent wrote its own percentages with `f"{util:.2f}%"`, so a
#: card read "Central Distribution Centre is running at 54.00% of its stated
#: capacity" — two digits of precision that are always zero, on a screen whose
#: whole point is that a leader can read it at a glance. One rule, one place.
_pct = format_pct


def _units(value: Any) -> str:
    number = _num(value)
    return f"{number:,.0f} units" if number is not None else "an unrecorded quantity"


def _name(row: Dict[str, Any]) -> str:
    return str(row.get("name") or row.get("facility_name")
               or row.get("facility_id") or row.get("id") or "this site")


def _role_noun(row: Dict[str, Any]) -> str:
    """
    What to call the thing a reader would build next to this one.

    A plant at its ceiling is relieved by production capacity; a distribution
    centre by another distribution centre. Recommending "a new facility" for
    both is how a capacity recommendation ends up meaning nothing to the person
    who has to sponsor it.
    """
    role = str(row.get("role") or "").strip().upper()
    if role.startswith("PLANT") or role in {"FACTORY", "MANUFACTURING"}:
        return "plant"
    if role.startswith("DC") or "DISTRIB" in role or role in {"WAREHOUSE", "WH"}:
        return "distribution centre"
    return "facility"


def normalise_rows(rows: Iterable[Any]) -> List[Dict[str, Any]]:
    """
    Facility rows from any of the three shapes this product carries them in.

    `FacilityState` (the twin), the KPI layer's warehouse health row, and the
    scenario capacity account all describe the same site with different field
    names. Normalising here is what lets one ladder serve all three callers —
    the alternative was three ladders that drift, which is what this module was
    written to end.
    """
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if hasattr(row, "model_dump"):
            row = row.model_dump(mode="json")
        if not isinstance(row, dict):
            continue

        util = row.get("utilization_pct")
        if util is None:
            util = row.get("util_pct")
        if util is None:
            util = row.get("peak_utilization_pct")

        capacity = row.get("capacity_units")
        if capacity is None:
            capacity = row.get("capacity")
        if capacity is None:
            capacity = row.get("rated_capacity_per_period")

        throughput = row.get("throughput_units")
        if throughput is None:
            throughput = row.get("throughput")
        if throughput is None:
            throughput = row.get("peak_throughput_units")

        is_open = row.get("is_open")
        region = row.get("region")
        out.append({
            "facility_id": row.get("facility_id") or row.get("id") or "",
            "name": (row.get("facility_name") or row.get("name")
                     or row.get("facility_id") or row.get("id") or ""),
            "role": row.get("role") or "",
            "region": (str(region).strip() or None) if region else None,
            "is_open": True if is_open is None else bool(is_open),
            "util_pct": _num(util),
            "capacity": _num(capacity),
            "throughput": _num(throughput),
            "fixed_cost": _num(row.get("total_facility_cost")
                               if row.get("total_facility_cost") is not None
                               else row.get("fixed_cost_per_year")),
        })
    return out


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def build_actions(
    rows: Sequence[Any],
    *,
    unserved_demand: Optional[float] = None,
    demand_change_is_unscoped: bool = False,
    missing_inputs: int = 0,
    limit: int = 3,
    focus_ids: Sequence[str] = (),
    prefer: Optional[Sequence[str]] = None,
    exclude: Sequence[str] = (),
) -> List[StrategicAction]:
    """
    The ranked interventions this network's own solved rows justify.

    `limit` caps the list because a recommendation that names five things has
    recommended nothing. Three is what fits above the fold on the cards these
    feed, and the ladder is ordered so the three kept are the three that matter.

    ONE NETWORK, SEVERAL QUESTIONS
    ------------------------------
    The three arguments below exist because this ladder has two different jobs
    and used to do only the first.

    Asked "what should be done about this NETWORK", the answer is the top rung
    and the cross-rung suppression is right: do not propose consolidating a
    site while another one is on fire.

    Asked "what should be done about THIS FINDING" — which is what an insights
    feed asks, once per card — the same call returned the same top rung every
    time. Seven findings on one loaded network, seven identical sentences, six
    of which did not answer the finding printed above them: a card reporting
    idle sites recommended bringing more capacity online.

      `focus_ids`  the sites the finding is ABOUT. Within each rung these sort
                   first, so a card headed "Nagpur DC is at 97%" recommends
                   expanding Nagpur DC rather than whichever site happens to be
                   busiest network-wide.
      `prefer`     the rungs that ANSWER this finding, best first. The result
                   is filtered to these and ordered by them. Passing it also
                   lifts the cross-rung suppression, because a network can have
                   a saturated site AND an idle one and both are real findings
                   with opposite answers.
      `exclude`    rungs an earlier finding has already claimed, so two cards
                   in one briefing cannot print the same recommendation.

    With `prefer` set, an empty list is a legitimate answer — this finding has
    no intervention in the data — and the caller says something else. NO_ACTION
    is only ever returned for the network-wide question, where "nothing meets
    the bar" is itself the finding.
    """
    if prefer is not None:
        for key in prefer:
            assert key in ACTION_KEYS, key
    sites = normalise_rows(rows)
    live = [s for s in sites if s["is_open"]]
    closed = [s for s in sites
              if not s["is_open"] and (s["capacity"] or 0) > 0]

    saturated = sorted(
        [s for s in live if (s["util_pct"] or 0) >= SATURATED_PCT],
        key=lambda s: -(s["util_pct"] or 0))
    loaded = sorted(
        [s for s in live if LOADED_PCT <= (s["util_pct"] or 0) < SATURATED_PCT],
        key=lambda s: -(s["util_pct"] or 0))
    idle = sorted(
        [s for s in live
         if s["util_pct"] is not None and s["util_pct"] < IDLE_PCT],
        key=lambda s: (s["util_pct"] or 0))

    # The sites this finding is about come first inside every rung, so the
    # recommendation names the site the reader has just read about. Ordering
    # rather than filtering: a finding about one site can still be answered by
    # a rung that needs the rest of the network (there is no reopening without
    # something closed to reopen).
    focus = {str(f) for f in (focus_ids or []) if f}

    def _focused_first(sites: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not focus:
            return sites
        return ([s for s in sites if str(s["facility_id"]) in focus]
                + [s for s in sites if str(s["facility_id"]) not in focus])

    tight = _focused_first(saturated + loaded)
    idle = _focused_first(idle)
    closed = _focused_first(closed)
    unserved = _num(unserved_demand)
    actions: List[StrategicAction] = []

    # ---- 1. Capacity already built, and switched off ----------------------
    if closed and (tight or (unserved is not None and unserved > 0)):
        site = closed[0]
        actions.append(StrategicAction(
            key="REOPEN_FACILITY",
            label=f"Bring {_name(site)} back into the network",
            reason=(
                f"{_units(site['capacity'])} of capacity sits closed at "
                f"{_name(site)}"
                + (f" in {site['region']}" if site["region"] else "")
                + ", while the network is short of room elsewhere. Capacity "
                  "that already exists costs less to use than capacity that "
                  "has to be built."),
            scenario={"action": "OPEN_FACILITY", "open_mode": "EXISTING",
                      "facility_id": site["facility_id"],
                      "name": f"Reopen {_name(site)}"},
            target={"facility_id": site["facility_id"], "name": _name(site),
                    "region": site["region"]},
        ))

    # ---- 2. Expand where the network is actually constrained --------------
    if tight:
        site = tight[0]
        room = ("has no usable room left" if site in saturated
                else "is close enough to its ceiling that the next increase "
                     "in demand has nowhere to go")
        actions.append(StrategicAction(
            key="ADD_CAPACITY",
            label=f"Expand capacity at {_name(site)}",
            reason=(
                f"{_name(site)} runs at {_pct(site['util_pct'])} of its rated "
                f"capacity and {room}. It is the constraint on this network: "
                f"nothing more moves through it until it has headroom."),
            scenario={"action": "CHANGE_CAPACITY",
                      "facility_id": site["facility_id"],
                      "name": f"More capacity at {_name(site)}"},
            target={"facility_id": site["facility_id"], "name": _name(site),
                    "region": site["region"]},
        ))
    elif unserved is not None and unserved > 0:
        actions.append(StrategicAction(
            key="ADD_CAPACITY",
            label="Add capacity where the plan runs short",
            reason=(
                f"This plan leaves {_units(unserved)} of demand unserved while "
                f"no single site reaches its ceiling, so the shortfall is "
                f"spread across the network rather than sitting at one place."),
            scenario={"action": "CHANGE_CAPACITY",
                      "name": "Relieve the network shortfall"},
        ))

    # ---- 3. Build, but only where nothing else will do --------------------
    #
    # The test is deliberately strict. "A site is busy" is not a case for
    # capital: it is a case for expanding that site. Building is justified only
    # where a whole region is full AND has nothing closed to bring back — and
    # on an upload that does not state regions, the honest fallback is the
    # saturated site itself, named, rather than a region invented for the
    # sentence.
    build_for = _region_without_room(live, closed)
    if build_for is not None:
        region, example = build_for
        noun = _role_noun(example)
        actions.append(StrategicAction(
            key="OPEN_NEW_FACILITY",
            label=f"Establish a new {noun} in {region}",
            reason=(
                f"Every site serving {region} is past the {LOADED_PCT:,.0f}% "
                f"mark in this plan and none is closed, so there is nothing "
                f"left there to expand into or bring back. This is the "
                f"condition under which building is the cheapest answer "
                f"rather than the first one."),
            scenario={"action": "OPEN_FACILITY", "open_mode": "NEW",
                      "region": region,
                      "name": f"New {noun} in {region}"},
            target={"region": region},
        ))
    elif tight and not closed:
        # THE SECOND OPTION ON THE SAME FINDING, NOT A COMPETING ONE.
        #
        # A site at 90% has two answers and a leader is owed both: expand it,
        # or build alongside it. Expanding ranks first because it is cheaper —
        # but it is not always available, and a recommendation that offers only
        # the cheap option to a site that cannot physically take another bay
        # has offered nothing. Reachable at the LOADED band rather than only at
        # saturation: by the time a site is at 99% the decision is late, and
        # the lead time on a new building is measured in years.
        site = tight[0]
        noun = _role_noun(site)
        where = f" near {site['region']}" if site["region"] else ""
        actions.append(StrategicAction(
            key="OPEN_NEW_FACILITY",
            label=f"Establish a new {noun}{where}",
            reason=(
                f"{_name(site)} is at {_pct(site['util_pct'])} of capacity and "
                f"there is nothing closed to bring back. If expanding it is "
                f"constrained by site, labour or licence, a second {noun} is "
                f"the remaining way to add room — and it is the option with "
                f"the longest lead time, so it is decided first."),
            scenario={"action": "OPEN_FACILITY", "open_mode": "NEW",
                      "region": site["region"] or "",
                      "name": f"New {noun}{where}"},
            target={"facility_id": site["facility_id"], "name": _name(site),
                    "region": site["region"]},
        ))

    # ---- 4. The opposite finding ------------------------------------------
    # `not tight` is the NETWORK-WIDE judgement: do not propose taking capacity
    # out while somewhere else has none left. Asked about the idle-sites
    # finding specifically, that suppression is the bug — it is the only rung
    # that answers it, and withholding it left the card recommending the exact
    # opposite of what it had just reported.
    if idle and (prefer is not None or not tight):
        site = idle[0]
        actions.append(StrategicAction(
            key="CONSOLIDATE",
            label=f"Test consolidating {_name(site)}",
            reason=(
                f"{_name(site)} carries its full fixed cost while running at "
                f"{_pct(site['util_pct'])} of capacity. Moving its volume onto "
                f"sites with room is worth pricing before any capacity is "
                f"added anywhere."),
            scenario={"action": "CLOSE_FACILITY",
                      "facility_id": site["facility_id"],
                      "name": f"Consolidate {_name(site)}"},
            target={"facility_id": site["facility_id"], "name": _name(site),
                    "region": site["region"]},
        ))

    # ---- 5. Growth stated wider than it is happening ----------------------
    if demand_change_is_unscoped:
        actions.append(StrategicAction(
            key="SCOPE_DEMAND_GROWTH",
            label="Re-run this growth for the region it is happening in",
            reason=(
                "This scenario grew every demand row in the network. Loading "
                "every site with growth that is happening in one region "
                "overstates the case for expanding the ones that are not."),
            scenario={"action": "CHANGE_DEMAND",
                      "name": "Growth, scoped to its region"},
        ))

    # ---- 6. The answer rests on less than it should -----------------------
    if missing_inputs > 0:
        actions.append(StrategicAction(
            key="REQUEST_DATA",
            label="Obtain the inputs this analysis did not have",
            reason=(
                f"{missing_inputs} input this analysis needed was not in the "
                f"upload, so part of the answer rests on less evidence than "
                f"the rest of it."),
        ))

    if exclude:
        spent = {str(k) for k in exclude}
        actions = [a for a in actions if a.key not in spent]

    if prefer is not None:
        rank = {key: i for i, key in enumerate(prefer)}
        actions = sorted((a for a in actions if a.key in rank),
                         key=lambda a: rank[a.key])
    elif not actions:
        # Only for the network-wide question. Under `prefer`, or after an
        # exclusion, an empty list means "this finding has no intervention
        # left to offer" — which is not the same claim as "this network needs
        # no change", and printing the second for the first would be false.
        if not exclude:
            actions.append(_nothing_to_do(live, unserved))

    kept = actions[:limit]
    for index, action in enumerate(kept, start=1):
        object.__setattr__(action, "priority", index)
    for action in kept:
        assert action.key in ACTION_KEYS, action.key
    return kept


def _region_without_room(
    live: Sequence[Dict[str, Any]],
    closed: Sequence[Dict[str, Any]],
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    A region whose every open site is full and which has nothing closed.

    Returns `(region, an example site)` or None. None is the answer for an
    upload that states no regions at all — and saying "we cannot tell you which
    region needs a site" is the only honest response to that, rather than
    naming one the data does not support.
    """
    by_region: Dict[str, List[Dict[str, Any]]] = {}
    for site in live:
        if site["region"]:
            by_region.setdefault(site["region"], []).append(site)
    closed_regions = {s["region"] for s in closed if s["region"]}

    for region, sites in sorted(by_region.items()):
        if region in closed_regions:
            continue
        rated = [s for s in sites if s["util_pct"] is not None]
        if not rated:
            continue
        # LOADED, not saturated. A region whose every site is past 90% has no
        # room to absorb growth, and waiting for all of them to reach 99%
        # before naming it is waiting until the decision is too late to act on.
        if all(s["util_pct"] >= LOADED_PCT for s in rated):
            return region, rated[0]
    return None


def _nothing_to_do(live: Sequence[Dict[str, Any]],
                   unserved: Optional[float]) -> StrategicAction:
    """
    Why nothing is recommended — which is a finding, not an empty list.

    "No recommended actions" is a blank space where an answer should be. A
    reader who has been told the network is healthy has been told something;
    a reader shown nothing concludes the screen is broken.
    """
    if not live:
        reason = ("No solved facility rows travelled with this result, so what "
                  "each site is carrying is not known for it. Re-run the "
                  "analysis to see what the plan asks of each one.")
    elif unserved is not None and unserved <= 0:
        reason = ("This plan serves all of its demand and no site is at its "
                  "ceiling. Nothing in the network is constraining it, so "
                  "there is no capacity change to justify.")
    else:
        reason = ("No site is at its ceiling, no capacity is sitting closed, "
                  "and no region has run out of room. Nothing here meets the "
                  "bar for a network change.")
    return StrategicAction(key="NO_ACTION",
                           label="No network change is indicated",
                           reason=reason)
