"""
Digital Twin — state projection.

Turns authoritative engine CONTRACTS into a `DigitalTwinState`. Nothing here
solves, ranks or scores: every function is a copy, a sum, or an explicit record
of absence.

What this module is allowed to import is the design. It takes
`NetworkStateResult` (the frozen MILP contract), `FacilityResilienceRegistry`
(the REI contract) and `RiskAssessment` (the RF contract) — result objects, all
of them. It imports no engine, so it *cannot* produce a number that did not
already exist upstream. A test asserts that import boundary against the compiled
source, because a docstring promising it is not a guarantee.

The one derived figure is `share_of_total_units`, a ratio between two
authoritative sums, computed so a viewer can heat-map lanes without every client
reimplementing the division.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from netgravity.orchestrator.schemas.twin import (
    DigitalTwinState,
    FacilityState,
    FlowAggregate,
    FlowState,
    RiskContext,
    StorageMode,
    TwinCalculationStatus,
    TwinKPIs,
    TwinProvenance,
    TwinStateType,
    UnavailableValue,
    ValueStatus,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def make_state_id(
    snapshot_id: str,
    state_type: TwinStateType,
    scenario_id: Optional[str] = None,
) -> str:
    """
    Deterministic state identity.

    The same snapshot, type and scenario always produce the same id, so a
    re-run overwrites its own state rather than accumulating near-duplicates,
    and a client can construct the id it wants to fetch without a lookup.

    Type is part of the key because one snapshot legitimately carries both a
    BASELINE (as-is) and an OPTIMIZED state, and conflating them would let an
    optimum overwrite the observed picture.
    """
    if scenario_id:
        return f"tws_{snapshot_id}_{scenario_id}"
    return f"tws_{snapshot_id}_{state_type.value.lower()}"


# ---------------------------------------------------------------------------
# Element projection
# ---------------------------------------------------------------------------

def _facility_states(
    state: Any,
    rei_rows: Dict[str, Any],
    rf_rows: Dict[str, Any],
    rf_not_computable: Dict[str, str],
) -> List[FacilityState]:
    """
    Project `FacilitySummary` rows, attaching exposure and risk where they exist.

    A facility with no REI row keeps `rei=None` and `NOT_COMPUTED`. It is never
    given 0.0: on a [0,1] relative scale, zero is the value of the *least
    exposed node in the network*, which is a specific and misleading claim to
    make about a node nobody assessed.
    """
    out: List[FacilityState] = []
    for fs in state.facilities:
        rei_row = rei_rows.get(fs.facility_id)
        rf_row = rf_rows.get(fs.facility_id)

        rei_value: Optional[float] = None
        rei_status = ValueStatus.NOT_COMPUTED
        rei_rank: Optional[int] = None
        risk_class: Optional[str] = None

        if rei_row is not None:
            rei_value = rei_row.rei
            rei_rank = rei_row.rank
            risk_class = rei_row.risk_classification.value
            if rei_value is not None:
                rei_status = ValueStatus.AVAILABLE
            elif rei_row.calculation_status.value in ("ERROR", "TIME_LIMIT"):
                rei_status = ValueStatus.FAILED
            else:
                # INFEASIBLE or SKIPPED: the assessment ran and legitimately
                # produced no comparable number.
                rei_status = ValueStatus.NOT_COMPUTABLE

        rf_value: Optional[float] = None
        rf_status = ValueStatus.NOT_COMPUTED
        if rf_row is not None:
            rf_value = rf_row.risk_factor
            rf_status = ValueStatus.AVAILABLE
        elif fs.facility_id in rf_not_computable:
            rf_status = ValueStatus.NOT_COMPUTABLE

        out.append(FacilityState(
            facility_id=fs.facility_id,
            facility_name=fs.facility_name,
            role=fs.role,
            is_open=fs.is_open,
            throughput_units=fs.throughput_units,
            capacity_units=fs.capacity_units,
            utilization_pct=fs.utilization_pct,
            baseline_status=fs.baseline_status,
            contract_status=fs.contract_status,
            closure_cost_charged=fs.closure_cost_charged,
            rei=rei_value,
            rei_status=rei_status,
            rei_rank=rei_rank,
            risk_classification=risk_class,
            risk_factor=rf_value,
            risk_factor_status=rf_status,
        ))
    return out


def _flow_states(state: Any) -> List[FlowState]:
    """Project lane flows, adding each lane's share of total network volume."""
    total_units = sum(f.flow_units for f in state.flows)
    return [
        FlowState(
            origin_id=f.origin_id,
            destination_id=f.destination_id,
            flow_units=f.flow_units,
            transport_cost=f.transport_cost,
            distance_km=f.distance_km,
            carbon_kg=f.carbon_kg,
            share_of_total_units=(
                round(f.flow_units / total_units, 8) if total_units > 0 else 0.0
            ),
        )
        for f in state.flows
    ]


