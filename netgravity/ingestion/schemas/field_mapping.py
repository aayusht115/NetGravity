"""
NetGravity — Field Mapping Decisions
=====================================
The result of deciding what every column in one record set means.

WHY A "DECISION" AND NOT JUST A MAPPING
---------------------------------------
The old ColumnMapping recorded an answer: this column becomes that field, at
this confidence. That is enough to APPLY a mapping and not much else. It
cannot tell a reviewer why the answer was reached, what the alternatives
were, or which independent methods agreed — and those are exactly what a
person needs in order to confirm quickly and correctly.

A ColumnDecision keeps every opinion separately: what memory knew, what the
model said, what the deterministic alias table said. The final answer is
derived from those, and the workings stay attached. That is what lets the
review layer ask a specific question ("this has meant units shipped for
vendor_a and units returned for vendor_b") instead of showing a bare
low-confidence flag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from netgravity.ingestion.schemas.content import ContentClassification, ContentType

# How the final answer was reached.
BY_MEMORY_EXACT = "memory:exact"
BY_MEMORY_GENERALISED = "memory:generalised"
BY_AI_AND_DICTIONARY = "ai+dictionary"
BY_AI = "ai"
BY_DICTIONARY = "dictionary"
BY_NONE = "unmapped"


@dataclass
class MappingOption:
    """One candidate meaning offered to the reviewer, with its provenance."""

    target_field: str
    suggested_by: str                       # "memory" | "ai" | "dictionary"
    rationale: str = ""
    support: int = 0                        # how many senders back it (memory only)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "target_field": self.target_field,
            "suggested_by": self.suggested_by,
            "rationale": self.rationale,
            "support": self.support,
        }


@dataclass
class ColumnDecision:
    """What one column means, how that was decided, and what else it could be."""

    source_column: str
    target_field: Optional[str] = None
    confidence: float = 0.0
    decided_by: str = BY_NONE

    needs_review: bool = True
    review_reasons: List[str] = field(default_factory=list)

    # Every opinion, kept apart rather than collapsed.
    ai_target: Optional[str] = None
    ai_confidence: float = 0.0
    ai_reasoning: str = ""
    dictionary_target: Optional[str] = None
    memory_scope: str = "none"
    memory_rationale: str = ""

    options: List[MappingOption] = field(default_factory=list)
    sample_values: List[str] = field(default_factory=list)

    # Unit handling, carried through from the model's reading.
    source_unit: Optional[str] = None
    target_unit: Optional[str] = None
    conversion_factor: float = 1.0

    @property
    def is_mapped(self) -> bool:
        return bool(self.target_field)

    @property
    def methods_agree(self) -> bool:
        """True when the model and the alias table independently concur."""
        return bool(self.ai_target) and self.ai_target == self.dictionary_target

    @property
    def methods_conflict(self) -> bool:
        """
        Both methods produced an answer and they differ. Distinct from the
        dictionary simply having no entry, which is silence, not dissent.
        """
        return (bool(self.ai_target) and bool(self.dictionary_target)
                and self.ai_target != self.dictionary_target)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_column": self.source_column,
            "target_field": self.target_field,
            "confidence": round(self.confidence, 3),
            "decided_by": self.decided_by,
            "needs_review": self.needs_review,
            "review_reasons": list(self.review_reasons),
            "ai_target": self.ai_target,
            "ai_confidence": round(self.ai_confidence, 3),
            "ai_reasoning": self.ai_reasoning,
            "dictionary_target": self.dictionary_target,
            "memory_scope": self.memory_scope,
            "memory_rationale": self.memory_rationale,
            "options": [o.as_dict() for o in self.options],
            "sample_values": list(self.sample_values),
            "source_unit": self.source_unit,
            "target_unit": self.target_unit,
            "conversion_factor": self.conversion_factor,
        }


@dataclass
class SheetMapping:
    """Every column decision for one record set, plus how it was classified."""

    record_key: str
    source_id: str
    origin_label: str = ""
    classification: ContentClassification = field(default_factory=ContentClassification)
    decisions: List[ColumnDecision] = field(default_factory=list)
    unmapped_columns: List[str] = field(default_factory=list)
    proposed_by: str = "rules"
    notes: List[str] = field(default_factory=list)

    @property
    def content_type(self) -> ContentType:
        return self.classification.content_type

    @property
    def destination(self) -> str:
        return self.classification.destination

    @property
    def pending(self) -> List[ColumnDecision]:
        return [d for d in self.decisions if d.needs_review]

    @property
    def needs_review(self) -> bool:
        return self.classification.needs_review or bool(self.pending)

    @property
    def rename_map(self) -> Dict[str, str]:
        """Only settled columns. A column awaiting review is not applied."""
        return {
            d.source_column: d.target_field
            for d in self.decisions
            if d.target_field and not d.needs_review
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "record_key": self.record_key,
            "source_id": self.source_id,
            "origin_label": self.origin_label,
            "content_type": self.content_type.value,
            "destination": self.destination,
            "classification_confidence": round(self.classification.confidence, 3),
            "classification_needs_review": self.classification.needs_review,
            "proposed_by": self.proposed_by,
            "decisions": [d.as_dict() for d in self.decisions],
            "unmapped_columns": list(self.unmapped_columns),
            "notes": list(self.notes),
        }
