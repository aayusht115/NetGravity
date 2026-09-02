"""
NetGravity — Binding the stores to durable storage
==================================================
One place where every in-memory store is connected to the database and
reloaded, so start-up order is explicit and the coupling between the engine
package and the application's storage lives at exactly one seam.

`netgravity/` never imports this module. The orchestrator's stores expose a
`persist_hook`/`restore_hook` pair and know nothing about SQLite, JSON columns,
or the application layer — the direction of the dependency is what keeps the
engine usable on its own, which is the same discipline that keeps the
orchestrator blueprint's `authenticator` out of the package.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable, List

from app.backend.services import persistence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Execution traces
# ---------------------------------------------------------------------------

#: How long a sealed trace is kept. Long enough to answer "why did it say that"
#: about anything a planner is still acting on; short enough that the table does
#: not grow without bound. Overridable, because a regulated deployment may have
#: a stated retention period of its own.
_TRACE_RETENTION_DAYS = 90


class _TraceSink:
    """
    The audit logger's durable half.

    Implements `netgravity.orchestrator.audit.audit_logger.TraceSink` by
    structural match rather than by inheritance, which is the whole point of
    that protocol: the engine package must not import the application's
    database to be able to keep a record.
    """

    def save(self, trace: Any) -> None:
        document = trace.to_dict()
        # A trace's own timestamps are ISO strings; the column is numeric so it
        # can be indexed and swept. Parsed from what the trace already carries
        # rather than stamped with "now", so the ordering is the ordering of the
        # executions and not of the writes.
        started_at = _epoch(getattr(trace, "started_at", "")) or time.time()
        persistence.save_execution_trace(
            trace.execution_id,
            document,
            actor_id    = getattr(trace, "actor_id", "") or "",
            intent      = str(getattr(trace, "interpreted_intent", "") or ""),
            workflow    = str(getattr(trace, "workflow_id", "") or ""),
            snapshot_id = str(getattr(trace, "baseline_snapshot_id", "") or ""),
            status      = str(getattr(trace, "final_status", "") or ""),
            started_at  = started_at,
        )
        self._sweep()

    def load(self, execution_id: str) -> Any:
        return persistence.load_execution_trace(execution_id)

    def recent(self, n: int) -> List[Any]:
        return persistence.load_execution_traces(limit=max(1, int(n)))

    def _sweep(self) -> None:
        """Drop traces past the retention window. Cheap, and only occasionally."""
        global _last_trace_sweep
        now = time.time()
        if now - _last_trace_sweep < 3600:
            return
        _last_trace_sweep = now
        try:
            removed = persistence.purge_execution_traces(
                now - _TRACE_RETENTION_DAYS * 86400)
            if removed:
                logger.info("durability.traces.purged rows=%d older_than_days=%d",
                            removed, _TRACE_RETENTION_DAYS)
        except Exception as exc:  # noqa: BLE001 — housekeeping, never the request
            logger.warning("durability.traces.purge_failed error=%s", exc)


_last_trace_sweep = 0.0


def _epoch(iso: str) -> float:
    """An ISO timestamp as seconds, or 0.0 when it cannot be read."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def _persist_snapshot(snapshot: Any) -> None:
    """Write one observed network snapshot to the database."""
    persistence.guarded(persistence.save_snapshot)(
        snapshot.snapshot_id,
        snapshot.network_id,
        {
            "snapshot_id": snapshot.snapshot_id,
            "network_id": snapshot.network_id,
            "data_version": snapshot.data_version,
            "label": snapshot.label,
            "created_at": snapshot.created_at,
            "is_hypothetical": snapshot.is_hypothetical,
            # `CanonicalNetwork` is a Pydantic model and serialises itself. The
            # alternative — a table per record type — would be a second
            # definition of the network schema that has to be kept in step by
            # hand with the one the solver reads.
            "network": snapshot.network.model_dump(mode="json"),
        },
        time.time(),
    )


def _restore_snapshots() -> Iterable[Any]:
    from netgravity.orchestrator.state.stores import NetworkSnapshot
    from netgravity.schemas.network import CanonicalNetwork

    out: List[Any] = []
    for doc in persistence.load_snapshots():
        try:
            out.append(NetworkSnapshot(
                snapshot_id=doc["snapshot_id"],
                network_id=doc["network_id"],
                data_version=doc.get("data_version", ""),
                network=CanonicalNetwork.model_validate(doc["network"]),
                created_at=doc.get("created_at", ""),
                label=doc.get("label", ""),
                is_hypothetical=bool(doc.get("is_hypothetical", False)),
            ))
        except Exception as exc:  # noqa: BLE001
            # One unreadable network must not stop every other project loading.
            logger.error(
                "durability.snapshot_unreadable snapshot_id=%s error=%s",
                doc.get("snapshot_id"), exc,
            )
    return out


# ---------------------------------------------------------------------------
# Materialised scenario networks
# ---------------------------------------------------------------------------

def _persist_scenario_network(record: Any) -> None:
    persistence.guarded(persistence.save_scenario_network)(
        record.scenario_id,
        record.parent_snapshot_id,
        {
            "scenario_id": record.scenario_id,
            "parent_snapshot_id": record.parent_snapshot_id,
            "version": record.version,
            "label": record.label,
            "overrides": list(record.overrides),
            "created_by": record.created_by,
            "source": record.source,
            "status": getattr(record, "status", ""),
            "network": record.network.model_dump(mode="json"),
        },
        time.time(),
    )


