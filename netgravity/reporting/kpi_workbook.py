"""
NetGravity — the KPI screen, as a workbook
=========================================
What a reader takes away from the KPI screen when they need it in a spreadsheet
rather than on a page.

WHY A WORKBOOK AND NOT THE CSV THIS REPLACES
--------------------------------------------
The screen carries five different populations — the network's own figures,
every facility's health, the capacity reading behind the utilisation charts,
the facility cost split, and the corridors. A CSV can hold exactly one of
those, so the button that wrote one had to pick, and it picked "the health
table plus the selected facility" — a file whose contents a reader had to open
to discover. Each population is a sheet here, named for what is on it.

EVERY FIGURE IS THE BACKEND'S OWN
---------------------------------
Nothing in this module computes a KPI. It reads the solved records — the same
`WarehouseHealthKPI` rows the table drew and the same authoritative KPI layer
the scorecard reads — and lays them out. The one thing it adds is the COVER,
which states what the figures are of: the project, the horizon, the lens, and
the filters that were applied when the reader pressed the button. A sheet of
numbers with no statement of scope is a sheet nobody can check later.

ABSENCE IS NOT ZERO
-------------------
A reading the solve did not produce is written as an empty cell, never as 0.
A spreadsheet is exactly where that distinction gets lost — a zero sums, an
empty cell does not — so it is enforced at the point of writing.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

#: Kearney purple, for the one row on each sheet that is a heading.
_HEAD_FILL = PatternFill("solid", fgColor="6B2FA0")
_HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
_TITLE_FONT = Font(bold=True, size=13, color="1A1A2E")
_LABEL_FONT = Font(bold=True, size=10, color="5A5A72")
_THIN = Side(style="thin", color="E2E8F0")
_BORDER = Border(bottom=_THIN)


def _num(value: Any) -> Optional[float]:
    """A number, or absence. Never a substituted zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _kpi_value(block: Dict[str, Any], metric_id: str) -> Optional[float]:
    """
    One authoritative KPI's value, and only when it is VALID.

    The KPI layer reports a status beside every figure. A record that is not
    VALID carries a value that means nothing, and writing it into a cell
    would launder it into a fact.
    """
    row = (block or {}).get(metric_id)
    if not isinstance(row, dict):
        return _num(row)
    if str(row.get("status") or "").upper() != "VALID":
        return None
    return _num(row.get("value"))


def _cell_safe(value: Any) -> Any:
    """
    Something a cell can actually hold.

    A `WarehouseHealthKPI` field is not always a scalar — `inventory_status`
    is a typed `SectionStatus`, and openpyxl refuses it outright rather than
    stringifying it, which took the whole export down on the first real row.
    Enums render as their value, other objects as their text, and None stays
    None so an absent reading stays an empty cell.
    """
    if value is None or isinstance(value, (str, int, float)):
        return value
    inner = getattr(value, "value", None)
    if isinstance(inner, (str, int, float)):
        return inner
    status = getattr(value, "status", None)
    if status is not None:
        return _cell_safe(status)
    return str(value)


def _sheet(wb: Workbook, title: str):
    ws = wb.create_sheet(title=title[:31])
    ws.sheet_view.showGridLines = False
    return ws


def _header(ws, row: int, columns: Sequence[str]) -> None:
    for col, name in enumerate(columns, start=1):
        cell = ws.cell(row=row, column=col, value=name)
        cell.fill = _HEAD_FILL
        cell.font = _HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _write(ws, row: int, values: Sequence[Any],
           number_format: Optional[Dict[int, str]] = None) -> None:
    """One row. `None` is written as an empty cell, deliberately."""
    for col, raw in enumerate(values, start=1):
        value = _cell_safe(raw)
        cell = ws.cell(row=row, column=col, value=value)
        cell.border = _BORDER
        if number_format and col in number_format and value is not None:
            cell.number_format = number_format[col]


def _autosize(ws, minimum: int = 10, maximum: int = 46) -> None:
    for column in ws.columns:
        longest = 0
        letter = get_column_letter(column[0].column)
        for cell in column:
            text = "" if cell.value is None else str(cell.value)
            longest = max(longest, min(len(text), maximum))
        ws.column_dimensions[letter].width = max(minimum, longest + 3)


