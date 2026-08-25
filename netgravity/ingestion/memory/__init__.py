"""
NetGravity — Ingestion Memory
==============================
What the system has learned, so it stops asking the same question.

    FieldMemory      column meaning for tabular data, scope resolved from
                     evidence (see field_memory.py)
    DocumentMemory   document shape for PDFs, matched on wording rather than
                     exact bytes (see document_memory.py)
"""

from netgravity.ingestion.memory.document_memory import (
    DocumentMemory,
    DocumentPattern,
    PatternMatch,
    signature,
    similarity,
)
from netgravity.ingestion.memory.field_memory import (
    GENERALISE_AFTER_SOURCES,
    SCOPE_CONFLICT,
    SCOPE_EXACT,
    SCOPE_GENERALISED,
    SCOPE_NONE,
    SCOPE_SUGGESTED,
    FieldMemory,
    FieldObservation,
    MemoryResolution,
)
from netgravity.ingestion.memory.field_catalog import CatalogEntry, FieldCatalog

__all__ = [
    "FieldMemory", "FieldObservation", "MemoryResolution",
    "CatalogEntry", "FieldCatalog",
    "GENERALISE_AFTER_SOURCES",
    "SCOPE_EXACT", "SCOPE_GENERALISED", "SCOPE_SUGGESTED",
    "SCOPE_CONFLICT", "SCOPE_NONE",
    "DocumentMemory", "DocumentPattern", "PatternMatch", "signature", "similarity",
]
