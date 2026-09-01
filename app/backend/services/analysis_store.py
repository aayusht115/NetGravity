"""
NetGravity — The analysis computed from an uploaded network
============================================================
A project's analysis is the authoritative KPI layer's complete reading of one
network snapshot: network KPIs, per-facility metrics, resilience and risk, lane
flows, triggered thresholds and the evidence package behind all of them.

Why this exists
---------------
It was recomputed from scratch on every request that touched it. Five KPI
endpoints each called the orchestrator independently, and the only thing
between them and a fresh MILP solve was a 120-second in-process cache written
AFTER the solve returned — so opening a project fired five concurrent solves of
the same network, and two minutes later fired five more. On the client network
that is twenty to forty seconds each.

Nothing about that computation is time-varying. A snapshot is immutable by
construction (`SnapshotManager.assert_fresh`), the solver is deterministic, and
the KPI layer is a pure function of the execution it reads. Recomputing it
produces the same answer at full price — and where a MILP has ties, it can
produce a DIFFERENT optimum of the same cost, so a user paging between screens
could watch facility assignments change underneath them.

So the analysis is computed once per network version, kept, and served.

Keyed by (snapshot_id, data_version)
------------------------------------
`data_version` is the snapshot's own version string. A stored analysis is only
ever returned for the exact network version it was computed from; re-uploading
data produces a new snapshot and a new analysis rather than showing yesterday's
answer against today's numbers. There is no TTL, because time is not what makes
this stale — a different network is.

Single-flight
-------------
Concurrent requests for an analysis that does not exist yet share ONE solve.
The previous arrangement had every caller start its own, then race to write the
cache, so the cost of a cold start scaled with how many panels the page opened
at once.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: Bumped whenever the SHAPE of a serialised analysis changes.
#:
#: The cache key is `(snapshot_id, data_version, variant)`, and `data_version`
#: describes the NETWORK — it is a hash of facilities, products, demands and
#: lanes. Adding a block to the analysis document does not change any of those,
#: so every existing entry stays valid by that key and is served forever: the
#: code produces the new shape and no caller ever sees it, on exactly the
#: networks that have been analysed before.
#:
#: That is not hypothetical. It is how `horizon.by_facility` came back empty on
#: a network whose horizon was solved correctly — the answer was right and a
#: document written before the field existed was what got returned.
_ANALYSIS_VERSION = 2


class AnalysisService:
    """Computes a snapshot's analysis at most once, keeps it, and serves it."""

    def __init__(self) -> None:
        self._memory: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        #: One lock per snapshot, so a solve of network A never blocks a read
        #: of network B.
        self._solving: Dict[str, threading.Lock] = {}

    # ------------------------------------------------------------------
    def _slot(self, snapshot_id: str) -> threading.Lock:
        with self._lock:
            lock = self._solving.get(snapshot_id)
            if lock is None:
                lock = threading.Lock()
                self._solving[snapshot_id] = lock
            return lock

    def _cached(self, snapshot_id: str, data_version: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._memory.get(snapshot_id)
        if entry and entry.get("data_version") == data_version:
            return entry
        from app.backend.services import persistence
        try:
            stored = persistence.load_analysis(snapshot_id, data_version)
        except Exception as exc:  # noqa: BLE001 — a read failure recomputes
            logger.error("analysis.read_failed snapshot_id=%s error=%s", snapshot_id, exc)
            return None
        if stored:
            with self._lock:
                self._memory[snapshot_id] = stored
        return stored

    @staticmethod
    def _key(snapshot_id: str, variant: str = "") -> str:
        """
        The storage key for one analysis of one snapshot.

        A `variant` lets a snapshot hold more than one analysis produced by a
        DIFFERENT workflow — the resilience assessment is the case that needs
        it, because REI re-solves the network once per facility and so must be
        requested and cached separately from the baseline solve rather than
        added to it.

        Composed into the key rather than given its own table or its own
        service: one store, one single-flight lock, one persistence path. A
        second cache for the second kind of analysis is how the two come to
        expire under different rules.

        `_ANALYSIS_VERSION` rides in the same key, so a change to the document's
        SHAPE invalidates entries that a change to the network would not.
        """
        variant = f"v{_ANALYSIS_VERSION}:{variant}" if variant else f"v{_ANALYSIS_VERSION}"
        return f"{snapshot_id}#{variant}"

    def peek(self, snapshot_id: str, data_version: str,
             variant: str = "") -> Optional[Dict[str, Any]]:
        """The stored analysis if there is one, without computing anything."""
        return self._cached(self._key(snapshot_id, variant), data_version)

    def get(self, snapshot_id: str, data_version: str,
            compute: Callable[[], Dict[str, Any]],
            variant: str = "") -> Dict[str, Any]:
        """
        This snapshot's analysis, computing it once if it is not already known.

        `compute` runs the solve and returns the serialised analysis. It is
        called at most once per (snapshot, version, variant) across all threads.
        """
        snapshot_id = self._key(snapshot_id, variant)
        cached = self._cached(snapshot_id, data_version)
        if cached is not None:
            return cached

        with self._slot(snapshot_id):
            # Re-check inside the lock: another thread may have finished the
            # solve while this one waited, and it must not run a second.
            cached = self._cached(snapshot_id, data_version)
            if cached is not None:
                return cached

            started = time.time()
            analysis = compute()
            analysis["snapshot_id"] = snapshot_id
            analysis["data_version"] = data_version
            analysis["computed_at"] = time.time()
            analysis["compute_seconds"] = round(analysis["computed_at"] - started, 3)

            with self._lock:
                self._memory[snapshot_id] = analysis

            from app.backend.services import persistence
            persistence.guarded(persistence.save_analysis)(
                snapshot_id, data_version, analysis, analysis["computed_at"])
            logger.info(
                "analysis.computed snapshot_id=%s version=%s seconds=%.1f",
                snapshot_id, data_version, analysis["compute_seconds"])
            return analysis

    def invalidate(self, snapshot_id: str) -> None:
        """
        Forget every analysis of a snapshot, in memory and in the database.

        Every VARIANT, not just the baseline one. Dropping the baseline while
        leaving a resilience assessment behind would leave the two describing
        different versions of the same network, which is worse than having
        neither.
        """
        prefix = f"{snapshot_id}#"
        with self._lock:
            keys = [k for k in self._memory
                    if k == snapshot_id or k.startswith(prefix)]
            for key in keys:
                self._memory.pop(key, None)
        from app.backend.services import persistence
        for key in {snapshot_id, *keys}:
            persistence.guarded(persistence.delete_analysis)(key)

    def load(self) -> int:
        """Warm the in-memory view from the database at start-up."""
        from app.backend.services import persistence
        count = 0
        for snapshot_id, data_version, document, computed_at in persistence.load_all_analyses():
            if not document:
                continue
            document["snapshot_id"] = snapshot_id
            document["data_version"] = data_version
            document["computed_at"] = computed_at
            with self._lock:
                self._memory[snapshot_id] = document
            count += 1
        return count


#: One service per process.
analysis_service = AnalysisService()


def _horizon_of(ctx: Any) -> Dict[str, Any]:
    """
    The planning horizon the solve in `ctx` actually covered.

    Empty `period_labels` is the honest answer for a network whose upload
    carried no calendar, and `periods_modelled` is then 1 — never a fabricated
    "Period 1" standing in for a month the data never named.
    """
    states = getattr(ctx, "network_states", None) or {}
    state = (states.get("optimization.solve")
             or (next(iter(states.values())) if len(states) == 1 else None))
    if state is None:
        return {"periods_modelled": 1, "period_labels": {},
                "first_period": None, "last_period": None, "cost_per_period": None}

    labels = dict(getattr(state, "period_labels", None) or {})
    ordered = [labels[k] for k in sorted(labels, key=lambda s: int(s))] if labels else []

    # Solved utilisation and throughput per facility per period.
    #
    # A KPIResult carries one number, so a series cannot travel as one — and
    # without it a period selector has nothing solved to show for the month a
    # user picked, which is why choosing a period changed the recorded history
    # on screen and nothing the solver produced. Read from the state, not
    # recomputed: these are the same figures `peak_utilization_pct` is the
    # maximum of.
    by_facility: Dict[str, Any] = {}
    for facility in getattr(state, "facilities", None) or []:
        utilisation = dict(getattr(facility, "utilization_by_period", None) or {})
        throughput = dict(getattr(facility, "throughput_by_period", None) or {})
        if not utilisation and not throughput:
            continue
        by_facility[facility.facility_id] = {
            "utilisation": utilisation,
            "throughput": throughput,
            "peak_utilisation_pct": getattr(facility, "peak_utilization_pct", None),
        }

    return {
        "periods_modelled": int(getattr(state, "periods_modelled", 1) or 1),
        "period_labels": labels,
        "first_period": ordered[0] if ordered else None,
        "last_period": ordered[-1] if ordered else None,
        "cost_per_period": getattr(state, "cost_per_period", None),
        "by_facility": by_facility,
    }


def serialise_analysis(registry: Any, ctx: Any) -> Dict[str, Any]:
    """
    Everything the KPI endpoints report, read once from one execution.

    Every registry call here is a pure read of the context the solve produced,
    so gathering them together costs nothing beyond the solve that has already
    happened — and it is what lets a later request answer any of the five
    questions without going near the engine.
    """
    network = registry.network_kpis(ctx)
    facilities = registry.facility_kpis(ctx)
    resilience = registry.facility_resilience_kpis(ctx)
    risk = registry.facility_risk_kpis(ctx)

    def dump(results: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v.model_dump(mode="json") for k, v in results.items()}

    return {
        "execution_id": getattr(ctx, "execution_id", ""),
        # What span of time every cost and volume figure below covers.
        #
        # Read from the solve itself, not inferred. Without it a caller has a
        # cost and no way to know whether it is one month or twelve — and the
        # two differ by a factor of twelve with nothing in the number to say
        # which. Publishing it here means no consumer has to divide, and the
        # per-period figure has one owner rather than one per screen.
        "horizon": _horizon_of(ctx),
        "kpis": dump(network),
        "triggered_thresholds": [
            t.model_dump(mode="json")
            for t in registry.evaluate_thresholds(list(network.values()))
        ],
        "facilities": {fid: dump(metrics) for fid, metrics in facilities.items()},
        "facility_resilience": {fid: dump(metrics) for fid, metrics in resilience.items()},
        "facility_risk": {fid: dump(metrics) for fid, metrics in risk.items()},
        "flows": registry.flow_kpis(ctx),
        "evidence": registry.evidence_package(ctx).model_dump(mode="json"),
    }
