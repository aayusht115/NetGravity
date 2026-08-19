"""
NetGravity — Ingestion Report Renderer
=======================================
Turns an IngestionReport into the terminal output a human reads.

This is deliberately the same information the future 'Data Ingestion Console'
screen would render — one report object, two presentations.
"""

from __future__ import annotations

from typing import List

from netgravity.ingestion.schemas.ingest_result import (
    IngestionReport,
    RowIssue,
    Severity,
)

RULE = "─" * 66


def _sev_icon(sev: Severity) -> str:
    return {Severity.ERROR: "✗", Severity.WARNING: "⚠", Severity.INFO: "ℹ"}[sev]


def render(report: IngestionReport, *, max_issues: int = 25,
           verbose: bool = False) -> str:
    out: List[str] = []
    out.append("")
    out.append(f"NetGravity Ingestion — run {report.started_at}")
    out.append(RULE)

    if not report.files:
        out.append("  (no source files found)")

    # --- per-file summary ---
    for f in report.files:
        name = f.source_file if len(f.source_file) <= 24 else "…" + f.source_file[-23:]
        bits = [f"✓ {f.rows_accepted} ok"]
        rejected = f.rows_rejected
        flagged = f.rows_flagged
        if rejected:
            bits.append(f"✗ {rejected} rejected")
        if flagged:
            bits.append(f"⚠ {flagged} flagged")

        tag = ""
        if f.ai_used:
            tag = "  [AI: STUBBED]" if f.ai_stubbed else "  [AI: live]"

        out.append(f"  {name:<26} {f.rows_read:>5} rows   {'  '.join(bits)}{tag}")

    # --- issues ---
    issues = [i for i in report.all_issues if i.severity != Severity.INFO]
    if verbose:
        issues = report.all_issues

    if issues:
        out.append("")
        shown = issues[:max_issues]
        for i in shown:
            out.append(f"  {_sev_icon(i.severity)} {i.render()}")
        if len(issues) > len(shown):
            out.append(f"  … and {len(issues) - len(shown)} more "
                       f"(use --verbose to see all)")

    # --- AI notes ---
    ai_notes = [(f.source_file, n) for f in report.files for n in f.ai_notes]
    if ai_notes:
        out.append("")
        out.append("  AI extraction notes:")
        for src, note in ai_notes[:max_issues]:
            out.append(f"    · {src}: {note}")

    # --- assembled network ---
    out.append("")
    if report.network_assembled:
        c = report.counts
        out.append(
            "  Network assembled:  "
            f"{c.get('facilities', 0)} facilities · "
            f"{c.get('markets', 0)} markets · "
            f"{c.get('products', 0)} product(s) · "
            f"{c.get('lanes', 0)} lanes · "
            f"{c.get('demands', 0)} demand records"
        )
        if report.engine_validation_passed is not None:
            status = "PASSED" if report.engine_validation_passed else "FAILED"
            n_err = sum(1 for s in report.engine_validation_issues if s.startswith("ERROR"))
            n_warn = sum(1 for s in report.engine_validation_issues if s.startswith("WARNING"))
            out.append(f"  Engine validation:  {status} "
                       f"({n_err} errors, {n_warn} warnings)")
            for line in report.engine_validation_issues[:max_issues]:
                out.append(f"     · {line}")
        if report.snapshot_path:
            out.append(f"  Snapshot written:   {report.snapshot_path}")
        if report.data_version:
            out.append(f"  Data version:       {report.data_version}")
    else:
        out.append("  Network NOT assembled — fix the errors above and re-run.")

    # --- extras (contracts / signals summaries) ---
    for key, value in report.extras.items():
        out.append(f"  {key}: {value}")

    out.append(RULE)
    verdict = "OK" if report.ok else "FAILED"
    out.append(f"  Result: {verdict}   "
               f"({report.total_rows_accepted}/{report.total_rows_read} rows accepted, "
               f"{len(report.errors)} errors, {len(report.warnings)} warnings)")
    out.append("")
    return "\n".join(out)
