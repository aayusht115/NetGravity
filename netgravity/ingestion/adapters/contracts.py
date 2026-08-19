"""
NetGravity — Contract / Rate-Card Adapter
==========================================
Reads freight contracts (PDF or text) and produces structured cost rules.

Extracted surcharges do NOT overwrite lane rates — see schemas/contract.py.
They are a separate adjustment layer so the contracted (headline) rate and
the effective rate remain visible side by side, which is the whole point of
the vendor-comparison story.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.ai.contract_reader import extract_contract
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.schemas.contract import ContractRule
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity

SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}


def read_text(path: Path) -> Tuple[str, Optional[str]]:
    """
    Extract text from a contract file.

    Returns (text, warning). PDF support is optional: if pypdf is not
    installed we say so plainly rather than failing the whole run.
    """
    suffix = path.suffix.lower()

    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace"), None

    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", ("pypdf is not installed — cannot read PDF contracts. "
                        "Run `pip install pypdf`.")
        try:
            reader = PdfReader(str(path))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n".join(pages).strip()
            if not text:
                return "", ("PDF contains no extractable text (likely a scan). "
                            "OCR would be required.")
            return text, None
        except Exception as exc:
            return "", f"failed to read PDF: {type(exc).__name__}: {exc}"

    return "", f"unsupported contract file type '{suffix}'"


def ingest_file(path: Path, config: IngestionConfig,
                known_locations: Optional[List[str]] = None
                ) -> Tuple[Optional[ContractRule], FileResult]:
    """Extract one contract file."""
    result = FileResult(
        source_file=path.name, adapter="contracts", rows_read=1, ai_used=True,
    )

    text, warning = read_text(path)
    if warning:
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-013",
            message=warning, source_file=path.name,
        ))
    if not text:
        result.rows_rejected = 1
        result.ai_stubbed = config.stub_mode
        return None, result

    client = get_client(config)
    rule, note = extract_contract(
        client, text,
        source_key=f"contracts/{path.name}",
        filename=path.name,
        known_locations=known_locations,
        stub_key="contract",
    )

    result.ai_stubbed = rule.extracted_by == "stub"
    result.ai_notes.append(note)
    result.rows_accepted = 1

    # Surface the business finding as a first-class issue, not buried in a log
    if rule.has_hidden_cost:
        for s in rule.surcharges:
            if not (s.applies_to_location_ids or s.applies_to_pin_codes):
                continue
            scope = (", ".join(s.applies_to_location_ids)
                     or f"{len(s.applies_to_pin_codes)} pin codes")
            result.issues.append(RowIssue(
                severity=Severity.WARNING, code="R-014",
                message=(
                    f"{rule.vendor_name}: headline {rule.base_rate:g} {rule.rate_unit} "
                    f"understates true cost — a {s.rate:g} {s.rate_unit} "
                    f"{s.surcharge_type.value} surcharge applies to {scope}"
                ),
                source_file=path.name,
            ))

    # Low-confidence extractions must be reviewed, not trusted silently
    for s in rule.surcharges:
        if s.confidence.value == "LOW":
            result.issues.append(RowIssue(
                severity=Severity.WARNING, code="R-015",
                message=f"low-confidence extraction of "
                        f"{s.surcharge_type.value} surcharge — verify against source",
                source_file=path.name,
            ))

    return rule, result


def ingest_directory(contract_dir: Path, config: IngestionConfig,
                     known_locations: Optional[List[str]] = None
                     ) -> Tuple[List[ContractRule], List[FileResult]]:
    contract_dir = Path(contract_dir)
    rules: List[ContractRule] = []
    results: List[FileResult] = []

    for path in sorted(contract_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        rule, result = ingest_file(path, config, known_locations)
        results.append(result)
        if rule is not None:
            rules.append(rule)

    return rules, results


def compare_vendors(rules: List[ContractRule], location_id: str) -> List[dict]:
    """
    Rank vendors by EFFECTIVE cost at one destination.

    This is the calculation behind the headline story: the cheapest-looking
    vendor is not the cheapest vendor everywhere. Pure arithmetic on extracted
    values — no model involvement in the numbers.
    """
    rows = []
    for r in rules:
        effective = r.effective_rate_for(location_id)
        rows.append({
            "vendor": r.vendor_name,
            "headline_rate": r.base_rate,
            "effective_rate": effective,
            "premium": effective - r.base_rate,
            "unit": r.rate_unit,
        })
    return sorted(rows, key=lambda r: r["effective_rate"])
