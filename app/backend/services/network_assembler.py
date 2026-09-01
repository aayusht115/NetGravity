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

#: How many observed periods the model carries variables for.
#:
#: Twelve is one full seasonal cycle. That is the shortest horizon that sees
#: every season exactly once, which is the whole reason to model more than one
#: period: a network sized on the mean month is a different network from one
#: sized on the peak month, and only a horizon that contains the peak can tell
#: them apart. A longer horizon adds trend rather than seasonality, and trend
#: is already carried — by `_SIGMA_WINDOW` into safety stock, and by the
#: forecasting engine, which is the right instrument for it.
#:
#: This is a bound on what is MODELLED, not on what is kept. The full observed
#: history is still stored (`demand_history_store`) and still measured for
#: variability; the horizon is the part the MILP is asked to reason over.
_HORIZON_PERIODS = 12

#: A ceiling on the demand table the horizon may produce, in rows.
#:
#: Solve cost grows linearly in the number of periods — measured on the sample
#: client network at 82 variables and 0.02 s for one period against 3,032 and
#: 0.13 s for thirty-six. That is affordable because the network is small. The
#: same horizon over a client with four thousand market-product pairs is not,
#: and a planning tool that becomes unusable on a large upload has failed at
#: exactly the size where it is worth the most.
#:
#: So the horizon shortens to fit the budget rather than the budget being
#: assumed. What that costs is stated in the assumptions, never absorbed
#: silently: a client whose horizon was cut needs to know their answer covers
#: fewer months than their file does.
_MAX_MODELLED_DEMAND_ROWS = 20_000


