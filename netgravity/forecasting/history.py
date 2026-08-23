"""
Observed history for forecasting.

Turns the rows the ingestion pipeline already produces into the
`DemandTimeSeries` the engines consume.

    client files → ingestion → STAGING zone → build_series() → DemandTimeSeries

The staging zone is where this belongs, and it was designed for it before any
forecasting code existed. `ContentType.SHIPMENT_LOG` and `HISTORICAL_VOLUME`
route to `DEST_STAGING` with the comment: *"Transactional history. It is
forecasting input (Layer 3), NOT network structure. Loading it into the network
would silently alter the Digital Twin."* This module is the consumer that
comment anticipated.

**No second historical store.** Nothing here persists anything. Ingestion writes
the rows, this reads them into memory for one request, and `CanonicalNetwork`
remains the only authoritative model of the network itself. A `DemandTimeSeries`
is observed evidence about the past, not a competing description of the network.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from netgravity.forecasting.schemas import DemandPoint, DemandTimeSeries, Frequency

logger = logging.getLogger(__name__)

#: Column names the staging rows use, in the order the field mapper produces
#: them. Matches `_SHIPMENT_FIELDS` in `ingestion/ai/field_mapper.py` — the two
#: must agree, and a test pins that they do.
_MARKET_KEYS = ("market_id", "destination_id", "market")
_PRODUCT_KEYS = ("product_id", "sku", "product")
_PERIOD_KEYS = ("period", "date", "month", "week")
_QUANTITY_KEYS = ("quantity", "units", "volume", "qty")


def _first(row: Dict[str, Any], keys: Sequence[str]) -> Optional[Any]:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def _coerce_period(value: Any) -> Optional[int]:
    """
    Read a period index from whatever the source wrote.

    Accepts an integer directly. A date string is NOT parsed into a period here:
    mapping calendar dates onto planning periods needs a calendar the ingestion
    layer owns, and guessing one would silently misalign a whole series. Such
    rows are skipped and counted.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
    return None


def build_series(
    rows: Iterable[Dict[str, Any]],
    *,
    frequency: Frequency = Frequency.MONTH,
    min_periods: int = 1,
) -> Tuple[List[DemandTimeSeries], List[str]]:
    """
    Group staging rows into per market-product histories.

    Rows for the same (market, product, period) are SUMMED — a shipment log
    holds one row per despatch, and several despatches into one market in one
    period are one period's demand, not several observations of it.

    Args:
        rows: Staging rows, each carrying market, product, period and quantity
            under any of the recognised column names.
        frequency: What one period means. Recorded, not inferred.
        min_periods: Series shorter than this are dropped and reported.

    Returns:
        (series, warnings). Every row that could not be read is counted in the
        warnings rather than silently discarded.
    """
    buckets: Dict[Tuple[str, str], Dict[int, float]] = defaultdict(dict)
    skipped_missing = 0
    skipped_period = 0
    skipped_quantity = 0

    for row in rows:
        market = _first(row, _MARKET_KEYS)
        product = _first(row, _PRODUCT_KEYS)
        if market is None or product is None:
            skipped_missing += 1
            continue

        period = _coerce_period(_first(row, _PERIOD_KEYS))
        if period is None:
            skipped_period += 1
            continue

        raw_qty = _first(row, _QUANTITY_KEYS)
        try:
            quantity = float(raw_qty)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            skipped_quantity += 1
            continue
        if quantity < 0:
            skipped_quantity += 1
            continue

        key = (str(market), str(product))
        buckets[key][period] = buckets[key].get(period, 0.0) + quantity

    warnings: List[str] = []
    if skipped_missing:
        warnings.append(f"{skipped_missing} row(s) skipped: no market or product id")
    if skipped_period:
        warnings.append(
            f"{skipped_period} row(s) skipped: period is not an integer index. Date "
            f"strings are not converted here, because mapping dates onto planning "
            f"periods needs a calendar this layer does not own."
        )
    if skipped_quantity:
        warnings.append(f"{skipped_quantity} row(s) skipped: quantity missing or negative")

    series: List[DemandTimeSeries] = []
    short: List[str] = []
    for (market, product), by_period in sorted(buckets.items()):
        if len(by_period) < min_periods:
            short.append(f"{market}/{product}")
            continue
        series.append(DemandTimeSeries(
            market_id=market,
            product_id=product,
            frequency=frequency,
            history=[
                DemandPoint(period=p, quantity=q)
                for p, q in sorted(by_period.items())
            ],
        ))

    if short:
        warnings.append(
            f"{len(short)} pair(s) had fewer than {min_periods} period(s) and were "
            f"not built into a series: {', '.join(sorted(short)[:5])}"
            f"{'...' if len(short) > 5 else ''}"
        )

    logger.info(
        "forecasting.history.built series=%d rows_skipped=%d",
        len(series), skipped_missing + skipped_period + skipped_quantity,
    )
    return series, warnings


def load_staging_history(
    staging_dir: Path,
    *,
    frequency: Frequency = Frequency.MONTH,
    min_periods: int = 1,
) -> Tuple[List[DemandTimeSeries], List[str]]:
    """
    Read every staging JSON under a directory into series.

    Matches what `ingestion/tabular.py::save_staging` writes:
    `standardized/tabular/<label>/<content_type>.json`, each a JSON array of
    row dicts.

    A directory that does not exist is not an error — it means nothing
    transactional has been ingested yet, which is an ordinary state for a fresh
    install and is reported as a warning rather than an exception.
    """
    if not staging_dir.exists():
        return [], [f"no staging directory at '{staging_dir}'; no history is available"]

    rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    files = sorted(staging_dir.rglob("*.json"))

    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"could not read staging file '{path.name}': {exc}")
            continue
        if isinstance(payload, list):
            rows.extend(r for r in payload if isinstance(r, dict))
        else:
            warnings.append(f"staging file '{path.name}' is not a JSON array; skipped")

    if not files:
        warnings.append(f"no staging files under '{staging_dir}'")

    series, build_warnings = build_series(
        rows, frequency=frequency, min_periods=min_periods,
    )
    return series, warnings + build_warnings


def series_for_network(
    series: Sequence[DemandTimeSeries],
    market_product_pairs: Iterable[Tuple[str, str]],
) -> Tuple[List[DemandTimeSeries], List[str]]:
    """
    Keep only history matching pairs the network actually has.

    History for a market the network does not contain cannot be optimised
    against, and forecasting it would spend effort producing a number with
    nowhere to go. The pairs with no history are returned by name so the caller
    can see the coverage gap rather than discover it as a missing forecast.
    """
    wanted = set(market_product_pairs)
    available = {(s.market_id, s.product_id): s for s in series}

    matched = [available[key] for key in sorted(wanted & set(available))]
    missing = sorted(f"{m}/{p}" for m, p in (wanted - set(available)))
    return matched, missing


__all__ = ["build_series", "load_staging_history", "series_for_network"]
