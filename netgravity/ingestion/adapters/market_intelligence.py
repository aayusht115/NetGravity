"""
NetGravity — Market Intelligence Document Adapter
==================================================
Reads a news article, circular or notice — PDF, text or markdown — and turns
what it STATES into dated, sourced `MarketIntelligenceSignal` records.

HOW THIS DIFFERS FROM `adapters/signals.py`
--------------------------------------------
`signals.py` ingests signals that are ALREADY structured: a JSON file where
somebody (or something) has done the reading. This adapter starts from prose.
Both end at the same place — the same schema, the same guardrail, the same
`passed_guardrail` gate — so a signal typed on a spreadsheet, extracted from a
PDF, or seeded from JSON is scored by exactly one policy.

There is a third route with no adapter at all: a SPREADSHEET of signals rides
the ordinary tabular pipeline as `ContentType.MARKET_SIGNAL` and lands in
staging, because a sheet with headers needs classification and column mapping,
not document reading. Three intake shapes, one schema, one guardrail.

WHAT THIS ADAPTER WILL NOT DO
-----------------------------
It will not fetch. There is no HTTP call, no feed reader and no scraper here,
by decision: signals arrive because a person supplied them. The seam for a
future automated fetcher already exists one layer up (`sources/`, the same
place an ERP or WMS connector would plug in), so adding one later needs no
change to this file.

It will not create or alter a scenario. Extraction produces evidence. Whether
a signal warrants a what-if is a question for a person, asked through the
orchestrator.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from netgravity.ingestion import document_text
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, get_client
from netgravity.ingestion.ai.signal_reader import extract_signals
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.guardrails import apply as apply_guardrails
from netgravity.ingestion.guardrails import load_policy
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity
from netgravity.ingestion.schemas.signal import (
    MarketIntelligenceSignal,
    SignalConfidence,
)

#: Same document types the contract adapter reads, from the same shared list.
SUPPORTED_SUFFIXES = set(document_text.SUPPORTED_SUFFIXES)


def ingest_file(path: Path, config: IngestionConfig,
                known_entity_ids: Optional[Set[str]] = None,
                ) -> Tuple[List[MarketIntelligenceSignal], FileResult]:
    """
    Read one document and return the signals it states, guardrail-scored.

    Every signal is returned, passed or filtered. Filtering happens at the
    point of consumption by checking `signal.passed_guardrail`, never by
    dropping records here — a filter nobody can inspect is indistinguishable
    from a bug.
    """
    path = Path(path)
    result = FileResult(source_file=path.name, adapter="market_intelligence",
                        rows_read=1, ai_used=True)

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        # Costs nothing. Refusing an unreadable type before the model is the
        # same discipline the contract adapter applies: never spend a call
        # from a shared budget to rediscover something the filename said.
        result.ai_used = False
        result.rows_rejected = 1
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-028",
            message=(f"'{path.name}' is a {path.suffix or 'extensionless'} "
                     f"file; this adapter reads "
                     f"{', '.join(sorted(SUPPORTED_SUFFIXES))}. If the signals "
                     f"are in a spreadsheet, send it through the tabular "
                     f"pipeline instead — it is a supported route."),
            source_file=path.name,
        ))
        return [], result

    text, warning, quality_failed = document_text.read_document(path)

    # The quality judgement applies to PDFs ONLY, and that is not a shortcut.
    # `pdf_quality.assess()` exists to catch pypdf EXTRACTION artefacts — a
    # broken font encoding that returns characters which look like text and
    # are not. A .txt or .md file was never extracted from anything, so there
    # is no extraction to distrust; a short pasted news snippet would fail the
    # characters-per-page heuristic purely for being short, and would then be
    # forced to LOW confidence for no reason a reviewer could defend.
    #
    # (The contract adapter still applies the check to text files. That is
    # pre-existing behaviour, it is covered by tests, and changing it is a
    # separate decision — not one to make silently from here.)
    if path.suffix.lower() != ".pdf" and text and quality_failed:
        quality_failed = False
        warning = None      # and do not report a finding we just discounted

    if warning:
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-013",
            message=warning, source_file=path.name,
        ))

    if not text:
        # No text, no second route, no API call. See document_text: OCR is
        # parked, and an unreadable file is reported by name and reason
        # rather than filled in with assumed values.
        result.ai_used = False
        result.rows_rejected = 1
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-027",
            message=(
                f"'{path.name}' could not be read: no usable text could be "
                f"extracted from it. If this is a scanned or photographed "
                f"page it needs OCR, which is not implemented (parked). The "
                f"file was rejected rather than filled with assumed values."
            ),
            source_file=path.name,
        ))
        result.ai_stubbed = config.stub_mode
        return [], result

    client = get_client(config)
    signals, rejections, note = extract_signals(
        client, text,
        filename=path.name,
        known_entity_ids=sorted(known_entity_ids or []),
    )

    result.ai_stubbed = bool(signals) and signals[0].structured_by == "stub"
    result.ai_failed = LLM_FAILURE_MARKER in note
    result.ai_notes.append(note)

    for reason in rejections:
        result.rows_rejected += 1
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-029",
            message=reason, source_file=path.name,
        ))

    if not signals:
        result.ai_notes.append(
            "no signals were extracted — the document states no change that "
            "would move a cost, a transit time, a capacity or a demand"
        )
        return [], result

    if quality_failed:
        _ring_fence_degraded(signals, result, path)

    result.rows_accepted = len(signals)

    # --- the guardrail gate: deterministic, versioned, and the same one the
    # --- JSON and spreadsheet routes pass through.
    policy = load_policy()
    signals = apply_guardrails(signals, known_entity_ids=known_entity_ids,
                               policy=policy)

    filtered = [s for s in signals if not s.passed_guardrail]
    for s in filtered:
        result.issues.append(RowIssue(
            severity=Severity.INFO, code="R-GUARD",
            message=(f"filtered [{s.bucket.value}] \"{s.title}\" — "
                     f"{s.verdict.reason if s.verdict else 'no verdict'}"),
            source_file=path.name,
        ))

    result.ai_notes.append(
        f"guardrail policy {policy.version} "
        f"(owner: {policy.owner or 'unassigned'}) — "
        f"{len(signals) - len(filtered)}/{len(signals)} signals passed"
    )
    return signals, result


def ingest_directory(document_dir: Path, config: IngestionConfig,
                     known_entity_ids: Optional[Set[str]] = None,
                     ) -> Tuple[List[MarketIntelligenceSignal], List[FileResult]]:
    """Read every supported document in a folder."""
    document_dir = Path(document_dir)
    all_signals: List[MarketIntelligenceSignal] = []
    results: List[FileResult] = []

    for path in sorted(document_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        signals, result = ingest_file(path, config, known_entity_ids)
        all_signals.extend(signals)
        results.append(result)

    return all_signals, results


def _ring_fence_degraded(signals: Sequence[MarketIntelligenceSignal],
                         result: FileResult, path: Path) -> None:
    """
    Mark signals extracted from text that FAILED the quality checks.

    The text was still worth sending — a corrupt text layer is often only
    partly corrupt — but confidence is forced to LOW whatever the model
    claimed. The model scored its certainty from the text it was handed; it
    had no way to know that text was already judged untrustworthy, so the
    pipeline applies that judgement.

    Confidence is not cosmetic here. It feeds the guardrail's relevance score
    directly (LOW carries a penalty), so a salvaged read is correspondingly
    less likely to clear the bar — which is the intended outcome.
    """
    for signal in signals:
        signal.confidence = SignalConfidence.LOW

    result.issues.append(RowIssue(
        severity=Severity.WARNING, code="R-027",
        message=(
            f"'{path.name}' was read from text that failed the quality "
            f"checks. Every signal from it is forced to LOW confidence — "
            f"confirm against the source document before acting on it."
        ),
        source_file=path.name,
    ))
