"""
NetGravity — Warehouse health, growth sizing and deep-dive rankings
===================================================================
Peak versus average, ranked, and sized against a stated growth assumption.

WHAT THIS IS FOR
----------------
The authoritative layer already answers "how utilised is this site?". It could
not answer the four questions a network planner actually opens a warehouse
review to ask:

  * Is this site tight ON AVERAGE or tight IN ITS WORST PERIOD? A DC at 43%
    for the year and 91% in March is at 43% by the average and out of room in
    March, and only one of those two numbers decides whether the footprint
    works.
  * WHERE do I look first? Twenty facilities and one utilisation column is a
    table, not a finding.
  * What would this footprint need if demand grew at the rate I am planning
    against?
  * What did optimising actually change, site by site?

WHAT THIS IS NOT
----------------
Not a second KPI engine, and it owns no arithmetic that another module owns.

  * Utilisation — average AND peak — is read verbatim from `FacilitySummary`.
    The MILP computes both; this module re-derives neither, so a ranking here
    can never disagree with the utilisation on the facility screen.
  * Cost per facility is the engine's own attribution (`total_facility_cost`),
    carried across the contract bridge unchanged.
  * Inventory is the sum of the I_{i,k,t} decisions the solve produced.

What it DOES compute is the reading over those figures: max and mean over the
periods, counts against a configured threshold, ordering, subtraction between
two solved states, and one explicit multiplication by a growth rate the caller
states. Each is named at its site below.

THE GROWTH RATE IS AN INPUT, NEVER A DEFAULT
--------------------------------------------
`future_requirements` needs a rate of demand growth, and there is no honest
default for one. A hardcoded +20% would put a sized capacity gap, in units, on
a screen — an authoritative-looking number resting on an assumption nobody
made. So the caller states the rate and where it came from, and with no
assumption the section reports INSUFFICIENT_EVIDENCE and says what it needs,
rather than returning rows built on a guess.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field

from netgravity.config.defaults import UTILIZATION_THRESHOLDS
from netgravity.orchestrator.schemas.kpi import KPIStatus

logger = logging.getLogger(__name__)

#: Roles that hold stock — what this report means by "warehouse".
#:
#: DC and WAREHOUSE are THE SAME THING and are used interchangeably, here and
#: throughout the product. They are separate `NodeRole` values only because an
#: upload may use either word, and both must land in the same place rather than
#: leaving a network of DCs reporting that it has no warehouses.
#:
#: A plant is not one. It has a capacity and a utilisation, so it belongs in
#: the capacity-constraint ranking beside them — but no amount of warehousing
#: relieves a plant that cannot make enough.
STORAGE_ROLES = {"WAREHOUSE", "DC", "DEPOT", "DARKSTORE", "CROSS_DOCK"}

#: Demand nodes.
#:
#: A solved state does not normally contain any: the MILP emits a facility
#: decision only where it decided something, and a market has no capacity to
#: utilise and no cost to attribute. This guard is belt-and-braces for a state
#: assembled some other way — a market carries an unbounded nominal capacity
#: (1e12), so one that slipped through would sit at 0.0% at the bottom of every
#: ranking. It is NOT what keeps markets out of the tables.
MARKET_ROLES = {"MARKET", "CUSTOMER"}

#: How long a ranking is. One constant, so the tables and the narrative cannot
#: disagree about what "top" means.
TOP_N = 10

#: Utilisation at or above which a period counts as a bottleneck, and at or
#: below which a site is carrying capacity nobody uses. Read from
#: `config/defaults.py` — the same numbers the KPI threshold catalogue and the
#: reasoning agent's capacity insight draw their line at, so a table cannot
#: call a site tight that the briefing beside it calls healthy.
OVER_UTILISED_PCT = UTILIZATION_THRESHOLDS["over_threshold"] * 100.0
UNDER_UTILISED_PCT = UTILIZATION_THRESHOLDS["under_threshold"] * 100.0

#: The share of a warehouse's capacity a planner sizes new capacity against.
#: Not a measurement and not a NetGravity policy — a stated planning input,
#: carried on the assumption so it appears beside every figure it produced.
DEFAULT_TARGET_UTILISATION_PCT = 85.0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SectionStatus(BaseModel):
    """
    Why a block of this report is empty.

    Every section that can be unavailable carries one. An empty list with no
    status reads as "there is nothing to report"; three of these sections can
    be empty because an INPUT was missing, which is a different statement and
    the one a reader has to be able to tell apart.
    """
    status: KPIStatus = KPIStatus.VALID
    reason: str = ""

    model_config = ConfigDict(extra="forbid")


class WarehouseHealthKPI(BaseModel):
    """One facility, read on a per-period basis."""

    facility_id: str
    facility_name: str
    role: str
    region: Optional[str] = None
    country: Optional[str] = None
    is_open: bool = False
    baseline_status: Optional[str] = None

    #: Capacity for ONE period, which is the basis the upload states it on and
    #: the basis every throughput figure below is on. `FacilitySummary` carries
    #: the horizon total; pairing that with one period's volume is the
    #: twelvefold error the contract's own docstring warns about.
    rated_capacity_per_period: float = 0.0

    avg_throughput_units: float = 0.0
    peak_throughput_units: float = 0.0
    #: Which period the peak fell in, so "the worst month" has a name.
    peak_period: Optional[str] = None

    #: Read verbatim from the solve. `avg_utilization_pct` IS the contract's
    #: `utilization_pct` (horizon throughput over horizon capacity, which is
    #: the mean of the per-period ratios); `peak_utilization_pct` is the
    #: engine's own worst period. Neither is recomputed here.
    avg_utilization_pct: float = 0.0
    peak_utilization_pct: float = 0.0

    #: None — not 0.0 — where the solve produced no inventory decisions.
    avg_inventory_units: Optional[float] = None
    peak_inventory_units: Optional[float] = None
    inventory_status: SectionStatus = Field(default_factory=SectionStatus)

    periods_observed: int = 1
    bottleneck_periods_count: int = 0
    is_bottleneck: bool = False

    #: Capacity left in the worst period. Negative where the plan runs the site
    #: past its stated capacity, which a solved plan should not do and which is
    #: therefore worth showing rather than clamping at zero.
    headroom_units_peak: float = 0.0

    #: CRITICAL / TIGHT / HEALTHY / UNDERUSED / NOT_OPERATING — the priority
    #: order a screen reads top-down, from the thresholds above and nothing
    #: else.
    health_band: str = "HEALTHY"

    #: The engine's cost attribution for this site.
    fixed_cost: float = 0.0
    handling_cost: float = 0.0
    holding_cost: float = 0.0
    opening_cost: float = 0.0
    total_facility_cost: float = 0.0

    model_config = ConfigDict(extra="forbid")


class GrowthAssumption(BaseModel):
    """
    The stated rate of demand growth a sizing is built on, and its provenance.

    `source` and `description` travel with it so every capacity gap this report
    produces can be shown beside the assumption that produced it. A sized gap
    whose assumption is not on screen is an authoritative-looking number with
    an invisible input.
    """
    #: Percent, e.g. 20.0 for +20%. Applies where no regional rate matches.
    network_pct: Optional[float] = None
    #: region name -> percent. Takes precedence over `network_pct`.
    by_region: Dict[str, float] = Field(default_factory=dict)
    target_utilization_pct: float = DEFAULT_TARGET_UTILISATION_PCT
    #: USER_STATED | FORECAST_ENGINE — never inferred by this module.
    source: str = "USER_STATED"
    description: str = ""

    model_config = ConfigDict(extra="forbid")

    def is_stated(self) -> bool:
        return self.network_pct is not None or bool(self.by_region)


class FutureCapacityRequirement(BaseModel):
    """What one site would need at the stated growth rate. Arithmetic, not a plan."""

    facility_id: str
    facility_name: str
    region: Optional[str] = None
    current_capacity_per_period: float = 0.0
    #: The growth rate actually applied here, and how it was chosen — a
    #: facility can inherit a regional rate from the markets it serves, from
    #: its own region, or from the network-wide rate.
    growth_pct: float = 0.0
    growth_basis: str = ""
    projected_peak_throughput: float = 0.0
    target_utilization_pct: float = DEFAULT_TARGET_UTILISATION_PCT
    required_capacity: float = 0.0
    capacity_gap_units: float = 0.0
    expansion_needed: bool = False
    #: MAINTAIN | EXPAND_CAPACITY | OPEN_CANDIDATE_DC
    recommended_action: str = "MAINTAIN"
    recommended_action_reason: str = ""

    model_config = ConfigDict(extra="forbid")


class FacilityDeltaRow(BaseModel):
    """One site, before and after — two solved states, subtracted."""

    facility_id: str
    facility_name: str
    role: str = ""
    baseline_status: str = "CLOSED"
    optimized_status: str = "CLOSED"
    baseline_throughput: float = 0.0
    optimized_throughput: float = 0.0
    throughput_delta: float = 0.0
    baseline_peak_utilization_pct: float = 0.0
    optimized_peak_utilization_pct: float = 0.0
    utilization_delta_pts: float = 0.0
    baseline_cost: float = 0.0
    optimized_cost: float = 0.0
    #: baseline_cost - optimized_cost. FACILITY cost only — see
    #: `WarehouseDeepDiveReport.savings_basis`.
    facility_cost_savings: float = 0.0
    #: What changed, in words, for a reader scanning the table.
    change: str = ""

    model_config = ConfigDict(extra="forbid")


class CorridorDeltaRow(BaseModel):
    """One lane, before and after — two solved plans, subtracted."""

    origin_id: str
    destination_id: str
    baseline_units: float = 0.0
    optimized_units: float = 0.0
    units_delta: float = 0.0
    baseline_transport_cost: float = 0.0
    optimized_transport_cost: float = 0.0
    #: baseline - optimized. Positive means the optimised plan spends less
    #: moving volume down this lane.
    transport_cost_savings: float = 0.0
    change: str = ""

    model_config = ConfigDict(extra="forbid")


class FacilityCostDriver(BaseModel):
    """One site's spend, split the way the engine charged it."""

    facility_id: str
    facility_name: str
    region: Optional[str] = None
    total_facility_cost: float = 0.0
    fixed_cost: float = 0.0
    handling_cost: float = 0.0
    holding_cost: float = 0.0
    opening_cost: float = 0.0
    #: This site's share of what every facility in the plan cost. A ratio of
    #: two figures from the same solve.
    share_of_facility_spend: Optional[float] = None

    model_config = ConfigDict(extra="forbid")


