"""
Tests for netgravity.action_agent.report_builder.

No PDF library is a project dependency (pypdf reads contract PDFs only) —
report_builder.py hand-writes minimal PDF bytes directly. The claim under
test is simply that the output is a VALID, single-page PDF a real reader
can open and that carries the given text — using pypdf (an existing
dependency) as the reader, not asserting anything about layout.
"""

from __future__ import annotations

import io

from pypdf import PdfReader

from netgravity.action_agent.report_builder import (
    build_investigate_pdf,
    build_pdf,
    build_recommendation_pdf,
)


def _text_of(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def test_build_pdf_is_valid_and_contains_title_and_body():
    pdf_bytes = build_pdf("Test Title", ["First paragraph.", "Second paragraph."])
    text = _text_of(pdf_bytes)
    assert "Test Title" in text
    assert "First paragraph." in text
    assert "Second paragraph." in text


def test_recommendation_pdf_contains_headline_and_narrative():
    pdf_bytes = build_recommendation_pdf(
        headline="Close Pune DC", narrative="Utilization has fallen below 40%.",
        kpi_summary="Cost impact: -12L/year")
    text = _text_of(pdf_bytes)
    assert "Close Pune DC" in text
    assert "Utilization has fallen below 40%." in text
    assert "Cost impact: -12L/year" in text


def test_investigate_pdf_contains_reason():
    pdf_bytes = build_investigate_pdf(
        headline="Review lane disruption", narrative="A key lane is at risk.",
        reason="required risk evidence unresolved")
    text = _text_of(pdf_bytes)
    assert "required risk evidence unresolved" in text


def test_special_characters_are_escaped_without_corrupting_the_pdf():
    pdf_bytes = build_pdf("Title (with) parens", ["A line with a backslash \\ in it."])
    text = _text_of(pdf_bytes)
    assert "Title (with) parens" in text
