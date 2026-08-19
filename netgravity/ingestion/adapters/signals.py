"""
NetGravity — External Signal Adapter
=====================================
Ingests dated, sourced external signals and runs every one through the
guardrail before it is allowed to influence anything downstream.

Filtered signals are retained with passed=False so the guardrail's own
decisions are auditable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.guardrails import apply as apply_guardrails
from netgravity.ingestion.guardrails import load_policy
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity
from netgravity.ingestion.schemas.signal import (
    ExternalSignal,
    SignalBucket,
    SignalConfidence,
    SignalDirection,
)


def _parse_signal(raw: Dict[str, Any], index: int, file: str
                  ) -> Tuple[Optional[ExternalSignal], List[RowIssue]]:
    issues: List[RowIssue] = []

    for field in ("signal_id", "title", "published_date"):
        if not raw.get(field):
            issues.append(RowIssue(
                severity=Severity.ERROR, code="R-001",
                message=f"signal is missing required field '{field}'",
                source_file=file, row_number=index,
            ))
    if any(i.severity == Severity.ERROR for i in issues):
        return None, issues

    def _enum(cls, value, default):
        try:
            return cls(str(value).upper())
        except (ValueError, AttributeError):
            return default

    signal = ExternalSignal(
        signal_id=str(raw["signal_id"]),
        title=str(raw["title"]),
        source_title=str(raw.get("source_title") or raw.get("source") or ""),
        source_url=raw.get("source_url"),
        published_date=str(raw["published_date"]),
        effective_date=raw.get("effective_date"),
        bucket=_enum(SignalBucket, raw.get("bucket"), SignalBucket.UNKNOWN),
        direction=_enum(SignalDirection, raw.get("direction"), SignalDirection.NEUTRAL),
        magnitude=str(raw.get("magnitude") or ""),
        affected_entities=[str(e) for e in (raw.get("affected_entities") or [])],
        geography=str(raw.get("geography") or ""),
        confidence=_enum(SignalConfidence, raw.get("confidence"), SignalConfidence.MEDIUM),
        rationale=str(raw.get("rationale") or ""),
        structured_by="seed",
    )
    return signal, issues


def ingest_file(path: Path, config: IngestionConfig,
                known_entity_ids: Optional[Set[str]] = None
                ) -> Tuple[List[ExternalSignal], FileResult]:
    result = FileResult(source_file=path.name, adapter="signals")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result.issues.append(RowIssue(
            severity=Severity.ERROR, code="R-010",
            message=f"signal file is not valid JSON: {exc}", source_file=path.name,
        ))
        return [], result

    raw_signals = payload if isinstance(payload, list) else payload.get("signals", [])
    result.rows_read = len(raw_signals)

    signals: List[ExternalSignal] = []
    for i, raw in enumerate(raw_signals, start=1):
        signal, issues = _parse_signal(raw, i, path.name)
        result.issues.extend(issues)
        if signal is None:
            result.rows_rejected += 1
            continue
        signals.append(signal)
        result.rows_accepted += 1

    # --- the guardrail gate ---
    policy = load_policy()
    signals = apply_guardrails(signals, known_entity_ids=known_entity_ids, policy=policy)

    filtered = [s for s in signals if not s.passed_guardrail]
    for s in filtered:
        result.issues.append(RowIssue(
            severity=Severity.INFO, code="R-GUARD",
            message=f"filtered [{s.bucket.value}] \"{s.title}\" — "
                    f"{s.verdict.reason if s.verdict else 'no verdict'}",
            source_file=path.name,
        ))

    result.ai_notes.append(
        f"guardrail policy {policy.version} (owner: {policy.owner or 'unassigned'}) — "
        f"{len(signals) - len(filtered)}/{len(signals)} signals passed"
    )
    return signals, result


def ingest_directory(signal_dir: Path, config: IngestionConfig,
                     known_entity_ids: Optional[Set[str]] = None
                     ) -> Tuple[List[ExternalSignal], List[FileResult]]:
    signal_dir = Path(signal_dir)
    all_signals: List[ExternalSignal] = []
    results: List[FileResult] = []

    for path in sorted(signal_dir.glob("*.json")):
        signals, result = ingest_file(path, config, known_entity_ids)
        all_signals.extend(signals)
        results.append(result)

    return all_signals, results
