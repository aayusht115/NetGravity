#!/usr/bin/env python3
"""
NetGravity — live check of all three PDF reading paths
=======================================================
Runs the REAL ingestion code against three mock PDFs, one per case, and
prints what happened plus the token usage for the whole run.

    python scripts/verify_pdf_paths.py

ONE ROUTE: pypdf extracts the text, the text goes to the model.

    1. CLEAN TEXT      extracted normally. The everyday case.
    2. GARBLED TEXT    pypdf extracts something that fails the quality
                       checks. It is STILL sent to the model — imperfect
                       text is all we have and is often partly recoverable
                       — but everything from it is forced to LOW confidence
                       and flagged (R-027), never cached, and never learned
                       as a document template.
    3. NO TEXT AT ALL  reported as unreadable and rejected (R-027). No
                       second attempt is made. OCR would be the real fix
                       and is deliberately PARKED, not built.

Useful flags:
    --full     print the extracted text in full rather than truncated
    --trace    also log the exact prompt sent to the model and the raw,
               unparsed response it returned

The mock PDFs are generated into a temp directory at runtime — nothing is
written into the repo, and there are no extra dependencies to install.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from netgravity.ingestion.adapters import contracts as adapter  # noqa: E402
from netgravity.ingestion.config import IngestionConfig  # noqa: E402
from netgravity.ingestion.ai.client import fetch_gateway_usage  # noqa: E402
from netgravity.telemetry import ledger  # noqa: E402

REAL_CONTRACT = (REPO_ROOT / "data" / "mock" / "india" / "contracts"
                 / "pdf_samples" / "transcorp_rate_card.pdf")

_SHOW_FULL = False


# ---------------------------------------------------------------------------
# Mock PDF generation — stdlib only, no reportlab needed
# ---------------------------------------------------------------------------

def _write_pdf(path: Path, lines: list) -> Path:
    """Emit a minimal one-page PDF containing `lines`. Empty list -> a scan."""
    content = "BT /F1 10 Tf 40 780 Td 12 TL\n"
    for line in lines:
        safe = line.replace("\\", "").replace("(", "").replace(")", "")
        content += f"({safe}) Tj T*\n"
    content += "ET"

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        ("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources "
         "<< /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = "%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{obj}\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n"
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n")

    path.write_bytes(out.encode("latin-1"))
    return path


def _garbled(path: Path) -> Path:
    """A text layer that exists but is corrupt — the QUIET failure."""
    return _write_pdf(path, ["x" * 80] * 12)


def _scan(path: Path) -> Path:
    """A page with no text layer at all."""
    return _write_pdf(path, [])


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _run_case(label: str, path: Path, config: IngestionConfig) -> None:
    print()
    print("=" * 72)
    print(label)
    print("=" * 72)

    text, warning, quality_failed = adapter.read_contract(path)
    print(f"  pypdf text length   : {len(text)} chars")
    print(f"  quality warning     : {warning or '(none — text is trusted)'}")
    print(f"  quality failed      : {quality_failed}")

    # STEP 1 OF THE PIPELINE, shown in full: what pypdf pulled out of the
    # file. This is the ONLY thing the model ever sees of this document, so
    # when an extraction looks wrong, this is the first place to look — the
    # fault is usually here rather than in the model.
    if text:
        print("  --- TEXT EXTRACTED FROM PDF (this is what gets sent) ---")
        shown = text if _SHOW_FULL else text[:600]
        for line in shown.splitlines():
            print(f"    | {line}")
        if not _SHOW_FULL and len(text) > 600:
            print(f"    | ... [{len(text) - 600} more chars — "
                  f"run with --full to see everything]")
        print("  --- END EXTRACTED TEXT ---")

    rule, result = adapter.ingest_file(path, config, None, None,
                                       use_cache=False)

    print(f"  rows accepted       : {result.rows_accepted}")
    print(f"  rows rejected       : {result.rows_rejected}")
    print(f"  ai_failed           : {result.ai_failed}")
    for issue in result.issues:
        print(f"  issue [{issue.code}]     : {issue.message}")
    for note in result.ai_notes:
        print(f"  note                : {note}")

    if rule is None:
        print("  RESULT              : no contract extracted (file rejected)")
        return

    print(f"  RESULT              : extracted '{rule.vendor_name}' "
          f"[{rule.contract_id}]")
    print(f"  base rate           : {rule.base_rate} {rule.rate_unit}")
    print(f"  confidence          : {rule.extraction_confidence.value}")
    print(f"  surcharges          : {len(rule.surcharges)}")
    print(f"  extracted by        : {rule.extracted_by}")


def _print_gateway_budget(config: IngestionConfig, when: str) -> None:
    """
    Show the gateway's SHARED, CUMULATIVE budget.

    Worth printing either side of a run: the budget does not reset daily and
    is shared with everyone holding the same token, so "what did this run
    cost the pool" is a question worth being able to answer exactly. This
    endpoint costs no budget and no request quota.
    """
    if (config.llm_provider or "").lower() != "gateway":
        return
    try:
        usage = fetch_gateway_usage(config)
    except Exception as exc:
        print(f"  gateway budget ({when}): could not read — {exc}")
        return
    print(f"  gateway budget ({when}): "
          f"${usage.get('remaining_usd', '?')} remaining of "
          f"${usage.get('budget_usd', '?')}  |  "
          f"{usage.get('requests_today', '?')}/"
          f"{usage.get('max_requests_per_day', '?')} requests today")


def main() -> int:
    global _SHOW_FULL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                        help="print extracted text in full, not truncated")
    parser.add_argument("--trace", action="store_true",
                        help="log the exact prompt sent to the model and the "
                             "raw response it returned")
    args = parser.parse_args()
    _SHOW_FULL = args.full

    if args.trace:
        # Turns on the DEBUG traffic log inside ai/client.py: every prompt
        # sent and every raw response received, verbatim.
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                            format="%(message)s")
        logging.getLogger("netgravity.ingestion.ai.client").setLevel(
            logging.DEBUG)

    config = IngestionConfig()
    print(config.describe())
    if config.key_warning:
        print(f"\n  *** CONFIG WARNING: {config.key_warning} ***\n")
    _print_gateway_budget(config, "before")

    if config.stub_mode:
        print("\n*** STUB MODE — no API key found, so nothing will actually "
              "be sent to a model and this run proves nothing about live "
              "behaviour. Check .env and run from the repo root. ***")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        if REAL_CONTRACT.exists():
            _run_case("CASE 1 — clean text (the everyday path)",
                      REAL_CONTRACT, config)
        else:
            print(f"\n(skipping case 1: {REAL_CONTRACT} not found)")

        _run_case("CASE 2 — garbled text (quality checks fail, text exists)",
                  _garbled(tmp_path / "garbled.pdf"), config)

        _run_case("CASE 3 — scan with no text layer (OCR would be needed)",
                  _scan(tmp_path / "scan.pdf"), config)

    print()
    print("=" * 72)
    print(ledger().summary())
    _print_gateway_budget(config, "after")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
