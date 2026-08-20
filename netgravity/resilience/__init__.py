"""
NetGravity — Resilience Analysis
=================================
Two complementary capabilities, both driven by the single authoritative MILP:

  engine.py  ResilienceEngine — standalone disruption scenarios
             (facility failure, lane failure, capacity loss, demand surge).

  rei.py     Facility Resilience Assessment & Risk Exposure Index (REI) —
             deterministic, cost-based relative exposure ranking across
             facilities, evaluated under one shared disruption assumption.

Neither module formulates an optimisation model. Both call
`netgravity.optimization.milp.solve`.
"""

from netgravity.resilience.engine import ResilienceEngine, compute_rerouted_volume
from netgravity.resilience.rei import (
    BaselineSolveError,
    FacilityNotFoundError,
    InvalidDisruptionTargetError,
    NoEligibleFacilitiesError,
    ResilienceAssessmentError,
    ResilienceBaseline,
    assess_facility_resilience,
    assess_network_resilience,
    classify_risk,
    compute_baseline,
    discover_eligible_facilities,
    normalize_rei,
)

__all__ = [
    "ResilienceEngine",
    "compute_rerouted_volume",
    "assess_facility_resilience",
    "assess_network_resilience",
    "compute_baseline",
    "discover_eligible_facilities",
    "normalize_rei",
    "classify_risk",
    "ResilienceBaseline",
    "ResilienceAssessmentError",
    "FacilityNotFoundError",
    "InvalidDisruptionTargetError",
    "NoEligibleFacilitiesError",
    "BaselineSolveError",
]
