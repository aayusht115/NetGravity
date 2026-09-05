"""
NetGravity — Data Completeness Gate
=====================================
Deterministic, rule-based check for whether an uploaded dataset has enough
data to run the network analysis, and separately, whether it has enough to
get the RICHEST possible results.

WHY THIS IS SEPARATE FROM THE ENGINE'S OWN VALIDATION
------------------------------------------------------
netgravity.validation.checks answers "is this CanonicalNetwork solvable".
By the time a CanonicalNetwork exists, every FacilityRecord/LaneRecord/
DemandRecord field has already been defaulted (e.g.
`fixed_cost_per_year: float = 0.0`), so "the client never sent this column"
and "the client sent zero" are no longer distinguishable.

This module runs one step earlier, against `TabularResult.network_rows` —
the canonically-renamed dict rows produced by ai/field_mapper.py right after
column mapping and before any Pydantic record is built. That is the only
point in the pipeline where column absence is still a fact, not a default.

No model call. No judgement calls about data quality. Just: was this column
present with a non-blank value, for this specific record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from netgravity.ingestion.schemas.content import ContentType
from netgravity.schemas.network import parse_facility_status

# ---------------------------------------------------------------------------
# Field registries — plain data, not prompts.
#
# `display_label` strings are used verbatim in any user-facing text (emails,
# review UI) — never `canonical_key`, which is an internal engine field name
# a planner would not recognise.
# ---------------------------------------------------------------------------

#: Used where a spec applies to any facility, whatever its role.
ENTITY_FACILITY = "Facility"
ENTITY_SUPPLY = "Supply Location"
ENTITY_DC = "Candidate DC"
ENTITY_DEMAND_ZONE = "Demand Zone"
ENTITY_LANE = "Lane"

#: Facility rows are bucketed by their raw `role` column into one of these
#: two entity types. SUPPLIER/PLANT are the supply side; everything else
#: (DC, WAREHOUSE, DEPOT, CROSS_DOCK, DARKSTORE, or unrecognised/blank) is
#: treated as a candidate DC, matching this tool's primary candidate-DC
#: network-design use case and the same uppercase role convention
#: adapters/structured.py already checks role_raw against.
_SUPPLY_ROLES = {"SUPPLIER", "PLANT"}

#: `FacilityStatus` members as plain strings, so a spec can name one without
#: this module importing the solver's schema into its registry declarations.
STATUS_EXISTING = "EXISTING"
STATUS_CANDIDATE = "CANDIDATE"
STATUS_CLOSED = "CLOSED"


@dataclass(frozen=True)
class RequiredFieldSpec:
    canonical_key: str
    display_label: str
    unit: str
    entity_type: str
    content_type: ContentType
    # Only for FACILITY rows: which role-bucket this spec applies to.
    role_bucket: Optional[str] = None
    # Only for LANE rows: "source_to_dc" or "dc_to_demand".
    lane_leg: Optional[str] = None


@dataclass(frozen=True)
class OptionalFieldSpec:
    canonical_key: str
    display_label: str
    unit: str
    what_it_unlocks: str
    content_type: ContentType
    #: Alternate canonical key that also satisfies this field (e.g. a lane
    #: emission override standing in for a facility-level factor). Checked
    #: dataset-wide in addition to `canonical_key`.
    alt_canonical_key: Optional[str] = None
    #: Special-cased: satisfied by contracts being present at all, not by a
    #: tabular column. See check_completeness(has_contracts=...).
    satisfied_by_contracts: bool = False
    #: Only for FACILITY rows: which role-bucket this spec applies to
    #: ("supply" or "dc"), same meaning as RequiredFieldSpec.role_bucket.
    #:
    #: Set it when the field is meaningless for the other bucket. A scoped
    #: spec is checked ROW BY ROW inside its bucket and names the entities
    #: that are missing it, instead of the dataset-wide "is this key
    #: anywhere" check an unscoped spec gets. A dataset with no rows in the
    #: bucket at all is not flagged — there is nothing there to be missing.
    role_bucket: Optional[str] = None
    #: Check this field ROW BY ROW even with no role/status filter, naming the
    #: rows that lack it.
    #:
    #: The default dataset-wide check asks "is this key anywhere", which is
    #: right for a field that describes the network (a carbon factor, a rate
    #: card) and wrong for one that describes each row. One facility stating a
    #: status satisfied the whole file, so a mixed upload — Delhi EXISTING,
    #: Mumbai blank — reported no gap, and Mumbai was then invisible to the
    #: status-scoped checks below as well. It fell through everything.
    per_entity: bool = False
    #: Entity noun for the report, when the role bucket does not supply one.
    entity_type: Optional[str] = None
    #: Only for FACILITY rows: which `FacilityStatus` this spec applies to.
    #:
    #: A SECOND, independent filter, because role and status answer different
    #: questions. `role_bucket` separates the supply side from the
    #: distribution side; this separates a site that exists from one that is
    #: only proposed. Opening cost needs both: it is meaningless for a plant
    #: AND meaningless for a warehouse already operating, and conflating the
    #: two reported every existing warehouse as missing a cost it will never
    #: incur.
    facility_status: Optional[str] = None


REQUIRED_FIELDS: List[RequiredFieldSpec] = [
    RequiredFieldSpec("facility_name", "Supply Location Name", "",
                      ENTITY_SUPPLY, ContentType.FACILITY, role_bucket="supply"),
    RequiredFieldSpec("capacity_units_per_period", "Daily Supply Capacity (units/day)",
                      "units/day", ENTITY_SUPPLY, ContentType.FACILITY, role_bucket="supply"),
    RequiredFieldSpec("market_name", "Demand Zone Name", "",
                      ENTITY_DEMAND_ZONE, ContentType.MARKET),
    RequiredFieldSpec("quantity", "Daily Demand Quantity (units/day)", "units/day",
                      ENTITY_DEMAND_ZONE, ContentType.DEMAND),
    RequiredFieldSpec("facility_name", "Candidate DC Name", "",
                      ENTITY_DC, ContentType.FACILITY, role_bucket="dc"),
    RequiredFieldSpec("capacity_units_per_period", "DC Daily Throughput Capacity (units/day)",
                      "units/day", ENTITY_DC, ContentType.FACILITY, role_bucket="dc"),
    RequiredFieldSpec("fixed_cost_per_year", "DC Annual Fixed Cost (₹ lakh/year)",
                      "₹ lakh/year", ENTITY_DC, ContentType.FACILITY, role_bucket="dc"),
    RequiredFieldSpec("rate_per_unit", "Transport Cost: Source → DC (₹/unit)",
                      "₹/unit", ENTITY_LANE, ContentType.LANE, lane_leg="source_to_dc"),
    RequiredFieldSpec("rate_per_unit", "Transport Cost: DC → Demand Zone (₹/unit)",
                      "₹/unit", ENTITY_LANE, ContentType.LANE, lane_leg="dc_to_demand"),
]

OPTIONAL_FIELDS: List[OptionalFieldSpec] = [
    OptionalFieldSpec("carbon_emission_factor", "Carbon Emission Factor (kg CO₂/unit)",
                      "kg CO₂/unit", "would let us include a carbon-impact KPI",
                      ContentType.FACILITY, alt_canonical_key="emission_factor_override"),
    OptionalFieldSpec("service_level", "Service Level Target (%)", "%",
                      "would let us score results against your target fill rate",
                      ContentType.DEMAND),
    OptionalFieldSpec("sla_days", "Maximum Delivery Lead Time (days)", "days",
                      "would let us flag lanes that miss your delivery window",
                      ContentType.MARKET, alt_canonical_key=None),
    OptionalFieldSpec("fuel_surcharge_pct", "Contract Rate Card / Surcharge Details", "",
                      "would let us price transport more precisely",
                      ContentType.LANE, satisfied_by_contracts=True),
    OptionalFieldSpec("volume", "Historical Monthly Demand (last 12 months)", "units/month",
                      "would let us forecast demand instead of assuming it's flat",
                      ContentType.HISTORICAL_VOLUME),
    OptionalFieldSpec("lane_capacity", "Lane / Route Capacity Limit (units/day)",
                      "units/day", "would let us flag routes that can't carry the flow",
                      ContentType.LANE),
    # Optional, not required: a candidate without it still solves. But it
    # solves on a defaulted 0.0 (adapters/structured.py), and the MILP
    # objective prices opening cost for candidates (milp.py opening_cost_term)
    # — so an absent column makes building a new DC look free, quietly biasing
    # the solver toward opening one.
    # Ranked above opening cost deliberately: without it, nothing below can
    # tell an operating warehouse from a proposed one, and the two assemblers
    # in this codebase default it in OPPOSITE directions (EXISTING in
    # adapters/structured.py, CANDIDATE in network_assembler.py). Which one a
    # dataset gets is not something a planner should discover from the answer.
    OptionalFieldSpec("status", "Facility Status (existing or proposed)", "",
                      "would let us tell the sites you already operate from the "
                      "ones you are considering, instead of assuming",
                      ContentType.FACILITY,
                      # Per ROW: one facility stating a status says nothing
                      # about the one beside it, and a row with no status is
                      # invisible to every status-scoped check below.
                      per_entity=True, entity_type=ENTITY_FACILITY),
    OptionalFieldSpec("opening_cost", "Candidate DC Opening Cost (₹ lakh)", "₹ lakh",
                      "would let us weigh the one-time cost of building a new DC, "
                      "instead of treating it as free to build",
                      ContentType.FACILITY, role_bucket="dc",
                      # CANDIDATE, not merely "not a plant". The MILP prices
                      # opening cost only for `fac.is_candidate`
                      # (milp.py opening_cost_term), so an existing warehouse
                      # will never incur one and reporting it as missing sends
                      # a planner looking for a number that does not exist.
                      facility_status=STATUS_CANDIDATE),
]


def _present(row: Dict[str, Any], key: str) -> bool:
    if key not in row:
        return False
    value = row[key]
    return value is not None and str(value).strip() != ""


def _role_bucket(row: Dict[str, Any]) -> str:
    role_raw = str(row.get("role") or "").strip().upper()
    return "supply" if role_raw in _SUPPLY_ROLES else "dc"


def _facility_status(row: Dict[str, Any]) -> Optional[str]:
    """
    The status this row STATES, or None when it states none.

    Never defaulted here. The two assemblers default an absent status
    differently — adapters/structured.py to EXISTING, network_assembler.py to
    CANDIDATE — and a completeness check that picked either would contradict
    one of them. "The file does not say" is its own answer, and it is handled
    as one below.
    """
    status = parse_facility_status(row.get("status"))
    return status.value if status is not None else None


def _facility_name(row: Dict[str, Any]) -> str:
    return str(row.get("facility_name") or row.get("facility_id") or "(unnamed)")


@dataclass
class MissingField:
    canonical_key: str
    display_label: str
    unit: str
    entity_type: str
    entity_name: str = ""
    what_it_unlocks: str = ""
    #: Every named entity this gap applies to.
    #:
    #: A required gap is reported one row at a time, so this holds the single
    #: name already in `entity_name`. A role-scoped optional gap is reported
    #: once for the whole field and lists every entity missing it, so a reader
    #: gets "3 candidate DCs" without the email repeating the same line three
    #: times. Consumers that only understand `entity_name` keep working: it is
    #: still filled in, joined, for exactly that reason.
    entity_names: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.entity_names and self.entity_name:
            self.entity_names = [self.entity_name]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "canonical_key": self.canonical_key,
            "display_label": self.display_label,
            "unit": self.unit,
            "entity_type": self.entity_type,
            "entity_name": self.entity_name,
            "entity_names": list(self.entity_names),
            "what_it_unlocks": self.what_it_unlocks,
        }


@dataclass
class CompletenessReport:
    missing_required: List[MissingField] = field(default_factory=list)
    missing_optional: List[MissingField] = field(default_factory=list)

    @property
    def is_blocking(self) -> bool:
        return bool(self.missing_required)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "missing_required": [m.as_dict() for m in self.missing_required],
            "missing_optional": [m.as_dict() for m in self.missing_optional],
        }


def _check_facility_and_market_required(outcome, missing: List[MissingField]) -> None:
    facility_rows = outcome.network_rows.get(ContentType.FACILITY) or []
    market_rows = outcome.network_rows.get(ContentType.MARKET) or []
    demand_rows = outcome.network_rows.get(ContentType.DEMAND) or []

    facility_specs = [s for s in REQUIRED_FIELDS if s.content_type == ContentType.FACILITY]
    for row in facility_rows:
        bucket = _role_bucket(row)
        entity_type = ENTITY_SUPPLY if bucket == "supply" else ENTITY_DC
        entity_name = _facility_name(row)
        for spec in facility_specs:
            if spec.role_bucket != bucket:
                continue
            if not _present(row, spec.canonical_key):
                missing.append(MissingField(
                    spec.canonical_key, spec.display_label, spec.unit,
                    entity_type, entity_name))

    market_specs = [s for s in REQUIRED_FIELDS if s.content_type == ContentType.MARKET]
    for row in market_rows:
        entity_name = str(row.get("market_name") or row.get("market_id") or "(unnamed)")
        for spec in market_specs:
            if not _present(row, spec.canonical_key):
                missing.append(MissingField(
                    spec.canonical_key, spec.display_label, spec.unit,
                    ENTITY_DEMAND_ZONE, entity_name))

    demand_specs = [s for s in REQUIRED_FIELDS if s.content_type == ContentType.DEMAND]
    if demand_specs and not demand_rows:
        # Demand may legitimately arrive on the zones sheet itself (see
        # field_aliases.py) rather than a separate DEMAND content type. Only
        # flag demand quantity as missing when there is truly nowhere it
        # could have come from.
        for row in market_rows:
            if not _present(row, "quantity"):
                entity_name = str(row.get("market_name") or row.get("market_id") or "(unnamed)")
                for spec in demand_specs:
                    missing.append(MissingField(
                        spec.canonical_key, spec.display_label, spec.unit,
                        ENTITY_DEMAND_ZONE, entity_name))
    else:
        for row in demand_rows:
            entity_name = str(row.get("market_id") or "(unnamed)")
            for spec in demand_specs:
                if not _present(row, spec.canonical_key):
                    missing.append(MissingField(
                        spec.canonical_key, spec.display_label, spec.unit,
                        ENTITY_DEMAND_ZONE, entity_name))


def _check_lane_required(outcome, missing: List[MissingField]) -> None:
    facility_rows = outcome.network_rows.get(ContentType.FACILITY) or []
    market_rows = outcome.network_rows.get(ContentType.MARKET) or []
    lane_rows = outcome.network_rows.get(ContentType.LANE) or []
    lane_specs = [s for s in REQUIRED_FIELDS if s.content_type == ContentType.LANE]
    if not lane_specs or not lane_rows:
        return

    id_bucket: Dict[str, str] = {}
    id_name: Dict[str, str] = {}
    for row in facility_rows:
        fid = str(row.get("facility_id") or "")
        if fid:
            id_bucket[fid] = _role_bucket(row)
            id_name[fid] = _facility_name(row)
    for row in market_rows:
        mid = str(row.get("market_id") or "")
        if mid:
            id_bucket[mid] = "demand"
            id_name[mid] = str(row.get("market_name") or mid)

    for row in lane_rows:
        origin_id = str(row.get("origin_id") or "")
        dest_id = str(row.get("destination_id") or "")
        origin_bucket = id_bucket.get(origin_id)
        dest_bucket = id_bucket.get(dest_id)

        if origin_bucket == "supply" and dest_bucket == "dc":
            leg = "source_to_dc"
        elif origin_bucket == "dc" and dest_bucket == "demand":
            leg = "dc_to_demand"
        else:
            # Endpoints couldn't be resolved against known facilities/markets
            # (e.g. still awaiting mapping review) — not this check's job to
            # guess, so the lane is skipped rather than misclassified.
            continue

        origin_name = id_name.get(origin_id, origin_id)
        dest_name = id_name.get(dest_id, dest_id)
        entity_name = f"{origin_name} → {dest_name}"
        for spec in lane_specs:
            if spec.lane_leg != leg:
                continue
            if not _present(row, spec.canonical_key):
                missing.append(MissingField(
                    spec.canonical_key, spec.display_label, spec.unit,
                    ENTITY_LANE, entity_name))


#: Entity label to report a role-scoped gap against. Same buckets
#: `_role_bucket()` returns.
_BUCKET_ENTITY_TYPE = {"supply": ENTITY_SUPPLY, "dc": ENTITY_DC}


def _check_scoped_optional(spec: OptionalFieldSpec, rows: List[Dict[str, Any]],
                           missing: List[MissingField]) -> None:
    """
    A scoped optional field, checked row by row inside its own scope.

    Rows outside the scope are not just ignored for the verdict — they are
    invisible to it. Opening cost on an existing supply plant is not a gap, and
    neither is opening cost on a warehouse that is already operating: both are
    category errors, and a dataset with no candidate sites at all has nothing
    to be missing.
    """
    # Each filter applies only when the spec declares it, so a spec may scope
    # by role, by status, or by both.
    scoped = list(rows)
    if spec.role_bucket:
        scoped = [r for r in scoped if _role_bucket(r) == spec.role_bucket]
    if spec.facility_status:
        # A row that states no status is invisible to a status-scoped spec.
        # We cannot know whether it is in scope, and guessing produces the
        # worst outcome available: forty operating warehouses reported as
        # missing a build cost they will never incur. What that file is
        # actually missing is the status column, which is its own registry
        # entry below and is what the reader is told instead.
        scoped = [r for r in scoped if _facility_status(r) == spec.facility_status]
    if not scoped:
        return

    absent = [r for r in scoped
              if not _present(r, spec.canonical_key)
              and not (spec.alt_canonical_key and _present(r, spec.alt_canonical_key))]
    if not absent:
        return

    names = [_facility_name(r) for r in absent]
    missing.append(MissingField(
        spec.canonical_key, spec.display_label, spec.unit,
        entity_type=(spec.entity_type
                     or _BUCKET_ENTITY_TYPE.get(spec.role_bucket or "", "")),
        entity_name=", ".join(names),
        what_it_unlocks=spec.what_it_unlocks,
        entity_names=names))


def _check_optional(outcome, has_contracts: bool, missing: List[MissingField]) -> None:
    for spec in OPTIONAL_FIELDS:
        if spec.satisfied_by_contracts and has_contracts:
            continue

        rows = outcome.network_rows.get(spec.content_type) or []
        if spec.content_type == ContentType.HISTORICAL_VOLUME:
            rows = outcome.staging_rows.get(ContentType.HISTORICAL_VOLUME.value) or []

        if spec.role_bucket or spec.facility_status or spec.per_entity:
            _check_scoped_optional(spec, rows, missing)
            continue

        found = any(_present(r, spec.canonical_key) for r in rows)
        if not found and spec.alt_canonical_key:
            found = any(_present(r, spec.alt_canonical_key) for r in rows)
            if not found:
                # The alt key may live on a different content type (e.g. a
                # lane-level emission override standing in for the
                # facility-level carbon factor).
                lane_rows = outcome.network_rows.get(ContentType.LANE) or []
                found = any(_present(r, spec.alt_canonical_key) for r in lane_rows)

        if not found and not rows and not spec.satisfied_by_contracts:
            missing.append(MissingField(
                spec.canonical_key, spec.display_label, spec.unit,
                entity_type="", what_it_unlocks=spec.what_it_unlocks))
        elif not found and rows:
            missing.append(MissingField(
                spec.canonical_key, spec.display_label, spec.unit,
                entity_type="", what_it_unlocks=spec.what_it_unlocks))


def check_completeness(outcome, has_contracts: bool = False) -> CompletenessReport:
    """
    Compare a parsed (but not yet Pydantic-built) dataset against the
    required/optional field registries.

    `outcome` is a `netgravity.ingestion.tabular.TabularResult` — typed as
    duck-typed `Any` here (network_rows / staging_rows dict attributes) to
    avoid a circular import between tabular.py and this module.
    """
    missing_required: List[MissingField] = []
    missing_optional: List[MissingField] = []

    _check_facility_and_market_required(outcome, missing_required)
    _check_lane_required(outcome, missing_required)
    _check_optional(outcome, has_contracts, missing_optional)

    return CompletenessReport(missing_required=missing_required,
                              missing_optional=missing_optional)
