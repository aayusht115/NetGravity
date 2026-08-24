"""
NetGravity — Document text extraction (shared)
===============================================
ONE place that turns a file on disk into text, for every pipeline that reads
a written document rather than a spreadsheet.

WHY THIS MODULE EXISTS
----------------------
Two pipelines now read prose documents: the contract / rate-card adapter and
the market-intelligence adapter. Both need exactly the same three things —
pull the text out, judge whether that text can be trusted, and fail honestly
when it cannot.

Copying that logic would not have been a style problem. It would have been a
correctness one: the "no text at all" case and the "text came out but it is
corrupt" case carry deliberate, documented behaviour (no API call is spent on
an unreadable file; a degraded read is never cached and never learned as a
template). A second copy drifts, and the copy that drifts is the one that
quietly starts spending budget on scans or trusting garbled figures.

WHAT DELIBERATELY IS *NOT* HERE
-------------------------------
Spreadsheet reading. `sources/files.py` already discovers and reads every CSV
and every sheet of every workbook for the tabular pipeline, and a spreadsheet
of market signals rides that exact path — see `ContentType.MARKET_SIGNAL`. No
second Excel reader was written, because no second Excel reader is needed.

`adapters/distributor.py` and `adapters/structured.py` still hold their own
small CSV/Excel readers. They are left alone on purpose: they work, they are
covered by tests, and consolidating them would be a refactor with real risk
and no caller asking for it today. New document-reading code belongs here.

OCR IS PARKED, NOT BUILT (decision recorded 2026-08-21)
-------------------------------------------------------
A page with no text layer is an image. Reading it needs OCR, which is
deferred. Until then an image-only PDF is reported as unreadable, by name and
reason, and rejected — never filled in with assumed values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from netgravity.ingestion.pdf_quality import assess

#: File types this module can turn into text.
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}

#: Message used when a PDF parses but yields nothing. Kept as a constant so
#: the contract and market-intelligence pipelines report the same reason in
#: the same words, and so a test can assert on it without copying prose.
NO_TEXT_LAYER_WARNING = (
    "PDF contains no extractable text (likely a scan or image-only page). "
    "OCR would be required to read it, and OCR is not implemented yet — "
    "parked for a future iteration."
)


def read_text(path: Path) -> Tuple[str, Optional[str]]:
    """
    Extract text from a document file.

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
            return "", ("pypdf is not installed — cannot read PDF documents. "
                        "Run `pip install pypdf`.")
        try:
            reader = PdfReader(str(path))
            pages = [(p.extract_text() or "") for p in reader.pages]
            text = "\n".join(pages).strip()
            if not text:
                # See the module docstring: this is the OCR case, and OCR is
                # parked. Do not send an empty string anywhere downstream —
                # report the reason and let the caller reject the file.
                return "", NO_TEXT_LAYER_WARNING
            return text, None
        except Exception as exc:
            return "", f"failed to read PDF: {type(exc).__name__}: {exc}"

    return "", f"unsupported document file type '{suffix}'"


def page_count(path: Path) -> int:
    """Pages in a PDF, for scaling the emptiness check. 1 for plain text."""
    if path.suffix.lower() != ".pdf":
        return 1
    try:
        from pypdf import PdfReader
        return max(1, len(PdfReader(str(path)).pages))
    except Exception:
        return 1


def read_document(path: Path) -> Tuple[str, Optional[str], bool]:
    """
    Read a document and judge whether the extracted text can be TRUSTED.

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

    The third value is NOT a routing decision — it is a CONFIDENCE signal.
    pypdf fails quietly as well as loudly: a broken font encoding returns
    text that looks like text and is not. That text is still sent to the
    model (it is all we have, and it is often partially recoverable), but
    everything extracted from it is forced to LOW confidence and flagged, so
    nobody mistakes a salvage job for a clean read.
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