# ─────────────────────────────────────────────────────────────
# The sheets
# ─────────────────────────────────────────────────────────────

def _cover(wb: Workbook, scope: Dict[str, Any]) -> None:
    """
    WHAT THESE FIGURES ARE OF.

    A workbook outlives the screen it came from, so the scope that produced it
    travels with it: which project, which snapshot, which horizon, which lens
    and which filters. Without those a reader six weeks later has a sheet of
    numbers and no way to reproduce them.
    """
    ws = wb.active
    ws.title = "Cover"
    ws.sheet_view.showGridLines = False

    ws["A1"] = "Netgravity — Network KPIs & Analytics"
    ws["A1"].font = _TITLE_FONT
    ws["A2"] = "by Kearney"
    ws["A2"].font = Font(size=10, color="8E8EA0")

    rows = [
        ("Project", scope.get("project_name") or scope.get("project_id") or ""),
        ("View", scope.get("lens_label") or ""),
        ("Filters applied", scope.get("filters") or "None"),
        ("Horizon", scope.get("horizon") or ""),
        ("Facilities in this view", scope.get("facility_count")),
        ("Corridors in this view", scope.get("corridor_count")),
        ("Snapshot", scope.get("snapshot_id") or ""),
        ("Execution", scope.get("execution_id") or ""),
        ("Analysis computed", scope.get("computed_at") or ""),
        ("Exported", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
    ]
    for i, (label, value) in enumerate(rows, start=4):
        ws.cell(row=i, column=1, value=label).font = _LABEL_FONT
        ws.cell(row=i, column=2, value=value)

    note = (
        "Every figure in this workbook is produced by the optimisation engine "
        "and read through the authoritative KPI layer. Nothing here is "
        "estimated, averaged or re-derived by the export. An empty cell means "
        "the solve produced no reading for that field — it does not mean zero."
    )
    ws.cell(row=len(rows) + 5, column=1, value=note).alignment = Alignment(
        wrap_text=True, vertical="top")
    ws.merge_cells(start_row=len(rows) + 5, start_column=1,
                   end_row=len(rows) + 8, end_column=6)
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 52


def _network_sheet(wb: Workbook, kpis: Dict[str, Any], currency: str) -> None:
    """The figures the scorecard states, with the status the layer gave them."""
    ws = _sheet(wb, "Network")
    ws["A1"] = "The network, as solved"
    ws["A1"].font = _TITLE_FONT
    _header(ws, 3, ["Metric", "Value", "Unit", "Status", "Owner"])

    row = 4
    for metric_id, record in sorted((kpis or {}).items()):
        if not isinstance(record, dict):
            continue
        _write(ws, row, [
            record.get("display_name") or metric_id,
            _num(record.get("value")),
            record.get("unit") or "",
            record.get("status") or "",
            record.get("authoritative_owner") or "",
        ], {2: "#,##0.####"})
        row += 1
    _autosize(ws)


def _facilities_sheet(wb: Workbook, rows: List[Any], currency: str) -> None:
    """Every site on screen, in the order the health table ranked them."""
    ws = _sheet(wb, "Facility health")
    ws["A1"] = "Every facility in this view"
    ws["A1"].font = _TITLE_FONT
    money = f"#,##0.00"
    _header(ws, 3, [
        "Facility ID", "Facility", "Role", "Region", "Open in this plan",
        "Health band", "Rated capacity / period", "Average throughput",
        "Peak throughput", "Busiest period", "Average utilisation %",
        "Peak utilisation %", "Headroom in peak (units)",
        "Periods observed", "Periods at or above threshold",
        f"Fixed cost ({currency})", f"Handling cost ({currency})",
        "Average stock held", "Peak stock held", "Stock reading",
    ])
    r = 4
    for k in rows:
        _write(ws, r, [
            getattr(k, "facility_id", None),
            getattr(k, "facility_name", None),
            getattr(k, "role", None),
            getattr(k, "region", None) or None,
            "Yes" if getattr(k, "is_open", False) else "No",
            getattr(k, "health_band", None),
            _num(getattr(k, "rated_capacity_per_period", None)),
            _num(getattr(k, "avg_throughput_units", None)),
            _num(getattr(k, "peak_throughput_units", None)),
            getattr(k, "peak_period", None) or None,
            _num(getattr(k, "avg_utilization_pct", None)),
            _num(getattr(k, "peak_utilization_pct", None)),
            _num(getattr(k, "headroom_units_peak", None)),
            _num(getattr(k, "periods_observed", None)),
            _num(getattr(k, "bottleneck_periods_count", None)),
            _num(getattr(k, "fixed_cost", None)),
            _num(getattr(k, "handling_cost", None)),
            _num(getattr(k, "avg_inventory_units", None)),
            _num(getattr(k, "peak_inventory_units", None)),
            getattr(k, "inventory_status", None) or None,
        ], {7: "#,##0", 8: "#,##0", 9: "#,##0", 11: "0.0", 12: "0.0",
            13: "#,##0", 16: money, 17: money, 18: "#,##0", 19: "#,##0"})
        r += 1
    _autosize(ws)


def _corridors_sheet(wb: Workbook, flows: List[Dict[str, Any]],
                     currency: str) -> None:
    """Every corridor the solve routed volume on."""
    ws = _sheet(wb, "Corridors")
    ws["A1"] = "Corridors in this view"
    ws["A1"].font = _TITLE_FONT
    _header(ws, 3, [
        "Origin", "Destination", "Flow (units)", "Flow (units / period)",
        f"Transport cost ({currency})", "Distance (km)", "Carbon (kg)",
    ])
    r = 4
    for f in flows or []:
        _write(ws, r, [
            f.get("origin_id"), f.get("destination_id"),
            _num(f.get("flow_units")), _num(f.get("flow_units_per_period")),
            _num(f.get("transport_cost")), _num(f.get("distance_km")),
            _num(f.get("carbon_kg")),
        ], {3: "#,##0", 4: "#,##0", 5: "#,##0.00", 6: "#,##0.0", 7: "#,##0.0"})
        r += 1
    _autosize(ws)


def _cost_sheet(wb: Workbook, report: Any, currency: str) -> None:
    """Where the facility spend sits, by site."""
    ws = _sheet(wb, "Facility cost")
    ws["A1"] = "Facility cost, by site"
    ws["A1"].font = _TITLE_FONT
    _header(ws, 3, ["Facility", f"Fixed ({currency})", f"Handling ({currency})",
                    f"Total attributed ({currency})"])
    r = 4
    money = "#,##0.00"
    for k in (getattr(report, "health_kpis", None) or []):
        fixed = _num(getattr(k, "fixed_cost", None))
        handling = _num(getattr(k, "handling_cost", None))
        total = None if fixed is None and handling is None else (
            (fixed or 0.0) + (handling or 0.0))
        _write(ws, r, [
            getattr(k, "facility_name", None) or getattr(k, "facility_id", None),
            fixed, handling, total,
        ], {2: money, 3: money, 4: money})
        r += 1

    # WHAT DOES NOT BELONG TO A SITE, said rather than left as a gap.
    r += 1
    ws.cell(row=r, column=1, value=(
        "Inventory holding is decided for the network as a whole rather than "
        "per site, so these rows do not sum to the network's total cost. The "
        "network figure is on the Network sheet."
    )).alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=1, end_row=r + 2, end_column=4)
    _autosize(ws)


def build_kpi_workbook(*, kpis: Dict[str, Any], report: Any,
                       flows: List[Dict[str, Any]], scope: Dict[str, Any],
                       currency: str = "") -> bytes:
    """
    The KPI screen as an .xlsx, in memory.

    `report` is a `WarehouseDeepDiveReport` already narrowed to what the
    screen was showing — the selection is applied by the caller, so this
    module writes what it is given and decides nothing about scope.
    """
    wb = Workbook()
    _cover(wb, scope)
    _network_sheet(wb, kpis, currency)
    _facilities_sheet(wb, getattr(report, "health_kpis", None) or [], currency)
    _cost_sheet(wb, report, currency)
    _corridors_sheet(wb, flows, currency)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


__all__ = ["build_kpi_workbook"]
