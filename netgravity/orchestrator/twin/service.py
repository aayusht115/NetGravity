"""
Digital Twin — service interface.

The Digital Twin's public surface:

    DigitalTwinService.update(state)                    publish
    DigitalTwinService.get(snapshot_id, scenario_id)    read
    DigitalTwinService.compare(baseline_id, scenario_id) diff

Framework-free by design. It returns Pydantic models, holds no HTTP or
rendering concerns and imports nothing from Flask, so a future visualisation
frontend — web, notebook or otherwise — attaches without the core moving.

**The service does not calculate.** `compare()` subtracts two authoritative
values and reports which way a metric moved; it never re-derives cost, exposure
or risk. Where a value is missing on either side it emits a `NOT_COMPARABLE`
row naming the absent side, rather than treating absence as zero and reporting
a delta that reads as a real change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from netgravity.orchestrator.schemas.twin import (
    DeltaDirection,
    DigitalTwinState,
    FacilityChange,
    FacilityState,
    FlowPage,
    FlowState,
    LaneChange,
    MetricDelta,
    StorageMode,
    TwinComparison,
    TwinStateRef,
    TwinStateType,
    TwinStateView,
)
from netgravity.orchestrator.twin.builder import apply_delta, build_flow_aggregate, to_delta
from netgravity.orchestrator.twin.store import DigitalTwinStore, TwinStateNotFound

logger = logging.getLogger(__name__)


#: Default page size for flow sets. Chosen so a single default response stays
#: renderable on a large network; callers wanting everything pass limit=0.
DEFAULT_FLOW_LIMIT = 500

#: KPIs compared by `compare()`, in report order. Cost first because it is the
#: figure a decision usually turns on.
_COMPARED_KPIS: Sequence[str] = (
    "business_network_cost",
    "solver_objective",
    "shortage_penalty_cost",
    "total_demand",
    "served_demand",
    "unserved_demand",
    "demand_fill_rate",
    "n_facilities_open",
    "n_facilities_closed",
    "avg_utilization_pct",
    "max_utilization_pct",
    "total_carbon_kg",
    "pct_demand_in_sla",
)

#: Below this two float readings are treated as equal, matching the tolerance
#: the delta compressor uses.
_EPS = 1e-6


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DigitalTwinService:
    """
    Read/write access to Digital Twin state.

    Constructed with a store so tests and future persistence can substitute
    one. The orchestrator holds a single instance.
    """

    def __init__(self, store: Optional[DigitalTwinStore] = None) -> None:
        # `is not None`, NOT `or`: the store defines `__len__`, so an EMPTY
        # store is falsy and `store or DigitalTwinStore()` would silently
        # discard the one the caller passed — every write landing in a store
        # nobody else can see.
        self.store = store if store is not None else DigitalTwinStore()

    # ==================================================================
    # Write
    # ==================================================================

    def update(
        self,
        state: DigitalTwinState,
        *,
        compress_against_baseline: bool = True,
    ) -> TwinStateRef:
        """
        Publish a state.

        A SCENARIO state is stored as a delta against its snapshot's baseline
        when one exists, so N scenarios on a snapshot cost N deltas rather than
        N copies of the network. When no baseline has been published the
        scenario is stored FULL — a delta against nothing is not readable, and
        silently dropping the state would be worse than storing it whole.

        Returns:
            A handle to the published state.
        """
        if not (compress_against_baseline
                and state.state_type is TwinStateType.SCENARIO
                and state.storage_mode is StorageMode.FULL):
            return self.store.put(state)

        # Find-diff-store is one operation. Interleaving a baseline replacement
        # between the find and the store would leave the delta diffed against
        # one baseline and stored against another.
        with self.store.transaction():
            base = self.store.baseline_for_internal(state.snapshot_id)
            if base is not None and base.storage_mode is StorageMode.FULL:
                state = to_delta(state, base)
                logger.info(
                    "twin.state.compressed state_id=%s base=%s facilities=%d flows=%d",
                    state.state_id, base.state_id,
                    len(state.facilities), len(state.flows),
                )
            return self.store.put(state)

    # ==================================================================
    # Read
    # ==================================================================

    def get(
        self,
        snapshot_id: str,
        scenario_id: Optional[str] = None,
        *,
        state_type: Optional[TwinStateType] = None,
        flow_offset: int = 0,
        flow_limit: Optional[int] = DEFAULT_FLOW_LIMIT,
        include_flows: bool = True,
    ) -> TwinStateView:
        """
        Read a state as a complete, renderable view.

        A delta is materialised against its base here, so a caller never has to
        know how the state was stored. Flows are paginated by default: a
        100-facility network can carry thousands of lanes, and returning them
        all on every read makes the common case pay for the rare one.

        Args:
            flow_limit: `None` or `0` returns every lane.
            include_flows: False skips the flow list entirely and returns only
                the aggregate — the cheap path for a summary view.

        Raises:
            TwinStateNotFound: nothing published for this snapshot/scenario.
        """
        # `find_internal`: `_view` builds its own containers and never mutates,
        # so copying the whole state first would be pure cost — and on a large
        # flow set it is exactly what made the summary path as expensive as the
        # full one.
        state = self.store.find_internal(
            snapshot_id, scenario_id=scenario_id, state_type=state_type,
        )
        if state is None:
            raise TwinStateNotFound(
                f"No Digital Twin state published for snapshot '{snapshot_id}'"
                + (f", scenario '{scenario_id}'." if scenario_id else ".")
            )
        return self._view(state, flow_offset, flow_limit, include_flows)

    def get_by_id(
        self,
        state_id: str,
        *,
        flow_offset: int = 0,
        flow_limit: Optional[int] = DEFAULT_FLOW_LIMIT,
        include_flows: bool = True,
    ) -> TwinStateView:
        """Read a state by its identifier."""
        return self._view(
            self.store.get_internal(state_id), flow_offset, flow_limit, include_flows,
        )

    def materialize(self, state_id: str) -> DigitalTwinState:
        """
        Return a state with any delta applied, as a FULL state.

        The form comparison works from, and the form a caller wanting the whole
        picture in one object should ask for.
        """
        state = self.store.get_internal(state_id)
        if state.storage_mode is not StorageMode.DELTA:
            # Handed outward, so it gets the safe copy; `apply_delta` below
            # already builds fresh containers and needs no second one.
            return self.store.get(state_id)
        return apply_delta(state, self.store.get_internal(state.base_state_id or ""))

    def list_states(self, snapshot_id: Optional[str] = None) -> List[TwinStateRef]:
        return self.store.list_refs(snapshot_id)

    def list_scenarios(self, snapshot_id: str) -> List[TwinStateRef]:
        """Every scenario published against one baseline snapshot."""
        return self.store.scenarios_for(snapshot_id)

    # ------------------------------------------------------------------

    def _view(
        self,
        state: DigitalTwinState,
        flow_offset: int,
        flow_limit: Optional[int],
        include_flows: bool,
    ) -> TwinStateView:
        """Materialise, paginate and present."""
        was_delta = state.storage_mode is StorageMode.DELTA
        base_id = state.base_state_id
        if was_delta:
            state = apply_delta(state, self.store.get_internal(base_id or ""))

        total = len(state.flows)
        if include_flows:
            offset = max(0, flow_offset)
            if flow_limit in (None, 0):
                items = list(state.flows[offset:])
                limit = total
            else:
                items = list(state.flows[offset:offset + int(flow_limit)])
                limit = int(flow_limit)
        else:
            offset, items, limit = 0, [], 0

        # For a materialised delta the stored aggregate describes the delta's
        # own lane subset, not the merged network. Recomputing it from the
        # merged flows is a sum of authoritative values, not a new measurement.
        aggregate = (
            build_flow_aggregate(state.flows) if was_delta else state.flow_aggregate
        )

        return TwinStateView(
            state_id=state.state_id,
            snapshot_id=state.snapshot_id,
            scenario_id=state.scenario_id,
            state_type=state.state_type,
            calculation_status=state.calculation_status,
            provenance=state.provenance,
            facilities=list(state.facilities),
            flows=FlowPage(items=items, offset=offset, limit=limit, total=total),
            flow_aggregate=aggregate,
            kpis=state.kpis,
            risk=state.risk,
            decisions=list(state.decisions),
            unavailable=list(state.unavailable),
            materialized_from_delta=was_delta,
            base_state_id=base_id,
        )

    # ==================================================================
    # Compare
    # ==================================================================

    def compare(self, baseline_state_id: str, comparison_state_id: str) -> TwinComparison:
        """
        Diff two published states.

        Both sides are materialised first, so a delta compares correctly against
        its own base. Every KPI present on both sides yields a `MetricDelta`;
        anything present on one side only lands in `incomparable` with the
        reason, because a delta computed against a missing value is a fabricated
        change.

        Comparing across snapshots is permitted but flagged: the difference then
        blends a change in the network with a change in the decision, and cannot
        be attributed to either.
        """
        base = self.materialize(baseline_state_id)
        comp = self.materialize(comparison_state_id)

        warnings: List[str] = []
        same_snapshot = base.snapshot_id == comp.snapshot_id
        if not same_snapshot:
            warnings.append(
                f"Comparing across snapshots ('{base.snapshot_id}' vs "
                f"'{comp.snapshot_id}'): the difference mixes a change in the "
                f"observed network with a change in the decision and cannot be "
                f"attributed to either."
            )

        deltas, incomparable = self._kpi_deltas(base, comp, warnings)

        return TwinComparison(
            baseline_state_id=base.state_id,
            comparison_state_id=comp.state_id,
            baseline_snapshot_id=base.snapshot_id,
            comparison_snapshot_id=comp.snapshot_id,
            comparison_scenario_id=comp.scenario_id,
            same_snapshot=same_snapshot,
            kpi_deltas=deltas,
            facility_changes=self._facility_changes(base.facilities, comp.facilities),
            lane_changes=self._lane_changes(base.flows, comp.flows),
            incomparable=incomparable,
            warnings=warnings,
            generated_at=_utc_now(),
        )

    def compare_scenario(self, snapshot_id: str, scenario_id: str) -> TwinComparison:
        """
        Compare a scenario against its own snapshot's baseline.

        The common case, expressed so a caller does not have to look up two
        state ids to ask the obvious question.
        """
        base = self.store.baseline_for(snapshot_id)
        if base is None:
            raise TwinStateNotFound(
                f"No baseline state published for snapshot '{snapshot_id}'; there is "
                f"nothing to compare scenario '{scenario_id}' against."
            )
        scenario = self.store.find(snapshot_id, scenario_id=scenario_id)
        if scenario is None:
            raise TwinStateNotFound(
                f"No state published for scenario '{scenario_id}' on snapshot "
                f"'{snapshot_id}'."
            )
        return self.compare(base.state_id, scenario.state_id)

    # ------------------------------------------------------------------

    @staticmethod
    def _kpi_deltas(
        base: DigitalTwinState,
        comp: DigitalTwinState,
        warnings: List[str],
    ) -> tuple[List[MetricDelta], List[MetricDelta]]:
        """One `MetricDelta` per compared KPI, split by whether it is comparable."""
        if base.kpis is None or comp.kpis is None:
            missing = []
            if base.kpis is None:
                missing.append(f"baseline '{base.state_id}'")
            if comp.kpis is None:
                missing.append(f"comparison '{comp.state_id}'")
            warnings.append(
                f"No KPI comparison is possible: {' and '.join(missing)} produced no "
                f"KPIs. Every metric is reported NOT_COMPARABLE rather than "
                f"defaulted to zero."
            )
            return [], [
                MetricDelta(
                    metric=name,
                    direction=DeltaDirection.NOT_COMPARABLE,
                    reason=f"no KPIs on {' and '.join(missing)}",
                )
                for name in _COMPARED_KPIS
            ]

        deltas: List[MetricDelta] = []
        incomparable: List[MetricDelta] = []

        for name in _COMPARED_KPIS:
            left = getattr(base.kpis, name, None)
            right = getattr(comp.kpis, name, None)

            if left is None or right is None:
                absent = []
                if left is None:
                    absent.append("baseline")
                if right is None:
                    absent.append("comparison")
                incomparable.append(MetricDelta(
                    metric=name,
                    baseline_value=left,
                    comparison_value=right,
                    direction=DeltaDirection.NOT_COMPARABLE,
                    reason=f"no value on {' and '.join(absent)} side",
                ))
                continue

            left_f, right_f = float(left), float(right)
            abs_delta = round(right_f - left_f, 6)
            pct_delta = (
                round(abs_delta / abs(left_f) * 100.0, 6) if abs(left_f) > _EPS else None
            )
            if abs(abs_delta) <= _EPS:
                direction = DeltaDirection.UNCHANGED
            elif abs_delta > 0:
                direction = DeltaDirection.INCREASED
            else:
                direction = DeltaDirection.DECREASED

            deltas.append(MetricDelta(
                metric=name,
                baseline_value=left_f,
                comparison_value=right_f,
                abs_delta=abs_delta,
                pct_delta=pct_delta,
                direction=direction,
                # A percentage against a zero baseline is undefined, not infinite.
                reason=("baseline is zero, so no percentage change is defined"
                        if pct_delta is None else ""),
            ))

        return deltas, incomparable

    @staticmethod
    def _facility_changes(
        base: Sequence[FacilityState],
        comp: Sequence[FacilityState],
    ) -> List[FacilityChange]:
        """
        Per-facility differences, including those that did not change.

        Unchanged facilities are retained because "Delhi stayed open" is an
        answer to "what did this scenario do to Delhi?", and a caller filtering
        to the changes can do so trivially. Losing the row would make absence
        ambiguous between unchanged and not-present.
        """
        base_map: Dict[str, FacilityState] = {f.facility_id: f for f in base}
        comp_map: Dict[str, FacilityState] = {f.facility_id: f for f in comp}
        out: List[FacilityChange] = []

        for fid in sorted(set(base_map) | set(comp_map)):
            b = base_map.get(fid)
            c = comp_map.get(fid)

            if b is None and c is not None:
                out.append(FacilityChange(
                    facility_id=fid, facility_name=c.facility_name, change="ADDED",
                    comparison_is_open=c.is_open,
                ))
                continue
            if c is None and b is not None:
                out.append(FacilityChange(
                    facility_id=fid, facility_name=b.facility_name, change="REMOVED",
                    baseline_is_open=b.is_open,
                ))
                continue
            assert b is not None and c is not None

            if b.is_open and not c.is_open:
                change = "CLOSED"
            elif not b.is_open and c.is_open:
                change = "OPENED"
            else:
                change = "UNCHANGED_OPEN" if c.is_open else "UNCHANGED_CLOSED"

            out.append(FacilityChange(
                facility_id=fid,
                facility_name=c.facility_name,
                change=change,
                baseline_is_open=b.is_open,
                comparison_is_open=c.is_open,
                throughput_delta=round(c.throughput_units - b.throughput_units, 4),
                utilization_delta_pct=round(c.utilization_pct - b.utilization_pct, 4),
            ))
        return out

    @staticmethod
    def _lane_changes(
        base: Sequence[FlowState],
        comp: Sequence[FlowState],
    ) -> List[LaneChange]:
        """
        Lanes whose flow differs.

        Unchanged lanes are omitted — unlike facilities, whose open/closed state
        is the decision itself. A large network has far more lanes than
        facilities, and listing every unchanged one would swamp the changes it
        exists to surface.
        """
        base_map: Dict[str, FlowState] = {f.lane_key: f for f in base}
        comp_map: Dict[str, FlowState] = {f.lane_key: f for f in comp}
        out: List[LaneChange] = []

        for key in sorted(set(base_map) | set(comp_map)):
            b = base_map.get(key)
            c = comp_map.get(key)

            if b is None and c is not None:
                out.append(LaneChange(
                    origin_id=c.origin_id, destination_id=c.destination_id,
                    change="ADDED", comparison_units=c.flow_units,
                    units_delta=c.flow_units,
                ))
            elif c is None and b is not None:
                out.append(LaneChange(
                    origin_id=b.origin_id, destination_id=b.destination_id,
                    change="REMOVED", baseline_units=b.flow_units,
                    units_delta=round(-b.flow_units, 4),
                ))
            elif b is not None and c is not None:
                delta = c.flow_units - b.flow_units
                if abs(delta) <= _EPS:
                    continue
                out.append(LaneChange(
                    origin_id=c.origin_id, destination_id=c.destination_id,
                    change="INCREASED" if delta > 0 else "DECREASED",
                    baseline_units=b.flow_units, comparison_units=c.flow_units,
                    units_delta=round(delta, 4),
                ))
        return out


__all__ = ["DigitalTwinService", "DEFAULT_FLOW_LIMIT"]
