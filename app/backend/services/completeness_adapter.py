"""
The data-completeness check, run against the upload path the product uses.

WHY THIS FILE EXISTS
--------------------
There are two ingestion paths in this repository. `netgravity/ingestion/`
is the full pipeline — profiling, AI column mapping, a review console, a
session store — and `netgravity/ingestion/completeness.py` was written
against it, reading `TabularResult.network_rows`.

The product's own upload does not go through it. The browser posts to
`/api/ingestions/preview/upload-and-parse`, which calls
`network_extractor.build_network_from_dataframes()` and hands the result to
`network_assembler`. So the completeness gate — and therefore every
missing-data action item and every missing-data email — would never have
fired for a single real upload.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
It is an adapter, not a second checker. `check_completeness()` is duck-typed:
it wants an object with `.network_rows` (a dict keyed by ContentType) and
`.staging_rows`. This file builds exactly that from the extractor's own
parsed structure and calls the one existing implementation, so the required
and optional field registries stay in one place. Add a field there and both
paths see it.

The only real work here is the key translation. The extractor names its
columns for the browser (`capacity`, `fixedCost`, `slaDays`); the registry
names them for the engine (`capacity_units_per_period`,
`fixed_cost_per_year`, `sla_days`). The map below is that correspondence,
written out once, explicitly — never inferred by string-munging, which is
how `fixedCost` and `fixed_cost_per_year` would quietly stop matching the
day either side is renamed.

WHAT ABSENCE MEANS HERE
-----------------------
The extractor has already read the workbook, and it writes `None` for a
column it did not find (deliberately — see its `capacity` note: "treated as
uncapacitated rather than given a default size"). So `None` here still
means "the client never sent this", which is the distinction the whole
check rests on. What is NOT distinguishable at this point is a client who
sent a genuine zero: the extractor's `_num()` returns 0.0 for that, and the
check reads it as present. That is the correct reading — a stated zero is
data — and it is why this runs on the extractor's output rather than on the
`CanonicalNetwork`, where an absent field and a defaulted one look alike.
"""

from __future__ import annotations

from typing import Any, Dict, List

from netgravity.ingestion.completeness import CompletenessReport, check_completeness
from netgravity.ingestion.schemas.content import ContentType

#: Extractor key -> registry key, per entity kind. One direction only: this
#: file never writes back into the extractor's structure.
_FACILITY_KEYS = {
    "id": "facility_id",
    "name": "facility_name",
    "capacity": "capacity_units_per_period",
    "fixedCost": "fixed_cost_per_year",
    "handlingCost": "handling_cost_per_unit",
    "carbonFactor": "carbon_emission_factor",
}

_MARKET_KEYS = {
    "id": "market_id",
    "name": "market_name",
    "demand": "quantity",
    "slaDays": "sla_days",
    "serviceLevel": "service_level",
}

_LANE_KEYS = {
    "from": "origin_id",
    "to": "destination_id",
    "cost": "rate_per_unit",
    "capacity": "lane_capacity",
    "emissionFactor": "emission_factor_override",
}


def _translate(row: Dict[str, Any], keys: Dict[str, str]) -> Dict[str, Any]:
    """
    Carry over only the keys the registry knows about.

    A key the extractor did not write is simply absent from the result,
    which is what `completeness._present()` reads as missing — the same
    thing it reads for a key present with a blank value.
    """
    out: Dict[str, Any] = {}
    for source_key, registry_key in keys.items():
        if source_key in row:
            out[registry_key] = row[source_key]
    return out


class _RowsView:
    """
    The two attributes `check_completeness()` reads, and nothing else.

    Deliberately not a `TabularResult`: constructing one would mean carrying
    the whole tabular pipeline's shape into a path that does not use it, and
    the check has never needed more than these two dictionaries.
    """

    def __init__(self, network_rows: Dict[Any, List[Dict[str, Any]]],
                 staging_rows: Dict[str, List[Dict[str, Any]]]):
        self.network_rows = network_rows
        self.staging_rows = staging_rows


def rows_from_structure(structure: Dict[str, Any]) -> _RowsView:
    """
    The extractor's parsed structure, in the shape the registry reads.

    `role` is written explicitly per facility rather than inferred from the
    name: the registry buckets a facility into "supply" or "dc" from that
    column, and the extractor has already made that decision (it returns
    plants and DCs as two separate lists). Re-deriving it here would be a
    second classifier that could disagree with the first.
    """
    facilities: List[Dict[str, Any]] = []
    for plant in structure.get("plants") or []:
        row = _translate(plant, _FACILITY_KEYS)
        row["role"] = "PLANT"
        facilities.append(row)
    for dc in structure.get("dcs") or []:
        row = _translate(dc, _FACILITY_KEYS)
        row["role"] = "DC"
        facilities.append(row)

    markets = [_translate(m, _MARKET_KEYS) for m in (structure.get("markets") or [])]
    lanes = [_translate(l, _LANE_KEYS) for l in (structure.get("lanes") or [])]

    # Demand arrives on the markets sheet in this path (the extractor folds a
    # Demand_History time series onto each market's `demand`), so there is no
    # separate DEMAND content type. completeness.py already handles exactly
    # this case — "demand may legitimately arrive on the zones sheet itself" —
    # and falls back to checking `quantity` on the market rows.
    network_rows: Dict[Any, List[Dict[str, Any]]] = {
        ContentType.FACILITY: facilities,
        ContentType.MARKET: markets,
        ContentType.LANE: lanes,
        ContentType.DEMAND: [],
    }

    # The client's own recorded history, which is what makes the "we could
    # forecast instead of assuming flat demand" optional field satisfied.
    history = structure.get("demandHistory") or []
    staging_rows = {
        ContentType.HISTORICAL_VOLUME.value: [
            {"volume": row.get("quantity")} for row in history
            if isinstance(row, dict)
        ],
    }
    return _RowsView(network_rows, staging_rows)


def check_structure(structure: Dict[str, Any]) -> CompletenessReport:
    """
    Run the one completeness implementation against an extractor structure.

    `has_contracts` is read from the structure itself rather than passed in:
    the extractor records contract rules on the structure when a rate card
    was uploaded, and that is the same fact the optional-field registry asks
    about ("Contract Rate Card / Surcharge Details").
    """
    has_contracts = bool(structure.get("contracts") or structure.get("laneRates"))
    return check_completeness(rows_from_structure(structure), has_contracts=has_contracts)
