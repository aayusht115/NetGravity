"""
NetGravity — Contract & Cost-Adjustment Schemas
================================================
Structured output of reading a freight contract or rate card.

KEY DESIGN DECISION
-------------------
Extracted surcharges do NOT overwrite LaneRecord.rate_per_unit.

They live here as a separate cost-adjustment layer that is applied when the
network is assembled. This keeps two numbers visible side by side:

    contracted rate   (the headline number on the rate card)
    effective rate    (what it actually costs once hidden clauses apply)

That distinction IS the business story: a vendor quoting Rs.10/kg with a
Rs.5/kg non-serviceable-location surcharge is more expensive than a vendor
quoting Rs.12/kg flat, for the locations that carry the surcharge.
Overwriting the base rate would hide exactly what we want to show.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SurchargeType(str, Enum):
    NSL = "NSL"                  # non-serviceable location
    FUEL = "FUEL"
    PEAK_SEASON = "PEAK_SEASON"
    HANDLING = "HANDLING"
    OTHER = "OTHER"


class ExtractionConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SurchargeRule(BaseModel):
    """One conditional cost rule pulled out of a contract."""
    surcharge_type: SurchargeType
    rate: float
    rate_unit: str = "INR/kg"

    # Which entities this applies to. Empty applies_to means it applies
    # to every lane under the parent contract (e.g. a blanket fuel surcharge).
    applies_to_location_ids: List[str] = Field(default_factory=list)
    applies_to_pin_codes: List[str] = Field(default_factory=list)

    confidence: ExtractionConfidence = ExtractionConfidence.MEDIUM
    source_excerpt: str = ""          # the clause text this came from — provenance
    source_page: Optional[int] = None

    def applies_to(self, location_id: str) -> bool:
        if not self.applies_to_location_ids and not self.applies_to_pin_codes:
            return True   # blanket surcharge
        return location_id in self.applies_to_location_ids


class ContractRule(BaseModel):
    """A parsed freight contract / rate card."""
    contract_id: str
    vendor_name: str

    base_rate: float
    rate_unit: str = "INR/kg"

    surcharges: List[SurchargeRule] = Field(default_factory=list)

    minimum_volume: Optional[float] = None
    minimum_volume_unit: Optional[str] = None
    penalty_clauses: List[str] = Field(default_factory=list)

    contract_start_date: Optional[str] = None
    contract_end_date: Optional[str] = None

    # Provenance — points back into the raw zone
    source_file_key: str = ""
    extracted_by: str = "stub"        # "stub" | "<provider>:<model>"
    extraction_confidence: ExtractionConfidence = ExtractionConfidence.MEDIUM

    def effective_rate_for(self, location_id: str) -> float:
        """Base rate plus every surcharge that applies at this location."""
        total = self.base_rate
        for s in self.surcharges:
            if s.applies_to(location_id):
                total += s.rate
        return total

    @property
    def has_hidden_cost(self) -> bool:
        """True if any surcharge applies to only SOME locations — the trap case."""
        return any(
            s.applies_to_location_ids or s.applies_to_pin_codes
            for s in self.surcharges
        )