def build_flow_aggregate(flows: Sequence[FlowState]) -> FlowAggregate:
    """
    Roll a flow set up to network level.

    Exists so a client rendering a 100-facility network can show structure
    without paging through every lane. Plain sums of authoritative values.
    """
    by_origin: Dict[str, float] = {}
    by_destination: Dict[str, float] = {}
    total_units = 0.0
    total_cost = 0.0
    total_carbon = 0.0

    for f in flows:
        by_origin[f.origin_id] = by_origin.get(f.origin_id, 0.0) + f.flow_units
        by_destination[f.destination_id] = (
            by_destination.get(f.destination_id, 0.0) + f.flow_units
        )
        total_units += f.flow_units
        total_cost += f.transport_cost
        total_carbon += f.carbon_kg

    return FlowAggregate(
        total_lanes=len(flows),
        total_flow_units=round(total_units, 4),
        total_transport_cost=round(total_cost, 4),
        total_carbon_kg=round(total_carbon, 6),
        units_by_origin={k: round(v, 4) for k, v in sorted(by_origin.items())},
        units_by_destination={k: round(v, 4) for k, v in sorted(by_destination.items())},
    )


def _kpis(state: Any) -> TwinKPIs:
    """
    Carry network KPIs across.

    `cost_components` is copied wholesale rather than field-by-field so a new
    cost category added to `CostBreakdown` reaches the twin without a change
    here — and, more importantly, cannot be silently dropped from a total the
    viewer sees.
    """
    costs = state.costs
    demand = state.demand
    return TwinKPIs(
        business_network_cost=costs.business_network_cost,
        solver_objective=costs.solver_objective,
        shortage_penalty_cost=costs.shortage_penalty_cost,
        cost_components={
            "facility_cost": costs.facility_cost,
            "opening_cost": costs.opening_cost,
            "closure_cost": costs.closure_cost,
            "transport_cost": costs.transport_cost,
            "handling_cost": costs.handling_cost,
            "inventory_cost": costs.inventory_cost,
            "carbon_cost": costs.carbon_cost,
        },
        reconciliation_is_closed=costs.reconciliation_is_closed,
        total_demand=demand.total_demand,
        served_demand=demand.served_demand,
        unserved_demand=demand.unserved_demand,
        demand_fill_rate=demand.demand_fill_rate,
        n_facilities_open=len(state.open_facilities),
        n_facilities_closed=len(state.closed_facilities),
        avg_utilization_pct=state.avg_utilization_pct,
        max_utilization_pct=state.max_utilization_pct,
        # What span the cost and volume figures above cover, so prose written
        # about them can say so instead of assuming one period.
        periods_modelled=getattr(state, "periods_modelled", None),
        cost_per_period=getattr(state, "cost_per_period", None),
        total_carbon_kg=state.total_carbon_kg,
        pct_demand_in_sla=(state.service.pct_demand_in_sla if state.service else None),
    )


