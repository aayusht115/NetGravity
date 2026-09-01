"""
NetGravity — Uploaded Data → CanonicalNetwork Assembler
=======================================================
Turns the structure extracted from a user's uploaded workbook into a real
`CanonicalNetwork`, so that their own data — not a synthetic fixture — is what
the MILP solves.

This is the step that was missing from the application: the parser produced
plain dicts, the engine consumes `CanonicalNetwork`, and nothing joined them.

What this module does NOT do, deliberately:

  * It does not optimise. Assembly is not analysis.
  * It does not invent facts. Every field it cannot derive from the upload is
    left at the engine's documented default, and every substitution it makes is
    recorded in the returned `assumptions` list so the user can see it. A
    silent default is how a spreadsheet becomes a confident wrong answer.
  * It does not fabricate demand. A network with no demand rows cannot be
    solved, and the caller is told that rather than handed a zero-demand
    network that trivially "succeeds".
"""

from __future__ import annotations

import logging
import statistics
from typing import Any, Dict, List, Tuple

from app.backend.services.errors import ValidationError
from netgravity.ingestion.builder import build_network
from netgravity.schemas.network import (
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationMode,
    ProductRecord,
)

logger = logging.getLogger(__name__)

#: Used when an uploaded workbook carries no product dimension at all — which
#: is the common case for a network-design dataset. Declared once, visibly,
#: rather than sprinkled through the assembly code.
_DEFAULT_PRODUCT_ID = "PROD_ALL"
_DEFAULT_PRODUCT_NAME = "All products (aggregate)"

#: Periods of history used to measure demand variability. Twelve months is the
#: usual planning convention and keeps a multi-year trend from being counted as
#: period-to-period uncertainty.
_SIGMA_WINDOW = 12


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def diagnose_servability(network: Any) -> List[Dict[str, Any]]:
    """
    Per-market check: can this demand physically reach this market at all?

    This is NOT a second optimiser. It evaluates a *necessary* condition with
    plain arithmetic on the user's own numbers — for each market, the inbound
    lanes whose transit time meets that market's SLA, and whether their
    combined capacity covers the demand. A market failing this can never be
    served whatever the solver does.

    It exists because "INFEASIBLE" on its own is a dead end for the person who
    uploaded the file. Knowing that Delhi needs 9,433 units but its only
    SLA-eligible lane carries 3,230 is the difference between a blank screen
    and a decision.

    Returns one row per market that cannot be fully served, most severe first.
    """
    demand_by_market: Dict[str, float] = {}
    sla_by_market: Dict[str, float | None] = {}
    for record in network.demands:
        demand_by_market[record.market_id] = (
            demand_by_market.get(record.market_id, 0.0) + record.quantity
        )
        sla = getattr(record, "sla_days", None)
        if sla is not None:
            prior = sla_by_market.get(record.market_id)
            # The tightest SLA stated for the market binds it.
            sla_by_market[record.market_id] = sla if prior is None else min(prior, sla)

    findings: List[Dict[str, Any]] = []
    for market_id, demand in demand_by_market.items():
        sla = sla_by_market.get(market_id)
        eligible, blocked_by_sla = [], 0
        for lane in network.lanes:
            if lane.destination_id != market_id:
                continue
            if sla is not None and lane.lead_time_days > sla:
                blocked_by_sla += 1
                continue
            eligible.append(lane)

        if not eligible:
            findings.append({
                "market_id": market_id, "demand": demand, "sla_days": sla,
                "eligible_lanes": 0, "capacity": 0.0, "shortfall": demand,
                "reason": (
                    f"No inbound lane reaches {market_id} within its "
                    f"{sla:g}-day service level"
                    if sla is not None else
                    f"No inbound lane reaches {market_id}"
                ) + (f" ({blocked_by_sla} lane(s) are too slow)." if blocked_by_sla else "."),
            })
            continue

        # An uncapacitated eligible lane means capacity cannot be the binding
        # constraint here, so the market passes this check.
        if any(getattr(l, "lane_capacity", None) in (None, 0) for l in eligible):
            continue

        capacity = sum(l.lane_capacity for l in eligible)
        if capacity + 1e-9 < demand:
            findings.append({
                "market_id": market_id, "demand": demand, "sla_days": sla,
                "eligible_lanes": len(eligible), "capacity": capacity,
                "shortfall": demand - capacity,
                "reason": (
                    f"{market_id} needs {demand:,.0f} units but the "
                    f"{len(eligible)} lane(s) that meet its {sla:g}-day service "
                    f"level carry only {capacity:,.0f} — short {demand - capacity:,.0f}."
                    + (f" {blocked_by_sla} further lane(s) reach it but are too slow."
                       if blocked_by_sla else "")
                ),
            })

    findings.sort(key=lambda f: f["shortfall"], reverse=True)
    return findings


