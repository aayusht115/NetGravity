"""
NetGravity — Action Agent Report Builder
===========================================
Builds the PDF attachment for recommendation/investigate emails, from
already-computed KPI results and scenario definitions — never a new
computation.

No PDF-generation library is a project dependency today (pypdf is used only
to READ contract PDFs). Rather than adding one for a small, plain-text
attachment, this writes a minimal single-page PDF directly using the
stdlib — a well-known, small technique. If the attachment's needs grow
(tables, charts) a real library becomes the right call; that day isn't
today.
"""

from __future__ import annotations

from typing import List


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _wrap(text: str, width: int = 95) -> List[str]:
    lines: List[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > width and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def build_pdf(title: str, sections: List[str]) -> bytes:
    """
    A minimal single-page PDF: a bold title line followed by wrapped body
    text, Helvetica 11pt. Enough for "here is the reasoning and numbers
    behind this recommendation" — not a layout engine.
    """
    lines: List[str] = [title, ""]
    for section in sections:
        lines.extend(_wrap(section))
        lines.append("")

    # Content stream: start near the top, one Tj per line, moving down.
    y = 760
    ops = ["BT", "/F1 11 Tf"]
    ops.append(f"1 0 0 1 50 {y} Tm")
    first = True
    for line in lines:
        if not first:
            ops.append("0 -14 Td")
        ops.append(f"({_escape(line)}) Tj")
        first = False
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1", errors="replace")

    objects: List[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    objects.append(
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + obj + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    ).encode("ascii")
    return bytes(out)


def build_recommendation_pdf(*, headline: str, narrative: str,
                             kpi_summary: str = "") -> bytes:
    sections = [narrative]
    if kpi_summary:
        sections.append(kpi_summary)
    return build_pdf(headline, sections)


def build_investigate_pdf(*, headline: str, narrative: str,
                          reason: str = "") -> bytes:
    sections = [narrative]
    if reason:
        sections.append(f"Why this needs a human decision: {reason}")
    return build_pdf(headline, sections)