def _risk_context(
    registry: Any,
    assessment: Any,
    rei_unavailable_reason: Optional[str],
) -> Optional[RiskContext]:
    """
    Summarise the risk layer's own conclusions.

    Returns None only when neither REI nor RF was attempted — an absent risk
    context means "not part of this workflow", which is different from
    "attempted and failed", and the latter produces a populated context whose
    statuses say so.
    """
    if registry is None and assessment is None and rei_unavailable_reason is None:
        return None

    ctx: Dict[str, Any] = {}

    if registry is not None:
        top = registry.results[0] if registry.results else None
        ctx.update({
            "rei_batch_id": registry.batch_id,
            "rei_batch_status": registry.batch_status.value,
            "rei_snapshot_id": registry.network_snapshot_id,
            "rei_status": (ValueStatus.AVAILABLE if registry.results
                           else ValueStatus.NOT_COMPUTABLE),
            "max_rei": top.rei if top else None,
            "highest_exposure_facility": top.facility_id if top else None,
            "n_facilities_assessed": registry.n_facilities_assessed,
            "n_infeasible": registry.n_infeasible,
        })
    elif rei_unavailable_reason is not None:
        ctx["rei_status"] = ValueStatus.UNAVAILABLE

    if assessment is not None:
        if assessment.results:
            ctx["max_risk_factor"] = assessment.max_risk_factor
            ctx["risk_factor_status"] = ValueStatus.AVAILABLE
        else:
            ctx["risk_factor_status"] = ValueStatus.NOT_COMPUTABLE
            ctx["not_computable_reasons"] = sorted({
                row.not_computable_reason.value
                for row in assessment.not_computable
                if row.not_computable_reason is not None
            })

    return RiskContext(**ctx)


# ---------------------------------------------------------------------------
# State construction
# ---------------------------------------------------------------------------

def build_twin_state(
    *,
    snapshot_id: str,
    state_type: TwinStateType,
    network_state: Any,
    execution_id: Optional[str] = None,
    scenario_id: Optional[str] = None,
    scenario_version: Optional[int] = None,
    parent_snapshot_id: Optional[str] = None,
    scenario_overrides: Optional[Sequence[str]] = None,
    rei_registry: Any = None,
    risk_assessment: Any = None,
    unavailable: Optional[Sequence[UnavailableValue]] = None,
    calculation_status: Optional[TwinCalculationStatus] = None,
) -> DigitalTwinState:
    """
    Build a FULL twin state from an authoritative `NetworkStateResult`.

    Args:
        snapshot_id:    Observed snapshot this state belongs to.
        state_type:     BASELINE, OPTIMIZED or SCENARIO.
        network_state:  A `NetworkStateResult` — the frozen MILP contract, never
                        a raw `OptimizationResult` and never a `CanonicalNetwork`.
                        Accepting only the contract is what makes it impossible
                        for this function to solve anything.
        rei_registry:   `FacilityResilienceRegistry`, when exposure was assessed.
        risk_assessment: `RiskAssessment`, when RF was computed.
        unavailable:    Evidence the orchestrator knows is missing.
        calculation_status: Overrides the inferred status. Supply it when the
                        orchestrator knows something the state cannot show —
                        a stale snapshot, for instance, which looks complete.

    Returns:
        An immutable `DigitalTwinState` with `storage_mode=FULL`.
    """
    missing = list(unavailable or [])

    rei_rows: Dict[str, Any] = {}
    if rei_registry is not None:
        rei_rows = {r.facility_id: r for r in rei_registry.results}

    rf_rows: Dict[str, Any] = {}
    rf_not_computable: Dict[str, str] = {}
    if risk_assessment is not None:
        rf_rows = {
            r.facility_id: r for r in risk_assessment.results
            if r.facility_id is not None
        }
        rf_not_computable = {
            r.facility_id: (r.not_computable_reason.value
                            if r.not_computable_reason else "UNKNOWN")
            for r in risk_assessment.not_computable
            if r.facility_id is not None
        }

    facilities = _facility_states(network_state, rei_rows, rf_rows, rf_not_computable)
    flows = _flow_states(network_state)

    rei_missing_reason = next(
        (u.reason for u in missing if u.capability == "resilience.assess"), None,
    )

    metadata = network_state.metadata
    provenance = TwinProvenance(
        snapshot_id=snapshot_id,
        data_version=network_state.data_version,
        network_id=network_state.network_id,
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        parent_snapshot_id=parent_snapshot_id,
        scenario_overrides=list(scenario_overrides or []),
        run_id=metadata.run_id,
        solver_status=metadata.solver_status.value,
        optimality_label=metadata.optimality_label,
        execution_id=execution_id,
        model_version=metadata.model_version,
        optimization_mode=network_state.optimization_mode,
        is_hypothetical=network_state.is_hypothetical,
        generated_at=_utc_now(),
    )

    status = calculation_status or (
        TwinCalculationStatus.PARTIAL if missing else TwinCalculationStatus.COMPLETE
    )

    return DigitalTwinState(
        state_id=make_state_id(snapshot_id, state_type, scenario_id),
        snapshot_id=snapshot_id,
        scenario_id=scenario_id,
        state_type=state_type,
        storage_mode=StorageMode.FULL,
        provenance=provenance,
        calculation_status=status,
        facilities=facilities,
        flows=flows,
        flow_aggregate=build_flow_aggregate(flows),
        kpis=_kpis(network_state),
        risk=_risk_context(rei_registry, risk_assessment, rei_missing_reason),
        decisions=list(scenario_overrides or []),
        unavailable=missing,
    )