class WarehouseDeepDiveReport(BaseModel):
    """Everything the warehouse review reads, from one solved state (or two)."""

    #: Which solve this describes, so a report cannot be shown against another
    #: network's numbers.
    network_id: str = ""
    periods_modelled: int = 1
    currency: Optional[str] = None

    health_kpis: List[WarehouseHealthKPI] = Field(default_factory=list)

    #: Which region each demand market sits in.
    #:
    #: Carried so a growth rate stated by REGION can be applied to the sites
    #: that actually serve that region — the markets are facilities in the
    #: model and are otherwise excluded from this report, so without this a
    #: stored report could only ever apply one network-wide rate.
    market_regions: Dict[str, Optional[str]] = Field(default_factory=dict)

    top_capacity_constraints: List[WarehouseHealthKPI] = Field(default_factory=list)
    top_warehouses_by_utilization: List[WarehouseHealthKPI] = Field(default_factory=list)
    top_facilities_driving_cost: List[FacilityCostDriver] = Field(default_factory=list)

    top_savings_opportunities: List[FacilityDeltaRow] = Field(default_factory=list)
    facility_before_after: List[FacilityDeltaRow] = Field(default_factory=list)
    comparison_status: SectionStatus = Field(default_factory=SectionStatus)

    #: The same comparison, lane by lane.
    #:
    #: Carried beside the facility rows because an optimisation that holds the
    #: footprint fixed changes only the ROUTING — so on that comparison every
    #: facility row is unchanged by construction and this is the only half with
    #: anything in it.
    top_corridor_savings: List[CorridorDeltaRow] = Field(default_factory=list)
    corridor_before_after: List[CorridorDeltaRow] = Field(default_factory=list)
    #: What a facility-cost saving does and does not include. On the report
    #: rather than in the screen that renders it, so every consumer states the
    #: same caveat.
    savings_basis: str = ""
    #: WHICH optimisation the "after" column is, when there is one.
    #:
    #: Carried for the same reason as `savings_basis`, and it matters more: an
    #: optimisation that keeps the footprint fixed cannot change any site's
    #: status, so a table of unchanged rows means "the routing is already
    #: optimal", NOT "there is nothing to change about this network". Without
    #: this line a reader cannot tell those apart.
    comparison_basis: str = ""
    #: What the plan THIS report describes costs, from the same cost layer
    #: every other screen reads.
    business_cost: Optional[float] = None
    #: What the plan it is being COMPARED AGAINST costs, when there is one.
    baseline_business_cost: Optional[float] = None
    #: `baseline_business_cost - business_cost` — positive means this plan is
    #: cheaper. The authoritative network figure: facility savings do not sum
    #: to it and must not be read as though they did.
    business_cost_delta: Optional[float] = None

    future_requirements: List[FutureCapacityRequirement] = Field(default_factory=list)
    future_status: SectionStatus = Field(default_factory=SectionStatus)
    growth_assumption: Optional[GrowthAssumption] = None
    total_capacity_gap_units: float = 0.0
    n_sites_needing_expansion: int = 0

    # ---- roll-ups a screen leads with -----------------------------------
    #: Storage sites in this plan, and how many of them the plan uses.
    #: `n_open` counts EVERY open facility, plants included — the two are
    #: different populations and a screen must not read one as the other.
    n_warehouses: int = 0
    n_warehouses_open: int = 0
    n_open: int = 0
    n_bottlenecks: int = 0
    n_underused: int = 0
    #: Mean of the OPEN sites' peak utilisations. A mean of maxima, which is
    #: not the network's peak and is not called one.
    avg_peak_utilization_pct: Optional[float] = None
    total_facility_spend: float = 0.0

    status: SectionStatus = Field(default_factory=SectionStatus)

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Reading one solved state
# ---------------------------------------------------------------------------

