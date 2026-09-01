"""
NetGravity — Uploaded Demand History Store
==========================================
Holds the observed demand history that came in with a user's upload, and hands
it to the orchestrator's `forecast.demand` capability.

Why this exists
---------------
The forecasting engine, the `history_provider` service hook and the
snapshot-scoped `series_for_network` filter were all already built and tested.
The only missing link was that an *uploaded* network's history never reached
them: `app.py` registered a provider that reads the ingestion staging zone on
disk, and the API upload path writes to no staging zone. So every uploaded
network reported "no observed demand history", and the forecast screen stayed
empty no matter how much history the workbook contained — the sample workbook
carries 504 observations across 36 months.

This module does not forecast. It stores observations and returns them for a
snapshot, keyed by `network_id` so one project's history can never be served
for another project's network.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Sequence, Tuple

logger = logging.getLogger(__name__)



class _DurableByNetwork:
    """
    Write-through persistence for the three upload-scoped stores below.

    Each holds something the CLIENT provided with their upload — demand
    history, capacity history, external signals — keyed by `network_id`. They
    were process-local, so a restart left a network still bound to a project
    but with its history gone: the forecast screen went quiet and the "vs
    recorded" column reverted to a dash, on data the user had definitely
    uploaded.

    The hosting application supplies the two callables; this module holds no
    opinion about where the rows go.
    """

    #: (network_id, rows) -> None
    _persist = None
    #: () -> {network_id: rows}
    _restore = None

    def bind_persistence(self, persist, restore) -> None:
        self._persist = persist
        self._restore = restore

    def _write_through(self, network_id: str, rows) -> None:
        if self._persist is None:
            return
        self._persist(network_id, self._serialise(rows))

    def load(self) -> int:
        """Reload every network's rows. Returns how many networks were restored."""
        if self._restore is None:
            return 0
        restored = 0
        for network_id, rows in (self._restore() or {}).items():
            try:
                with self._lock:
                    self._by_network[network_id] = self._deserialise(rows)
                restored += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "history.restore_failed network_id=%s error=%s", network_id, exc)
        return restored

    # Plain dict rows by default; the demand store overrides both.
    @staticmethod
    def _serialise(rows):
        return [dict(r) for r in rows]

    @staticmethod
    def _deserialise(rows):
        return [dict(r) for r in rows]


class DemandHistoryStore(_DurableByNetwork):
    """Observed demand history, keyed by `network_id`, written through to disk."""

    @staticmethod
    def _serialise(rows):
        # `DemandTimeSeries` is a Pydantic model and serialises itself; a plain
        # dict is accepted too, because the staging-file path produces those.
        return [r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r)
                for r in rows]

    @staticmethod
    def _deserialise(rows):
        from netgravity.forecasting.schemas import DemandTimeSeries
        return [DemandTimeSeries.model_validate(r) for r in rows]

    def __init__(self) -> None:
        self._by_network: Dict[str, List[Any]] = {}
        self._lock = threading.Lock()

    # -- writing -------------------------------------------------------
    def put(self, network_id: str, series: Sequence[Any]) -> None:
        with self._lock:
            self._by_network[network_id] = list(series)
        self._write_through(network_id, series)
        logger.info(
            "demand_history.stored network_id=%s series=%d", network_id, len(series)
        )

    # -- reading -------------------------------------------------------
    def for_snapshot(self, snapshot: Any) -> Tuple[List[Any], List[str]]:
        """`history_provider` contract: snapshot -> (series, warnings)."""
        network_id = getattr(getattr(snapshot, "network", None), "network_id", None)
        if not network_id:
            return [], ["snapshot carries no network id, so no history could be matched"]
        with self._lock:
            series = list(self._by_network.get(network_id, []))
        if not series:
            return [], [
                f"no uploaded demand history is held for network '{network_id}'"
            ]
        return series, []

    def has(self, network_id: str) -> bool:
        with self._lock:
            return bool(self._by_network.get(network_id))


class UploadedSignalStore(_DurableByNetwork):
    """External signals that arrived with an upload, keyed by `network_id`.

    Signals are not part of `CanonicalNetwork` — they are context about the
    world rather than structure — so they are held beside it rather than
    forced into the network schema.
    """

    def __init__(self) -> None:
        self._by_network: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, network_id: str, signals: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            self._by_network[network_id] = list(signals)
        self._write_through(network_id, signals)
        logger.info(
            "signals.stored network_id=%s count=%d", network_id, len(signals)
        )

    def get(self, network_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._by_network.get(network_id, []))


