"""
Orchestrator — Extraction / Parsing Agent.

    CLIENT DATA ──┐
                  ├──► ExtractionParsingAgent ──► ExtractionResult ──► Orchestrator
    EXTERNAL   ───┘
    SOURCES

THE ARCHITECTURAL POINT
───────────────────────
The data-ingestion pipeline in `netgravity/ingestion/` is the **client-data
implementation component of this agent**. It is not a separate agent and is not
wrapped in one here. This module is a routing and acceptance layer: roughly two
hundred lines over thirteen thousand, because the pipeline already does file
discovery, parsing, schema detection, column mapping, normalisation, row
validation, entity resolution, canonicalisation and provenance, and rebuilding
any of that to make the architecture "look like an agent" would be the worst
possible use of the work.

What this layer adds, and the pipeline does not have:

1. **Routing.** Which extraction capability applies to this source.
2. **Acceptance.** A deterministic verdict — ACCEPTED / WARNING /
   HUMAN_REVIEW_REQUIRED / REJECTED — over the pipeline's findings.
3. **An orchestrator-facing contract.** `ExtractionResult`, which carries a
   canonical snapshot and structured evidence and nothing about workbooks.
4. **Snapshot registration.** Handing the network to the existing
   `SnapshotManager` so REI's fingerprint, cache and staleness rules apply
   unchanged.

WHAT IT DOES NOT DO
───────────────────
Parse. Compute. Score. The agent never touches MILP, REI, RF or governance, and
`ExtractionResult` has no field able to carry their outputs. In particular it
does not calculate RF: an external signal supplies P, the Orchestrator pairs it
with REI, and `RF = P + REI − P·REI` stays where it is.

OBSERVED DATA ONLY
──────────────────
Everything this agent produces is OBSERVED state. It registers snapshots; it
never writes a scenario, and the scenario path does not run through here at
all. That separation is structural — there is no code path from an
`ExtractionRequest` to a `ScenarioIntentSpec`.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from netgravity.orchestrator.schemas.extraction import (
    ExtractionProvenance,
    ExtractionRequest,
    ExtractionResult,
    ExtractionStatus,
    SourceType,
    ValidationFinding,
    ValidationSeverity,
)

logger = logging.getLogger(__name__)

#: Suffixes the client-data pipeline can parse. Anything else is UNSUPPORTED
#: rather than attempted — a parser guessing at an unknown format is how
#: corrupt records enter a canonical snapshot.
TABULAR_SUFFIXES = frozenset({".csv", ".xlsx", ".xls", ".tsv"})
DOCUMENT_SUFFIXES = frozenset({".pdf", ".txt", ".md"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExtractionParsingAgent:
    """
    Routes a source to the right extraction capability and validates the output.

    Args:
        snapshots: the orchestrator's existing `SnapshotManager`, or None. When
            supplied and `register_snapshot` is set, an accepted network is
            registered through it — the SAME mechanism every other snapshot
            uses, so material fingerprinting, REI cache keys and stale-evidence
            checks apply without modification.
    """

    def __init__(self, snapshots: Optional[Any] = None) -> None:
        self.snapshots = snapshots

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """
        Run one extraction.

        Never raises. A malformed source, a missing directory or an unparseable
        workbook becomes a REJECTED result with the reason stated — an
        extraction layer that throws leaves the caller unable to tell "bad data"
        from "broken system", and the two need different responses.
        """
        started = time.perf_counter()
        started_at = _utc_now()

        source_type = request.source_type
        if source_type == SourceType.CLIENT_DATA_DIRECTORY:
            source_type = self._infer_source_type(request.source)

        logger.info(
            "extraction.started ingestion_id=%s source_type=%s source=%s",
            request.ingestion_id, source_type.value, request.source,
        )

        if source_type == SourceType.UNSUPPORTED:
            return self._rejected(
                request, started, started_at, source_type,
                f"Unsupported source '{request.source}'. Recognised formats are "
                f"{sorted(TABULAR_SUFFIXES | DOCUMENT_SUFFIXES)} in a directory "
                f"or as a single file.",
            )

        if source_type == SourceType.EXTERNAL_SIGNAL_TEXT:
            return self._extract_external_signal(request, started, started_at)

        if source_type == SourceType.MARKET_INTELLIGENCE_DOC:
            return self._extract_market_intelligence(request, started, started_at)

        return self._extract_client_data(request, started, started_at, source_type)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_source_type(source: str) -> SourceType:
        """
        Decide what a source is from the filesystem, not from a model.

        Routing selects which deterministic pipeline runs, so it is settled by
        looking at the path — the same reasoning that keeps workflow selection
        out of the conversational LLM's hands.
        """
        path = Path(source)
        if path.is_dir():
            return SourceType.CLIENT_DATA_DIRECTORY
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix in TABULAR_SUFFIXES:
                return SourceType.CLIENT_DATA_FILE
            if suffix in DOCUMENT_SUFFIXES:
                return SourceType.CLIENT_DATA_FILE
            return SourceType.UNSUPPORTED
        return SourceType.UNSUPPORTED

    # ------------------------------------------------------------------
    # Client data
    # ------------------------------------------------------------------

    def _extract_client_data(
        self,
        request: ExtractionRequest,
        started: float,
        started_at: str,
        source_type: SourceType,
    ) -> ExtractionResult:
        """
        Delegate to the ingestion pipeline and adjudicate what it returns.

        Imported lazily: the pipeline pulls in pandas and openpyxl, and an
        orchestrator that only ever answers questions about an already-loaded
        network should not pay that import cost.
        """
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.pipeline import run_ingestion

        config = IngestionConfig()
        if not request.allow_ai:
            # Stub mode: the pipeline runs rules-only and makes no network call.
            config.llm_api_key = None

        try:
            outcome = run_ingestion(
                Path(request.source),
                config=config,
                save=request.save_snapshot,
                unified=True,
                auto_confirm=request.auto_confirm_mappings,
            )
        except Exception as exc:                       # noqa: BLE001
            logger.warning("extraction.failed ingestion_id=%s error=%s",
                           request.ingestion_id, exc)
            return self._rejected(
                request, started, started_at, source_type,
                f"Ingestion failed: {type(exc).__name__}: {exc}",
            )

        report = outcome.report
        findings = self._findings_from(report)
        provenance = self._provenance_from(
            request, report, source_type, started_at, config,
        )

        network = outcome.network
        review_items = self._review_items_from(outcome)
        status = self._adjudicate(network, findings, review_items, report)

        snapshot_id = None
        if status in (ExtractionStatus.ACCEPTED, ExtractionStatus.WARNING) \
                and network is not None and request.register_snapshot:
            snapshot_id = self._register(network, request)

        result = ExtractionResult(
            status=status,
            ingestion_id=request.ingestion_id,
            canonical_data=network,
            snapshot_id=snapshot_id,
            data_version=(network.data_version if network is not None else None),
            external_signals=list(outcome.signals),
            validation_results=findings,
            warnings=[f.message for f in findings
                      if f.severity == ValidationSeverity.WARNING],
            errors=[f.message for f in findings
                    if f.severity == ValidationSeverity.ERROR],
            provenance=provenance,
            review_items=review_items,
            duration_seconds=round(time.perf_counter() - started, 4),
        )
        logger.info(
            "extraction.completed ingestion_id=%s status=%s data_version=%s "
            "snapshot_id=%s rows=%d duration=%.3fs",
            request.ingestion_id, status.value, result.data_version, snapshot_id,
            report.total_rows_read, result.duration_seconds,
        )
        return result

    # ------------------------------------------------------------------
    # Acceptance — deterministic, from the pipeline's own findings
    # ------------------------------------------------------------------

    @staticmethod
    def _adjudicate(
        network: Optional[Any],
        findings: List[ValidationFinding],
        review_items: List[dict],
        report: Any,
    ) -> ExtractionStatus:
        """
        Turn the pipeline's findings into one verdict.

        Order matters and is deliberate: no network at all is a rejection
        whatever else happened; an outstanding human decision outranks a
        warning, because proceeding would decide it by default; and warnings do
        not block, because client data is never perfectly clean and a system
        that refuses everything imperfect refuses everything.
        """
        if network is None:
            return ExtractionStatus.REJECTED
        if any(f.severity == ValidationSeverity.ERROR for f in findings):
            return ExtractionStatus.REJECTED
        if getattr(report, "engine_validation_passed", None) is False:
            return ExtractionStatus.REJECTED
        if any(bool(item.get("blocking", True)) for item in review_items):
            return ExtractionStatus.HUMAN_REVIEW_REQUIRED
        if review_items:
            # Unfamiliar fields are preserved and visible, but do not prevent
            # an otherwise valid canonical network from being used.
            return ExtractionStatus.WARNING
        if any(f.severity == ValidationSeverity.WARNING for f in findings):
            return ExtractionStatus.WARNING
        return ExtractionStatus.ACCEPTED

    @staticmethod
    def _findings_from(report: Any) -> List[ValidationFinding]:
        """
        Lift the pipeline's row issues into the agent's contract, preserving
        provenance down to the row.
        """
        severity_map = {
            "ERROR": ValidationSeverity.ERROR,
            "WARNING": ValidationSeverity.WARNING,
            "INFO": ValidationSeverity.INFO,
        }
        findings: List[ValidationFinding] = []
        for issue in getattr(report, "all_issues", []) or []:
            raw = getattr(getattr(issue, "severity", None), "value", None) \
                or str(getattr(issue, "severity", "INFO"))
            findings.append(ValidationFinding(
                severity=severity_map.get(raw.upper(), ValidationSeverity.INFO),
                code=str(getattr(issue, "code", "") or "ROW_ISSUE"),
                message=str(getattr(issue, "message", "") or raw),
                # Field names mirror `RowIssue` exactly. Provenance down to the
                # row is the difference between "invalid capacity" and an
                # actionable report against a 40,000-row workbook.
                where={
                    k: v for k, v in (
                        ("file", getattr(issue, "source_file", None)),
                        ("row", getattr(issue, "row_number", None)),
                        ("column", getattr(issue, "column", None)),
                        ("raw_value", getattr(issue, "raw_value", None)),
                    ) if v is not None
                },
            ))

        for message in getattr(report, "engine_validation_issues", []) or []:
            findings.append(ValidationFinding(
                severity=ValidationSeverity.ERROR,
                code="ENGINE_VALIDATION",
                message=str(message),
                where={"stage": "validate_network"},
            ))
        return findings

    @staticmethod
    def _review_items_from(outcome: Any) -> List[dict]:
        """
        Column mappings or rows a person must confirm.

        Read from the pipeline's own review request rather than re-derived, so
        there is one definition of "needs a human" and the agent cannot drift
        from it.
        """
        request = getattr(outcome, "review_request", None)
        if request is None or getattr(request, "is_empty", True):
            return []
        items: List[dict] = []
        for item in getattr(request, "items", []) or []:
            if dataclasses.is_dataclass(item) and not isinstance(item, type):
                items.append(dataclasses.asdict(item))
            elif hasattr(item, "model_dump"):
                items.append(item.model_dump(mode="json"))
            else:
                items.append(dict(item))
        return items

    @staticmethod
    def _provenance_from(
        request: ExtractionRequest,
        report: Any,
        source_type: SourceType,
        started_at: str,
        config: Any,
    ) -> ExtractionProvenance:
        files = []
        # "AI-assisted" means a real model produced part of this result. A
        # stubbed call returns canned output and is emphatically not assistance
        # — the pipeline records the two separately for exactly this reason, and
        # collapsing them would let demo output be read as a live extraction.
        ai_live = False
        ai_stubbed = False
        for entry in getattr(report, "files", []) or []:
            used = bool(getattr(entry, "ai_used", False))
            stubbed = bool(getattr(entry, "ai_stubbed", False))
            ai_live = ai_live or (used and not stubbed)
            ai_stubbed = ai_stubbed or stubbed
            files.append({
                "source_file": str(getattr(entry, "source_file", "") or ""),
                "adapter": str(getattr(entry, "adapter", "") or ""),
                "rows_read": int(getattr(entry, "rows_read", 0) or 0),
                "rows_accepted": int(getattr(entry, "rows_accepted", 0) or 0),
                "rows_rejected": int(getattr(entry, "rows_rejected", 0) or 0),
                # Recorded per file, not per run: one AI-assisted sheet in an
                # otherwise rules-parsed batch must be visible as such.
                "ai_used": bool(getattr(entry, "ai_used", False)),
                "ai_stubbed": bool(getattr(entry, "ai_stubbed", False)),
            })
        return ExtractionProvenance(
            ingestion_id=request.ingestion_id,
            source=str(request.source),
            source_type=source_type,
            started_at=started_at,
            completed_at=_utc_now(),
            files=files,
            # True only if a LIVE model produced part of the result.
            ai_assisted=ai_live,
            ai_provider=(str(getattr(config, "provider", "")) or None
                         if ai_live else None),
            counts={
                "files": len(files),
                "ai_stubbed_files": sum(1 for f in files if f["ai_stubbed"]),
                "rows_read": int(getattr(report, "total_rows_read", 0) or 0),
                "rows_accepted": int(getattr(report, "total_rows_accepted", 0) or 0),
                **{str(k): int(v) for k, v in
                   (getattr(report, "counts", {}) or {}).items()
                   if isinstance(v, (int, float))},
            },
        )

    # ------------------------------------------------------------------
    # Snapshot registration — the EXISTING mechanism, not a new one
    # ------------------------------------------------------------------

    def _register(self, network: Any, request: ExtractionRequest) -> Optional[str]:
        """
        Register the extracted network as an OBSERVED snapshot.

        Uses the orchestrator's `SnapshotManager` unchanged. That is what makes
        material fingerprinting, REI cache keys and stale-evidence detection
        apply to ingested data automatically: a network is a network, whatever
        produced it, and there is deliberately no second snapshot system.
        """
        if self.snapshots is None:
            logger.info("extraction.snapshot_not_registered reason=no_manager")
            return None
        try:
            snapshot = self.snapshots.register(
                network, label=f"ingestion:{request.ingestion_id}",
            )
        except Exception as exc:                      # noqa: BLE001
            # A registration failure must not lose the extraction: the caller
            # still has the canonical network and can register it themselves.
            logger.warning("extraction.snapshot_failed error=%s", exc)
            return None
        return snapshot.snapshot_id

    # ------------------------------------------------------------------
    # External signals
    # ------------------------------------------------------------------

    def _extract_external_signal(
        self, request: ExtractionRequest, started: float, started_at: str,
    ) -> ExtractionResult:
        """
        Extract a structured external signal from free text.

        Delegates to the existing `ExternalSignalAgent`, which already produces
        MAIN's `ExternalSignal` — event type, affected node, severity, and a
        probability taken ONLY from an explicit statement in the source. The
        agent stops at the signal. It does not look up REI and it does not
        compute RF; the Orchestrator pairs P with REI and the RF calculator owns
        the formula.
        """
        from netgravity.orchestrator.agents.external_signal_agent import (
            ExternalSignalAgent,
        )

        known: List[str] = list(request.options.get("known_facility_ids", []) or [])
        signal = ExternalSignalAgent(None).interpret(
            request.source, known_facility_ids=known, allow_llm=False,
        )

        findings: List[ValidationFinding] = []
        if signal.event_probability is None:
            findings.append(ValidationFinding(
                severity=ValidationSeverity.WARNING,
                code="NO_EVENT_PROBABILITY",
                message=(
                    "The source states no probability, so none was extracted. RF "
                    "will report NOT_COMPUTABLE for this signal rather than "
                    "inferring a likelihood from severity."
                ),
                where={"source_type": SourceType.EXTERNAL_SIGNAL_TEXT.value},
            ))

        return ExtractionResult(
            status=(ExtractionStatus.WARNING if findings
                    else ExtractionStatus.ACCEPTED),
            ingestion_id=request.ingestion_id,
            canonical_data=None,
            external_signals=[signal],
            validation_results=findings,
            warnings=[f.message for f in findings],
            provenance=ExtractionProvenance(
                ingestion_id=request.ingestion_id,
                source=request.source[:500],
                source_type=SourceType.EXTERNAL_SIGNAL_TEXT,
                started_at=started_at,
                completed_at=_utc_now(),
                ai_assisted=False,
                counts={"signals": 1},
            ),
            duration_seconds=round(time.perf_counter() - started, 4),
        )

    def _extract_market_intelligence(
        self, request: ExtractionRequest, started: float, started_at: str,
    ) -> ExtractionResult:
        """
        Read a market-intelligence DOCUMENT — a news article, circular or
        notice — into guardrail-scored `MarketIntelligenceSignal` records.

        Delegates to the ingestion adapter, which owns document reading, model
        prompting and the guardrail policy. Nothing is re-implemented here;
        this method routes, adjudicates a status and states provenance.

        THREE THINGS THIS DELIBERATELY DOES NOT DO
            - It does not fetch. The document is supplied; there is no HTTP
              call anywhere beneath this method.
            - It does not compute an event probability, and there is no field
              on the result that could carry one. Market context is not a
              hazard likelihood.
            - It does not create or alter a scenario. Whether a signal is worth
              a what-if is a question for a person, asked through the
              orchestrator — not an inference drawn during parsing.

        A guardrail-FILTERED signal is still returned, carrying its verdict.
        Suppressing it would leave a reader unable to distinguish "the filter
        rejected this" from "the filter never saw it".
        """
        from netgravity.ingestion.adapters import market_intelligence
        from netgravity.ingestion.config import load_config

        config = load_config()
        known = set(request.options.get("known_facility_ids", []) or [])

        signals, file_result = market_intelligence.ingest_file(
            Path(request.source), config, known_entity_ids=known,
        )

        findings = self._findings_from(file_result)
        errors = [f.message for f in findings
                  if f.severity == ValidationSeverity.ERROR]
        warnings = [f.message for f in findings
                    if f.severity == ValidationSeverity.WARNING]

        if errors:
            status = ExtractionStatus.REJECTED
        elif not signals:
            # Nothing extracted is not an error — most articles say nothing
            # that moves a cost. But it is not silent success either.
            status = ExtractionStatus.WARNING
            warnings.append(
                f"No market signals were extracted from '{Path(request.source).name}'. "
                f"The document states no change that would move a cost, a "
                f"transit time, a capacity or a demand."
            )
        elif warnings:
            status = ExtractionStatus.WARNING
        else:
            status = ExtractionStatus.ACCEPTED

        passed = [s for s in signals if getattr(s, "passed_guardrail", False)]

        return ExtractionResult(
            status=status,
            ingestion_id=request.ingestion_id,
            canonical_data=None,
            # Market intelligence NEVER lands in `external_signals`. See the
            # schema: one mixed list would force consumers to type-test their
            # way to the difference between a hazard and a price change.
            market_intelligence=list(signals),
            validation_results=findings,
            warnings=warnings,
            errors=errors,
            provenance=ExtractionProvenance(
                ingestion_id=request.ingestion_id,
                source=request.source[:500],
                source_type=SourceType.MARKET_INTELLIGENCE_DOC,
                started_at=started_at,
                completed_at=_utc_now(),
                ai_assisted=bool(file_result.ai_used and not file_result.ai_stubbed),
                counts={"signals": len(signals),
                        "passed_guardrail": len(passed),
                        "filtered": len(signals) - len(passed)},
            ),
            duration_seconds=round(time.perf_counter() - started, 4),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _rejected(
        request: ExtractionRequest,
        started: float,
        started_at: str,
        source_type: SourceType,
        message: str,
    ) -> ExtractionResult:
        return ExtractionResult(
            status=ExtractionStatus.REJECTED,
            ingestion_id=request.ingestion_id,
            errors=[message],
            validation_results=[ValidationFinding(
                severity=ValidationSeverity.ERROR,
                code="EXTRACTION_REJECTED",
                message=message,
            )],
            provenance=ExtractionProvenance(
                ingestion_id=request.ingestion_id,
                source=str(request.source)[:500],
                source_type=source_type,
                started_at=started_at,
                completed_at=_utc_now(),
            ),
            duration_seconds=round(time.perf_counter() - started, 4),
        )
