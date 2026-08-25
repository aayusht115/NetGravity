"""
NetGravity — Unified Tabular Ingestion
=======================================
One path for every table, whatever its shape and wherever it came from.

WHAT IT REPLACES
----------------
There used to be two readers with different capabilities and different
assumptions:

    structured.py   CSV only, exact filenames (facilities.csv), no AI, fed
                    the optimiser directly.
    distributor.py  CSV + first sheet of Excel, AI column mapping, fed the
                    staging zone.

Which one ran was decided by the folder a file happened to sit in. That is
the wrong basis for the decision — a distributor can send a facility list,
and a client can send shipment history — and it left real gaps: a client
workbook with five tabs lost four of them, and a file named anything other
than the expected name was invisible.

THE PIPELINE NOW
----------------
    discover      any CSV/Excel, any name, EVERY sheet          (sources/)
    classify      what IS this, from the row data               (ai/classifier)
    map           what does each column mean                    (ai/field_mapper)
    review        ask about what is genuinely uncertain         (review.py)
    route         by content type, not by folder                (schemas/content)

The existing parsers are deliberately kept and reused, not rewritten. They
carry hard-won behaviour — MONTH normalisation, the R-021 period-mismatch
safety net, the R-022 per-trip capacity refusal — and none of that is
affected by how the rows were named on the way in.

WHY A FIRST RUN ASKS QUESTIONS
------------------------------
Optimiser-bound rows are held until their mapping is confirmed. That is the
point, not an oversight: a wrong facility mapping produces an authoritative
wrong recommendation. `auto_confirm` exists for unattended runs and records
the confirmation as machine-made, so the audit trail never claims a human
looked when nobody did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from netgravity.ingestion import review as review_module
from netgravity.ingestion.adapters import structured
from netgravity.ingestion.ai.classifier import classify
from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER
from netgravity.ingestion.ai.field_mapper import build_mapping
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.memory.field_catalog import FieldCatalog
from netgravity.ingestion.schemas.content import (
    DEST_HOLD,
    DEST_NETWORK,
    DEST_STAGING,
    ContentType,
)
from netgravity.ingestion.schemas.field_mapping import SheetMapping
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity
from netgravity.ingestion.sources import DataSource, discover
from netgravity.ingestion.sources.base import RecordSet
from netgravity.ingestion.storage.base import StorageBackend


@dataclass
class TabularResult:
    """Everything one unified pass produced."""

    mappings: List[SheetMapping] = field(default_factory=list)
    results: List[FileResult] = field(default_factory=list)
    #: Canonically-renamed rows per content type, ready for the parsers.
    network_rows: Dict[ContentType, List[Dict[str, Any]]] = field(default_factory=dict)
    staging_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    held: List[SheetMapping] = field(default_factory=list)

    @property
    def review_request(self) -> review_module.ReviewRequest:
        return review_module.build_request(self.mappings)

    @property
    def needs_review(self) -> bool:
        return any(m.needs_review for m in self.mappings)


def _apply(record_set: RecordSet, mapping: SheetMapping) -> List[Dict[str, Any]]:
    """
    Rename a record set's rows to canonical field names.

    Only SETTLED columns are applied. A column still awaiting review is left
    out entirely rather than applied provisionally — a provisional mapping
    that reaches a parser is indistinguishable from a confirmed one once the
    row is built, and that is precisely the silent-wrong-number failure this
    layer exists to prevent.
    """
    renames = mapping.rename_map
    if not renames:
        return []
    rows: List[Dict[str, Any]] = []
    for row in record_set.rows:
        renamed = {
            target: row.get(source)
            for source, target in renames.items()
            if source in row
        }
        if any(v is not None and str(v).strip() != "" for v in renamed.values()):
            rows.append(renamed)
    return rows


def ingest_tabular(source: Path, config: IngestionConfig,
                   storage: Optional[StorageBackend] = None,
                   *, known_ids: Optional[Sequence[str]] = None,
                   auto_confirm: bool = False,
                   sources: Optional[Sequence[DataSource]] = None,
                   catalog_scope: str = "default",
                   content_type_overrides: Optional[Dict[str, str]] = None) -> TabularResult:
    """
    Run the unified path over a directory (or an explicit list of sources).

    `sources` is the seam an ERP/WMS connector plugs into later: it takes any
    DataSource, so nothing here is file-specific.
    """
    outcome = TabularResult()
    memory = (FieldMemory(storage, namespace=catalog_scope)
              if storage is not None else None)
    catalog = (FieldCatalog(storage, client_id=catalog_scope)
               if storage is not None else None)
    client = get_client(config)
    known = list(known_ids or [])

    for data_source in (sources if sources is not None else discover(Path(source))):
        for record_set in data_source.record_sets():
            outcome.results.append(
                _ingest_record_set(record_set, client, memory, known,
                                   outcome, auto_confirm, catalog,
                                   content_type_overrides or {}))

    return outcome


def _ingest_record_set(record_set: RecordSet, client, memory: Optional[FieldMemory],
                       known_ids: Sequence[str], outcome: TabularResult,
                       auto_confirm: bool,
                       catalog: Optional[FieldCatalog] = None,
                       content_type_overrides: Optional[Dict[str, str]] = None) -> FileResult:
    result = FileResult(
        source_file=record_set.origin.label,
        adapter="tabular",
        rows_read=record_set.row_count,
        ai_used=not client.stub_mode,
        ai_stubbed=client.stub_mode,
    )

    if record_set.warning:
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-024",
            message=record_set.warning, source_file=record_set.origin.label))
        return result

    if record_set.is_empty:
        return result

    classification = classify(client, record_set)
    override = (content_type_overrides or {}).get(record_set.key)
    if override:
        classification.content_type = ContentType.parse(override)
        classification.confidence = 1.0
        classification.needs_review = False
        classification.review_reasons = []
        classification.proposed_by = "human:content-type-override"
    mapping = build_mapping(client, record_set, classification,
                            memory=memory, catalog=catalog,
                            known_ids=known_ids)
    outcome.mappings.append(mapping)

    result.ai_notes.extend(mapping.notes)
    result.ai_failed = any(LLM_FAILURE_MARKER in note for note in mapping.notes)
    result.ai_notes.append(
        f"classified as {classification.content_type.value} "
        f"({classification.confidence:.0%}, via {classification.proposed_by}) "
        f"-> {mapping.destination} zone")

    if auto_confirm and mapping.needs_review:
        _auto_confirm(mapping, memory, result)

    for decision in mapping.pending:
        result.issues.append(RowIssue(
            severity=Severity.INFO, code="R-025",
            message=(f"'{decision.source_column}' -> "
                     f"{decision.target_field} awaiting confirmation: "
                     f"{'; '.join(decision.review_reasons)}"),
            source_file=record_set.origin.label))

    destination = mapping.destination

    if destination == DEST_HOLD:
        outcome.held.append(mapping)
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-026",
            message=(f"could not determine what '{record_set.origin.label}' "
                     f"contains — held for a human to label rather than "
                     f"routed on a guess"),
            source_file=record_set.origin.label))
        return result

    rows = _apply(record_set, mapping)
    result.rows_accepted = len(rows)
    result.rows_rejected = max(0, record_set.row_count - len(rows))

    if destination == DEST_NETWORK:
        outcome.network_rows.setdefault(mapping.content_type, []).extend(rows)
    elif destination == DEST_STAGING:
        outcome.staging_rows.setdefault(mapping.content_type.value, []).extend(rows)

    return result


def _auto_confirm(mapping: SheetMapping, memory: Optional[FieldMemory],
                  result: FileResult) -> None:
    """
    Settle everything pending without a human, for unattended runs.

    Recorded as confirmed_by="auto" so memory never claims a person reviewed
    something nobody looked at. A later human confirmation replaces it.
    """
    request = review_module.build_request([mapping])
    decisions = [
        review_module.ReviewDecision(
            item_id=item.item_id,
            value=item.proposed_value or review_module.NOT_NEEDED,
            decided_by="auto",
            note="auto-confirmed by an unattended run",
        )
        for item in request.items if item.proposed_value
    ]
    if not decisions:
        return
    review_module.apply(request, decisions, [mapping], memory)
    result.ai_notes.append(
        f"{len(decisions)} mapping(s) auto-confirmed (unattended run) — "
        f"recorded as machine-confirmed, not human-confirmed")


def parse_into_records(outcome: TabularResult) -> Dict[str, Any]:
    """
    Hand canonically-named rows to the EXISTING parsers.

    Order matters, exactly as it did before: facilities and markets first so
    referential checks downstream have the identifiers they need.
    """
    facilities: List[Any] = []
    products: List[Any] = []
    demands: List[Any] = []
    lanes: List[Any] = []
    results: List[FileResult] = []

    for content_type, parser in (
        (ContentType.FACILITY, structured.parse_facilities),
        (ContentType.MARKET, structured.parse_markets),
    ):
        rows = outcome.network_rows.get(content_type)
        if rows:
            records, file_result = parser(rows)
            facilities += records
            results.append(file_result)

    node_ids = {f.id for f in facilities}
    market_ids = {f.id for f in facilities
                  if getattr(f.role, "value", str(f.role)) == "MARKET"}

    rows = outcome.network_rows.get(ContentType.PRODUCT)
    if rows:
        records, file_result = structured.parse_products(rows)
        products += records
        results.append(file_result)
    product_ids = {p.id for p in products}

    rows = outcome.network_rows.get(ContentType.DEMAND)
    if rows:
        records, file_result = structured.parse_demand(rows, market_ids, product_ids)
        demands += records
        results.append(file_result)

    rows = outcome.network_rows.get(ContentType.LANE)
    if rows:
        records, file_result = structured.parse_lanes(rows, node_ids)
        lanes += records
        results.append(file_result)

    return {
        "facilities": facilities,
        "products": products,
        "demands": demands,
        "lanes": lanes,
        "results": results,
    }


def save_staging(outcome: TabularResult, storage: StorageBackend,
                 label: str) -> List[str]:
    """Write transaction-shaped rows to the staging zone as forecasting input."""
    written: List[str] = []
    for content_type, rows in outcome.staging_rows.items():
        if not rows:
            continue
        written.append(storage.save_text(
            "standardized",
            f"tabular/{label}/{content_type.lower()}.json",
            json.dumps(rows, indent=2, default=str),
        ))
    return written
