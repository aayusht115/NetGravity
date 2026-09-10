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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

    def periods(self, network_id: str) -> List[str]:
        """Every period label present in the recorded capacity history, ordered.

        Chronological because the labels the extractor keeps are the client's
        own (`"2023-09"`), and those sort correctly as strings. A label that
        does not follow that shape still sorts deterministically, which is what
        a selector needs — it simply may not read as a calendar.
        """
        seen = {str(r.get("period") or "").strip()
                for r in self.get(network_id)}
        return sorted(p for p in seen if p)

    def utilisation_series(self, network_id: str,
                           facility_id: Optional[str] = None
                           ) -> Dict[str, Any]:
        """
        Recorded utilisation per period — the client's own measurement.

        This is the one genuine time series an uploaded network carries. It is
        NOT a solver output: `used / available` are two columns the client
        supplied on the same row, and dividing them is what those columns mean.
        Kept distinct from the solved utilisation for exactly that reason — a
        chart mixing "what the plan does" with "what the sites did" would be
        two different quantities on one axis.

        `facility_id=None` aggregates the network: used and available are summed
        per period before dividing, so a small site cannot swing the ratio the
        way averaging per-facility percentages would.

        Returns `{"periods": [...], "points": [{"period", "available", "used",
        "utilisationPct"}], "facility_id": ...}`. A period whose figures cannot
        form a ratio is present with `utilisationPct: None` — a gap in the line,
        never a zero.
        """
        rows = self.get(network_id)
        if facility_id:
            rows = [r for r in rows
                    if str(r.get("facilityId") or "").strip() == facility_id]

        by_period: Dict[str, Dict[str, float]] = {}
        for row in rows:
            period = str(row.get("period") or "").strip()
            if not period:
                continue
            bucket = by_period.setdefault(period, {"available": 0.0, "used": 0.0,
                                                   "rows": 0})
            available, used = row.get("available"), row.get("used")
            if isinstance(available, (int, float)):
                bucket["available"] += float(available)
            if isinstance(used, (int, float)):
                bucket["used"] += float(used)
            bucket["rows"] += 1

        points = []
        for period in sorted(by_period):
            bucket = by_period[period]
            available, used = bucket["available"], bucket["used"]
            pct = (round(used / available * 100.0, 2)
                   if available > 0 else None)
            points.append({
                "period": period,
                "available": round(available, 3),
                "used": round(used, 3),
                "utilisationPct": pct,
                "facilities": int(bucket["rows"]),
            })
        return {
            "facility_id": facility_id,
            "periods": [p["period"] for p in points],
            "points": points,
        }



