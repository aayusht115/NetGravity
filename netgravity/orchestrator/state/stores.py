"""
Orchestrator — State, snapshot and scenario stores.

This module enforces the single most important invariant in the control plane:

    HYPOTHETICAL SCENARIO STATE CAN NEVER OVERWRITE OBSERVED NETWORK STATE.

Observed networks live in the snapshot store, keyed by a content hash and
frozen. Scenarios are a separate overlay that reference a parent snapshot by id
and are always tagged `is_hypothetical=True`. There is deliberately no API that
writes a scenario back into a snapshot.

Storage is in-memory behind narrow interfaces. A database can replace the
internals later without changing a caller.
"""

from __future__ import annotations

import copy
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.orchestrator.exceptions import (
    InvalidScenarioError,
    MissingDataError,
    StaleSnapshotError,
)
from netgravity.schemas.network import CanonicalNetwork

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Snapshots — observed network state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkSnapshot:
    """
    An immutable, content-addressed view of the OBSERVED network.

    `data_version` reuses `CanonicalNetwork.compute_data_version()` — a hash of
    facilities, products, demands and lanes — so two snapshots with the same id
    provably describe the same network.

    Frozen dataclass, and `network` is deep-copied on ingest, so nothing handed
    out can mutate the stored original.
    """
    snapshot_id: str
    network_id: str
    data_version: str
    network: CanonicalNetwork
    created_at: str = field(default_factory=_utc_now)
    label: str = ""
    # Observed state is never hypothetical. Present so every artefact in the
    # system answers the same question the same way.
    is_hypothetical: bool = False