def assemble_network_from_structure(
    structure: Dict[str, Any],
    *,
    network_id: str = "uploaded_network",
    description: str = "",
) -> Tuple[Any, List[str], List[str]]:
    """
    Build a `CanonicalNetwork` from the extractor's parsed structure.

    Returns `(network, assumptions, issues)`:
      * `assumptions` — every default this assembler had to apply, in words.
        The UI shows these; they are not hidden.
      * `issues`      — row-level problems reported by the canonical builder.

    Raises `ValidationError` when the upload cannot form a solvable network,
    naming what is missing.
    """
    plants = structure.get("plants") or []
    dcs = structure.get("dcs") or []
    markets = structure.get("markets") or []
    lanes_in = structure.get("lanes") or []

    assumptions: List[str] = []

    if not plants and not dcs:
        raise ValidationError(
            "No plants or distribution centres were recognised in the uploaded "
            "files, so no network can be built.",
            context={"markets_found": len(markets), "lanes_found": len(lanes_in)},
        )
    if not markets:
        raise ValidationError(
            "No demand markets were recognised in the uploaded files. A network "
            "with no demand cannot be optimised.",
            context={"plants_found": len(plants), "dcs_found": len(dcs)},
        )

    # ---- Facilities --------------------------------------------------
    facilities: List[FacilityRecord] = []
    seen: set[str] = set()
    missing_status: List[str] = []
    unknown_status: List[str] = []

    def add_facility(raw: Dict[str, Any], role: NodeRole) -> None:
        fid = str(raw.get("id") or "").strip()
        if not fid or fid in seen:
            return
        seen.add(fid)

        capacity = _as_float(raw.get("capacity"))
        handling = _as_float(raw.get("handlingCost"))
        record = FacilityRecord(
            id=fid,
            name=str(raw.get("name") or fid),
            role=role,
            # Prefer the coordinate the upload actually stated. `lat`/`lng` may
            # have been nudged for map legibility when nodes share a location;
            # `latSource`/`lngSource` hold the original in that case.
            latitude=_as_float(raw.get("latSource")) if raw.get("latSource") is not None
                     else _as_float(raw.get("lat")),
            longitude=_as_float(raw.get("lngSource")) if raw.get("lngSource") is not None
                      else _as_float(raw.get("lng")),
            handling_cost_per_unit=handling if handling is not None else 0.0,
        )

        # Whether the facility already exists is not cosmetic. In a brownfield
        # optimisation an EXISTING facility is one the client operates today,
        # so closing it is a decision with a consequence; a CANDIDATE is one
        # they might build. The record defaults to CANDIDATE, and the uploaded
        # `Status` column was being dropped — so eight live facilities were
        # offered to the solver as greenfield options and it "optimised" by
        # shutting three of them, including two of the three plants.
        raw_status = str(raw.get("status") or "").strip().upper()
        if role != NodeRole.MARKET:
            if raw_status in ("EXISTING", "ACTIVE", "OPEN", "OPERATIONAL", "TRUE", "1"):
                record.status = FacilityStatus.EXISTING
            elif raw_status in ("CLOSED", "INACTIVE", "FALSE", "0"):
                record.status = FacilityStatus.CLOSED
            elif raw_status in ("CANDIDATE", "PLANNED", "PROPOSED"):
                record.status = FacilityStatus.CANDIDATE
            elif raw_status:
                unknown_status.append(f"{fid} ({raw_status})")
            else:
                missing_status.append(fid)
        if capacity is not None and capacity > 0:
            if role == NodeRole.PLANT:
                record.production_capacity_units_per_period = capacity
            record.capacity_units_per_period = capacity
        elif role != NodeRole.MARKET:
            assumptions.append(
                f"{fid}: no capacity found in the upload; the facility is "
                f"modelled as uncapacitated."
            )
        if handling is None and role == NodeRole.DC:
            assumptions.append(
                f"{fid}: no handling cost per unit in the upload; modelled as "
                f"zero rather than estimated."
            )

        fixed = _as_float(raw.get("fixedCost"))
        if fixed is not None and fixed > 0:
            # The engine wants an annual figure. A monthly cost is the common
            # convention in these workbooks, so anything that looks like a
            # monthly figure is annualised — and either way the interpretation
            # is stated, because reading a yearly cost as monthly would
            # overstate fixed cost twelvefold.
            record.fixed_cost_per_year = fixed * 12.0
            assumptions.append(
                f"{fid}: fixed cost read as ₹{fixed:,.0f}/month and annualised "
                f"to ₹{record.fixed_cost_per_year:,.0f}/year."
            )
        facilities.append(record)

    for p in plants:
        add_facility(p, NodeRole.PLANT)
    for d in dcs:
        add_facility(d, NodeRole.DC)
    for m in markets:
        add_facility(m, NodeRole.MARKET)

    if missing_status:
        assumptions.append(
            f"{len(missing_status)} facility/facilities carried no status "
            f"column and are modelled as candidates the optimiser may open or "
            f"leave closed: {', '.join(missing_status)}."
        )
    if unknown_status:
        assumptions.append(
            f"Unrecognised facility status, modelled as a candidate rather "
            f"than assumed operational: {', '.join(unknown_status)}."
        )

    # ---- Products ----------------------------------------------------
    # A real client workbook carries a Products sheet and splits demand by
    # product. Collapsing that to one aggregate product would silently discard
    # the per-product freight rates, which differ by up to 35% in the sample
    # data — so the product dimension is kept whenever the upload has one.
    product_rows = structure.get("products") or []
    product_ids: List[str] = []
    for p in product_rows:
        pid = str(p.get("id") or "").strip()
        if pid and pid not in product_ids:
            product_ids.append(pid)

    if product_ids:
        products = []
        no_weight, no_value = [], []
        for p in product_rows:
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            record = ProductRecord(id=pid, name=str(p.get("name") or pid))
            # Unit weight drives the carbon calculation, which works in
            # tonne-kilometres. Leaving the 1.0 kg default in place while the
            # workbook says 0.42 kg overstated this product's emissions by more
            # than a factor of two.
            weight = _as_float(p.get("unitWeightKg"))
            if weight is not None and weight > 0:
                record.weight_kg = weight
            else:
                no_weight.append(pid)
            # Unit value drives inventory holding cost (holding_rate x value).
            # With the default 0.0 the whole inventory term evaluated to zero,
            # so a network carrying ₹78.50 and ₹118.00 goods reported no
            # holding cost at all.
            value = _as_float(p.get("unitCost"))
            if value is not None and value > 0:
                record.unit_value = value
            else:
                no_value.append(pid)
            products.append(record)
        if no_weight:
            assumptions.append(
                f"No unit weight for {', '.join(no_weight)}; carbon is computed "
                f"at the engine default of 1 kg/unit for these."
            )
        if no_value:
            assumptions.append(
                f"No unit value for {', '.join(no_value)}; inventory holding "
                f"cost is not charged for these rather than estimated."
            )
    else:
        products = [ProductRecord(id=_DEFAULT_PRODUCT_ID, name=_DEFAULT_PRODUCT_NAME)]
        product_ids = [_DEFAULT_PRODUCT_ID]
        assumptions.append(
            "No product dimension was found in the upload; all demand is modelled "
            "as a single aggregate product."
        )

    # ---- Demand ------------------------------------------------------
    # Per-product demand from the history when the upload has one; otherwise
    # the market-level figure, split evenly across products only if the upload
    # itself gave no product breakdown.
    history = structure.get("demandHistory") or []
    latest_period = ""
    per_pair: Dict[Tuple[str, str], float] = {}
    series: Dict[Tuple[str, str], List[float]] = {}
    if history:
        periods = [h.get("period") for h in history if h.get("period")]
        if periods:
            latest_period = max(periods)
            for h in history:
                mid = str(h.get("marketId") or "").strip()
                pid = str(h.get("productId") or "").strip() or product_ids[0]
                qty = _as_float(h.get("quantity"))
                if not mid or qty is None:
                    continue
                if h.get("period") == latest_period:
                    per_pair[(mid, pid)] = per_pair.get((mid, pid), 0.0) + qty
                series.setdefault((mid, pid), []).append(qty)

    # Demand variability, for the safety-stock term the inventory module already
    # owns. `DemandRecord.std_dev` defaults to 0.0, and a sigma of zero means no
    # safety stock at all — so a network with 36 months of observed demand
    # reported holding cost as though demand were perfectly known in advance.
    # Nothing is invented here: sigma is the sample standard deviation of the
    # client's own history for that market and product. It is taken over the
    # most recent `_SIGMA_WINDOW` periods because these series trend upward
    # (Delhi/P001 runs 3,972 -> 5,862 over three years) and a standard deviation
    # taken across the whole span would measure that growth as if it were
    # week-to-week uncertainty, oversizing the buffer.
    std_by_pair: Dict[Tuple[str, str], float] = {}
    for key, values in series.items():
        window = values[-_SIGMA_WINDOW:]
        if len(window) >= 2:
            std_by_pair[key] = statistics.stdev(window)

    demands: List[DemandRecord] = []
    markets_without_demand: List[str] = []
    sigma_pairs = 0
    for m in markets:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        sla = _as_float(m.get("slaDays"))

        pairs = {pid: qty for (mkt, pid), qty in per_pair.items() if mkt == mid}
        if not pairs:
            qty = _as_float(m.get("demand"))
            if qty is None or qty <= 0:
                markets_without_demand.append(mid)
                continue
            pairs = {product_ids[0]: qty}

        for pid, qty in pairs.items():
            if qty is None or qty <= 0:
                continue
            record = DemandRecord(market_id=mid, product_id=pid, quantity=qty)
            if sla is not None and sla > 0:
                record.sla_days = sla
            sigma = std_by_pair.get((mid, pid))
            if sigma is not None and sigma > 0:
                record.std_dev = sigma
                sigma_pairs += 1
            demands.append(record)

    if per_pair:
        assumptions.append(
            f"Demand is the latest period on record ({latest_period}) from the "
            f"uploaded demand history, kept split by product."
        )
    if sigma_pairs:
        assumptions.append(
            f"Demand variability for {sigma_pairs} market-product pair(s) is the "
            f"sample standard deviation of the last {_SIGMA_WINDOW} periods of "
            f"the uploaded history, which is what sizes safety stock. Pairs with "
            f"fewer than two observations carry no variability and therefore no "
            f"safety stock."
        )

    if markets_without_demand:
        assumptions.append(
            f"{len(markets_without_demand)} market(s) had no demand quantity and "
            f"were excluded: {', '.join(markets_without_demand[:6])}"
            f"{'…' if len(markets_without_demand) > 6 else ''}."
        )
    if not demands:
        raise ValidationError(
            "None of the recognised markets carried a demand quantity, so there "
            "is nothing to optimise.",
            context={"markets_found": len(markets)},
        )

    # ---- Lanes -------------------------------------------------------
    # One LaneRecord per origin-destination pair.
    #
    # The upload may price a lane per product (the sample data's rates differ
    # by ~34% between products). The MILP keys its arcs on
    # (origin, destination, mode, product) but takes `rate_per_unit` from the
    # lane and skips any duplicate key, so emitting one lane per product does
    # NOT give each product its own rate — the first lane's rate would be
    # applied to every product and the rest silently dropped.
    #
    # So the per-product rates are collapsed to a single lane rate weighted by
    # the actual demand mix, which is the closest defensible single number, and
    # the weighting is declared. An unweighted mean would misprice the lane
    # whenever the products sell in different volumes.
    product_mix: Dict[str, float] = {}
    for record in demands:
        product_mix[record.product_id] = product_mix.get(record.product_id, 0.0) + record.quantity
    mix_total = sum(product_mix.values())

    def _blended_rate(rates: Dict[str, float]) -> float:
        if len(rates) == 1:
            return next(iter(rates.values()))
        if mix_total > 0 and any(p in product_mix for p in rates):
            weighted = sum(r * product_mix.get(p, 0.0) for p, r in rates.items())
            weight = sum(product_mix.get(p, 0.0) for p in rates)
            if weight > 0:
                return weighted / weight
        return sum(rates.values()) / len(rates)

    lanes: List[LaneRecord] = []
    lanes_missing_rate = 0
    lanes_skipped_unknown_node = 0
    blended = 0

    for lane in lanes_in:
        origin = str(lane.get("from") or "").strip()
        dest = str(lane.get("to") or "").strip()
        if not origin or not dest:
            continue
        if origin not in seen or dest not in seen:
            lanes_skipped_unknown_node += 1
            continue

        priced: Dict[str, float] = {}
        for pid, raw in (lane.get("ratesByProduct") or {}).items():
            value = _as_float(raw)
            if value is not None and value > 0:
                priced[pid] = value

        if priced:
            rate = _blended_rate(priced)
            if len(priced) > 1:
                blended += 1
        else:
            rate = _as_float(lane.get("cost"))
        if rate is None or rate <= 0:
            lanes_missing_rate += 1
            continue

        record = LaneRecord(origin_id=origin, destination_id=dest, rate_per_unit=rate)
        dist = _as_float(lane.get("distance"))
        if dist is not None and dist > 0:
            record.distance_km = dist
        lead = _as_float(lane.get("leadTime"))
        if lead is not None and lead > 0:
            record.lead_time_days = lead
        cap = _as_float(lane.get("capacity"))
        if cap is not None and cap > 0:
            record.lane_capacity = cap
        lanes.append(record)

    if lanes_skipped_unknown_node:
        assumptions.append(
            f"{lanes_skipped_unknown_node} lane(s) referenced an origin or "
            f"destination that is not in the facilities or markets sheets and "
            f"were excluded."
        )
    if blended:
        assumptions.append(
            f"{blended} lane(s) are priced per product in the upload. The "
            f"optimiser carries one rate per lane, so each was set to the "
            f"demand-weighted average of its product rates."
        )

    if lanes_missing_rate:
        assumptions.append(
            f"{lanes_missing_rate} lane(s) had no freight rate and were excluded. "
            f"A lane with no cost cannot be priced, and guessing a rate would "
            f"change the optimal answer."
        )
    if not lanes:
        raise ValidationError(
            "No priced lanes were recognised in the uploaded files. The network "
            "needs at least one lane with a freight rate to be solvable.",
            context={"lanes_parsed": len(lanes_in),
                     "lanes_without_rate": lanes_missing_rate},
        )

    # ---- Assemble ----------------------------------------------------
    network, row_issues = build_network(
        facilities=facilities,
        products=products,
        demands=demands,
        lanes=lanes,
        network_id=network_id,
        description=description or "Assembled from user-uploaded files",
    )

    # A client's own network is frequently unable to serve all of its demand
    # within its own service levels — that is a finding, and the planner still
    # needs the plan. Permit the solver to fall back to a shortage-priced model
    # if, and only if, the strict one proves infeasible; it then reports the
    # stranded volume as `unserved_demand` instead of reporting nothing.
    network.config.relax_to_shortage_when_infeasible = True

    # The baseline a client sees on opening the app is their network AS IT IS,
    # not a redesign of it. `OptimizationConfig` defaults to
    # BROWNFIELD_SCENARIO_OPTIMIZATION, which treats every facility as a
    # decision — so the "current network" panel reported the cost of a network
    # with three of the client's eight sites shut, at ₹9.6M/month against the
    # ₹18.1M/month they actually run. The redesign is a genuine and valuable
    # finding, but it belongs in a scenario, not in the baseline.
    #
    # ACTUAL_AS_IS_EVALUATION pins the existing footprint open and marks the
    # result `is_hypothetical=False`. Its own documented caveat still holds and
    # is stated below: with no observed shipment volumes in the upload, the
    # allocation across that fixed footprint is cost-minimal rather than a
    # replay of what actually shipped.
    network.config.optimization_mode = OptimizationMode.ACTUAL_AS_IS_EVALUATION
    assumptions.append(
        "Baseline KPIs evaluate the network as uploaded, with every active "
        "facility open. The upload carries no observed shipment volumes, so "
        "flow across that fixed footprint is the cost-minimal allocation "
        "rather than a replay of recorded shipments."
    )

    issues = [
        getattr(i, "message", None) or str(i) for i in row_issues
    ]

    # Pre-flight servability. A network that fails this will come back
    # INFEASIBLE from the solver, and "INFEASIBLE" alone tells the user
    # nothing they can act on — so the specific markets and the binding
    # constraint are surfaced here, at upload time.
    for finding in diagnose_servability(network):
        issues.append(finding["reason"])

    logger.info(
        "network.assembled facilities=%d demands=%d lanes=%d assumptions=%d issues=%d",
        len(facilities), len(demands), len(lanes), len(assumptions), len(issues),
    )
    return network, assumptions, issues