def _role_of(facility: Any) -> str:
    return str(getattr(facility, "role", "") or "").upper()


def _is_market(facility: Any) -> bool:
    return _role_of(facility) in MARKET_ROLES


def _band(kpi: "WarehouseHealthKPI") -> str:
    """
    The priority order a screen reads top-down, from the configured thresholds.

    NOT_OPERATING is first because it is a different KIND of statement: a
    closed or unbuilt site has no utilisation to be healthy or tight, and
    calling it "healthy at 0%" is exactly the failure
    `test_warehouse_planning_ui.py` already records against the facility panel.
    """
    if not kpi.is_open:
        return "NOT_OPERATING"
    if kpi.peak_utilization_pct >= 100.0:
        return "CRITICAL"
    if kpi.peak_utilization_pct >= OVER_UTILISED_PCT:
        return "TIGHT"
    if kpi.peak_utilization_pct <= UNDER_UTILISED_PCT:
        return "UNDERUSED"
    return "HEALTHY"


def compute_warehouse_health(
    state: Any, *, roles: Optional[Sequence[str]] = None,
) -> List[WarehouseHealthKPI]:
    """
    Peak versus average for every non-market facility in one solved state.

    Reads `NetworkStateResult.facilities`. Markets are excluded: they are
    facilities in the model with an unbounded nominal capacity, and including
    them puts a row of 0.0% at the bottom of every ranking.

    The per-period series (`throughput_by_period`, `utilization_by_period`) is
    empty on a single-period solve BY DESIGN — one period's series would
    restate the average. Such a state is read as ONE period rather than as no
    periods, so its peak is its average and a bottleneck count of 1 means that
    single period was tight.
    """
    periods = max(1, int(getattr(state, "periods_modelled", 1) or 1))
    wanted = {r.upper() for r in roles} if roles else None

    out: List[WarehouseHealthKPI] = []
    for fac in getattr(state, "facilities", None) or []:
        if _is_market(fac):
            continue
        if wanted is not None and _role_of(fac) not in wanted:
            continue

        horizon_capacity = float(getattr(fac, "capacity_units", 0.0) or 0.0)
        rated = horizon_capacity / periods if periods else horizon_capacity

        by_period = {str(k): float(v) for k, v in
                     (getattr(fac, "throughput_by_period", None) or {}).items()}
        util_by_period = {str(k): float(v) for k, v in
                          (getattr(fac, "utilization_by_period", None) or {}).items()}

        # Average throughput per period. Taken from the contract's own field
        # rather than divided again here, so this cannot round differently from
        # the figure the facility screen shows.
        avg_tp = float(getattr(fac, "throughput_units_per_period", 0.0) or 0.0)
        if not avg_tp and periods:
            avg_tp = float(getattr(fac, "throughput_units", 0.0) or 0.0) / periods

        if by_period:
            peak_period, peak_tp = max(by_period.items(), key=lambda kv: kv[1])
        else:
            peak_period, peak_tp = None, avg_tp
        if peak_tp <= 0:
            # A site the plan never shipped through has no busiest period. The
            # series still has an entry for every period, all of them zero, and
            # `max` returns the first — which would put a specific month beside
            # a volume of nothing.
            peak_period = None

        avg_util = float(getattr(fac, "utilization_pct", 0.0) or 0.0)
        peak_util = float(getattr(fac, "peak_utilization_pct", 0.0) or 0.0) or avg_util

        # Periods at or above the threshold. From the per-period utilisation
        # series where the solve produced one; from the single period's own
        # utilisation where it did not — a count over one period, reported as
        # such by `periods_observed`.
        if util_by_period:
            observed = len(util_by_period)
            bottlenecks = sum(1 for u in util_by_period.values() if u >= OVER_UTILISED_PCT)
        else:
            observed = 1 if periods == 1 else periods
            bottlenecks = 1 if (periods == 1 and peak_util >= OVER_UTILISED_PCT) else 0

        avg_inv = getattr(fac, "avg_inventory_units", None)
        inv_status = SectionStatus()
        if avg_inv is None:
            inv_status = SectionStatus(
                status=KPIStatus.INSUFFICIENT_EVIDENCE,
                reason=("This solve produced no inventory decisions for this "
                        "site. A single-period model has no next period to "
                        "carry stock into, so it holds none — this is not a "
                        "reading of zero stock."),
            )

        kpi = WarehouseHealthKPI(
            facility_id=fac.facility_id,
            facility_name=fac.facility_name,
            role=_role_of(fac),
            region=getattr(fac, "region", None),
            country=getattr(fac, "country", None),
            is_open=bool(getattr(fac, "is_open", False)),
            baseline_status=getattr(fac, "baseline_status", None),
            rated_capacity_per_period=round(rated, 4),
            avg_throughput_units=round(avg_tp, 4),
            peak_throughput_units=round(peak_tp, 4),
            peak_period=peak_period,
            avg_utilization_pct=round(avg_util, 2),
            peak_utilization_pct=round(peak_util, 2),
            avg_inventory_units=avg_inv,
            peak_inventory_units=getattr(fac, "peak_inventory_units", None),
            inventory_status=inv_status,
            periods_observed=observed,
            bottleneck_periods_count=bottlenecks,
            is_bottleneck=peak_util >= OVER_UTILISED_PCT,
            headroom_units_peak=round(rated - peak_tp, 4),
            fixed_cost=float(getattr(fac, "fixed_cost", 0.0) or 0.0),
            handling_cost=float(getattr(fac, "handling_cost", 0.0) or 0.0),
            holding_cost=float(getattr(fac, "holding_cost", 0.0) or 0.0),
            opening_cost=float(getattr(fac, "opening_cost", 0.0) or 0.0),
            total_facility_cost=float(getattr(fac, "total_facility_cost", 0.0) or 0.0),
        )
        kpi.health_band = _band(kpi)
        out.append(kpi)
    return out