def build_unavailable_state(
    *,
    snapshot_id: str,
    state_type: TwinStateType,
    calculation_status: TwinCalculationStatus,
    unavailable: Sequence[UnavailableValue],
    execution_id: Optional[str] = None,
    scenario_id: Optional[str] = None,
    scenario_version: Optional[int] = None,
    parent_snapshot_id: Optional[str] = None,
    data_version: Optional[str] = None,
    network_id: Optional[str] = None,
    solver_status: Optional[str] = None,
    rei_registry: Any = None,
    risk_assessment: Any = None,
) -> DigitalTwinState:
    """
    Build a state for a run that produced NO usable network result.

    A failed or infeasible run still gets a twin state, deliberately. The
    alternative — publishing nothing — leaves whatever was published last on
    screen, so a viewer sees a healthy network and no indication that the run
    behind it failed. An empty state carrying `FAILED` and the reason is the
    honest picture.

    `kpis` is None rather than a `TwinKPIs` of zeros. Zero cost and zero unmet
    demand describe a network that ran perfectly for free.
    """
    provenance = TwinProvenance(
        snapshot_id=snapshot_id,
        data_version=data_version,
        network_id=network_id,
        scenario_id=scenario_id,
        scenario_version=scenario_version,
        parent_snapshot_id=parent_snapshot_id,
        solver_status=solver_status,
        execution_id=execution_id,
        # Nothing was evaluated, so nothing here describes observed reality.
        # BASELINE is the one type permitted to claim otherwise, and only when a
        # result actually exists.
        is_hypothetical=True,
        generated_at=_utc_now(),
    )
    return DigitalTwinState(
        state_id=make_state_id(snapshot_id, state_type, scenario_id),
        snapshot_id=snapshot_id,
        scenario_id=scenario_id,
        state_type=state_type,
        storage_mode=StorageMode.FULL,
        provenance=provenance,
        calculation_status=calculation_status,
        facilities=[],
        flows=[],
        flow_aggregate=FlowAggregate(),
        kpis=None,
        risk=_risk_context(rei_registry, risk_assessment, None),
        unavailable=list(unavailable),
    )


# ---------------------------------------------------------------------------
# Delta compression
# ---------------------------------------------------------------------------

#: Below this absolute difference two float readings are treated as equal.
#: Solver output carries floating-point noise; without a tolerance every lane
#: would look "changed" and a delta would be no smaller than a full copy.
_EPS = 1e-6


def _facility_differs(a: FacilityState, b: FacilityState) -> bool:
    return (
        a.is_open != b.is_open
        or abs(a.throughput_units - b.throughput_units) > _EPS
        or abs(a.capacity_units - b.capacity_units) > _EPS
        or abs(a.utilization_pct - b.utilization_pct) > _EPS
        or a.rei != b.rei
        or a.risk_factor != b.risk_factor
        or a.rei_rank != b.rei_rank
        or a.risk_classification != b.risk_classification
        or a.closure_cost_charged != b.closure_cost_charged
        or a.contract_status != b.contract_status
    )