class SnapshotManager:
    """
    Registry of observed network snapshots.

    Also the stale-state detector: an execution pins a `snapshot_id` at intake,
    and any later attempt to use a snapshot that is no longer current is caught
    rather than silently producing results from mixed network versions.
    """

    #: Optional durable backing, supplied by the hosting application.
    #:
    #: A callable pair rather than a database handle, so this package keeps no
    #: dependency on the application layer or on any storage technology:
    #:
    #:     persist(snapshot)  -> None      called after each new registration
    #:     restore()          -> Iterable[NetworkSnapshot]
    #:
    #: Left unset the manager behaves exactly as before: in memory, for the
    #: lifetime of the process.
    persist_hook = None
    restore_hook = None

    def __init__(self) -> None:
        self._snapshots: Dict[str, NetworkSnapshot] = {}
        self._current_id: Optional[str] = None
        #: Current snapshot PER NETWORK LINEAGE, keyed by `network_id`.
        #:
        #: Staleness is a statement about one network moving on, not about the
        #: process holding several networks at once. With a single network this
        #: is always {network_id: _current_id} and behaviour is unchanged; with
        #: several (one per project) it is what stops project B's snapshot from
        #: being called stale merely because project A ingested more recently.
        self._current_by_network: Dict[str, str] = {}
        self._lock = threading.RLock()

    def register(
        self,
        network: CanonicalNetwork,
        *,
        label: str = "",
        make_current: bool = True,
    ) -> NetworkSnapshot:
        """
        Freeze a network as an observed snapshot.

        The network is deep-copied, so subsequent mutation of the caller's
        object cannot alter stored observed state.
        """
        with self._lock:
            frozen = network.model_copy(deep=True)
            data_version = frozen.data_version or frozen.compute_data_version()
            snapshot_id = f"snap_{data_version[:12]}"

            existing = self._snapshots.get(snapshot_id)
            if existing is not None:
                # Re-registering identical content is not a new version, but it
                # does re-assert that this is the live snapshot for its network.
                self._current_by_network[existing.network_id] = snapshot_id
                if make_current:
                    self._current_id = snapshot_id
                return existing

            snapshot = NetworkSnapshot(
                snapshot_id=snapshot_id,
                network_id=frozen.network_id,
                data_version=data_version,
                network=frozen,
                label=label,
            )
            self._snapshots[snapshot_id] = snapshot
            # Newest registration always becomes current FOR ITS OWN NETWORK,
            # regardless of `make_current` — that flag governs the global
            # pointer used by callers that ask for "the" current snapshot, not
            # which version of this network is live.
            self._current_by_network[frozen.network_id] = snapshot_id
            if make_current or self._current_id is None:
                self._current_id = snapshot_id

            logger.info(
                "orchestrator.snapshot.registered snapshot_id=%s network_id=%s version=%s",
                snapshot_id, frozen.network_id, data_version,
            )

        # Outside the lock: a durable write must not hold up other registrations,
        # and a store that fails must not lose the in-memory snapshot with it.
        if self.persist_hook is not None:
            try:
                self.persist_hook(snapshot)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "orchestrator.snapshot.persist_failed snapshot_id=%s error=%s",
                    snapshot_id, exc,
                )
        return snapshot

    def restore(self) -> int:
        """
        Reload snapshots from the durable store, if one is configured.

        Registered directly into the map rather than through `register()`, so
        reloading does not re-derive ids or move the current pointer. Returns
        how many were restored.
        """
        if self.restore_hook is None:
            return 0
        restored = 0
        for snapshot in self.restore_hook():
            with self._lock:
                if snapshot.snapshot_id in self._snapshots:
                    continue
                self._snapshots[snapshot.snapshot_id] = snapshot
                self._current_by_network[snapshot.network_id] = snapshot.snapshot_id
                if self._current_id is None:
                    self._current_id = snapshot.snapshot_id
            restored += 1
        if restored:
            logger.info("orchestrator.snapshots.restored count=%d", restored)
        return restored

    def get(self, snapshot_id: str) -> NetworkSnapshot:
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            raise MissingDataError(
                f"Network snapshot '{snapshot_id}' is not registered.",
                context={"snapshot_id": snapshot_id, "known": sorted(self._snapshots)},
            )
        return snap

    @property
    def current_id(self) -> Optional[str]:
        return self._current_id

    def current(self) -> NetworkSnapshot:
        if self._current_id is None:
            raise MissingDataError(
                "No network snapshot has been registered. The orchestrator cannot "
                "run without an observed network."
            )
        return self.get(self._current_id)

    def assert_fresh(self, snapshot_id: str) -> NetworkSnapshot:
        """
        Verify a snapshot is still the current observed state OF ITS NETWORK.

        Freshness is scoped to one network's own lineage. A snapshot is stale
        only when a newer snapshot of the SAME `network_id` has been
        registered — which is exactly the situation the guard exists for: the
        observed network moved on, and results from two versions of it must not
        be combined.

        A snapshot belonging to a different network is not stale. In a
        multi-project deployment each project holds its own network, and
        comparing them against one global pointer would reject every project but
        whichever ingested most recently. With a single network this is
        identical to the previous behaviour.

        Raises:
            StaleSnapshotError: a newer version of this network exists.
        """
        snap = self.get(snapshot_id)
        current_for_network = self._current_by_network.get(snap.network_id)
        if current_for_network is not None and snapshot_id != current_for_network:
            raise StaleSnapshotError(
                f"Execution is pinned to snapshot '{snapshot_id}' but the current "
                f"observed snapshot for network '{snap.network_id}' is "
                f"'{current_for_network}'. Results from different network versions "
                f"must not be combined; re-plan against the current snapshot or "
                f"escalate.",
                context={"pinned": snapshot_id, "current": current_for_network,
                         "network_id": snap.network_id},
            )
        return snap

    def list_ids(self) -> List[str]:
        return sorted(self._snapshots)


# ---------------------------------------------------------------------------
# Scenarios — hypothetical overlays
# ---------------------------------------------------------------------------

@dataclass
class ScenarioRecord:
    """
    A hypothetical overlay on a parent snapshot.

    Always tagged hypothetical, always carries `parent_snapshot_id`, and stores
    its own materialised network separately from observed state. Two scenarios
    on the same parent are fully isolated from each other.
    """
    scenario_id: str
    parent_snapshot_id: str
    version: int
    label: str
    overrides: List[str]
    network: CanonicalNetwork
    created_at: str = field(default_factory=_utc_now)
    created_by: str = "system"
    source: str = "user_scenario"
    is_hypothetical: bool = True
    status: str = "CREATED"
    results: Dict[str, Any] = field(default_factory=dict)

    def provenance(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.version,
            "parent_snapshot_id": self.parent_snapshot_id,
            "is_hypothetical": self.is_hypothetical,
            "source": self.source,
            "overrides": list(self.overrides),
            "created_at": self.created_at,
            "created_by": self.created_by,
        }


