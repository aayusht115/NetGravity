"""
NetGravity — Data Source Interface
===================================
ONE shape that every origin of data must present, so nothing downstream has
to care where records came from.

WHY THIS EXISTS
---------------
Ingestion started file-shaped: open a path, read a sheet. That assumption
was baked into every adapter. But the real sources for this system are:

    1. ERP / WMS systems      — live, queried over an API or database
    2. Excel / CSV files      — uploaded by a person, clean or messy
    3. PDFs                   — contracts and rate cards

Only (2) and (3) are files. An ERP is a connection, not a path, and its
"tables" are not sheets. If the pipeline keeps asking sources for a file
path, adding an ERP later means rewriting the pipeline instead of adding a
connector.

So the pipeline asks for RECORD SETS, not files. A CSV yields one record
set. An Excel workbook yields one per sheet. An ERP yields one per table or
endpoint. Everything after this point — classification, column mapping,
human review, routing — is written against RecordSet and is therefore
already source-agnostic.

The ERP/WMS connector itself is a deliberate stub (see erp.py). The point of
this file is that plugging it in later requires no change anywhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class RecordOrigin:
    """
    Where one record set came from.

    This is provenance, and it is also what the memory layer keys on: a
    confirmed mapping is stored against the ORIGIN it was confirmed for, and
    only generalises to other origins once repeated evidence supports it.
    See memory/field_memory.py.
    """

    source_type: str = "file"          # "file" | "erp" | "wms"
    source_id: str = "unknown"         # sender/system identity, e.g. "vendor_a", "sap_prod"
    container: str = ""                # file name, table name, or endpoint
    sheet: Optional[str] = None        # sheet name, where the container has several

    @property
    def label(self) -> str:
        """Human-readable one-liner for reports and review screens."""
        base = self.container or self.source_id
        return f"{base}#{self.sheet}" if self.sheet else base

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "container": self.container,
            "sheet": self.sheet,
        }


@dataclass
class RecordSet:
    """
    One table's worth of rows, from any source.

    `columns` is kept separately from `rows` on purpose: a column that is
    present but entirely empty still needs to be mapped or explicitly marked
    unmapped, and it would be invisible if we only inspected row keys.
    """

    key: str                                        # stable id within the source
    columns: List[str] = field(default_factory=list)
    rows: List[Dict[str, Any]] = field(default_factory=list)
    origin: RecordOrigin = field(default_factory=RecordOrigin)
    warning: Optional[str] = None                   # non-fatal read problem

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def is_empty(self) -> bool:
        return not self.rows or not self.columns

    def sample_rows(self, limit: int = 5) -> List[Dict[str, Any]]:
        """
        Real rows to hand the model as evidence.

        Deliberately more than one: a single row cannot show a pattern, and
        pattern is exactly what distinguishes a shipment log from a product
        master when both happen to have a column called "Weight".
        """
        return self.rows[:limit]


class DataSource(ABC):
    """
    Anything that can produce record sets.

    Implementations must be lazy where they can be: record_sets() returns an
    iterator so an ERP connector can page through a large table without
    holding all of it in memory.
    """

    #: "file" | "erp" | "wms" — set by the implementation
    source_type: str = "unknown"

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Stable identity for this source (a sender, a system, an upload)."""

    @abstractmethod
    def record_sets(self) -> Iterator[RecordSet]:
        """Yield every record set this source can produce."""

    def describe(self) -> str:
        return f"{self.source_type}:{self.source_id}"