def _flow_differs(a: FlowState, b: FlowState) -> bool:
    return (
        abs(a.flow_units - b.flow_units) > _EPS
        or abs(a.transport_cost - b.transport_cost) > _EPS
    )


def to_delta(full: DigitalTwinState, base: DigitalTwinState) -> DigitalTwinState:
    """
    Compress a FULL scenario state against a baseline.

    Keeps only facilities and lanes that differ, plus removals — which a
    changed-entries list cannot express, since "this lane no longer carries
    flow" looks identical to "this lane was not mentioned".

    KPIs, risk and provenance are NOT compressed. They are small, fixed-size,
    and are exactly what a viewer reads first; making them require a base lookup
    would trade a real cost for an imaginary saving.

    A scenario that changes everything compresses to nothing, and that is
    correct — the delta is then the same size as the full state, never larger.

    Uses `model_copy`, which skips validation. Both storage invariants are set
    correctly here by construction, and re-running validation would mean a full
    dump/reload of every facility and lane on the write path. States arriving
    from outside are validated at construction, which is where an unchecked one
    could actually appear.
    """
    base_facilities = {f.facility_id: f for f in base.facilities}
    base_lanes = {f.lane_key: f for f in base.flows}

    changed_facilities = [
        f for f in full.facilities
        if f.facility_id not in base_facilities
        or _facility_differs(f, base_facilities[f.facility_id])
    ]
    present_facility_ids = {f.facility_id for f in full.facilities}
    removed_facility_ids = sorted(
        fid for fid in base_facilities if fid not in present_facility_ids
    )

    changed_flows = [
        f for f in full.flows
        if f.lane_key not in base_lanes or _flow_differs(f, base_lanes[f.lane_key])
    ]
    present_lane_keys = {f.lane_key for f in full.flows}
    removed_lane_keys = sorted(k for k in base_lanes if k not in present_lane_keys)

    return full.model_copy(update={
        "storage_mode": StorageMode.DELTA,
        "base_state_id": base.state_id,
        "facilities": changed_facilities,
        "flows": changed_flows,
        "removed_facility_ids": removed_facility_ids,
        "removed_lane_keys": removed_lane_keys,
    })


def apply_delta(delta: DigitalTwinState, base: DigitalTwinState) -> DigitalTwinState:
    """
    Rebuild a FULL state from a delta and its base.

    The inverse of `to_delta`. Round-tripping a state through both must return
    the original facility and flow sets exactly; a test asserts it.
    """
    if delta.storage_mode is not StorageMode.DELTA:
        return delta
    if delta.base_state_id != base.state_id:
        raise ValueError(
            f"Delta '{delta.state_id}' declares base '{delta.base_state_id}' but was "
            f"given '{base.state_id}'. Applying a delta to the wrong base would "
            f"produce a network state that never existed."
        )

    facilities = {f.facility_id: f for f in base.facilities}
    for fid in delta.removed_facility_ids:
        facilities.pop(fid, None)
    for f in delta.facilities:
        facilities[f.facility_id] = f

    lanes = {f.lane_key: f for f in base.flows}
    for key in delta.removed_lane_keys:
        lanes.pop(key, None)
    for f in delta.flows:
        lanes[f.lane_key] = f

    merged_facilities = sorted(facilities.values(), key=lambda f: f.facility_id)
    merged_flows = sorted(lanes.values(), key=lambda f: (f.origin_id, f.destination_id))

    return delta.model_copy(update={
        "storage_mode": StorageMode.FULL,
        "base_state_id": None,
        "facilities": merged_facilities,
        "flows": merged_flows,
        "removed_facility_ids": [],
        "removed_lane_keys": [],
    })


__all__ = [
    "make_state_id",
    "build_twin_state",
    "build_unavailable_state",
    "build_flow_aggregate",
    "to_delta",
    "apply_delta",
]
