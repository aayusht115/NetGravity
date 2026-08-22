"""
Orchestrator — Digital Twin state contract.

The typed payload the Orchestrator hands to the Digital Twin, and the views the
Digital Twin hands back out.

    Orchestrator  →  DigitalTwinState  →  DigitalTwinService  →  view / comparison

Three rules shape every model here.

**1. The twin represents; it does not calculate.**
Every number in a `DigitalTwinState` was produced by an authoritative engine —
MILP for decisions, costs and utilisation, REI for exposure, RF for risk — and
is carried through unchanged. There is no field the twin computes from raw
network data, and `twin/` imports no engine (asserted by test).

Comparison is the one place arithmetic happens, and it is subtraction between
two authoritative values that already exist. `compare()` is a listed Digital
Twin responsibility precisely because a delta is a *statement about* two
results, not a new result.

**2. Absence is a value.**
An unavailable KPI is `None` with a matching `UnavailableValue` naming why —
never `0.0`. `0.0` means "measured, and it was zero", which is a different fact
and, for a cost or a fill rate, a dangerous one to invent. `ValueStatus` records
which of the two a reader is looking at.

**3. Scenarios are deltas over a baseline, not copies.**
A scenario state stores only what CHANGED plus `base_state_id`. Nothing here
holds a `CanonicalNetwork`: states reference `snapshot_id`, and the network
itself stays single-sourced in `SnapshotManager`. Ten scenarios on one snapshot
cost ten delta records, not ten networks.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class TwinStateType(str, Enum):
    """
    What kind of network state this is.

    The distinction observed-vs-hypothetical is the one a viewer must never get
    wrong, so it is a required, explicit field rather than an inference from
    whether `scenario_id` happens to be set.
    """
    #: Observed network as evaluated — the as-is footprint, not an optimum.
    BASELINE  = "BASELINE"
    #: An optimisation of the observed network. Hypothetical: the solver chose
    #: this configuration, the business has not adopted it.
    OPTIMIZED = "OPTIMIZED"
    #: A what-if overlay on a parent snapshot. Always hypothetical.
    SCENARIO  = "SCENARIO"


class StorageMode(str, Enum):
    """How this state is physically stored."""
    #: Complete facility and flow lists.
    FULL  = "FULL"
    #: Only entries that DIFFER from `base_state_id`. Materialised on read.
    DELTA = "DELTA"


class ValueStatus(str, Enum):
    """
    Why a value is what it is.

    Exists so `None` is never ambiguous. A reader can always tell a figure that
    was measured from one that could not be produced, and for what reason.
    """
    AVAILABLE     = "AVAILABLE"       # produced by an authoritative engine
    UNAVAILABLE   = "UNAVAILABLE"     # the producing step did not deliver
    NOT_COMPUTED  = "NOT_COMPUTED"    # never requested for this run
    NOT_COMPUTABLE = "NOT_COMPUTABLE" # requested, but inputs made it impossible
    STALE         = "STALE"           # produced against a different snapshot
    FAILED        = "FAILED"          # the producing step errored


class TwinCalculationStatus(str, Enum):
    """
    Completeness of the state as a whole.

    A twin state is publishable in every one of these conditions — including
    `FAILED`. Refusing to represent a failed run would leave a viewer looking at
    the previous, stale state with no indication anything had gone wrong, which
    is worse than showing an explicitly empty one.
    """
    COMPLETE          = "COMPLETE"           # every expected input present
    PARTIAL           = "PARTIAL"            # some evidence missing, named below
    FAILED            = "FAILED"             # no usable engine result
    INFEASIBLE        = "INFEASIBLE"         # solver proved no solution exists
    STALE             = "STALE"              # snapshot moved on under the run


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class TwinProvenance(BaseModel):
    """
    Where this state came from.

    A viewer must be able to answer "which network version and which solver run
    produced what I am looking at?" from the state alone, without consulting the
    audit log. Everything needed for that answer is required or explicitly None.
    """
    #: Observed network snapshot. Always present — a state with no snapshot has
    #: no meaning, because there is nothing to say the numbers describe.
    snapshot_id: str
    data_version: Optional[str] = None
    network_id: Optional[str] = None

    #: Set only for SCENARIO states.
    scenario_id: Optional[str] = None
    scenario_version: Optional[int] = None
    #: The snapshot the scenario overlays. Equals `snapshot_id` in normal
    #: operation; kept separate so a mismatch is visible rather than assumed.
    parent_snapshot_id: Optional[str] = None
    scenario_overrides: List[str] = Field(default_factory=list)

    #: Identity of the solver run behind the decisions.
    run_id: Optional[str] = None
    solver_status: Optional[str] = None
    optimality_label: str = ""
    #: Orchestrator execution that produced this state.
    execution_id: Optional[str] = None

    model_version: Optional[str] = None
    optimization_mode: Optional[str] = None
    #: False only for an as-is evaluation of the observed network.
    is_hypothetical: bool = True

    generated_at: Optional[str] = None
    #: Always the orchestrator. Present so a state whose source is anything else
    #: is visibly wrong rather than quietly accepted.
    source: str = "orchestrator"

    model_config = ConfigDict(extra="forbid", frozen=True)


class UnavailableValue(BaseModel):
    """
    One thing the twin expected and did not get.

    Mirrors the orchestrator's own `UnavailableEvidence`, deliberately: the twin
    reports the same absences the control plane recorded, rather than inventing
    a second vocabulary for missing data.
    """
    field: str                       # dotted path, e.g. "kpis.business_network_cost"
    status: ValueStatus
    reason: str = ""
    #: Capability whose failure caused it, when known.
    capability: Optional[str] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Element states
# ---------------------------------------------------------------------------

class FacilityState(BaseModel):
    """
    One facility as the twin represents it.

    Every numeric field is copied from `FacilitySummary` (MILP) or the REI/RF
    registries. `utilization_pct` in particular is the MILP's own figure — the
    twin does not divide throughput by capacity, because the engine's definition
    of utilisation is the one the rest of the system reasons about.
    """
    facility_id: str
    facility_name: str = ""
    role: str = ""

    # ---- decision (MILP) ----
    is_open: bool
    throughput_units: float = 0.0
    capacity_units: float = 0.0
    utilization_pct: float = 0.0
    #: Observed status before any optimisation, so open→closed is visible.
    baseline_status: Optional[str] = None
    contract_status: str = "NONE"
    closure_cost_charged: float = 0.0

    # ---- risk context (REI / RF), None when not assessed ----
    #: Relative economic exposure ∈ [0,1] from the REI registry. None means the
    #: facility was not assessed or the assessment failed — never 0.0, which
    #: would read as "assessed, and it is the least exposed node".
    rei: Optional[float] = None
    rei_status: ValueStatus = ValueStatus.NOT_COMPUTED
    rei_rank: Optional[int] = None
    risk_classification: Optional[str] = None

    #: RF = P + REI − P·REI, from the risk layer. None unless a defensible event
    #: probability existed.
    risk_factor: Optional[float] = None
    risk_factor_status: ValueStatus = ValueStatus.NOT_COMPUTED

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def is_assessed_for_exposure(self) -> bool:
        return self.rei is not None


class FlowState(BaseModel):
    """
    One origin → destination allocation, aggregated across mode and product.

    Aggregation happens upstream in `build_network_state_result`; the twin
    carries the result. `share_of_total_units` is the one derived figure, and it
    is a ratio of two authoritative sums computed for display.
    """
    origin_id: str
    destination_id: str
    flow_units: float
    transport_cost: float = 0.0
    distance_km: float = 0.0
    carbon_kg: float = 0.0
    #: Fraction of total network flow on this lane, for heat-mapping.
    share_of_total_units: float = 0.0

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def lane_key(self) -> str:
        return f"{self.origin_id}->{self.destination_id}"


class FlowAggregate(BaseModel):
    """
    Rollups over the flow set.

    Large networks produce flow lists too big to send to a viewer at once. These
    totals let a client render network-level structure without paging through
    every lane, and are plain sums of authoritative per-lane values.
    """
    total_lanes: int = 0
    total_flow_units: float = 0.0
    total_transport_cost: float = 0.0
    total_carbon_kg: float = 0.0
    #: origin_id → units dispatched.
    units_by_origin: Dict[str, float] = Field(default_factory=dict)
    #: destination_id → units received.
    units_by_destination: Dict[str, float] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class TwinKPIs(BaseModel):
    """
    Network-level figures, carried from the authoritative result.

    Every field is Optional with no default of zero. A KPI that could not be
    produced is None and appears in the state's `unavailable` list.
    """
    business_network_cost: Optional[float] = None
    solver_objective: Optional[float] = None
    shortage_penalty_cost: Optional[float] = None
    cost_components: Dict[str, float] = Field(default_factory=dict)
    reconciliation_is_closed: Optional[bool] = None

    total_demand: Optional[float] = None
    served_demand: Optional[float] = None
    unserved_demand: Optional[float] = None
    demand_fill_rate: Optional[float] = None

    n_facilities_open: Optional[int] = None
    n_facilities_closed: Optional[int] = None
    avg_utilization_pct: Optional[float] = None
    max_utilization_pct: Optional[float] = None

    total_carbon_kg: Optional[float] = None
    pct_demand_in_sla: Optional[float] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskContext(BaseModel):
    """
    Network-level risk summary, when a risk assessment ran.

    Deliberately thin. The twin shows what the risk layer concluded; it holds no
    thresholds and draws no conclusions of its own.
    """
    rei_batch_id: Optional[str] = None
    rei_batch_status: Optional[str] = None
    rei_snapshot_id: Optional[str] = None
    rei_status: ValueStatus = ValueStatus.NOT_COMPUTED
    #: Highest REI in the registry, and the facility holding it.
    max_rei: Optional[float] = None
    highest_exposure_facility: Optional[str] = None
    n_facilities_assessed: Optional[int] = None
    n_infeasible: Optional[int] = None

    max_risk_factor: Optional[float] = None
    risk_factor_status: ValueStatus = ValueStatus.NOT_COMPUTED
    #: Reasons RF could not be computed, verbatim from the risk layer.
    not_computable_reasons: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# The state itself
# ---------------------------------------------------------------------------

class DigitalTwinState(BaseModel):
    """
    The Orchestrator → Digital Twin payload.

    One immutable record of one network state. Scenario states are stored as
    DELTA against a baseline `base_state_id`; use
    `DigitalTwinService.materialize()` to read one as a complete picture.

    Immutability is enforced by `frozen=True` plus deep-copy on ingest in the
    store, so a caller holding a reference cannot alter published state — the
    twin's equivalent of the snapshot guarantee.
    """
    #: Stable, deterministic identity. Same snapshot + scenario + type always
    #: yields the same id, so retrieval is reproducible.
    state_id: str
    snapshot_id: str
    scenario_id: Optional[str] = None
    state_type: TwinStateType

    storage_mode: StorageMode = StorageMode.FULL
    #: Required when `storage_mode` is DELTA — the state this one differs from.
    base_state_id: Optional[str] = None

    provenance: TwinProvenance
    calculation_status: TwinCalculationStatus = TwinCalculationStatus.COMPLETE

    #: For FULL, every facility. For DELTA, only those that differ from base.
    facilities: List[FacilityState] = Field(default_factory=list)
    #: For FULL, every lane carrying flow. For DELTA, lanes that changed;
    #: `removed_lane_keys` carries lanes that stopped carrying flow entirely,
    #: which a changed-entries list cannot express.
    flows: List[FlowState] = Field(default_factory=list)
    removed_lane_keys: List[str] = Field(default_factory=list)
    #: Facilities present in base but absent here (rare; shape changes).
    removed_facility_ids: List[str] = Field(default_factory=list)

    flow_aggregate: Optional[FlowAggregate] = None

    kpis: Optional[TwinKPIs] = None
    risk: Optional[RiskContext] = None

    #: The scenario's own decisions, verbatim from the scenario record.
    decisions: List[str] = Field(default_factory=list)

    #: Everything expected and not received. Empty on a COMPLETE state.
    unavailable: List[UnavailableValue] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _delta_requires_base(self) -> "DigitalTwinState":
        """
        A DELTA with no base is unreadable — there is nothing to apply it to.

        Caught here rather than at read time so a malformed state can never
        enter the store in the first place.
        """
        if self.storage_mode is StorageMode.DELTA and not self.base_state_id:
            raise ValueError(
                "storage_mode=DELTA requires base_state_id: a delta with no base "
                "state cannot be materialised into a network state."
            )
        if self.storage_mode is StorageMode.FULL and self.base_state_id:
            raise ValueError(
                "storage_mode=FULL must not carry base_state_id: a full state is "
                "self-contained, and a base reference would imply otherwise."
            )
        return self

    @model_validator(mode="after")
    def _scenario_type_matches_scenario_id(self) -> "DigitalTwinState":
        """
        `state_type` and `scenario_id` must agree.

        The pair is what a viewer reads to decide whether it is looking at
        reality. Allowing them to disagree would let a scenario be presented as
        observed state, which is the single failure this contract exists to
        prevent.
        """
        if self.state_type is TwinStateType.SCENARIO and not self.scenario_id:
            raise ValueError("state_type=SCENARIO requires a scenario_id.")
        if self.state_type is not TwinStateType.SCENARIO and self.scenario_id:
            raise ValueError(
                f"scenario_id is set but state_type is {self.state_type.value}; a "
                f"state carrying a scenario must declare itself SCENARIO."
            )
        return self

    @model_validator(mode="after")
    def _hypothetical_flag_matches_type(self) -> "DigitalTwinState":
        """Only a BASELINE state may claim to be observed."""
        if self.state_type is not TwinStateType.BASELINE and not self.provenance.is_hypothetical:
            raise ValueError(
                f"state_type={self.state_type.value} is hypothetical by definition, but "
                f"provenance.is_hypothetical is False."
            )
        return self

    @property
    def is_observed(self) -> bool:
        """True only for an as-is evaluation of the observed network."""
        return (self.state_type is TwinStateType.BASELINE
                and not self.provenance.is_hypothetical)

    @property
    def has_complete_evidence(self) -> bool:
        return self.calculation_status is TwinCalculationStatus.COMPLETE

    def facility(self, facility_id: str) -> Optional[FacilityState]:
        for f in self.facilities:
            if f.facility_id == facility_id:
                return f
        return None


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class TwinStateRef(BaseModel):
    """
    A lightweight handle to a stored state.

    Returned by `update()` and embedded in the orchestrator's response so a
    caller can fetch the state later without the full payload travelling on
    every workflow result — the difference between a constant-size response and
    one that grows with the network.
    """
    state_id: str
    snapshot_id: str
    scenario_id: Optional[str] = None
    state_type: TwinStateType
    calculation_status: TwinCalculationStatus
    n_facilities: int = 0
    n_flows: int = 0
    generated_at: Optional[str] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class FlowPage(BaseModel):
    """One page of a flow set, with enough context to request the next."""
    items: List[FlowState] = Field(default_factory=list)
    offset: int = 0
    limit: int = 0
    #: Total lanes available, so a client knows the size before paging.
    total: int = 0

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class TwinStateView(BaseModel):
    """
    A materialised, readable state.

    What `get()` and `materialize()` return. Distinct from `DigitalTwinState`
    because a view may be partial by design — flows paginated, details omitted —
    whereas the stored state is always whole. Conflating them would make it
    impossible to tell "this state has two lanes" from "you asked for two".
    """
    state_id: str
    snapshot_id: str
    scenario_id: Optional[str] = None
    state_type: TwinStateType
    calculation_status: TwinCalculationStatus
    provenance: TwinProvenance

    facilities: List[FacilityState] = Field(default_factory=list)
    flows: FlowPage = Field(default_factory=FlowPage)
    flow_aggregate: Optional[FlowAggregate] = None

    kpis: Optional[TwinKPIs] = None
    risk: Optional[RiskContext] = None
    decisions: List[str] = Field(default_factory=list)
    unavailable: List[UnavailableValue] = Field(default_factory=list)

    #: True when this view was assembled from a base state plus a delta.
    materialized_from_delta: bool = False
    base_state_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

class DeltaDirection(str, Enum):
    """
    Which way a metric moved, without judging whether that is good.

    "Cost increased" is a fact; "cost got worse" is an interpretation that
    depends on what was bought with it. The twin states the fact.
    """
    INCREASED  = "INCREASED"
    DECREASED  = "DECREASED"
    UNCHANGED  = "UNCHANGED"
    #: One side or the other had no value, so no delta exists.
    NOT_COMPARABLE = "NOT_COMPARABLE"


class MetricDelta(BaseModel):
    """
    One metric, both sides, and the difference.

    Both raw values are retained alongside the delta so a reader never has to
    trust the subtraction — and so a `NOT_COMPARABLE` row still shows which side
    was missing.
    """
    metric: str
    baseline_value: Optional[float] = None
    comparison_value: Optional[float] = None
    abs_delta: Optional[float] = None
    pct_delta: Optional[float] = None
    direction: DeltaDirection = DeltaDirection.NOT_COMPARABLE
    #: Set when the metric could not be compared, saying which side was absent.
    reason: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)


class FacilityChange(BaseModel):
    """A facility whose state differs between the two sides."""
    facility_id: str
    facility_name: str = ""
    #: OPENED | CLOSED | UNCHANGED_OPEN | UNCHANGED_CLOSED | ADDED | REMOVED
    change: str
    baseline_is_open: Optional[bool] = None
    comparison_is_open: Optional[bool] = None
    throughput_delta: Optional[float] = None
    utilization_delta_pct: Optional[float] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class LaneChange(BaseModel):
    """A lane whose flow differs between the two sides."""
    origin_id: str
    destination_id: str
    #: ADDED | REMOVED | INCREASED | DECREASED
    change: str
    baseline_units: Optional[float] = None
    comparison_units: Optional[float] = None
    units_delta: Optional[float] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class TwinComparison(BaseModel):
    """
    Two states, side by side.

    Names both sides by `state_id` rather than embedding them, so a comparison
    can be re-derived and audited against the exact states that produced it.
    """
    baseline_state_id: str
    comparison_state_id: str
    baseline_snapshot_id: str
    comparison_snapshot_id: str
    comparison_scenario_id: Optional[str] = None

    #: False when the two sides describe different observed networks. A delta
    #: across snapshots mixes a network change with a decision change and cannot
    #: be attributed to either, so it is flagged rather than silently reported.
    same_snapshot: bool = True

    kpi_deltas: List[MetricDelta] = Field(default_factory=list)
    facility_changes: List[FacilityChange] = Field(default_factory=list)
    lane_changes: List[LaneChange] = Field(default_factory=list)

    #: Metrics that exist on one side only, with the reason.
    incomparable: List[MetricDelta] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    generated_at: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    def delta(self, metric: str) -> Optional[MetricDelta]:
        for d in self.kpi_deltas:
            if d.metric == metric:
                return d
        for d in self.incomparable:
            if d.metric == metric:
                return d
        return None

    @property
    def facilities_opened(self) -> List[str]:
        return [c.facility_id for c in self.facility_changes if c.change == "OPENED"]

    @property
    def facilities_closed(self) -> List[str]:
        return [c.facility_id for c in self.facility_changes if c.change == "CLOSED"]


__all__ = [
    "TwinStateType",
    "StorageMode",
    "ValueStatus",
    "TwinCalculationStatus",
    "TwinProvenance",
    "UnavailableValue",
    "FacilityState",
    "FlowState",
    "FlowAggregate",
    "TwinKPIs",
    "RiskContext",
    "DigitalTwinState",
    "TwinStateRef",
    "FlowPage",
    "TwinStateView",
    "DeltaDirection",
    "MetricDelta",
    "FacilityChange",
    "LaneChange",
    "TwinComparison",
]
