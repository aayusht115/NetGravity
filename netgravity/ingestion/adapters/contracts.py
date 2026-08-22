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

from netgravity.ingestion.ai.cache import load_cached_contract, save_contract
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, get_client
from netgravity.ingestion.ai.contract_reader import extract_contract
from netgravity.ingestion.memory.document_memory import DocumentMemory
from netgravity.ingestion.pdf_quality import assess
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.schemas.contract import (
    ContractRule,
    ExtractionConfidence,
)
from netgravity.ingestion.schemas.ingest_result import FileResult, RowIssue, Severity
from netgravity.ingestion.storage.base import StorageBackend

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
                # PARKED, NOT BUILT (2026-08-21): a page with no text layer
                # at all — a scan or a photo of a document — cannot be
                # recovered by anything in this file. The only fix is OCR
                # (reading the image and turning it into text), which is a
                # separate, deliberately deferred piece of work — see
                # docs/ingestion_business_rules.md §10. Do not attempt to
                # send an empty string anywhere downstream; report the
                # reason and reject the file honestly instead.
                return "", ("PDF contains no extractable text (likely a scan "
                            "or image-only page). OCR would be required to "
                            "read it, and OCR is not implemented yet — "
                            "parked for a future iteration.")
            return text, None
        except Exception as exc:
            return "", f"failed to read PDF: {type(exc).__name__}: {exc}"

    return "", f"unsupported contract file type '{suffix}'"


def page_count(path: Path) -> int:
    """Pages in a PDF, for scaling the emptiness check. 1 for plain text."""
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        from pypdf import PdfReader
        return max(1, len(PdfReader(str(path)).pages))
    except Exception:
        return 1


def read_contract(path: Path) -> Tuple[str, Optional[str], bool]:
    """
    Read a contract and judge whether the extracted text can be TRUSTED.

    Returns (text, warning, quality_failed).

    ONE ROUTE ONLY (simplified 2026-08-21)
        pypdf extracts the text; the text goes to the model. That is the
        whole pipeline. There is no second route.

        The previous design escalated unreadable documents by sending the
        PDF file itself to the model. That was removed deliberately: the
        configured provider (Gemini via its OpenAI-compatible endpoint)
        rejects document parts outright, so the escalation could only ever
        spend an API call to rediscover the same 400 error. A file this
        pipeline cannot turn into text is now simply reported as unreadable.

    The third value is NOT a routing decision any more — it is a CONFIDENCE
    signal. pypdf fails quietly as well as loudly: a broken font encoding
    returns text that looks like text and is not. That text is still sent to
    the model (it is all we have, and it is often partially recoverable),
    but everything extracted from it is forced to LOW confidence and
    flagged, so nobody mistakes a salvage job for a clean read.
    """
    text, warning = read_text(path)

    if warning or not text:
        # Nothing usable came out. There is no second route to try, so the
        # quality flag is meaningless here.
        return text, warning, False

    quality = assess(text, page_count=path.suffix.lower() == ".pdf"
                     and page_count(path) or 1)
    if quality.usable:
        return text, None, False

    return text, (f"extracted text failed quality checks — {quality.summary}"), True


def ingest_file(path: Path, config: IngestionConfig,
                known_locations: Optional[List[str]] = None,
                storage: Optional[StorageBackend] = None,
                *, use_cache: bool = True
                ) -> Tuple[Optional[ContractRule], FileResult]:
    """
    Extract one contract file.

    `storage` enables the extraction cache. A contract whose text is unchanged
    since a previous run is served from cache and costs no model call. Pass
    use_cache=False (or omit storage) to force a fresh extraction.
    """
    result = FileResult(
        source_file=path.name, adapter="contracts", rows_read=1, ai_used=True,
    )

    text, warning, quality_failed = read_contract(path)
    if warning:
        result.issues.append(RowIssue(
            severity=Severity.WARNING, code="R-013",
            message=warning, source_file=path.name,
        ))

    # ------------------------------------------------------------------
    # ONE ROUTE: pypdf extracts text, the text goes to the model.
    #
    #   1. Clean text        -> extracted normally. The everyday case.
    #
    #   2. Poor-quality text -> still sent to the model, because imperfect
    #                           text is all we have and is often partly
    #                           recoverable. Everything from it is forced to
    #                           LOW confidence and flagged (R-027) so a
    #                           salvage job is never mistaken for a clean
    #                           read. Not cached, and never learned as a
    #                           document shape.
    #
    #   3. No text at all    -> reported as unreadable and rejected (R-027).
    #                           No second attempt is made. Sending the PDF
    #                           file itself was removed: the configured
    #                           provider rejects document parts, so it could
    #                           only spend an API call to rediscover that.
    #                           OCR would be the real fix and is PARKED.
    # ------------------------------------------------------------------
    if not text:
        _reject_unreadable(result, path)
        result.ai_stubbed = config.stub_mode
        return None, result

    # --- reuse a previous extraction of this exact document, if we have one ---
    # No quality guard is needed on the LOOKUP. A degraded extraction is
    # never SAVED (see _ring_fence_degraded), and the cache is keyed on the
    # document text itself — so text that fails the checks today failed them
    # when it was first seen too, and was never written. Guarding here as
    # well would only cost a re-extraction for no gain.
    cached = load_cached_contract(text, storage) if use_cache else None
    if cached is not None:
        result.ai_used = False
        result.ai_stubbed = False
        result.rows_accepted = 1
        result.ai_notes.append(
            f"reused cached extraction for '{path.name}' "
            f"(originally by {cached.extracted_by}; document text unchanged) "
            f"— no model call needed"
        )
        _flag_hidden_costs(cached, result, path.name)
        return cached, result

    client = get_client(config)
    rule, note = extract_contract(
        client, text,
        source_key=f"contracts/{path.name}",
        filename=path.name,
        known_locations=known_locations,
        stub_key="contract",
    )

    result.ai_stubbed = rule.extracted_by == "stub"
    result.ai_failed = LLM_FAILURE_MARKER in note
    result.ai_notes.append(note)
    result.rows_accepted = 1

    if quality_failed:
        _ring_fence_degraded(rule, result, path)
        _flag_hidden_costs(rule, result, path.name)
        return rule, result

    # Only genuine model output is cached — never stub data. See ai/cache.py.
    if use_cache and save_contract(rule, text, storage):
        result.ai_notes.append("extraction cached for future runs")

    _remember_document_shape(text, rule, path.name, storage, result)

    _flag_hidden_costs(rule, result, path.name)
    return rule, result


