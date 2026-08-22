"""
Digital Twin — state store.

An in-memory, thread-safe registry of published twin states, keyed by
`state_id`. Narrow enough that a database can replace the internals without a
caller noticing — the same shape as `SnapshotManager` and `ScenarioStore`, for
the same reason.

Two guarantees the store exists to provide:

**Baseline immutability.** States are frozen models, deep-copied on ingest and
on read. A caller cannot reach into a published baseline and change it, and one
scenario cannot mutate the baseline a sibling scenario is measured against.

**Scenario isolation.** Scenarios are indexed under their parent snapshot but
stored independently. Writing scenario B never touches scenario A's record, and
neither touches the baseline. Concurrency tests assert this rather than assuming
it.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Set

from netgravity.orchestrator.schemas.twin import (
    DigitalTwinState,
    StorageMode,
    TwinStateRef,
    TwinStateType,
)

logger = logging.getLogger(__name__)


class TwinStateNotFound(KeyError):
    """Raised when a requested state was never published."""


#: List-valued fields on `DigitalTwinState`. A defensive copy has to replace
#: these containers; it does NOT have to copy their contents.
_LIST_FIELDS = (
    "facilities", "flows", "removed_lane_keys", "removed_facility_ids",
    "decisions", "unavailable",
)


def _defensive_copy(state: DigitalTwinState) -> DigitalTwinState:
    """
    Isolate a state from its caller without deep-copying it.

    Every model in the twin contract is frozen, so no element can be edited in
    place; the only way a caller could reach into stored state is by mutating a
    list container it still holds a reference to. Replacing the containers
    closes that door, and costs a pointer copy per element rather than
    reconstructing every one.

    The difference is not cosmetic. A deep copy of a 50,000-lane state on every
    read made the cheap summary path cost exactly as much as the full one, which
    defeated the point of having it.
    """
    return state.model_copy(update={
        name: list(getattr(state, name)) for name in _LIST_FIELDS
    })


class DigitalTwinStore:
    """
    Registry of published Digital Twin states.

    Retrieval is deterministic: the same `state_id` always returns the same
    content until that exact state is republished.
    """

    def __init__(self) -> None:
        self._states: Dict[str, DigitalTwinState] = {}
        #: snapshot_id → state_ids, so scenarios on one baseline can be listed
        #: without scanning every state in the store.
        self._by_snapshot: Dict[str, List[str]] = {}
        #: base_state_id → delta state_ids stored against it. Needed to keep
        #: deltas honest when their base is republished; see `put`.
        self._dependents: Dict[str, Set[str]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(self, state: DigitalTwinState) -> TwinStateRef:
        """
        Publish a state.

        Deep-copied on the way in, so a caller mutating its own object
        afterwards cannot alter what the store holds. Republishing the same
        `state_id` replaces it — a re-run of the same snapshot and scenario is
        the same state, not a second one.

        **Republishing a base first frees its deltas.** State ids are
        deterministic, so a later run legitimately republishes the same baseline
        with different content — a workflow that also ran REI attaches exposure
        the first one had no way to know. Any delta stored against the old
        content would then materialise against the new content and describe a
        network that never existed. Those deltas are expanded to FULL against
        the base they were actually built from, before it is replaced.
        """
        with self._lock:
            stored = _defensive_copy(state)
            self._release_dependents(stored)
            self._states[stored.state_id] = stored

            if stored.storage_mode is StorageMode.DELTA and stored.base_state_id:
                self._dependents.setdefault(stored.base_state_id, set()).add(
                    stored.state_id,
                )

            ids = self._by_snapshot.setdefault(stored.snapshot_id, [])
            if stored.state_id not in ids:
                ids.append(stored.state_id)

            logger.info(
                "twin.state.published state_id=%s type=%s snapshot=%s scenario=%s "
                "status=%s storage=%s facilities=%d flows=%d",
                stored.state_id, stored.state_type.value, stored.snapshot_id,
                stored.scenario_id, stored.calculation_status.value,
                stored.storage_mode.value, len(stored.facilities), len(stored.flows),
            )
            return self.ref(stored)

    def _release_dependents(self, incoming: DigitalTwinState) -> None:
        """
        Expand any delta whose base is about to change underneath it.

        Called with the lock held. Compares only the facility and flow sets,
        because those are the only parts a delta borrows from its base — a
        republished baseline that differs solely in its timestamp changes
        nothing a dependent reads, and expanding on that would discard the
        compression for no reason.
        """
        previous = self._states.get(incoming.state_id)
        if previous is None:
            return
        dependents = self._dependents.get(incoming.state_id)
        if not dependents:
            return
        if (previous.facilities == incoming.facilities
                and previous.flows == incoming.flows):
            return

        # Imported here: the builder is a peer module and importing it at module
        # scope would make store↔builder a cycle.
        from netgravity.orchestrator.twin.builder import apply_delta

        for delta_id in sorted(dependents):
            delta = self._states.get(delta_id)
            if delta is None or delta.storage_mode is not StorageMode.DELTA:
                continue
            self._states[delta_id] = apply_delta(delta, previous)
            logger.info(
                "twin.delta.expanded state_id=%s base=%s reason=base_content_changed",
                delta_id, incoming.state_id,
            )
        self._dependents.pop(incoming.state_id, None)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, state_id: str) -> DigitalTwinState:
        """
        Return a published state.

        Raises:
            TwinStateNotFound: nothing was ever published under this id.
        """
        # Copied out as well as in: a reader that mutates its result must not be
        # able to corrupt the next reader's view.
        return _defensive_copy(self.get_internal(state_id))

    def get_internal(self, state_id: str) -> DigitalTwinState:
        """
        The stored object itself, with no copy.

        For callers inside the twin that only READ, and that build their own
        containers from what they find — the service's view assembly, above all,
        which slices one page out of a flow list and must not pay to copy the
        rest. Frozen models make this safe: there is nothing here a reader could
        alter even by accident. A caller that intends to hand the result outward
        must use `get`.

        Raises:
            TwinStateNotFound: nothing was ever published under this id.
        """
        with self._lock:
            state = self._states.get(state_id)
        if state is None:
            raise TwinStateNotFound(
                f"No Digital Twin state '{state_id}' has been published."
            )
        return state

    def has(self, state_id: str) -> bool:
        with self._lock:
            return state_id in self._states

    def find(
        self,
        snapshot_id: str,
        *,
        scenario_id: Optional[str] = None,
        state_type: Optional[TwinStateType] = None,
    ) -> Optional[DigitalTwinState]:
        """Look a state up by what it describes rather than by id."""
        found = self.find_internal(
            snapshot_id, scenario_id=scenario_id, state_type=state_type,
        )
        return None if found is None else _defensive_copy(found)

    def find_internal(
        self,
        snapshot_id: str,
        *,
        scenario_id: Optional[str] = None,
        state_type: Optional[TwinStateType] = None,
    ) -> Optional[DigitalTwinState]:
        """
        `find` without the copy. See `get_internal` for when that is safe.

        With no `state_type`, an OPTIMIZED state is preferred over a BASELINE
        one for the same snapshot: a workflow that optimised produced the more
        specific answer, and returning the as-is evaluation instead would show a
        viewer a network nobody asked about.
        """
        with self._lock:
            candidates = [
                self._states[sid] for sid in self._by_snapshot.get(snapshot_id, [])
                if sid in self._states
            ]

        matches = [s for s in candidates if s.scenario_id == scenario_id]
        if state_type is not None:
            matches = [s for s in matches if s.state_type is state_type]
        if not matches:
            return None

        preference = {
            TwinStateType.SCENARIO: 0,
            TwinStateType.OPTIMIZED: 1,
            TwinStateType.BASELINE: 2,
        }
        matches.sort(key=lambda s: preference[s.state_type])
        return matches[0]

    def baseline_for(self, snapshot_id: str) -> Optional[DigitalTwinState]:
        """
        The non-scenario state for a snapshot, if one has been published.

        Used as the base when compressing a scenario to a delta, and as the
        left-hand side of a default comparison.
        """
        return self.find(snapshot_id, scenario_id=None)

    def baseline_for_internal(self, snapshot_id: str) -> Optional[DigitalTwinState]:
        """`baseline_for` without the copy. See `get_internal`."""
        return self.find_internal(snapshot_id, scenario_id=None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """
        Hold the store lock across several operations.

        Compressing a scenario is read-then-write — find the baseline, diff
        against it, store the result — and those three steps must not be
        interleaved with another thread replacing that baseline. Without this,
        a scenario could be diffed against one baseline and stored against
        another, describing a network that never existed.

        `_release_dependents` does not cover the gap: it fires when a base is
        replaced *after* a delta registers against it, and here the replacement
        lands first.

        The lock is an `RLock`, so `put` inside a transaction is safe.
        """
        with self._lock:
            yield

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_refs(self, snapshot_id: Optional[str] = None) -> List[TwinStateRef]:
        """Handles for every published state, optionally filtered by snapshot."""
        with self._lock:
            if snapshot_id is None:
                states = list(self._states.values())
            else:
                states = [
                    self._states[sid]
                    for sid in self._by_snapshot.get(snapshot_id, [])
                    if sid in self._states
                ]
        return [self.ref(s) for s in sorted(states, key=lambda s: s.state_id)]

    def scenarios_for(self, snapshot_id: str) -> List[TwinStateRef]:
        """Every scenario state published against one baseline snapshot."""
        return [r for r in self.list_refs(snapshot_id) if r.scenario_id is not None]

    def list_snapshot_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._by_snapshot)

    def __len__(self) -> int:
        with self._lock:
            return len(self._states)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def ref(state: DigitalTwinState) -> TwinStateRef:
        """
        Build a handle for a state.

        For a DELTA the counts describe what is STORED, not what materialises.
        A ref is a description of the record; `materialize()` is where the
        complete picture is assembled, and reporting the merged counts here
        would make a delta look like a full copy.
        """
        return TwinStateRef(
            state_id=state.state_id,
            snapshot_id=state.snapshot_id,
            scenario_id=state.scenario_id,
            state_type=state.state_type,
            calculation_status=state.calculation_status,
            n_facilities=len(state.facilities),
            n_flows=len(state.flows),
            generated_at=state.provenance.generated_at,
        )

    def is_delta(self, state_id: str) -> bool:
        with self._lock:
            state = self._states.get(state_id)
        return state is not None and state.storage_mode is StorageMode.DELTA


__all__ = ["DigitalTwinStore", "TwinStateNotFound"]