# ---------------------------------------------------------------------------
# Growth sizing
# ---------------------------------------------------------------------------

def _market_regions(state: Any) -> Dict[str, Optional[str]]:
    """
    Which region each demand market sits in.

    From `NetworkStateResult.market_regions`, not from `facilities`. The MILP
    emits a facility decision only for the facilities it decides something
    ABOUT, so a market never appears in that list — which means filtering
    `facilities` for markets here would have looked like it was doing something
    and always returned nothing.
    """
    return dict(getattr(state, "market_regions", None) or {})


def _growth_for_facility(
    facility_id: str,
    own_region: Optional[str],
    assumption: GrowthAssumption,
    outbound: Dict[str, Dict[str, float]],
    market_regions: Dict[str, Optional[str]],
) -> Optional[Tuple[float, str]]:
    """
    The growth rate that applies to one site, and how it was arrived at.

    A regional rate is stated about DEMAND, and a warehouse's demand is the
    markets it actually ships to — which the solve has already decided. So the
    rate is the volume-weighted mean of the rates of the markets this site
    serves, falling back in order to the site's own region and then to the
    network-wide rate.

    Returns None where no stated rate reaches this facility at all, so the row
    is left out rather than sized at 0% growth — which would read as a
    considered forecast of flat demand.
    """
    by_region = assumption.by_region
    if by_region:
        served = outbound.get(facility_id) or {}
        weighted, weight = 0.0, 0.0
        for market_id, units in served.items():
            rate = by_region.get(market_regions.get(market_id) or "")
            if rate is not None and units > 0:
                weighted += rate * units
                weight += units
        if weight > 0:
            return round(weighted / weight, 4), "served markets"
        own = by_region.get(own_region or "")
        if own is not None:
            return float(own), f"region {own_region}"

    if assumption.network_pct is not None:
        return float(assumption.network_pct), "network-wide rate"
    return None