def _restore_scenario_networks() -> Iterable[Any]:
    from netgravity.orchestrator.state.stores import ScenarioRecord
    from netgravity.schemas.network import CanonicalNetwork

    out: List[Any] = []
    for doc in persistence.load_scenario_networks():
        try:
            record = ScenarioRecord(
                scenario_id=doc["scenario_id"],
                parent_snapshot_id=doc["parent_snapshot_id"],
                version=int(doc.get("version", 1)),
                label=doc.get("label", ""),
                overrides=list(doc.get("overrides") or []),
                network=CanonicalNetwork.model_validate(doc["network"]),
                created_by=doc.get("created_by", "system"),
                source=doc.get("source", "user_scenario"),
            )
            status = doc.get("status")
            if status:
                record.status = status
            out.append(record)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "durability.scenario_network_unreadable scenario_id=%s error=%s",
                doc.get("scenario_id"), exc,
            )
    return out


# ---------------------------------------------------------------------------
# Uploaded history and signals
# ---------------------------------------------------------------------------

def _bind_upload_stores() -> None:
    """
    Make the demand, capacity and signal stores write through.

    These hold what arrived with an upload — 36 months of demand history, 288
    rows of capacity history, external signals. They are inputs the user
    provided, not results, so losing them means the forecast and the "vs
    recorded" column go quiet after a restart even though the network is still
    bound.
    """
    from app.backend.services.demand_history_store import (
        capacity_history_store,
        demand_history_store,
        uploaded_signal_store,
    )

    demand_history_store.bind_persistence(
        lambda nid, rows: persistence.guarded(persistence.save_network_data)(
            "demand_history", nid, rows),
        lambda: persistence.load_network_data("demand_history"),
    )
    uploaded_signal_store.bind_persistence(
        lambda nid, rows: persistence.guarded(persistence.save_network_data)(
            "signals", nid, rows),
        lambda: persistence.load_network_data("signals"),
    )
    capacity_history_store.bind_persistence(
        lambda nid, rows: persistence.guarded(persistence.save_network_data)(
            "capacity_history", nid, rows),
        lambda: persistence.load_network_data("capacity_history"),
    )

    # What was uploaded to each project, how it was mapped, and what was
    # committed from it. Keyed by project rather than network, because a
    # preview exists before any network does.
    #
    # This was a module-level dict in `api/ingestion_dynamic.py`, which made
    # `commit` fail on any deployment running more than one worker — parse
    # lands on one process, commit on another — and left every solved project
    # reporting "Uploaded Files (0)" with no way to audit the data behind its
    # own numbers.
    from app.backend.services.dataset_store import dataset_store

    dataset_store.bind_persistence(
        lambda pid, doc: persistence.guarded(persistence.save_network_data)(
            "dataset", pid, doc),
        lambda: persistence.load_network_data("dataset"),
    )


# ---------------------------------------------------------------------------
def install(orchestrator: Any) -> dict:
    """
    Connect every store to the database and reload what is already there.

    Called once, at application start, after the orchestrator is built and
    before the first request is served. Returns a small report so the health
    endpoint can state what was restored rather than implying durability it
    does not have.
    """
    from app.backend.services.analysis_store import analysis_service
    from app.backend.services.project_registry import project_registry
    from app.backend.services.security import auth_service

    report = {
        # Which store, and where. "Is my work being kept, and where" should not
        # have to be inferred from a log line.
        "engine": persistence.database.kind,
        "database": persistence.database.path,
    }

    # Accounts and sessions first: nothing else is reachable without them.
    auth_service.load()
    report["users"] = len(auth_service._users_by_id)      # noqa: SLF001 — reporting only
    report["sessions"] = len(auth_service._sessions)      # noqa: SLF001

    if orchestrator is not None:
        # The workings, not just the answers.
        #
        # Execution traces lived in a 500-entry ring buffer in memory, so a
        # restart — or the 501st execution — discarded the record of which
        # capability produced a number, from which snapshot, under which
        # governance verdict. That is the one record whose entire purpose is to
        # be readable long afterwards, and it was the only one guaranteed not
        # to survive.
        orchestrator.audit.attach_sink(_TraceSink())
        report["execution_traces"] = persistence.count_execution_traces()

        orchestrator.snapshots.persist_hook = _persist_snapshot
        orchestrator.snapshots.restore_hook = _restore_snapshots
        report["snapshots"] = orchestrator.snapshots.restore()

        orchestrator.scenarios.persist_hook = _persist_scenario_network
        orchestrator.scenarios.restore_hook = _restore_scenario_networks
        report["scenario_networks"] = orchestrator.scenarios.restore()

    project_registry.load()
    report["projects"] = len(project_registry.list_all())

    _bind_upload_stores()
    from app.backend.services.demand_history_store import (
        capacity_history_store, demand_history_store, uploaded_signal_store,
    )
    report["demand_history"] = demand_history_store.load()
    report["signals"] = uploaded_signal_store.load()
    report["capacity_history"] = capacity_history_store.load()
    from app.backend.services.dataset_store import dataset_store
    report["datasets"] = dataset_store.load()

    # The analysis computed FROM those networks. Without this a restart kept
    # every project and every upload and threw away every KPI derived from
    # them, so the first person to open a project after a deploy paid for a
    # full MILP solve of a network that had already been solved.
    report["analyses"] = analysis_service.load()

    logger.info("durability.installed %s", report)
    return report
