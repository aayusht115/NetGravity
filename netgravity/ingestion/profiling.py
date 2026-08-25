"""Deterministic column profiling for mapping and consultant review."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional, Sequence

from netgravity.ingestion.field_aliases import infer_period_from_name
from netgravity.ingestion.schemas.field_mapping import ColumnProfile
from netgravity.ingestion.sources.base import RecordSet


_UNIT_HINTS = (
    ("kilogram", "kg"), ("kgs", "kg"), ("kg", "kg"),
    ("tonnes", "tonne"), ("tonne", "tonne"), ("tons", "tonne"),
    ("pallets", "pallet"), ("pallet", "pallet"),
    ("cartons", "carton"), ("carton", "carton"),
    ("cases", "case"), ("case", "case"),
    ("units", "unit"), ("unit", "unit"),
    ("percent", "%"), ("pct", "%"),
    ("inr", "INR"), ("rupees", "INR"),
)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        cleaned = str(value).replace(",", "").replace("Rs.", "").replace("₹", "").strip()
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _data_type(values: Sequence[Any]) -> str:
    if not values:
        return "empty"
    if all(_number(v) is not None for v in values):
        return "numeric"
    if all(isinstance(v, (date, datetime)) for v in values):
        return "date"
    lowered = {str(v).strip().lower() for v in values}
    if lowered and lowered <= {"true", "false", "yes", "no", "y", "n", "0", "1"}:
        return "boolean"
    return "text"


def _unit_hint(column: str) -> Optional[str]:
    words = set("".join(ch if ch.isalnum() else " " for ch in column.lower()).split())
    for token, unit in _UNIT_HINTS:
        if token in words:
            return unit
    return None


def profile_column(record_set: RecordSet, column: str,
                   known_ids: Sequence[str] = ()) -> ColumnProfile:
    raw = [row.get(column) for row in record_set.rows]
    values = [v for v in raw if v is not None and str(v).strip() != ""]
    numeric = [n for n in (_number(v) for v in values) if n is not None]
    known = {str(v).strip().lower() for v in known_ids}
    matches = sum(1 for v in values if str(v).strip().lower() in known)

    try:
        index = record_set.columns.index(column)
    except ValueError:
        index = -1
    adjacent = []
    if index >= 0:
        adjacent = record_set.columns[max(0, index - 2):index]
        adjacent += record_set.columns[index + 1:index + 3]

    return ColumnProfile(
        data_type=_data_type(values),
        non_empty_count=len(values),
        null_count=max(0, len(raw) - len(values)),
        null_percentage=(max(0, len(raw) - len(values)) / len(raw) if raw else 0.0),
        unique_count=len({str(v) for v in values}),
        minimum=min(numeric) if numeric and len(numeric) == len(values) else None,
        maximum=max(numeric) if numeric and len(numeric) == len(values) else None,
        adjacent_columns=adjacent,
        possible_unit=_unit_hint(column),
        possible_period=infer_period_from_name(column),
        known_id_matches=matches,
    )
