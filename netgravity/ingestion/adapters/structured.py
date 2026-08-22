"""
NetGravity — Structured Source Adapter
=======================================
Reads clean, known-format exports (ERP / WMS / TMS, or our own mock CSVs)
and converts them into the engine's record types.

NO AI IS USED HERE. This path is pure deterministic parsing plus row-level
validation — it is the backbone the AI-assisted adapters plug into, and it
must work without an API key.

Expected files in a source directory:
    facilities.csv          plants, DCs, candidate sites
    markets.csv             demand zones (become MARKET-role facilities)
    products.csv            product master
    demand.csv              demand per market/product/period
    lanes.csv               transport lanes
    historical_volume.csv   optional — observed history for forecasting
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from netgravity.ingestion.field_aliases import (
    DAYS_PER_MONTH,
    FACILITY_ALIASES,
    DEMAND_ALIASES,
    DEMAND_LOOKUP,
    FACILITY_LOOKUP,
    HISTORY_LOOKUP,
    LANE_LOOKUP,
    MARKET_ALIASES,
    MARKET_LOOKUP,
    PRODUCT_LOOKUP,
    detect_native_period,
    infer_period_from_name,
    monthly_conversion_factor,
    rename_rows,
)
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity
from netgravity.ingestion.validation import row_checks as rc
from netgravity.schemas.network import (
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    ProductRecord,
    TransportMode,
)

VALID_ROLES: Set[str] = {r.value for r in NodeRole}
VALID_STATUS: Set[str] = {s.value for s in FacilityStatus}
VALID_MODES: Set[str] = {m.value for m in TransportMode}

FACILITIES_FILE = "facilities.csv"
MARKETS_FILE = "markets.csv"
PRODUCTS_FILE = "products.csv"
DEMAND_FILE = "demand.csv"
LANES_FILE = "lanes.csv"
HISTORY_FILE = "historical_volume.csv"


def _normalise_header(name: object) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().upper() in {"TRUE", "T", "YES", "Y", "1"}


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _read_csv_bytes(data: bytes) -> List[Dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


# ---------------------------------------------------------------------------
# Individual file parsers
# ---------------------------------------------------------------------------

def parse_facilities(rows: List[Dict[str, str]],
                     file: str = FACILITIES_FILE) -> Tuple[List[FacilityRecord], FileResult]:
    # Capacity and throughput are per-period quantities, exactly like demand,
    # and must land on the SAME period or the model compares monthly demand to
    # daily capacity and reports a false infeasibility. The workbook is
    # ambiguous here — Capacity_Units is described as "units/day or units/year"
    # with a "units/month" example — so we convert whatever the column NAME
    # states, and where it states nothing we assume MONTH and say so.
    cap_periods = {
        f: detect_native_period(rows, FACILITY_ALIASES, f, default=None)
        for f in ("capacity_units_per_period", "min_throughput_per_period",
                  "observed_throughput")
    }

    rows = rename_rows(rows, FACILITY_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[FacilityRecord] = []
    ids: List[str] = []

    cap_factors: Dict[str, float] = {}
    for fname, period in cap_periods.items():
        cap_factors[fname] = monthly_conversion_factor(period) if period else 1.0
        if period and period != "MONTH":
            result.issues.append(RowIssue(
                severity=Severity.INFO, code="R-020",
                message=f"'{fname}' column is {period}-native; converted to "
                        f"MONTH by x{cap_factors[fname]:g} to match demand.",
                source_file=file,
            ))

    for n, row in enumerate(rows, start=2):   # start=2: row 1 is the header in Excel
        issues: List[RowIssue] = []
        issues += rc.require(row, ["facility_id", "facility_name", "role"], file, n)

        role_raw = (row.get("role") or "").strip().upper()
        status_raw = (row.get("status") or "EXISTING").strip().upper()
        issues += rc.check_enum(role_raw, VALID_ROLES, "role", file, n)
        issues += rc.check_enum(status_raw, VALID_STATUS, "status", file, n)

        lat, i = rc.as_float(row, "latitude", file, n, allow_negative=True); issues += i
        lon, i = rc.as_float(row, "longitude", file, n, allow_negative=True); issues += i
        issues += rc.check_coordinates(lat, lon, file, n)

        cap, i = rc.as_float(row, "capacity_units_per_period", file, n, default=1e12); issues += i
        thr, i = rc.as_float(row, "observed_throughput", file, n, default=None); issues += i
        # Never rescale the "effectively unlimited" sentinel.
        if cap is not None and cap < 1e11:
            cap *= cap_factors["capacity_units_per_period"]
        if thr is not None:
            thr *= cap_factors["observed_throughput"]
        fixed, i = rc.as_float(row, "fixed_cost_per_year", file, n, default=0.0); issues += i
        handling, i = rc.as_float(row, "handling_cost_per_unit", file, n, default=0.0); issues += i
        lead, i = rc.as_float(row, "replenishment_lead_time_days", file, n, default=1.0); issues += i
        # Aliased in the workbook (Opening_Cost / Closure_Cost / CapEx /
        # Min_Throughput_Per_Period) but previously never read — FacilityRecord
        # has had fields for all four since v1.1; they were just never wired up.
        opening, i = rc.as_float(row, "opening_cost", file, n, default=0.0); issues += i
        closure, i = rc.as_float(row, "closure_cost", file, n, default=0.0); issues += i
        capex_val, i = rc.as_float(row, "capex", file, n, default=0.0); issues += i
        min_thr, i = rc.as_float(row, "min_throughput_per_period", file, n, default=0.0); issues += i
        if min_thr is not None:
            min_thr *= cap_factors["min_throughput_per_period"]

        fid = (row.get("facility_id") or "").strip()
        issues += rc.check_capacity_vs_throughput(cap, thr, fid, file, n)

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        ids.append(fid)
        records.append(FacilityRecord(
            id=fid,
            name=(row.get("facility_name") or fid).strip(),
            role=NodeRole(role_raw),
            status=FacilityStatus(status_raw),
            latitude=lat,
            longitude=lon,
            capacity_units_per_period=cap if cap is not None else 1e12,
            fixed_cost_per_year=fixed or 0.0,
            handling_cost_per_unit=handling or 0.0,
            is_mandatory=_truthy(row.get("is_mandatory")),
            is_closable=_truthy(row.get("is_closable"), default=True),
            replenishment_lead_time_days=lead if lead is not None else 1.0,
            opening_cost=opening or 0.0,
            closure_cost=closure or 0.0,
            capex=capex_val or 0.0,
            min_throughput_per_period=min_thr or 0.0,
            region=(row.get("region") or "").strip() or None,
            country=(row.get("country") or "").strip() or "India",
            tags=[t for t in [(row.get("city") or "").strip(),
                              (row.get("state") or "").strip()] if t],
        ))
        result.rows_accepted += 1

    result.issues.extend(rc.check_duplicates(ids, file, "facility_id"))
    return records, result


def parse_markets(rows: List[Dict[str, str]],
                  file: str = MARKETS_FILE) -> Tuple[List[FacilityRecord], FileResult]:
    """Markets become MARKET-role facilities — the engine models them as nodes."""
    rows = rename_rows(rows, MARKET_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[FacilityRecord] = []
    ids: List[str] = []

    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        issues += rc.require(row, ["market_id", "market_name"], file, n)

        lat, i = rc.as_float(row, "latitude", file, n, allow_negative=True); issues += i
        lon, i = rc.as_float(row, "longitude", file, n, allow_negative=True); issues += i
        issues += rc.check_coordinates(lat, lon, file, n)

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        mid = (row.get("market_id") or "").strip()
        ids.append(mid)
        records.append(FacilityRecord(
            id=mid,
            name=(row.get("market_name") or mid).strip(),
            role=NodeRole.MARKET,
            status=FacilityStatus.EXISTING,
            latitude=lat,
            longitude=lon,
            # Markets are demand sinks: no capacity ceiling, never an open/close decision
            capacity_units_per_period=1e12,
            is_closable=False,
            region=(row.get("region") or "").strip() or None,
            country="India",
            tags=[t for t in [(row.get("priority_tier") or "").strip(),
                              (row.get("state") or "").strip()] if t],
        ))
        result.rows_accepted += 1

    result.issues.extend(rc.check_duplicates(ids, file, "market_id"))
    return records, result


def parse_products(rows: List[Dict[str, str]],
                   file: str = PRODUCTS_FILE) -> Tuple[List[ProductRecord], FileResult]:
    rows = rename_rows(rows, PRODUCT_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[ProductRecord] = []
    ids: List[str] = []

    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        issues += rc.require(row, ["product_id", "product_name"], file, n)

        weight, i = rc.as_float(row, "weight_kg", file, n, default=1.0); issues += i
        volume, i = rc.as_float(row, "volume_m3", file, n, default=0.001); issues += i
        value, i = rc.as_float(row, "unit_value", file, n, default=0.0); issues += i
        holding, i = rc.as_float(row, "holding_rate", file, n, default=0.25); issues += i

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        pid = (row.get("product_id") or "").strip()
        ids.append(pid)
        records.append(ProductRecord(
            id=pid,
            name=(row.get("product_name") or pid).strip(),
            unit=(row.get("unit") or "units").strip(),
            weight_kg=weight if weight is not None else 1.0,
            volume_m3=volume if volume is not None else 0.001,
            unit_value=value or 0.0,
            holding_rate=holding if holding is not None else 0.25,
        ))
        result.rows_accepted += 1

    result.issues.extend(rc.check_duplicates(ids, file, "product_id"))
    return records, result


def parse_demand(rows: List[Dict[str, str]], market_ids: Set[str],
                 product_ids: Set[str],
                 file: str = DEMAND_FILE) -> Tuple[List[DemandRecord], FileResult]:
    # Detect the quantity column's native period BEFORE renaming discards the
    # original header — e.g. 'Daily_Demand_Units' is DAY-native even though
    # it renames to the period-agnostic 'quantity'. The engine standardizes
    # on MONTH (OptimizationConfig.cost_period default), so every quantity is
    # converted to MONTH here, once, with the factor recorded for audit.
    native_period = detect_native_period(rows, DEMAND_ALIASES)
    qty_factor = monthly_conversion_factor(native_period)
    # std_dev is scaled from ITS OWN column name, not from the quantity
    # column's. The workbook mixes periods within one sheet: Daily_Demand_Units
    # is daily, but the Demand_Variability example reads "Std dev 200
    # units/month". Inheriting the quantity period would inflate variability
    # ~5.5x, and variability drives safety stock and inventory cost.
    # When it IS daily, scaling is sqrt(N) not N: summing N independent daily
    # draws scales the mean by N but the deviation by sqrt(N).
    std_period = detect_native_period(rows, DEMAND_ALIASES, "std_dev", default=None)
    std_factor = monthly_conversion_factor(std_period) ** 0.5 if std_period else 1.0

    rows = rename_rows(rows, DEMAND_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[DemandRecord] = []

    if qty_factor != 1.0:
        result.issues.append(RowIssue(
            severity=Severity.INFO, code="R-020",
            message=f"quantity column is {native_period}-native; converted "
                    f"to MONTH (engine convention) by x{qty_factor:g} "
                    f"(std_dev x{std_factor:.4g}, from its own column name). "
                    f"Assumes {DAYS_PER_MONTH} days/month.",
            source_file=file,
        ))

    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        issues += rc.require(row, ["market_id", "product_id", "quantity"], file, n)
        issues += rc.check_reference(row.get("market_id"), market_ids, "market_id", file, n)
        issues += rc.check_reference(row.get("product_id"), product_ids, "product_id", file, n)

        qty, i = rc.as_float(row, "quantity", file, n); issues += i
        std, i = rc.as_float(row, "std_dev", file, n, default=0.0); issues += i
        sla, i = rc.as_float(row, "sla_days", file, n, default=None); issues += i
        svc, i = rc.as_float(row, "service_level", file, n, default=0.95); issues += i
        period, i = rc.as_float(row, "period", file, n, default=1.0); issues += i
        priority, i = rc.as_float(row, "priority", file, n, default=1.0); issues += i

        if qty is not None:
            qty = qty * qty_factor
        if std is not None:
            std = std * std_factor

        # R-009 — service_level unit repair.
        #
        # A value like 95 clearly means 95%, not 9500%. Rejecting the row would
        # silently remove this market's demand from the network, and the
        # optimizer would then return a confident "optimal" answer to the wrong
        # problem — a far worse outcome than repairing an obvious unit error.
        # So: repair loudly inside the plausible range, reject only what cannot
        # be interpreted at all.
        if svc is not None and svc > 1.0:
            if svc <= 100.0:
                corrected = svc / 100.0
                issues.append(RowIssue(
                    severity=Severity.WARNING, code="R-009",
                    message=f"service_level {svc:g} looks like a percentage; "
                            f"interpreted as {corrected:.4g}. State it as a "
                            f"fraction in the source to remove this warning.",
                    source_file=file, row_number=n, column="service_level",
                    raw_value=str(svc),
                ))
                svc = corrected
            else:
                issues.append(RowIssue(
                    severity=Severity.ERROR, code="R-009",
                    message=f"service_level {svc:g} cannot be interpreted — "
                            f"expected a fraction in [0, 1] or a percentage in "
                            f"(1, 100]",
                    source_file=file, row_number=n, column="service_level",
                    raw_value=str(svc),
                ))

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        records.append(DemandRecord(
            market_id=(row.get("market_id") or "").strip(),
            product_id=(row.get("product_id") or "").strip(),
            period=int(period or 1),
            quantity=qty or 0.0,
            std_dev=std or 0.0,
            sla_days=sla,
            service_level=svc if svc is not None else 0.95,
            priority=int(priority or 1),
        ))
        result.rows_accepted += 1

    return records, result


def parse_demand_from_markets(rows: List[Dict[str, str]], market_ids: Set[str],
                              product_ids: Set[str],
                              file: str = MARKETS_FILE
                              ) -> Tuple[List[DemandRecord], FileResult]:
    """
    Derive demand records from a demand-zones sheet that carries demand columns.

    The client-facing workbook defines ONE 'Demand Zones' sheet holding both the
    zone attributes and Daily_Demand_Units, rather than a separate demand table.
    Rather than force clients to split their file, we read demand straight off
    the zones sheet when no dedicated demand file is supplied.

    With a single product in the catalogue, demand is attributed to it. With
    several, a Product_ID column is required, because splitting a zone's total
    across products would be a guess — and a guess here silently changes the
    optimum.

    Same MONTH normalization as parse_demand(): 'Daily_Demand_Units' is
    DAY-native and is converted here, once, with the factor recorded.
    """
    native_period = detect_native_period(rows, MARKET_ALIASES)
    qty_factor = monthly_conversion_factor(native_period)
    std_period = detect_native_period(rows, MARKET_ALIASES, "std_dev", default=None)
    std_factor = monthly_conversion_factor(std_period) ** 0.5 if std_period else 1.0

    rows = rename_rows(rows, MARKET_LOOKUP)
    result = FileResult(source_file=f"{file} (demand columns)",
                        adapter="structured")

    has_demand = any(r.get("quantity") not in (None, "") for r in rows)
    if not has_demand:
        return [], result

    result.rows_read = len(rows)
    products = sorted(product_ids)
    single_product = products[0] if len(products) == 1 else None

    if qty_factor != 1.0:
        result.issues.append(RowIssue(
            severity=Severity.INFO, code="R-020",
            message=f"quantity column is {native_period}-native; converted "
                    f"to MONTH (engine convention) by x{qty_factor:g} "
                    f"(std_dev x{std_factor:.4g}, from its own column name). "
                    f"Assumes {DAYS_PER_MONTH} days/month.",
            source_file=file,
        ))

    records: List[DemandRecord] = []
    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        qty, i = rc.as_float(row, "quantity", file, n); issues += i
        if qty is None:
            result.issues.extend(issues)
            continue
        qty = qty * qty_factor

        product_id = (row.get("product_id") or single_product or "").strip()
        if not product_id:
            issues.append(RowIssue(
                severity=Severity.ERROR, code="R-019",
                message="demand is on the zones sheet and the catalogue holds "
                        "multiple products, so a Product_ID column is required "
                        "— splitting the total across products would be a guess",
                source_file=file, row_number=n,
            ))
        else:
            issues += rc.check_reference(product_id, product_ids, "product_id", file, n)
        issues += rc.check_reference(row.get("market_id"), market_ids,
                                     "market_id", file, n)

        std, i = rc.as_float(row, "std_dev", file, n, default=0.0); issues += i
        if std is not None:
            std = std * std_factor
        sla, i = rc.as_float(row, "sla_days", file, n, default=None); issues += i

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        records.append(DemandRecord(
            market_id=(row.get("market_id") or "").strip(),
            product_id=product_id,
            period=1,
            quantity=qty,
            std_dev=std or 0.0,
            sla_days=sla,
        ))
        result.rows_accepted += 1

    return records, result


def parse_lanes(rows: List[Dict[str, str]], node_ids: Set[str],
                file: str = LANES_FILE) -> Tuple[List[LaneRecord], FileResult]:
    # LaneRecord.lane_capacity is a PER-PERIOD flow cap, enforced as a hard
    # upper bound in the MILP. The workbook's Capacity_Per_Trip is per
    # SHIPMENT ("maximum a single shipment can carry"). Loading one into the
    # other caps a lane at, say, 500 units/month when it really moves 500 per
    # trip every day = 15,000/month, which makes a healthy network infeasible.
    # Converting needs Transit_Frequency (trips per period); where that is
    # absent we leave the lane UNCAPACITATED and say so, because a wrong cap
    # is worse than no cap — no cap loses a real constraint, a wrong cap
    # invents a false one and silently changes the answer.
    per_trip_column = any(
        _normalise_header(k) == "capacitypertrip"
        for k in (rows[0].keys() if rows else []) if k is not None
    )

    rows = rename_rows(rows, LANE_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[LaneRecord] = []

    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        issues += rc.require(row, ["origin_id", "destination_id", "rate_per_unit"], file, n)
        issues += rc.check_reference(row.get("origin_id"), node_ids, "origin_id", file, n)
        issues += rc.check_reference(row.get("destination_id"), node_ids, "destination_id", file, n)

        mode_raw = (row.get("mode") or "ROAD").strip().upper()
        issues += rc.check_enum(mode_raw, VALID_MODES, "mode", file, n)

        rate, i = rc.as_float(row, "rate_per_unit", file, n); issues += i
        dist, i = rc.as_float(row, "distance_km", file, n, default=0.0); issues += i
        lead, i = rc.as_float(row, "lead_time_days", file, n, default=1.0); issues += i
        cap, i = rc.as_float(row, "lane_capacity", file, n, default=None); issues += i
        if cap is not None and per_trip_column:
            trips, ti = rc.as_float(row, "transit_frequency", file, n, default=None)
            issues += ti
            if trips and trips > 0:
                cap = cap * trips
            else:
                issues.append(RowIssue(
                    severity=Severity.WARNING, code="R-022",
                    message=f"Capacity_Per_Trip={cap:g} is per shipment, but the "
                            f"model needs a per-period cap and Transit_Frequency "
                            f"(trips per month) is missing. Lane left "
                            f"uncapacitated rather than capped at a wrong value.",
                    source_file=file, row_number=n, column="Capacity_Per_Trip",
                ))
                cap = None
        # Aliased (Emission_Factor_Override) but previously never read — the
        # field has existed on LaneRecord since v1.1.
        emis, i = rc.as_float(row, "emission_factor_override", file, n, default=None); issues += i

        issues += rc.check_lane_plausibility(dist, lead, file, n)

        origin = (row.get("origin_id") or "").strip()
        dest = (row.get("destination_id") or "").strip()
        if origin and origin == dest:
            issues.append(RowIssue(
                severity=Severity.ERROR, code="R-008",
                message=f"lane origin and destination are the same node '{origin}'",
                source_file=file, row_number=n, column="destination_id",
            ))

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        records.append(LaneRecord(
            origin_id=origin,
            destination_id=dest,
            mode=TransportMode(mode_raw),
            rate_per_unit=rate or 0.0,
            distance_km=dist or 0.0,
            lead_time_days=lead if lead is not None else 1.0,
            lane_capacity=cap,
            emission_factor_override=emis,
            is_active_baseline=_truthy(row.get("is_active_baseline"), default=True),
        ))
        result.rows_accepted += 1

    return records, result


def parse_history(rows: List[Dict[str, str]], node_ids: Set[str],
                  file: str = HISTORY_FILE) -> Tuple[List[Dict[str, Any]], FileResult]:
    """
    Historical volume is NOT part of CanonicalNetwork — the optimizer does not
    consume it. It is retained for the forecasting layer and written to the
    standardized zone, so it is validated and reported like everything else.
    """
    rows = rename_rows(rows, HISTORY_LOOKUP)
    result = FileResult(source_file=file, adapter="structured", rows_read=len(rows))
    records: List[Dict[str, Any]] = []

    for n, row in enumerate(rows, start=2):
        issues: List[RowIssue] = []
        issues += rc.require(row, ["node_id", "period", "volume"], file, n)
        issues += rc.check_reference(row.get("node_id"), node_ids, "node_id", file, n)

        vol, i = rc.as_float(row, "volume", file, n); issues += i
        orders, i = rc.as_float(row, "order_count", file, n, default=None); issues += i

        result.issues.extend(issues)
        if any(x.severity == Severity.ERROR for x in issues):
            result.rows_rejected += 1
            continue

        records.append({
            "node_id": (row.get("node_id") or "").strip(),
            "period": (row.get("period") or "").strip(),
            "volume": vol,
            "order_count": orders,
            "promotion_flag": _truthy(row.get("promotion_flag")),
            "holiday_flag": _truthy(row.get("holiday_flag")),
            "observed_or_planned": (row.get("observed_or_planned") or "OBSERVED").strip().upper(),
        })
        result.rows_accepted += 1

    return records, result


# ---------------------------------------------------------------------------
# Directory-level entry point
# ---------------------------------------------------------------------------

class StructuredSource:
    """Everything parsed out of one structured source directory."""

    def __init__(self) -> None:
        self.facilities: List[FacilityRecord] = []
        self.products: List[ProductRecord] = []
        self.demands: List[DemandRecord] = []
        self.lanes: List[LaneRecord] = []
        self.history: List[Dict[str, Any]] = []
        self.results: List[FileResult] = []


def ingest_directory(source_dir: Path) -> StructuredSource:
    """
    Parse every recognised CSV in a directory.

    Files are processed in dependency order so that referential checks have
    the master IDs they need: facilities and markets first, then products,
    then demand and lanes which reference both.
    """
    source_dir = Path(source_dir)
    src = StructuredSource()

    facilities: List[FacilityRecord] = []

    fac_path = source_dir / FACILITIES_FILE
    if fac_path.exists():
        recs, res = parse_facilities(_read_csv(fac_path))
        facilities += recs
        src.results.append(res)

    mkt_path = source_dir / MARKETS_FILE
    if mkt_path.exists():
        recs, res = parse_markets(_read_csv(mkt_path))
        facilities += recs
        src.results.append(res)

    src.facilities = facilities
    node_ids = {f.id for f in facilities}
    market_ids = {f.id for f in facilities if f.role == NodeRole.MARKET}

    prod_path = source_dir / PRODUCTS_FILE
    if prod_path.exists():
        recs, res = parse_products(_read_csv(prod_path))
        src.products = recs
        src.results.append(res)
    product_ids = {p.id for p in src.products}

    # Demand may arrive either as its own file, or as columns on the demand-zones
    # sheet — the workbook (NetGravity_Input_Data_Fields.xlsx, sheet 2) puts
    # Daily_Demand_Units on the zones sheet itself. Support both, preferring an
    # explicit demand file when one is present.
    dem_path = source_dir / DEMAND_FILE
    if dem_path.exists():
        recs, res = parse_demand(_read_csv(dem_path), market_ids, product_ids)
        src.demands = recs
        src.results.append(res)
    elif mkt_path.exists() and product_ids:
        recs, res = parse_demand_from_markets(
            _read_csv(mkt_path), market_ids, product_ids
        )
        if recs or res.rows_read:
            src.demands = recs
            src.results.append(res)

    lane_path = source_dir / LANES_FILE
    if lane_path.exists():
        recs, res = parse_lanes(_read_csv(lane_path), node_ids)
        src.lanes = recs
        src.results.append(res)

    hist_path = source_dir / HISTORY_FILE
    if hist_path.exists():
        recs, res = parse_history(_read_csv(hist_path), node_ids)
        src.history = recs
        src.results.append(res)

    return src