def _reject_unreadable(result: FileResult, path: Path) -> None:
    """
    Terminal state: the file could not be turned into text by any means
    currently built, so there is nothing to send to the model.

    NO API CALL IS MADE HERE, deliberately. The previous design spent one
    call asking the provider to read the document itself; the configured
    provider rejects document parts outright, so that call could only ever
    buy the same 400 error. An unreadable file now costs nothing.

    OCR IS PARKED, NOT BUILT (decision recorded 2026-08-21). A page with no
    text layer is an image; reading it needs OCR, which is deferred. Until
    then this is the honest ending — name the reason, reject the file, and
    never let an unreadable document quietly contribute figures to a cost
    model.
    """
    result.rows_rejected = 1
    result.issues.append(RowIssue(
        severity=Severity.WARNING, code="R-027",
        message=(
            f"'{path.name}' could not be read: no usable text could be "
            f"extracted from it. If this is a scanned or photographed page "
            f"it needs OCR, which is not implemented (parked). The file was "
            f"rejected rather than filled with assumed values."
        ),
        source_file=path.name,
    ))


def _ring_fence_degraded(rule: ContractRule, result: FileResult,
                         path: Path) -> None:
    """
    Mark an extraction that came from text which FAILED the quality checks.

    The text was still worth sending — it is all we have, and a corrupt text
    layer is often only partly corrupt. But three things must be true of the
    result, or a salvage job starts to look like a clean read:

      - confidence is forced to LOW on the rule and on every surcharge,
        whatever the model claimed. The model scored its certainty from the
        text it was handed; it had no way to know that text was already
        judged untrustworthy, so the pipeline applies that judgement.
      - it is NOT cached, so a degraded read can never be silently reused
        later as though it were sound. (The caller skips save_contract.)
      - it does NOT feed document-shape memory, because learning the wording
        shape of text we know is corrupt would poison future matches.
    """
    rule.extraction_confidence = ExtractionConfidence.LOW
    for surcharge in rule.surcharges:
        surcharge.confidence = ExtractionConfidence.LOW

    result.issues.append(RowIssue(
        severity=Severity.WARNING, code="R-027",
        message=(
            f"'{path.name}' was extracted from text that failed the quality "
            f"checks. Treat every figure as LOW confidence and confirm it "
            f"against the source document before use. Not cached, and not "
            f"learned as a document template."
        ),
        source_file=path.name,
    ))


def _remember_document_shape(text: str, rule: ContractRule, filename: str,
                             storage: Optional[StorageBackend],
                             result: FileResult) -> None:
    """
    Record the document's wording shape, and say when it looked familiar.

    Distinct from the exact-text cache: this recognises a RENEWAL — same
    template, new rates — which the cache necessarily misses because the
    bytes changed. Knowing a document is a known template is what lets a
    renewal be treated with more confidence than a stranger.
    """
    if storage is None or rule.extracted_by == "stub":
        return
    try:
        memory = DocumentMemory(storage)
        match = memory.find(text)
        if match.matched:
            result.ai_notes.append(
                f"document shape recognised — {match.rationale}")
        memory.record(text, document_name=filename,
                      labels={"vendor": rule.vendor_name or "",
                              "extracted_by": rule.extracted_by})
    except Exception:
        # Memory is an optimisation. Losing it must never fail an extraction
        # that otherwise succeeded.
        pass


def _flag_hidden_costs(rule: ContractRule, result: FileResult,
                       filename: str) -> None:
    """
    Raise the business findings as first-class issues.

    Runs for cached extractions too — a cached result must produce exactly the
    same warnings as a fresh one, otherwise the hidden-surcharge finding (the
    whole point of reading contracts) would quietly disappear on the second
    run and reappear only when the cache was cleared.
    """
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
                source_file=filename,
            ))

    # Low-confidence extractions must be reviewed, not trusted silently
    for s in rule.surcharges:
        if s.confidence.value == "LOW":
            result.issues.append(RowIssue(
                severity=Severity.WARNING, code="R-015",
                message=f"low-confidence extraction of "
                        f"{s.surcharge_type.value} surcharge — verify against source",
                source_file=filename,
            ))


def ingest_directory(contract_dir: Path, config: IngestionConfig,
                     known_locations: Optional[List[str]] = None,
                     storage: Optional[StorageBackend] = None,
                     *, use_cache: bool = True
                     ) -> Tuple[List[ContractRule], List[FileResult]]:
    contract_dir = Path(contract_dir)
    rules: List[ContractRule] = []
    results: List[FileResult] = []

    for path in sorted(contract_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        rule, result = ingest_file(path, config, known_locations,
                                   storage, use_cache=use_cache)
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
