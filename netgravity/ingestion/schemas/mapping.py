"""
NetGravity — Distributor Column Mapping Schema
===============================================
Every distributor sends a differently-shaped spreadsheet. The AI proposes a
mapping from their columns onto our canonical fields; a human confirms it
ONCE; the confirmed mapping is cached and reused for every later file from
that distributor, so repeat files cost no AI call and need no re-review.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class ColumnMapping(BaseModel):
    """One source column mapped onto one canonical field."""
    source_column: str
    target_field: str                  # canonical field name, e.g. "quantity"
    confidence: float = 0.0            # 0.0 - 1.0
    reasoning: str = ""

    # Unit handling — the classic distributor trap (kg vs units vs cartons)
    source_unit: Optional[str] = None
    target_unit: Optional[str] = None
    conversion_factor: float = 1.0     # multiply source value by this

    @property
    def needs_review(self) -> bool:
        """Below 0.90 confidence a human should look before we trust it."""
        return self.confidence < 0.90


class DistributorMapping(BaseModel):
    """A cached, reusable mapping for one distributor's file format."""
    distributor_id: str
    distributor_name: str = ""

    target_entity: str = "demand"      # which canonical table these rows become
    mappings: List[ColumnMapping] = Field(default_factory=list)

    unmapped_columns: List[str] = Field(default_factory=list)
    confirmed_by_human: bool = False
    created_at: str = ""
    proposed_by: str = "stub"          # "stub" | "<provider>:<model>"

    def as_rename_dict(self) -> Dict[str, str]:
        return {m.source_column: m.target_field for m in self.mappings}

    @property
    def needs_review(self) -> List[ColumnMapping]:
        return [m for m in self.mappings if m.needs_review]

    @property
    def mean_confidence(self) -> float:
        if not self.mappings:
            return 0.0
        return sum(m.confidence for m in self.mappings) / len(self.mappings)
