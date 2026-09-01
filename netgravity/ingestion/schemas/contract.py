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

    #: Site commitments the same document states — a lease term, a take-or-pay,
    #: a minimum-term clause. Separate from the surcharges because they answer a
    #: different question: not what shipping costs, but whether a site may be
    #: closed. See `FacilityCommitment`.
    facility_commitments: List["FacilityCommitment"] = Field(default_factory=list)

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


class FacilityCommitment(BaseModel):
    """
    A contractual commitment to keep a SITE — a lease, a take-or-pay, a
    minimum-term agreement.

    Why this exists separately from `ContractRule`
    ---------------------------------------------
    `ContractRule` is about what shipping COSTS. This is about whether a site
    may be CLOSED, which is a different question with a different consequence:
    the first changes a number in the objective, the second changes whether a
    decision is legal.

    The MILP has enforced it since V1.4. Constraint C5c pins `y_i = 1` for a
    facility whose `contract_status` is ACTIVE and which does not permit early
    closure; validation check V-015 names the conflict when a scenario tries to
    close one anyway; the Digital Twin reports it and `metrics/contracts.py`
    puts it in the contract summary.

    And nothing had ever set those fields. No ingestion path, no API, no
    scenario override — `contract_status` defaulted to NONE on every facility of
    every network, so the constraint was structurally present and permanently
    inert. A planner could be shown a recommendation to close a site the client
    was contractually unable to close, and nothing in the system was in a
    position to object.

    Everything here is read from the document or left None. There is no default
    lock-in and no assumed notice period: a commitment nobody stated is not a
    commitment, and inventing one would block a closure the client is free to
    make.
    """

    #: The facility this commitment binds, as an id in the client's own network
    #: where the document names one recognisably.
    facility_id: str = ""
    #: What the document called it, kept even when it could not be matched to an
    #: id — an unmatched commitment must be reportable, not discarded.
    facility_label: str = ""

    #: Is the commitment in force for the modelled period?
    is_active: Optional[bool] = None
    #: May the site be closed before the term ends?
    allows_early_closure: Optional[bool] = None

    #: What it costs to exit early, in the document's own currency. Feeds
    #: `FacilityRecord.closure_cost`, which the objective already charges.
    early_exit_penalty: Optional[float] = None
    penalty_currency: str = ""
    notice_period_days: Optional[int] = None

    term_start_date: Optional[str] = None
    term_end_date: Optional[str] = None

    confidence: ExtractionConfidence = ExtractionConfidence.MEDIUM
    #: The clause this came from. A commitment that changes whether a site may
    #: be closed must be traceable to the sentence that says so.
    source_excerpt: str = ""
    source_page: Optional[int] = None

    @property
    def prohibits_closure(self) -> bool:
        """
        True only when the document says BOTH that a commitment is in force and
        that early exit is not permitted.

        `allows_early_closure=None` — the document did not say — is deliberately
        NOT treated as a prohibition. An unstated term must not pin a facility
        open; it must be reported as unstated so somebody reads the contract.
        """
        return bool(self.is_active) and self.allows_early_closure is False

    @property
    def is_stated_enough_to_apply(self) -> bool:
        """Whether this commitment says anything a model can act on."""
        return bool(self.facility_id) and (
            self.is_active is not None
            or self.allows_early_closure is not None
            or self.early_exit_penalty is not None
        )