class UploadedForecastStore(_DurableByNetwork):
    """
    A forecast that ARRIVED WITH an upload, keyed by `network_id`.

    Held beside the network for the same reason signals and capacity history
    are, and kept out of `DemandHistoryStore` for a stronger one: that class
    implements `for_snapshot()`, which is the `history_provider` contract the
    orchestrator reads observed history through. This class deliberately does
    not implement it, and must not gain it. A projection is not an observation,
    nothing may be fitted to it, and it must be structurally unable to reach the
    forecasting engines — not merely absent from the call that would take it
    there.

    Nothing here forecasts. `series()` groups rows the upload stated, orders
    them by the upload's own period labels, and reports what it could not
    reconcile.
    """

    def __init__(self) -> None:
        self._by_network: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, network_id: str, rows: Sequence[Dict[str, Any]]) -> None:
        with self._lock:
            self._by_network[network_id] = [dict(r) for r in rows]
        self._write_through(network_id, rows)
        logger.info("uploaded_forecast.stored network_id=%s rows=%d",
                    network_id, len(rows))

    def get(self, network_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._by_network.get(network_id, [])]

    def has(self, network_id: str) -> bool:
        with self._lock:
            return bool(self._by_network.get(network_id))

    def clear(self, network_id: str) -> None:
        """Forget this network's forecast — a re-upload without one replaces it."""
        with self._lock:
            existed = self._by_network.pop(network_id, None) is not None
        self._write_through(network_id, [])
        if existed:
            logger.info("uploaded_forecast.cleared network_id=%s", network_id)

    def periods(self, network_id: str) -> List[str]:
        """Every period label the forecast states, ordered by the label itself."""
        seen = {str(r.get("period") or "").strip() for r in self.get(network_id)}
        return sorted(p for p in seen if p)

    def series(self, network_id: str, horizon: Optional[int] = None,
               ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """
        The stored rows grouped per market-product, in the upload's own
        calendar order.

        `period` is renumbered 1..N as an ORDERING INDEX, matching what
        `ForecastPoint.period` means everywhere else in this codebase, and the
        upload's own label is preserved on `timestamp` so no calendar is lost.

        Returns `(series, warnings)`. Nothing is extrapolated: an upload stating
        four periods against a horizon of six yields four points and a warning,
        never two invented ones.
        """
        grouped: Dict[Tuple[str, str], Dict[str, Dict[str, Any]]] = {}
        duplicates: List[str] = []
        undated = 0

        for row in self.get(network_id):
            market = str(row.get("marketId") or "").strip()
            label = str(row.get("period") or "").strip()
            if not market or not label:
                undated += 1
                continue
            product = str(row.get("productId") or "").strip() or "PROD_ALL"
            bucket = grouped.setdefault((market, product), {})
            if label in bucket:
                duplicates.append(f"{market}/{product}@{label}")
            # Last row for a period wins, and the collision is REPORTED.
            # Summing would silently double a projection — which is what two
            # rows for one period are not — and dropping it silently would lose
            # a figure the upload stated. Note this is the opposite rule from
            # `build_series_from_structure`, correctly: two observations of one
            # month are two facts, two forecasts of one month are a
            # contradiction.
            bucket[label] = row

        warnings: List[str] = []
        if duplicates:
            warnings.append(
                f"{len(duplicates)} duplicate forecast period(s) in the upload "
                f"({', '.join(duplicates[:5])}"
                f"{'…' if len(duplicates) > 5 else ''}); the last row stated for "
                f"each was used."
            )
        if undated:
            warnings.append(
                f"{undated} forecast row(s) name no period or no market and "
                f"could not be placed on a series."
            )

        short: List[str] = []
        out: List[Dict[str, Any]] = []
        for (market, product), by_label in sorted(grouped.items()):
            labels = sorted(by_label)
            if horizon is not None:
                if len(labels) > horizon:
                    labels = labels[:horizon]
                elif len(labels) < horizon:
                    short.append(f"{market}/{product}")
            points = []
            for index, label in enumerate(labels, start=1):
                row = by_label[label]
                mean = row.get("mean")
                p50 = row.get("p50")
                points.append({
                    "period": index,
                    "timestamp": label,
                    "mean": mean,
                    "p10": row.get("p10"),
                    # The upload's own median when it states one; the central
                    # estimate otherwise. Never a value derived from the band.
                    "p50": p50 if p50 is not None else mean,
                    "p90": row.get("p90"),
                    # Nothing adjusted this, so there is no prior estimate to
                    # recover. Present so the shape matches an engine forecast.
                    "baseline_mean": None,
                })
            out.append({"market_id": market, "product_id": product,
                        "points": points})

        if short and horizon is not None:
            warnings.append(
                f"{len(short)} series state fewer than the {horizon} period(s) "
                f"requested ({', '.join(short[:5])}"
                f"{'…' if len(short) > 5 else ''}); the shorter series is "
                f"returned rather than extended."
            )
        return out, warnings


#: One store per process, mirroring the other in-process registries.
demand_history_store = DemandHistoryStore()
uploaded_signal_store = UploadedSignalStore()
capacity_history_store = CapacityHistoryStore()
uploaded_forecast_store = UploadedForecastStore()


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


def build_uploaded_forecast(
    structure: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    The extractor's `uploadedForecast` rows, validated for storage.

    The counterpart of `build_series_from_structure`, and deliberately NOT that
    function: it returns plain rows rather than `DemandTimeSeries`, because
    `DemandTimeSeries` is the OBSERVED-history type and nothing holding a
    projection may be constructible as one.

    Returns `(rows, notes)`. Every row dropped is counted and named in the
    notes; nothing is discarded silently and nothing is repaired invisibly.
    """
    rows = structure.get("uploadedForecast") or []
    if not rows:
        return [], []

    kept: List[Dict[str, Any]] = []
    notes: List[str] = []
    dropped = crossed = negative = 0

    for row in rows:
        try:
            mean = float(row.get("mean"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        if mean != mean:  # NaN
            dropped += 1
            continue
        if mean < 0:
            # A negative forecast is not a small one. Dropped and counted
            # rather than clamped to zero, which would be this build inventing
            # a number to replace one it could not read.
            negative += 1
            continue
        if not str(row.get("period") or "").strip():
            dropped += 1
            continue
        if not str(row.get("marketId") or "").strip():
            dropped += 1
            continue

        p10, p90 = row.get("p10"), row.get("p90")
        if (isinstance(p10, (int, float)) and isinstance(p90, (int, float))
                and p10 > p90):
            # Reported, and the band is dropped rather than swapped: P10 above
            # P90 almost always means the two columns were mapped the wrong way
            # round, and silently reordering them hides the mapping error the
            # review screen exists to catch.
            p10 = p90 = None
            crossed += 1

        kept.append({
            "period": str(row.get("period")).strip(),
            "marketId": str(row.get("marketId")).strip(),
            "productId": (str(row.get("productId")).strip()
                          if row.get("productId") else None),
            "mean": mean,
            "p10": p10,
            "p50": row.get("p50"),
            "p90": p90,
        })

    if kept:
        markets = {r["marketId"] for r in kept}
        periods = {r["period"] for r in kept}
        banded = sum(1 for r in kept
                     if r["p10"] is not None and r["p90"] is not None)
        notes.append(
            f"{len(kept)} forecast row(s) stored for {len(markets)} market(s) "
            f"across {len(periods)} period(s). {banded} state a P10-P90 band; "
            f"{len(kept) - banded} state a central value only and are shown "
            f"without a confidence band. No model was fitted to any of them."
        )
    if dropped:
        notes.append(
            f"{dropped} forecast row(s) named no period or market, or carried "
            f"an unreadable quantity, and were not stored."
        )
    if negative:
        notes.append(
            f"{negative} forecast row(s) stated a negative quantity and were "
            f"not stored. A negative forecast was not read as zero."
        )
    if crossed:
        notes.append(
            f"{crossed} forecast row(s) stated P10 above P90, so no band was "
            f"kept for them - check that the two columns are mapped the right "
            f"way round."
        )
    return kept, notes