class ScenarioStore:
    """
    Isolated storage for hypothetical scenarios.

    There is intentionally no `promote_to_observed()` here. Turning a scenario
    into observed reality is a data-ingest decision made outside the control
    plane, not something a what-if run can do to itself.
    """

    #: Durable backing, supplied by the hosting application. Same shape and
    #: same reasoning as `SnapshotManager.persist_hook`: the materialised
    #: scenario network is what `/api/scenarios` reads to report which sites a
    #: scenario introduced and what the builder actually changed, so losing it
    #: on restart leaves a stored scenario unable to explain itself.
    persist_hook = None
    restore_hook = None

    def __init__(self) -> None:
        self._scenarios: Dict[str, ScenarioRecord] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        parent_snapshot_id: str,
        network: CanonicalNetwork,
        label: str,
        overrides: List[str],
        created_by: str = "system",
        source: str = "user_scenario",
        scenario_id: Optional[str] = None,
    ) -> ScenarioRecord:
        """
        Store a materialised scenario network.

        The network is deep-copied on the way in, so the caller cannot later
        mutate stored scenario state by accident.
        """
        with self._lock:
            sid = scenario_id or f"scn_{uuid.uuid4().hex[:10]}"
            if sid in self._scenarios:
                raise InvalidScenarioError(
                    f"Scenario '{sid}' already exists.", context={"scenario_id": sid},
                )
            record = ScenarioRecord(
                scenario_id=sid,
                parent_snapshot_id=parent_snapshot_id,
                version=1,
                label=label,
                overrides=list(overrides),
                network=network.model_copy(deep=True),
                created_by=created_by,
                source=source,
            )
            self._scenarios[sid] = record
            logger.info(
                "orchestrator.scenario.created scenario_id=%s parent=%s overrides=%s",
                sid, parent_snapshot_id, overrides,
            )

        if self.persist_hook is not None:
            try:
                self.persist_hook(record)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "orchestrator.scenario.persist_failed scenario_id=%s error=%s",
                    sid, exc,
                )
        return record

    def restore(self) -> int:
        """Reload materialised scenario networks from the durable store."""
        if self.restore_hook is None:
            return 0
        restored = 0
        for record in self.restore_hook():
            with self._lock:
                if record.scenario_id in self._scenarios:
                    continue
                self._scenarios[record.scenario_id] = record
            restored += 1
        if restored:
            logger.info("orchestrator.scenarios.restored count=%d", restored)
        return restored

    def get(self, scenario_id: str) -> ScenarioRecord:
        rec = self._scenarios.get(scenario_id)
        if rec is None:
            raise InvalidScenarioError(
                f"Scenario '{scenario_id}' not found.",
                context={"scenario_id": scenario_id, "known": sorted(self._scenarios)},
            )
        return rec

    def network_for(self, scenario_id: str) -> CanonicalNetwork:
        """Return a defensive copy of a scenario's network."""
        return self.get(scenario_id).network.model_copy(deep=True)

    def attach_results(self, scenario_id: str, key: str, value: Any) -> None:
        with self._lock:
            self.get(scenario_id).results[key] = value

    def set_status(self, scenario_id: str, status: str) -> None:
        with self._lock:
            self.get(scenario_id).status = status

    def list_for_snapshot(self, snapshot_id: str) -> List[ScenarioRecord]:
        return [s for s in self._scenarios.values() if s.parent_snapshot_id == snapshot_id]

    def list_ids(self) -> List[str]:
        return sorted(self._scenarios)


# ---------------------------------------------------------------------------
# Execution state store (idempotency)
# ---------------------------------------------------------------------------

class ExecutionStateStore:
    """
    Tracks executions and enforces request-level idempotency.

    Replaying the same `request_id` returns the original execution instead of
    re-running the workflow — important for scenario creation, optimization and
    anything with an external effect.
    """

    def __init__(self) -> None:
        self._by_execution: Dict[str, Any] = {}
        self._by_request: Dict[str, str] = {}
        self._approvals: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def find_by_request_id(self, request_id: str) -> Optional[Any]:
        with self._lock:
            exec_id = self._by_request.get(request_id)
            return self._by_execution.get(exec_id) if exec_id else None

    def put(self, context: Any) -> None:
        with self._lock:
            self._by_execution[context.execution_id] = context
            if context.request_id:
                self._by_request.setdefault(context.request_id, context.execution_id)

    def get(self, execution_id: str) -> Optional[Any]:
        return self._by_execution.get(execution_id)

    def put_approval(self, approval: Any) -> None:
        with self._lock:
            self._approvals[approval.approval_id] = approval

    def get_approval(self, approval_id: str) -> Optional[Any]:
        return self._approvals.get(approval_id)

    def list_pending_approvals(self) -> List[Any]:
        return [a for a in self._approvals.values() if a.status == "PENDING"]

    def list_execution_ids(self) -> List[str]:
        return sorted(self._by_execution)
