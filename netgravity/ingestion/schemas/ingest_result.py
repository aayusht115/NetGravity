"""
NetGravity — Ingestion Result Schemas
======================================
The objects the pipeline reports back with.

DESIGN NOTE — two validation layers, not one
--------------------------------------------
netgravity/validation/checks.py already validates an ASSEMBLED
CanonicalNetwork ("will this solve?", codes V-001..V-014).

This module supports the layer BEFORE that: row-level checks on raw input
("is this row even parseable?", codes R-001..R-0nn). Both run; they answer
different questions and neither replaces the other.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    ERROR = "ERROR"      # row is unusable — dropped from the network
    WARNING = "WARNING"  # row is kept but flagged for human review
    INFO = "INFO"        # noted for traceability only


class RowIssue(BaseModel):
    """A single problem found in a single input row."""
    severity: Severity
    code: str                              # e.g. "R-004"
    message: str
    source_file: str = ""
    row_number: Optional[int] = None       # 1-based, matching what a user sees in Excel
    column: Optional[str] = None
    raw_value: Optional[str] = None

    def render(self) -> str:
        loc = self.source_file or "?"
        if self.row_number is not None:
            loc += f" row {self.row_number}"
        if self.column:
            loc += f" [{self.column}]"
        return f"[{self.code}] {loc} — {self.message}"


class FileResult(BaseModel):
    """Outcome of ingesting one source file."""
    source_file: str
    adapter: str                           # "structured" | "distributor" | "contracts" | "signals"
    rows_read: int = 0
    rows_accepted: int = 0
    rows_rejected: int = 0
    issues: List[RowIssue] = Field(default_factory=list)

    # Set by AI-backed adapters so a reader can tell live extraction from stubs
    ai_used: bool = False
    ai_stubbed: bool = False
    # True when a LIVE call was attempted and failed. ai_stubbed alone is
    # ambiguous: it is also set when running deliberately without a key.
    ai_failed: bool = False
    ai_notes: List[str] = Field(default_factory=list)

    @property
    def rows_flagged(self) -> int:
        return len({i.row_number for i in self.issues
                    if i.severity == Severity.WARNING and i.row_number is not None})

    @property
    def ok(self) -> bool:
        return not any(i.severity == Severity.ERROR for i in self.issues)


class IngestionReport(BaseModel):
    """
    The full outcome of one ingestion run — this is what the CLI prints and
    what the future S3 'Data Ingestion Console' screen would render.
    """
    run_id: str
    started_at: str
    source: str
    files: List[FileResult] = Field(default_factory=list)

    # Populated once the network is assembled + engine-validated
    network_assembled: bool = False
    data_version: Optional[str] = None
    snapshot_path: Optional[str] = None
    engine_validation_passed: Optional[bool] = None
    engine_validation_issues: List[str] = Field(default_factory=list)

    counts: Dict[str, int] = Field(default_factory=dict)
    extras: Dict[str, Any] = Field(default_factory=dict)

    # Deterministic, rule-based data-completeness check (see
    # netgravity.ingestion.completeness). Reported always; blocking only
    # when NETGRAVITY_COMPLETENESS_BLOCKS_FINALIZE is set — see the note in
    # pipeline.py step 3b.
    missing_required: List[Dict[str, Any]] = Field(default_factory=list)
    missing_optional: List[Dict[str, Any]] = Field(default_factory=list)

    @property
    def total_rows_read(self) -> int:
        return sum(f.rows_read for f in self.files)

    @property
    def total_rows_accepted(self) -> int:
        return sum(f.rows_accepted for f in self.files)

    @property
    def all_issues(self) -> List[RowIssue]:
        return [i for f in self.files for i in f.issues]

    @property
    def errors(self) -> List[RowIssue]:
        return [i for i in self.all_issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[RowIssue]:
        return [i for i in self.all_issues if i.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors and (self.engine_validation_passed is not False)
