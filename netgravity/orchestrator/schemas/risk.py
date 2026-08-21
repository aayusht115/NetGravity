"""
Orchestrator — Risk schemas.

RF combines an EXTERNAL likelihood with NetGravity's OWN exposure measure. The
two come from different worlds and must stay distinguishable: P is evidence
about the outside world, REI is a deterministic property of the network.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RFStatus(str, Enum):
    """Whether a combined risk factor could legitimately be produced."""
    COMPUTED       = "COMPUTED"
    NOT_COMPUTABLE = "NOT_COMPUTABLE"   # an input was missing; nothing fabricated


class RFNotComputableReason(str, Enum):
    """
    Why RF could not be produced. Always reported instead of a fabricated value.

    Each reason is actionable: it tells the reader what is missing, not merely
    that something is.
    """
    NO_EVENT_PROBABILITY      = "NO_EVENT_PROBABILITY"       # no defensible P available
    NO_REI                    = "NO_REI"                     # exposure not in the registry
    NO_INPUTS                 = "NO_INPUTS"                  # neither available
    INVALID_INPUT             = "INVALID_INPUT"              # present but out of range
    # The REI exists but was computed against a different network snapshot, so
    # combining it with current data would mix incompatible versions.
    STALE_REI                 = "STALE_REI"
    # The external event could not be tied to an eligible network node, so there
    # is no exposure to combine the probability with.
    NODE_MAPPING_UNAVAILABLE  = "NODE_MAPPING_UNAVAILABLE"


class RiskFactorResult(BaseModel):
    """
    Deterministic combination of event likelihood and network exposure.

        RF = P + REI − (P × REI)

    This is the probabilistic OR of two independent contributors: risk
    materialises if the event happens *or* the network is exposed to it, and
    the product term removes the double count. RF is bounded in [0, 1] whenever
    both inputs are.

    Never produced by a language model. `formula` and `provenance` are carried
    so any figure can be recomputed and audited.
    """
    status: RFStatus = RFStatus.COMPUTED
    # Populated only when status is NOT_COMPUTABLE.
    not_computable_reason: Optional[RFNotComputableReason] = None

    # Inputs. None when unavailable — never defaulted to 0, which would read as
    # "measured, and it was zero".
    likelihood: Optional[float] = None    # P — external event probability, [0, 1]
    rei: Optional[float] = None           # REI — network exposure, ≤ 1.0
    risk_factor: Optional[float] = None   # RF — combined, [0, 1]; None if not computable
    formula: str = "RF = P + REI - P*REI"

    # Where each input came from (e.g. {"likelihood": "external_signal:noaa",
    # "rei": "rei_registry:CASE16@762b4d27"}).
    provenance: Dict[str, str] = Field(default_factory=dict)
    facility_id: Optional[str] = None
    notes: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def is_computed(self) -> bool:
        return self.status == RFStatus.COMPUTED and self.risk_factor is not None


class RiskAssessment(BaseModel):
    """RF across every assessed entity for one run."""
    results: List[RiskFactorResult] = Field(default_factory=list)
    # Entities where RF could NOT be produced, with the reason. Kept alongside
    # the computed rows so "not assessed" is visible, not silently absent.
    not_computable: List[RiskFactorResult] = Field(default_factory=list)
    max_risk_factor: Optional[float] = None
    highest_risk_entity: Optional[str] = None
    # Snapshot the exposure inputs were computed from.
    network_id: Optional[str] = None
    data_version: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    def get(self, facility_id: str) -> Optional[RiskFactorResult]:
        for r in self.results:
            if r.facility_id == facility_id:
                return r
        return None


class ReasoningResult(BaseModel):
    """
    Narrative synthesis over deterministic outputs.

    The reasoning agent EXPLAINS; it does not compute. Every number it cites
    must already exist in the deterministic results it was given, and its
    output is validated before it reaches a caller.
    """
    summary: str = ""
    key_drivers: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    recommendation: str = ""
    confidence: str = "LOW"          # LOW | MEDIUM | HIGH
    evidence: List[str] = Field(default_factory=list)

    # "llm" when model-generated, "template" when the deterministic fallback
    # produced it. Callers can tell narrative provenance at a glance.
    source: str = "template"
    validation_warnings: List[str] = Field(default_factory=list)

    # --- Numeric grounding (Change 3) ---
    # GROUNDED | GROUNDING_FAILED | NO_CLAIMS | NOT_CHECKED
    grounding_status: str = "NOT_CHECKED"
    # Per-claim adjudication against authoritative deterministic values.
    grounded_claims: List[Dict[str, Any]] = Field(default_factory=list)
    # Evidence expected but missing, so the narrative can say so rather than
    # imply a zero. Keyed by capability.
    unavailable_evidence: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @property
    def is_grounded(self) -> bool:
        return self.grounding_status in ("GROUNDED", "NO_CLAIMS", "NOT_CHECKED")