class CapacityHistoryStore(_DurableByNetwork):
    """Recorded available/used capacity per facility and period, by `network_id`.

    This is measurement, not model output: the client's own record of how much
    capacity each site had and how much of it was used. It is kept beside the
    network for the same reason signals are — `FacilityRecord` carries a
    *stated* capacity, and a monthly series of observations is not that.

    Nothing here computes a KPI. `latest_utilisation()` divides two numbers the
    client supplied on the same row, which is what those two columns mean.
    """

    def __init__(self) -> None:
        self._by_network: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, network_id: str, rows: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            self._by_network[network_id] = [dict(r) for r in rows]
        self._write_through(network_id, rows)
        logger.info(
            "capacity_history.stored network_id=%s rows=%d", network_id, len(rows)
        )

    def get(self, network_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._by_network.get(network_id, [])]

    def latest_utilisation(self, network_id: str) -> Dict[str, Dict[str, Any]]:
        """
        Per facility: the most recent recorded period, and its utilisation.

        Returns `{facility_id: {"period", "available", "used", "utilisationPct"}}`.
        A facility whose latest row lacks either figure is present without a
        percentage rather than absent or defaulted to zero — the row was
        uploaded, the ratio simply cannot be formed from it.
        """
        rows = self.get(network_id)
        latest: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            fid = str(row.get("facilityId") or "").strip()
            period = str(row.get("period") or "")
            if not fid:
                continue
            held = latest.get(fid)
            if held is None or period > str(held.get("period") or ""):
                latest[fid] = dict(row)

        out: Dict[str, Dict[str, Any]] = {}
        for fid, row in latest.items():
            available, used = row.get("available"), row.get("used")
            pct = None
            if (isinstance(available, (int, float)) and available
                    and isinstance(used, (int, float))):
                pct = round(used / available * 100.0, 2)
            out[fid] = {
                "period": row.get("period") or None,
                "available": available,
                "used": used,
                "utilisationPct": pct,
            }
        return out


#: One store per process, mirroring the other in-process registries.
demand_history_store = DemandHistoryStore()
uploaded_signal_store = UploadedSignalStore()
capacity_history_store = CapacityHistoryStore()


def build_series_from_structure(
    structure: Dict[str, Any],
    *,
    sla_by_market: Dict[str, float] | None = None,
) -> Tuple[List[Any], List[str]]:
    """
    Turn the extractor's `demandHistory` rows into `DemandTimeSeries`.

    Returns `(series, notes)`. Periods are sorted by their own label and
    numbered sequentially, because `DemandPoint.period` is an ordering index —
    the original label is preserved on `timestamp` so nothing is lost.

    A pair with fewer than two observations is dropped with a note: a single
    point is not a series, and forecasting from it would be an invention
    dressed as a projection.
    """
    from netgravity.forecasting.schemas import DemandPoint, DemandTimeSeries

    rows = structure.get("demandHistory") or []
    if not rows:
        return [], []

    grouped: Dict[Tuple[str, str], Dict[str, float]] = {}
    for row in rows:
        market = str(row.get("marketId") or "").strip()
        product = str(row.get("productId") or "").strip()
        period = str(row.get("period") or "").strip()
        try:
            quantity = float(row.get("quantity"))
        except (TypeError, ValueError):
            continue
        if not market or not period or quantity < 0:
            continue
        key = (market, product or "PROD_ALL")
        bucket = grouped.setdefault(key, {})
        bucket[period] = bucket.get(period, 0.0) + quantity

    series: List[Any] = []
    notes: List[str] = []
    too_short: List[str] = []

    for (market, product), periods in sorted(grouped.items()):
        labels = sorted(periods)
        if len(labels) < 2:
            too_short.append(f"{market}/{product}")
            continue
        points = [
            DemandPoint(period=index, quantity=periods[label], timestamp=label)
            for index, label in enumerate(labels)
        ]
        kwargs: Dict[str, Any] = {
            "market_id": market, "product_id": product, "history": points,
        }
        sla = (sla_by_market or {}).get(market)
        if sla is not None:
            kwargs["sla_days"] = sla
        series.append(DemandTimeSeries(**kwargs))

    if series:
        notes.append(
            f"{len(series)} market-product series built from the uploaded demand "
            f"history, {len(series[0].history)} periods each."
        )
    if too_short:
        notes.append(
            f"{len(too_short)} market-product pair(s) had only one observation and "
            f"cannot be forecast: {', '.join(too_short[:5])}"
            f"{'…' if len(too_short) > 5 else ''}."
        )
    return series, notes
