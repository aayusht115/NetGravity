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

    def __init__(self) -> None:
        self._snapshots: Dict[str, NetworkSnapshot] = {}
        self._current_id: Optional[str] = None
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
            if make_current or self._current_id is None:
                self._current_id = snapshot_id

            logger.info(
                "orchestrator.snapshot.registered snapshot_id=%s network_id=%s version=%s",
                snapshot_id, frozen.network_id, data_version,
            )
            return snapshot

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
        Verify a snapshot is still the current observed state.

        Raises:
            StaleSnapshotError: the observed network has moved on. Results from
                incompatible versions must never be combined, so the run stops.
        """
        snap = self.get(snapshot_id)
        if self._current_id is not None and snapshot_id != self._current_id:
            raise StaleSnapshotError(
                f"Execution is pinned to snapshot '{snapshot_id}' but the current "
                f"observed snapshot is '{self._current_id}'. Results from different "
                f"network versions must not be combined; re-plan against the "
                f"current snapshot or escalate.",
                context={"pinned": snapshot_id, "current": self._current_id},
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
            return record

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
