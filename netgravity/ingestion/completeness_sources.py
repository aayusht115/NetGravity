"""
NetGravity — Completeness for the preview upload path
=======================================================
`check_completeness()` reads `TabularResult.network_rows` — the canonically
renamed dict rows the session pipeline produces. The preview upload path
(`/api/ingestions/preview/upload-and-parse`) never builds one: it produces
the extractor's structure dict instead, and so ran no completeness check at
all. A gap that blocked or degraded a solve was therefore invisible on the
only upload screen the frontend actually uses.

This module closes that by RESHAPING the structure dict into the rows
`check_completeness` already understands — so both entry points are judged
by one registry, one set of rules and one report. There is no second
definition of "what counts as missing" here, and adding a field to
`REQUIRED_FIELDS`/`OPTIONAL_FIELDS` covers both paths at once.

The reshaping is a lookup table, not logic. The one thing that matters is
that the extractor leaves an absent column as None rather than defaulting it
(see network_extractor.py's `openingCost` comment) — absence is still a fact
by the time it gets here, which is the whole premise of completeness.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from netgravity.ingestion.completeness import CompletenessReport, check_completeness
from netgravity.ingestion.schemas.content import ContentType

#: structure key -> canonical key, per collection. Only fields the registries
#: actually look for; anything else on the row is irrelevant to this check.
_FACILITY_FIELDS = {
    "id": "facility_id",
    "name": "facility_name",
    "capacity": "capacity_units_per_period",
    "fixedCost": "fixed_cost_per_year",
    "openingCost": "opening_cost",
    "carbonFactor": "carbon_emission_factor",
}
_MARKET_FIELDS = {
    "id": "market_id",
    "name": "market_name",
    "demand": "quantity",
    "slaDays": "sla_days",
    "serviceLevel": "service_level",
}
_LANE_FIELDS = {
    "from": "origin_id",
    "to": "destination_id",
    "cost": "rate_per_unit",
    "capacity": "lane_capacity",
    "emissionFactor": "emission_factor_override",
}
_HISTORY_FIELDS = {
    "marketId": "market_id",
    "quantity": "volume",
    "period": "period",
}


def _facility_rows(rows: Any, role: str) -> List[Dict[str, Any]]:
    """
    Facility rows, carrying `status` only where the UPLOAD stated one.

    The extractor defaults an absent status to EXISTING so a file with no
    status column is not handed to the solver as a network of greenfield sites
    (see network_extractor.py). That default is right for the model and wrong
    for this check: copying it through would let a status-scoped spec judge
    rows on an assumption, which is how "40 operating warehouses are missing a
    build cost" gets printed. `statusStated` is what separates the two.
    """
    shaped = _rename(rows, _FACILITY_FIELDS, {"role": role})
    for row, source in zip(shaped, rows or []):
        if isinstance(source, dict) and source.get("statusStated") and source.get("status"):
            row["status"] = source["status"]
    return shaped


def _rename(rows: Any, fields: Mapping[str, str],
            extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Copy across only the keys the registries read, and only when the source
    actually carried a value.

    A None is DROPPED rather than copied, so `_present()` sees the same
    "this column was never there" it would see in the session pipeline. Copying
    it through as an explicit None would work too, but dropping keeps the two
    paths byte-identical in shape, which is what makes one registry honest
    about both.
    """
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        shaped: Dict[str, Any] = dict(extra or {})
        for source_key, canonical_key in fields.items():
            value = row.get(source_key)
            if value is not None:
                shaped[canonical_key] = value
        out.append(shaped)
    return out


class _StructureOutcome:
    """The two attributes `check_completeness` duck-types off TabularResult."""

    def __init__(self, network_rows, staging_rows):
        self.network_rows = network_rows
        self.staging_rows = staging_rows


def rows_from_structure(structure: Mapping[str, Any]) -> _StructureOutcome:
    """Reshape one extractor structure dict into completeness-check rows."""
    structure = structure or {}
    facilities = (
        # `role` is what completeness.py buckets on, and the extractor has
        # already made the plant/DC call — this just states it in the column
        # the registry reads.
        _facility_rows(structure.get("plants"), "PLANT")
        + _facility_rows(structure.get("dcs"), "DC")
    )
    return _StructureOutcome(
        network_rows={
            ContentType.FACILITY: facilities,
            ContentType.MARKET: _rename(structure.get("markets"), _MARKET_FIELDS),
            ContentType.LANE: _rename(structure.get("lanes"), _LANE_FIELDS),
            # Market demand arrives on the markets sheet here, which
            # completeness.py already handles as the "no separate DEMAND rows"
            # case rather than reporting every zone as missing demand.
            ContentType.DEMAND: [],
        },
        staging_rows={
            ContentType.HISTORICAL_VOLUME.value:
                _rename(structure.get("demandHistory"), _HISTORY_FIELDS),
        },
    )


def check_structure_completeness(structure: Mapping[str, Any],
                                 has_contracts: bool = False) -> CompletenessReport:
    """The same gate the session pipeline runs, against a preview upload."""
    return check_completeness(rows_from_structure(structure), has_contracts=has_contracts)
