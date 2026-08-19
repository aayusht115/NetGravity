"""
NetGravity — Row-Level (Pre-Assembly) Validation
=================================================
Runs on RAW rows, before a CanonicalNetwork exists.

Complements — does not duplicate — netgravity/validation/checks.py, which
runs AFTER assembly and answers "will this network solve?" (V-001..V-014).
This layer answers "is this row even usable?" (R-001..R-012).

CODES
-----
  R-001  required field missing
  R-002  value is not numeric
  R-003  negative value where only >= 0 is meaningful
  R-004  latitude / longitude out of range
  R-005  unknown enum value (role, status, mode)
  R-006  referential: referenced ID not found
  R-007  duplicate primary key
  R-008  implausible physical value (zero lead time on a long lane, etc.)
  R-009  suspicious unit (value magnitude implies wrong unit)
  R-010  date unparseable
  R-011  capacity below observed throughput
  R-012  demand present for a node that is not a market
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from netgravity.ingestion.schemas.ingest_result import RowIssue, Severity

# Physical plausibility bounds (India-focused prototype, generous on purpose)
INDIA_LAT_RANGE = (6.0, 38.0)
INDIA_LON_RANGE = (68.0, 98.0)
LONG_LANE_KM = 200.0          # beyond this, a 0-day lead time is not credible
MAX_PLAUSIBLE_KM = 5000.0     # longer than India end-to-end by a wide margin


def _issue(sev: Severity, code: str, msg: str, file: str,
           row: Optional[int] = None, col: Optional[str] = None,
           raw: Any = None) -> RowIssue:
    return RowIssue(
        severity=sev, code=code, message=msg, source_file=file,
        row_number=row, column=col,
        raw_value=None if raw is None else str(raw),
    )


def require(row: Dict[str, Any], fields: Iterable[str], file: str,
            row_no: int) -> List[RowIssue]:
    """R-001 — every named field must be present and non-blank."""
    issues = []
    for f in fields:
        value = row.get(f)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            issues.append(_issue(
                Severity.ERROR, "R-001",
                f"required field '{f}' is missing or blank",
                file, row_no, f, value,
            ))
    return issues


def as_float(row: Dict[str, Any], field: str, file: str, row_no: int,
             *, allow_negative: bool = False,
             default: Optional[float] = None) -> tuple[Optional[float], List[RowIssue]]:
    """R-002 / R-003 — parse a numeric field, reporting rather than raising."""
    issues: List[RowIssue] = []
    raw = row.get(field)

    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return default, issues

    try:
        # Tolerate thousands separators and stray currency symbols in
        # human-maintained spreadsheets.
        cleaned = str(raw).replace(",", "").replace("Rs.", "").replace("₹", "").strip()
        value = float(cleaned)
    except (TypeError, ValueError):
        issues.append(_issue(
            Severity.ERROR, "R-002",
            f"'{field}' is not numeric", file, row_no, field, raw,
        ))
        return default, issues

    if value < 0 and not allow_negative:
        issues.append(_issue(
            Severity.ERROR, "R-003",
            f"'{field}' must be >= 0", file, row_no, field, raw,
        ))
        return default, issues

    return value, issues


def check_coordinates(lat: Optional[float], lon: Optional[float], file: str,
                      row_no: int, *, geo_bounds: bool = True) -> List[RowIssue]:
    """R-004 — coordinates must be globally valid, and (warn) inside India."""
    issues: List[RowIssue] = []
    if lat is None or lon is None:
        return issues

    if not (-90.0 <= lat <= 90.0):
        issues.append(_issue(Severity.ERROR, "R-004",
                             f"latitude {lat} outside [-90, 90]", file, row_no, "latitude", lat))
    if not (-180.0 <= lon <= 180.0):
        issues.append(_issue(Severity.ERROR, "R-004",
                             f"longitude {lon} outside [-180, 180]", file, row_no, "longitude", lon))

    if geo_bounds and not issues:
        in_india = (INDIA_LAT_RANGE[0] <= lat <= INDIA_LAT_RANGE[1]
                    and INDIA_LON_RANGE[0] <= lon <= INDIA_LON_RANGE[1])
        if not in_india:
            issues.append(_issue(
                Severity.WARNING, "R-004",
                f"coordinates ({lat}, {lon}) fall outside India — check the source file",
                file, row_no, "latitude/longitude",
            ))
    return issues


def check_enum(value: Optional[str], allowed: Set[str], field: str, file: str,
               row_no: int) -> List[RowIssue]:
    """R-005 — categorical fields must use a known value."""
    if value is None or str(value).strip() == "":
        return []
    if str(value).strip().upper() not in allowed:
        return [_issue(
            Severity.ERROR, "R-005",
            f"'{field}' value '{value}' is not one of {sorted(allowed)}",
            file, row_no, field, value,
        )]
    return []


def check_reference(value: Optional[str], known_ids: Set[str], field: str,
                    file: str, row_no: int) -> List[RowIssue]:
    """R-006 — a referenced ID must exist in the master it points at."""
    if value is None or str(value).strip() == "":
        return []
    if str(value).strip() not in known_ids:
        return [_issue(
            Severity.ERROR, "R-006",
            f"'{field}' references unknown ID '{value}'",
            file, row_no, field, value,
        )]
    return []


def check_duplicates(ids: List[str], file: str, field: str = "id") -> List[RowIssue]:
    """R-007 — primary keys must be unique within a file."""
    seen: Set[str] = set()
    dupes: Set[str] = set()
    for i in ids:
        if i in seen:
            dupes.add(i)
        seen.add(i)
    return [
        _issue(Severity.ERROR, "R-007", f"duplicate {field} '{d}'", file, None, field, d)
        for d in sorted(dupes)
    ]


def check_lane_plausibility(distance_km: Optional[float], lead_time_days: Optional[float],
                            file: str, row_no: int) -> List[RowIssue]:
    """
    R-008 — physical sanity on a lane.

    A long corridor with zero transit time is the classic sign of a missing
    column or a unit mix-up, and it silently makes every SLA constraint pass.
    """
    issues: List[RowIssue] = []
    if distance_km is None:
        return issues

    if distance_km > MAX_PLAUSIBLE_KM:
        issues.append(_issue(
            Severity.WARNING, "R-008",
            f"distance {distance_km:,.0f} km is implausibly long for a domestic lane",
            file, row_no, "distance_km", distance_km,
        ))

    if lead_time_days is not None and lead_time_days <= 0 and distance_km > LONG_LANE_KM:
        issues.append(_issue(
            Severity.WARNING, "R-008",
            f"lead_time_days is {lead_time_days} for a {distance_km:,.0f} km lane — "
            f"zero transit time would make every SLA check pass automatically",
            file, row_no, "lead_time_days", lead_time_days,
        ))
    return issues


def check_capacity_vs_throughput(capacity: Optional[float], throughput: Optional[float],
                                 facility_id: str, file: str,
                                 row_no: int) -> List[RowIssue]:
    """R-011 — observed throughput above stated capacity means one of them is wrong."""
    if capacity is None or throughput is None or capacity <= 0:
        return []
    if throughput > capacity:
        return [_issue(
            Severity.WARNING, "R-011",
            f"facility '{facility_id}' observed throughput {throughput:,.0f} exceeds "
            f"stated capacity {capacity:,.0f} ({throughput / capacity:.0%} utilisation)",
            file, row_no, "observed_throughput", throughput,
        )]
    return []
