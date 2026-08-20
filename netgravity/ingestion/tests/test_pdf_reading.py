"""
PDF contract reading tests.

The contracts adapter has always accepted `.pdf` in SUPPORTED_SUFFIXES and
had a pypdf-based branch in read_text() — but until now nothing proved the
library was actually installed and actually worked end to end. These tests
pin that: a real PDF (built the same way a client's scanned/exported rate
card would arrive) must read out the same clauses a .txt version does, and a
PDF with no extractable text must fail with a clear, specific warning rather
than a crash or a silent empty extraction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.adapters import contracts as adapter
from netgravity.ingestion.config import IngestionConfig

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "contracts"
PDF_SAMPLE = CONTRACT_DIR / "pdf_samples" / "transcorp_rate_card.pdf"


def test_pypdf_is_installed():
    """
    requirements.txt / pyproject.toml both pin pypdf>=4.0.0. If it is not
    actually installed in the environment, read_text() degrades to a "not
    installed" warning instead of a crash — which is safe, but silently
    turns every PDF contract into a dropped file. Fail loudly here instead.
    """
    import pypdf  # noqa: F401  (import succeeding is the assertion)


def test_read_text_extracts_a_real_pdf_contract():
    assert PDF_SAMPLE.exists(), (
        f"missing sample PDF at {PDF_SAMPLE} — regenerate it (see the mock "
        f"data notes) before running this test"
    )
    text, warning = adapter.read_text(PDF_SAMPLE)
    assert warning is None
    assert "TRANSCORP LOGISTICS" in text
    assert "Rs. 10.00 per kg" in text          # base rate clause
    assert "Rs. 5.00 per kg" in text           # NSL surcharge clause


def test_pdf_with_no_extractable_text_warns_instead_of_crashing(tmp_path):
    """A scanned PDF with no text layer must not be silently treated as an
    empty (and therefore "successfully" extracted) contract."""
    from pypdf import PdfWriter

    blank = tmp_path / "scanned_no_text.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(blank, "wb") as fh:
        writer.write(fh)

    text, warning = adapter.read_text(blank)
    assert text == ""
    assert warning is not None
    assert "no extractable text" in warning.lower()


def test_unsupported_suffix_is_rejected_not_silently_skipped(tmp_path):
    bogus = tmp_path / "rate_card.docx"
    bogus.write_text("irrelevant")
    text, warning = adapter.read_text(bogus)
    assert text == ""
    assert "unsupported" in warning.lower()


def test_ingest_file_runs_end_to_end_on_a_pdf_in_stub_mode():
    """
    Full path: PDF -> read_text -> AI extraction (stub, no key configured in
    tests) -> ContractRule. Confirms the PDF branch feeds the rest of the
    pipeline exactly like the .txt branch already does, not just that the
    text extraction step works in isolation.
    """
    cfg = IngestionConfig()
    cfg.llm_api_key = None  # force stub mode; this test must not call a network
    rule, result = adapter.ingest_file(PDF_SAMPLE, cfg, known_locations=None,
                                       storage=None, use_cache=False)
    assert rule is not None
    assert result.rows_read == 1
    assert result.rows_accepted == 1
    assert not any(i.code == "R-013" for i in result.issues), (
        "a read_text() warning would surface as R-013 — none expected here"
    )