def compute_future_requirements(
    health: Sequence[WarehouseHealthKPI],
    state: Any,
    assumption: Optional[GrowthAssumption],
) -> Tuple[List[FutureCapacityRequirement], SectionStatus]:
    """
    Size a live solved state. Reads the market regions and the served volumes
    off the state and hands both to `size_future_requirements`, which is the
    one implementation.
    """
    return size_future_requirements(
        health, assumption,
        market_regions=_market_regions(state),
        flows=[{"origin_id": f.origin_id,
                "destination_id": f.destination_id,
                "flow_units": f.flow_units}
               for f in (getattr(state, "flows", None) or [])],
    )


def size_future_requirements(
    health: Sequence[WarehouseHealthKPI],
    assumption: Optional[GrowthAssumption],
    *,
    market_regions: Optional[Dict[str, Optional[str]]] = None,
    flows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Tuple[List[FutureCapacityRequirement], SectionStatus]:
    """
    What each site would need at the stated growth rate.

    Sizing, not planning. `RequiredCap = ProjectedPeak / target utilisation`
    says how much room that volume would need at the utilisation a planner is
    willing to run at. It does not say the volume would GO there, because
    nothing here has re-solved the network — the screen that shows it offers a
    scenario, which does.

    Takes the health rows rather than a state, so a stored report can be sized
    against a rate stated after it was computed without re-solving anything.
    """
    if assumption is None or not assumption.is_stated():
        return [], SectionStatus(
            status=KPIStatus.INSUFFICIENT_EVIDENCE,
            reason=("No demand growth rate has been stated, so no future "
                    "capacity requirement can be sized. State a growth rate — "
                    "or take the one the Forecast screen measured — and this "
                    "section will size the footprint against it. It is not "
                    "assumed, because a sized gap in units resting on an "
                    "invented rate is indistinguishable from a measured one."),
        )

    target = assumption.target_utilization_pct
    if target <= 0:
        return [], SectionStatus(
            status=KPIStatus.NOT_COMPUTABLE,
            reason=(f"A target utilisation of {target}% cannot size capacity: "
                    f"required capacity is the projected peak DIVIDED by it."),
        )

    regions = dict(market_regions or {})
    outbound: Dict[str, Dict[str, float]] = {}
    for flow in flows or []:
        destination = str(flow.get("destination_id") or "")
        if destination in regions:
            served = outbound.setdefault(str(flow.get("origin_id") or ""), {})
            served[destination] = (served.get(destination, 0.0)
                                   + float(flow.get("flow_units") or 0.0))

    # Is there anywhere in this network to put volume that does not fit? A site
    # the client has PROPOSED and this plan has not opened is a real, named
    # option; without one the only lever is expanding what already exists.
    has_unopened_candidate = any(
        k.baseline_status == "CANDIDATE" and not k.is_open for k in health)

    rows: List[FutureCapacityRequirement] = []
    skipped = 0
    for kpi in health:
        if not kpi.is_open:
            # A site the plan does not use has no solved peak to grow. Sizing
            # from a throughput of zero would report that it needs no capacity,
            # which is an artefact of it being shut.
            continue
        resolved = _growth_for_facility(
            kpi.facility_id, kpi.region, assumption, outbound, regions)
        if resolved is None:
            skipped += 1
            continue
        growth_pct, basis = resolved

        projected = kpi.peak_throughput_units * (1.0 + growth_pct / 100.0)
        required = projected / (target / 100.0)
        gap = max(0.0, required - kpi.rated_capacity_per_period)

        if gap <= 0:
            action = "MAINTAIN"
            reason = (f"At +{growth_pct:.1f}% this site's worst period still "
                      f"fits inside its stated capacity at a {target:.0f}% "
                      f"target.")
        elif has_unopened_candidate and kpi.role in STORAGE_ROLES:
            # Only for a site a warehouse could relieve. A plant that runs out
            # of production capacity is not helped by opening a distribution
            # centre, and saying so would send a planner to build the wrong
            # thing.
            action = "OPEN_CANDIDATE_DC"
            reason = (f"This site would be {gap:,.0f} units/period short at "
                      f"+{growth_pct:.1f}%. The network carries a proposed site "
                      f"this plan has not opened, so there is somewhere to put "
                      f"the volume — test it as a scenario before committing to "
                      f"expand here. Nothing here has re-solved the network.")
        else:
            action = "EXPAND_CAPACITY"
            # Two different situations reach here and they need different
            # sentences: a storage site with nowhere else to put the volume,
            # and a site no storage site could relieve however many are
            # proposed. One word for that site throughout — the reader of this
            # sentence is reading it on the standard facility screen.
            where = (
                "no proposed distribution centre can absorb production capacity"
                if kpi.role not in STORAGE_ROLES
                else "the network carries no proposed site to absorb it")
            reason = (f"This site would be {gap:,.0f} units/period short at "
                      f"+{growth_pct:.1f}%, and {where}. Sizing only — no plan "
                      f"has been solved for it.")

        rows.append(FutureCapacityRequirement(
            facility_id=kpi.facility_id,
            facility_name=kpi.facility_name,
            region=kpi.region,
            current_capacity_per_period=kpi.rated_capacity_per_period,
            growth_pct=growth_pct,
            growth_basis=basis,
            projected_peak_throughput=round(projected, 2),
            target_utilization_pct=target,
            required_capacity=round(required, 2),
            capacity_gap_units=round(gap, 2),
            expansion_needed=gap > 0,
            recommended_action=action,
            recommended_action_reason=reason,
        ))

    rows.sort(key=lambda r: -r.capacity_gap_units)
    return rows, SectionStatus(
        status=KPIStatus.VALID,
        reason=(f"{skipped} open site(s) had no stated growth rate reaching "
                f"them and were left out." if skipped else ""),
    )


# ---------------------------------------------------------------------------
# Before and after
# ---------------------------------------------------------------------------

def _status_word(kpi: "WarehouseHealthKPI") -> str:
    if kpi.is_open:
        return "OPEN"
    return "PROPOSED" if kpi.baseline_status == "CANDIDATE" else "CLOSED"


def compare_facilities(
    baseline_health: Optional[Sequence[WarehouseHealthKPI]],
    optimized_health: Sequence[WarehouseHealthKPI],
) -> Tuple[List[FacilityDeltaRow], SectionStatus]:
    """
    Two readings of the same footprint, subtracted site by site.

    Takes HEALTH ROWS rather than the two solved states, and deliberately. The
    baseline and the optimised plan are separate executions cached separately
    (see `app.backend.services.analysis_store`), so no single execution context
    holds both — and requiring one would mean re-solving the baseline every
    time somebody opened the optimised view. A stored report's rows rebuild
    into these models exactly, so the comparison joins what has already been
    computed instead of paying for it twice.

    Nothing is ranked into a claim here beyond the ordering: every row is one
    figure from one reading minus the same figure from the other. Facilities
    present in EITHER appear, so a site the optimiser opened is not silently
    absent from the "after" column.

    Throughput is compared PER PERIOD, the basis every other figure in this
    report is on. Two plans over the same horizon compare either way; two over
    different horizons compare only this way.
    """
    if not baseline_health:
        return [], SectionStatus(
            status=KPIStatus.INSUFFICIENT_EVIDENCE,
            reason=("A before-and-after needs two solved plans. This analysis "
                    "holds one."),
        )
    if not optimized_health:
        return [], SectionStatus(
            status=KPIStatus.NOT_COMPUTABLE,
            reason="One of the two plans reported no facilities.",
        )

    base = {k.facility_id: k for k in baseline_health}
    opt = {k.facility_id: k for k in optimized_health}

    rows: List[FacilityDeltaRow] = []
    for facility_id in sorted(set(base) | set(opt)):
        b, o = base.get(facility_id), opt.get(facility_id)
        ref = b if b is not None else o
        b_cost = b.total_facility_cost if b else 0.0
        o_cost = o.total_facility_cost if o else 0.0
        b_open = bool(b and b.is_open)
        o_open = bool(o and o.is_open)

        if b_open and not o_open:
            change = "Closed by the optimiser"
        elif o_open and not b_open:
            change = "Opened by the optimiser"
        elif not b_open and not o_open:
            change = "Not used in either plan"
        else:
            change = "Open in both plans"

        rows.append(FacilityDeltaRow(
            facility_id=facility_id,
            facility_name=ref.facility_name if ref else facility_id,
            role=ref.role if ref else "",
            baseline_status=_status_word(b) if b else "ABSENT",
            optimized_status=_status_word(o) if o else "ABSENT",
            baseline_throughput=round(b.avg_throughput_units, 2) if b else 0.0,
            optimized_throughput=round(o.avg_throughput_units, 2) if o else 0.0,
            throughput_delta=round((o.avg_throughput_units if o else 0.0)
                                   - (b.avg_throughput_units if b else 0.0), 2),
            baseline_peak_utilization_pct=round(b.peak_utilization_pct, 2) if b else 0.0,
            optimized_peak_utilization_pct=round(o.peak_utilization_pct, 2) if o else 0.0,
            utilization_delta_pts=round((o.peak_utilization_pct if o else 0.0)
                                        - (b.peak_utilization_pct if b else 0.0), 2),
            baseline_cost=round(b_cost, 2),
            optimized_cost=round(o_cost, 2),
            facility_cost_savings=round(b_cost - o_cost, 2),
            change=change,
        ))
    return rows, SectionStatus()


def compare_corridors(
    baseline_flows: Optional[Sequence[Mapping[str, Any]]],
    optimized_flows: Optional[Sequence[Mapping[str, Any]]],
) -> Tuple[List[CorridorDeltaRow], SectionStatus]:
    """
    Two solved plans, subtracted lane by lane.

    Takes the flow rows the KPI layer already publishes (`origin_id`,
    `destination_id`, `flow_units`, `transport_cost`) rather than either state,
    for the same reason `compare_facilities` takes health rows: the two plans
    are separately cached analyses and no single execution holds both.

    Lanes present in EITHER plan appear — a corridor the optimiser started
    using, and one it abandoned, are both the finding.
    """
    if not baseline_flows or not optimized_flows:
        return [], SectionStatus(
            status=KPIStatus.INSUFFICIENT_EVIDENCE,
            reason=("A lane-by-lane comparison needs two solved plans. This "
                    "analysis holds one."),
        )

    def keyed(flows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str], Mapping[str, Any]]:
        return {(str(f.get("origin_id") or ""), str(f.get("destination_id") or "")): f
                for f in flows}

    base, opt = keyed(baseline_flows), keyed(optimized_flows)

    def units(row: Optional[Mapping[str, Any]]) -> float:
        return float((row or {}).get("flow_units") or 0.0)

    def cost(row: Optional[Mapping[str, Any]]) -> float:
        return float((row or {}).get("transport_cost") or 0.0)

    rows: List[CorridorDeltaRow] = []
    for key in sorted(set(base) | set(opt)):
        b, o = base.get(key), opt.get(key)
        b_units, o_units = units(b), units(o)
        if b_units <= 0 and o_units <= 0:
            continue
        if b_units <= 0:
            change = "Newly used"
        elif o_units <= 0:
            change = "No longer used"
        elif o_units > b_units:
            change = "Carries more"
        elif o_units < b_units:
            change = "Carries less"
        else:
            change = "Unchanged"

        rows.append(CorridorDeltaRow(
            origin_id=key[0], destination_id=key[1],
            baseline_units=round(b_units, 2),
            optimized_units=round(o_units, 2),
            units_delta=round(o_units - b_units, 2),
            baseline_transport_cost=round(cost(b), 2),
            optimized_transport_cost=round(cost(o), 2),
            transport_cost_savings=round(cost(b) - cost(o), 2),
            change=change,
        ))
    return rows, SectionStatus()


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def build_warehouse_deep_dive(
    state: Any,
    *,
    baseline_health: Optional[Sequence[WarehouseHealthKPI]] = None,
    baseline_business_cost: Optional[float] = None,
    growth: Optional[GrowthAssumption] = None,
) -> WarehouseDeepDiveReport:
    """
    The whole warehouse read of one solved state, with two optional companions.

    `baseline_health` (with `baseline_business_cost`) enables the
    before-and-after and the savings ranking; `growth` enables the sizing.
    Neither is required and neither is invented: without them, those sections
    report why they are empty.
    """
    if state is None:
        return WarehouseDeepDiveReport(status=SectionStatus(
            status=KPIStatus.INSUFFICIENT_EVIDENCE,
            reason=("No solved network state is present in this analysis, so "
                    "there is nothing to read the footprint from."),
        ))

    health = compute_warehouse_health(state)
    if not health:
        return WarehouseDeepDiveReport(
            network_id=str(getattr(state, "network_id", "") or ""),
            status=SectionStatus(
                status=KPIStatus.NOT_COMPUTABLE,
                reason=("This network reports no facilities other than demand "
                        "markets, so it has no distribution footprint to read."),
            ))

    open_sites = [k for k in health if k.is_open]
    storage = [k for k in health if k.role in STORAGE_ROLES]

    # Rankings. Ordering, and nothing else: every value in every row is the
    # same value the health table carries.
    constraints = sorted(open_sites, key=lambda k: -k.peak_utilization_pct)[:TOP_N]
    by_utilisation = sorted((k for k in storage if k.is_open),
                            key=lambda k: -k.peak_utilization_pct)[:TOP_N]

    facility_spend = sum(k.total_facility_cost for k in health)
    cost_drivers = [
        FacilityCostDriver(
            facility_id=k.facility_id,
            facility_name=k.facility_name,
            region=k.region,
            total_facility_cost=round(k.total_facility_cost, 2),
            fixed_cost=round(k.fixed_cost, 2),
            handling_cost=round(k.handling_cost, 2),
            holding_cost=round(k.holding_cost, 2),
            opening_cost=round(k.opening_cost, 2),
            share_of_facility_spend=(round(k.total_facility_cost / facility_spend, 6)
                                     if facility_spend > 0 else None),
        )
        for k in sorted(health, key=lambda k: -k.total_facility_cost)[:TOP_N]
        if k.total_facility_cost > 0
    ]

    deltas, comparison_status = compare_facilities(baseline_health, health)
    savings = sorted((r for r in deltas if r.facility_cost_savings > 0),
                     key=lambda r: -r.facility_cost_savings)[:TOP_N]

    future, future_status = compute_future_requirements(health, state, growth)

    # The plan's own business cost, from the same cost layer every other screen
    # reads. Not summed from the facilities: facility cost is one component of
    # it, and adding the parts here would produce a second, smaller "network
    # cost" that disagrees with the one on the Overview.
    costs = getattr(state, "costs", None)
    value = getattr(costs, "business_network_cost", None) if costs else None
    plan_cost = round(float(value), 2) if isinstance(value, (int, float)) else None
    base_cost = (round(float(baseline_business_cost), 2)
                 if isinstance(baseline_business_cost, (int, float)) else None)
    peaks = [k.peak_utilization_pct for k in open_sites]

    return WarehouseDeepDiveReport(
        network_id=str(getattr(state, "network_id", "") or ""),
        periods_modelled=max(1, int(getattr(state, "periods_modelled", 1) or 1)),
        currency=getattr(state, "currency", None),
        health_kpis=health,
        market_regions=_market_regions(state),
        top_capacity_constraints=constraints,
        top_warehouses_by_utilization=by_utilisation,
        top_facilities_driving_cost=cost_drivers,
        top_savings_opportunities=savings,
        facility_before_after=deltas,
        comparison_status=comparison_status,
        savings_basis=(
            "A saving here is one site's FACILITY cost in the baseline less its "
            "facility cost in the optimised plan — fixed, opening, closure, "
            "handling and holding. It does not include the transport cost of "
            "moving that site's volume elsewhere, so these figures do not sum "
            "to the network saving. The network figure is stated separately."
        ) if deltas else "",
        business_cost=plan_cost,
        baseline_business_cost=base_cost,
        business_cost_delta=(round(base_cost - plan_cost, 2)
                             if base_cost is not None and plan_cost is not None
                             else None),
        future_requirements=future,
        future_status=future_status,
        growth_assumption=growth if (growth and growth.is_stated()) else None,
        total_capacity_gap_units=round(sum(r.capacity_gap_units for r in future), 2),
        n_sites_needing_expansion=sum(1 for r in future if r.expansion_needed),
        n_warehouses=len(storage),
        n_warehouses_open=sum(1 for k in storage if k.is_open),
        n_open=len(open_sites),
        n_bottlenecks=sum(1 for k in open_sites if k.is_bottleneck),
        n_underused=sum(1 for k in open_sites if k.health_band == "UNDERUSED"),
        avg_peak_utilization_pct=(round(sum(peaks) / len(peaks), 2) if peaks else None),
        total_facility_spend=round(facility_spend, 2),
    )


__all__ = [
    "DEFAULT_TARGET_UTILISATION_PCT",
    "CorridorDeltaRow",
    "FacilityCostDriver",
    "FacilityDeltaRow",
    "FutureCapacityRequirement",
    "GrowthAssumption",
    "OVER_UTILISED_PCT",
    "SectionStatus",
    "STORAGE_ROLES",
    "TOP_N",
    "UNDER_UTILISED_PCT",
    "WarehouseDeepDiveReport",
    "WarehouseHealthKPI",
    "build_warehouse_deep_dive",
    "compare_corridors",
    "compare_facilities",
    "compute_future_requirements",
    "compute_warehouse_health",
    "size_future_requirements",
]