def _as_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def choose_horizon(
    observed: List[str], rows_per_period: int,
) -> Tuple[List[str], List[str]]:
    """
    Which of the observed periods the MILP will carry variables for.

    Returns `(modelled, notes)` — the periods to model, most recent last, and
    the plain-language reasons for anything the caller should know about.

    Three things can shorten a horizon, and each is reported rather than
    applied quietly:

      * the data is shorter than `_HORIZON_PERIODS`, in which case the horizon
        is simply what there is;
      * the data is longer, and the most recent `_HORIZON_PERIODS` are taken —
        a full seasonal cycle, ending at the present;
      * the resulting table would exceed `_MAX_MODELLED_DEMAND_ROWS`, in which
        case the horizon shortens until it fits.

    The budget is evaluated against the row count the upload actually produces,
    not against a guess about network size, so a wide network is bounded by the
    same rule as a long one.
    """
    notes: List[str] = []
    if len(observed) <= 1:
        return list(observed), notes

    wanted = min(len(observed), _HORIZON_PERIODS)

    if rows_per_period > 0:
        affordable = max(1, _MAX_MODELLED_DEMAND_ROWS // rows_per_period)
        if affordable < wanted:
            notes.append(
                f"This upload states {rows_per_period:,} demand rows per period. "
                f"Modelling {wanted} periods would build a demand table of "
                f"{wanted * rows_per_period:,} rows, beyond the "
                f"{_MAX_MODELLED_DEMAND_ROWS:,}-row budget this solve is held "
                f"to, so the horizon is the most recent {affordable} period(s) "
                f"instead. Seasonality outside that window is not modelled."
            )
            wanted = affordable

    modelled = observed[-wanted:]
    if len(observed) > wanted and not notes:
        notes.append(
            f"The upload carries {len(observed)} periods of demand history "
            f"({observed[0]} to {observed[-1]}). The model covers the most "
            f"recent {wanted} ({modelled[0]} to {modelled[-1]}) — one full "
            f"seasonal cycle. The earlier periods still inform demand "
            f"variability and remain available to forecasting; they are not "
            f"solved as planning periods."
        )
    return modelled, notes


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

    Demand is measured PER PERIOD, against lane capacity that is also per
    period. A demand table stating a horizon would otherwise be added up and
    compared with one period's capacity, reporting every market as short by a
    factor equal to the length of the horizon — a fabricated crisis, and on the
    one screen whose job is to tell the user what is really wrong. The peak
    period is the one that has to be servable.
    """
    per_period: Dict[str, Dict[Any, float]] = {}
    sla_by_market: Dict[str, float | None] = {}
    for record in network.demands:
        bucket = per_period.setdefault(record.market_id, {})
        period = getattr(record, "period", 1)
        bucket[period] = bucket.get(period, 0.0) + record.quantity
        sla = getattr(record, "sla_days", None)
        if sla is not None:
            prior = sla_by_market.get(record.market_id)
            # The tightest SLA stated for the market binds it.
            sla_by_market[record.market_id] = sla if prior is None else min(prior, sla)

    demand_by_market: Dict[str, float] = {
        market_id: (max(buckets.values()) if buckets else 0.0)
        for market_id, buckets in per_period.items()
    }

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
    #
    # The history is read as a HORIZON, not as a single figure. Every period the
    # upload states is bucketed here; `choose_horizon` then decides how many of
    # them the model carries. Previously only `max(periods)` survived and every
    # other period was discarded after being counted for variability — so a
    # workbook stating three years of monthly demand was solved as one month,
    # the multi-period MILP built in `netgravity/optimization/periods.py` was
    # unreachable from any upload, and seasonality could not be asked about at
    # all because the model had never been told it existed.
    history = structure.get("demandHistory") or []

    #: {period label: {(market, product): quantity}}
    by_period: Dict[str, Dict[Tuple[str, str], float]] = {}
    for h in history:
        label = str(h.get("period") or "").strip()
        mid = str(h.get("marketId") or "").strip()
        pid = str(h.get("productId") or "").strip() or product_ids[0]
        qty = _as_float(h.get("quantity"))
        if not label or not mid or qty is None:
            continue
        bucket = by_period.setdefault(label, {})
        bucket[(mid, pid)] = bucket.get((mid, pid), 0.0) + qty

    # Calendar order, taken from the labels rather than from row order in the
    # file. A workbook is under no obligation to arrive sorted, and the sigma
    # window below is defined as "the most recent N periods" — which is a
    # different set of numbers if the rows happen to be ordered by market.
    observed_periods: List[str] = sorted(by_period)

    rows_per_period = max((len(b) for b in by_period.values()), default=0)
    modelled_periods, horizon_notes = choose_horizon(observed_periods, rows_per_period)
    assumptions.extend(horizon_notes)

    #: Integer index the engine uses <-> the label the client's file used.
    #: `DemandRecord.period` is an int everywhere in the engine, so the calendar
    #: has to be carried alongside rather than substituted into it.
    period_index: Dict[str, int] = {
        label: i + 1 for i, label in enumerate(modelled_periods)
    }
    period_labels: Dict[str, str] = {
        str(i + 1): label for i, label in enumerate(modelled_periods)
    }

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
    #
    # Measured over the whole observed history, not just the modelled horizon:
    # shortening the horizon for solve cost is a decision about what to OPTIMISE
    # over, and it should not also quietly narrow the evidence base for how
    # uncertain demand is.
    series: Dict[Tuple[str, str], List[float]] = {}
    for label in observed_periods:
        for key, qty in by_period[label].items():
            series.setdefault(key, []).append(qty)

    std_by_pair: Dict[Tuple[str, str], float] = {}
    for key, values in series.items():
        window = values[-_SIGMA_WINDOW:]
        if len(window) >= 2:
            std_by_pair[key] = statistics.stdev(window)

    demands: List[DemandRecord] = []
    markets_without_demand: List[str] = []
    markets_held_flat: List[str] = []
    sigma_pairs: set[Tuple[str, str]] = set()

    def add_demand(mid: str, pid: str, qty: float, period: int,
                   sla: float | None) -> None:
        record = DemandRecord(
            market_id=mid, product_id=pid, quantity=qty, period=period)
        if sla is not None and sla > 0:
            record.sla_days = sla
        # Variability describes the market-product PAIR, so the same sigma
        # travels with every period's row for that pair. It is not a property
        # of one month.
        sigma = std_by_pair.get((mid, pid))
        if sigma is not None and sigma > 0:
            record.std_dev = sigma
            sigma_pairs.add((mid, pid))
        demands.append(record)

    for m in markets:
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        sla = _as_float(m.get("slaDays"))

        # Every period this market appears in, at the quantity it recorded.
        # A pair absent from a period contributes no row for it, which is what
        # the data says: no demand was recorded that month. Inventing a zero or
        # carrying the previous month forward would both be a claim the upload
        # does not make.
        observed_rows = 0
        for label in modelled_periods:
            for (mkt, pid), qty in by_period[label].items():
                if mkt != mid or qty is None or qty <= 0:
                    continue
                add_demand(mid, pid, qty, period_index[label], sla)
                observed_rows += 1

        if observed_rows:
            continue

        # No history for this market. The markets sheet may still state a
        # single demand figure; it describes one period, and the only
        # defensible way to place it on a horizon is to hold it flat — which
        # is an assumption, so it is declared.
        qty = _as_float(m.get("demand"))
        if qty is None or qty <= 0:
            markets_without_demand.append(mid)
            continue
        for label in (modelled_periods or [""]):
            add_demand(mid, product_ids[0], qty,
                       period_index.get(label, 1), sla)
        if modelled_periods and len(modelled_periods) > 1:
            markets_held_flat.append(mid)

    if modelled_periods and len(modelled_periods) > 1:
        assumptions.append(
            f"Demand is modelled over {len(modelled_periods)} periods, "
            f"{modelled_periods[0]} to {modelled_periods[-1]}, kept split by "
            f"product. Each period binds its own demand and capacity, and stock "
            f"may be carried between them where a facility can hold it — so "
            f"cost, utilisation and service are horizon figures across those "
            f"{len(modelled_periods)} periods, not one period's."
        )
    elif modelled_periods:
        assumptions.append(
            f"The upload states a single demand period ({modelled_periods[0]}), "
            f"so that period is what is modelled. Seasonality cannot be "
            f"assessed from one period of data."
        )
    if markets_held_flat:
        assumptions.append(
            f"{len(markets_held_flat)} market(s) carried a single demand figure "
            f"with no period history and are held flat across the horizon: "
            f"{', '.join(markets_held_flat[:6])}"
            f"{'…' if len(markets_held_flat) > 6 else ''}. Their seasonality is "
            f"unknown, not zero."
        )
    if sigma_pairs:
        assumptions.append(
            f"Demand variability for {len(sigma_pairs)} market-product pair(s) is "
            f"the sample standard deviation of the last {_SIGMA_WINDOW} periods of "
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
        # So every downstream surface can say "2024-03" rather than "period 7".
        period_labels=period_labels,
    )

    # What a demand table stating several periods is solved as. Stated
    # explicitly rather than left to the schema default, because the assembler
    # is now the thing that decides how many periods there are — and a horizon
    # arriving at a solve whose policy nobody chose is how twelve months
    # silently becomes one.
    #
    # FULL_HORIZON is the choice that assumes least: it models what the data
    # says. The collapse policies remain available to a caller who explicitly
    # wants a cheaper single-period answer; none of them can tell a network that
    # carries the mean month from one that carries the peak.
    network.config.multi_period_policy = "FULL_HORIZON"

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
