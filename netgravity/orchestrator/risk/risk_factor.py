"""
Orchestrator — Deterministic Risk Factor (RF) calculation.

    RF = P + REI − (P × REI)

P is an EXTERNAL likelihood (how probable is the event?). REI is NetGravity's
OWN deterministic exposure measure (how much does this facility's loss cost us,
relative to the worst case?). RF is their probabilistic OR: risk materialises
if the event happens or the network is exposed, with the product term removing
the double count.

Properties, all asserted by tests:
    RF(0, r) = r          no event risk → exposure alone
    RF(p, 0) = p          no exposure   → likelihood alone
    RF(1, r) = 1          certain event → maximal
    RF(p, 1) = 1          maximal exposure → maximal
    0 ≤ RF ≤ 1            closed on the unit square
    monotonic increasing in both arguments
    symmetric in P and REI

**A language model never computes this.** It is pure arithmetic over validated
inputs, with provenance recorded so any figure can be recomputed.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from netgravity.orchestrator.exceptions import ValidationFailureError
from netgravity.orchestrator.schemas.risk import (
    RFNotComputableReason,
    RFStatus,
    RiskAssessment,
    RiskFactorResult,
)

logger = logging.getLogger(__name__)

RF_FORMULA = "RF = P + REI - P*REI"

#: Values may drift a hair outside [0,1] through float arithmetic; clamp only
#: within this tolerance and reject anything genuinely out of range.
_EPS = 1e-9


def _coerce_finite(value: float, name: str) -> float:
    """Coerce to a finite float or raise."""
    try:
        v = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationFailureError(
            f"{name} must be a number, got {value!r}.",
            context={"field": name, "value": repr(value)},
        ) from exc

    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        raise ValidationFailureError(
            f"{name} must be finite, got {v!r}.", context={"field": name},
        )
    return v


def _validate_probability(value: float, name: str) -> float:
    """
    Coerce and range-check a genuine probability (P).

    Raises:
        ValidationFailureError: not finite, or outside [0, 1] beyond floating
            point tolerance. A likelihood of 1.4 is a defect upstream, and
            silently squashing it would hide that.
    """
    v = _coerce_finite(value, name)
    if v < -_EPS or v > 1.0 + _EPS:
        raise ValidationFailureError(
            f"{name} must lie in [0, 1], got {v}. Out-of-range values are rejected "
            f"rather than clamped, because they indicate an upstream defect.",
            context={"field": name, "value": v},
        )
    return min(1.0, max(0.0, v))


def _validate_rei(value: float, notes: List[str]) -> float:
    """
    Coerce REI into RF's domain.

    REI is normalised as PI_i / max_j(PI_j), so it is bounded ABOVE by 1.0 but
    is NOT bounded below: a facility whose disruption *reduces* business cost
    has a negative PI and therefore a negative REI. NetGravity deliberately
    retains that negative value in the resilience registry rather than clamping
    it, because it flags a result worth investigating.

    RF, however, is a combination of probabilities and is only defined on
    [0, 1]. A negative REI means the network has **no economic exposure** to
    losing that facility, so it floors at 0 here and RF reduces to the event
    likelihood alone (RF = P + 0 − 0 = P). That is the semantically correct
    reading, and the raw value is recorded in `notes` so nothing is hidden.

    A REI ABOVE 1.0 is still rejected: normalisation guarantees a maximum of
    1.0, so exceeding it is a genuine defect.
    """
    v = _coerce_finite(value, "rei (REI)")

    if v > 1.0 + _EPS:
        raise ValidationFailureError(
            f"rei (REI) must not exceed 1.0, got {v}. REI is normalised against the "
            f"largest performance impact, so a value above 1.0 indicates a defect.",
            context={"field": "rei", "value": v},
        )

    if v < -_EPS:
        notes.append(
            f"Raw REI was {v:.6f} (negative): disrupting this facility REDUCES business "
            f"network cost, so the network has no economic exposure to its loss. Floored "
            f"to 0 for the risk combination, which makes RF equal the event likelihood. "
            f"The raw value is retained in the resilience registry."
        )
        return 0.0

    return min(1.0, max(0.0, v))


def not_computable(
    reason: RFNotComputableReason,
    *,
    likelihood: Optional[float] = None,
    rei: Optional[float] = None,
    facility_id: Optional[str] = None,
    provenance: Optional[Dict[str, str]] = None,
    note: str = "",
) -> RiskFactorResult:
    """
    Build an explicit NOT_COMPUTABLE result.

    Used instead of fabricating a value when an input is genuinely absent.
    "We could not compute this, and here is why" is a materially different — and
    far more defensible — statement than a made-up number.
    """
    explanations = {
        RFNotComputableReason.NO_EVENT_PROBABILITY: (
            "No defensible event probability (P) was available. Severity, confidence "
            "and source quality are NOT probabilities and are never substituted for one."
        ),
        RFNotComputableReason.NO_REI: (
            "Network exposure (REI) was unavailable, so combined risk cannot be formed."
        ),
        RFNotComputableReason.NO_INPUTS: (
            "Neither an event probability nor a network exposure value was available."
        ),
        RFNotComputableReason.INVALID_INPUT: (
            "An input was present but outside its valid range."
        ),
        RFNotComputableReason.STALE_REI: (
            "The available REI was computed against a different network snapshot. "
            "Combining it with current data would mix incompatible network versions, "
            "so RF is withheld until REI is recalculated."
        ),
        RFNotComputableReason.NODE_MAPPING_UNAVAILABLE: (
            "The external event could not be mapped to an eligible network node, so "
            "there is no exposure value to combine the probability with. RF is never "
            "computed against an arbitrary node."
        ),
    }
    notes = [explanations[reason]]
    if note:
        notes.append(note)

    return RiskFactorResult(
        status=RFStatus.NOT_COMPUTABLE,
        not_computable_reason=reason,
        likelihood=likelihood,
        rei=rei,
        risk_factor=None,
        formula=RF_FORMULA,
        provenance=dict(provenance or {}),
        facility_id=facility_id,
        notes=notes,
    )


def compute_risk_factor(
    likelihood: Optional[float],
    rei: Optional[float],
    *,
    facility_id: Optional[str] = None,
    provenance: Optional[Dict[str, str]] = None,
    notes: Optional[List[str]] = None,
) -> RiskFactorResult:
    """
    Combine external likelihood with network exposure.

    Args:
        likelihood: P — probability the event occurs, in [0, 1].
        rei:        REI — network exposure, at most 1.0. A negative REI means no
                    exposure and floors to 0 (see `_validate_rei`).
        facility_id: Entity this applies to.
        provenance: Where each input came from. Recorded verbatim.
        notes:      Caveats to surface with the figure.

    Returns:
        RiskFactorResult. Status is NOT_COMPUTABLE — never a fabricated value —
        when either input is absent.

    Raises:
        ValidationFailureError: an input is PRESENT but invalid (non-numeric,
            non-finite, P outside [0, 1], or REI above 1.0). Absent inputs are
            reported as NOT_COMPUTABLE; invalid inputs are a defect and raise.
    """
    # Missing is not the same as invalid, and neither is the same as zero.
    if likelihood is None and rei is None:
        return not_computable(RFNotComputableReason.NO_INPUTS,
                              facility_id=facility_id, provenance=provenance)
    if likelihood is None:
        return not_computable(RFNotComputableReason.NO_EVENT_PROBABILITY,
                              rei=rei, facility_id=facility_id, provenance=provenance)
    if rei is None:
        return not_computable(RFNotComputableReason.NO_REI,
                              likelihood=likelihood, facility_id=facility_id,
                              provenance=provenance)

    collected_notes: List[str] = list(notes or [])
    p = _validate_probability(likelihood, "likelihood (P)")
    r = _validate_rei(rei, collected_notes)

    rf = p + r - (p * r)
    # Guard the boundary against float drift so RF is exactly closed on [0,1].
    rf = min(1.0, max(0.0, rf))

    result = RiskFactorResult(
        status=RFStatus.COMPUTED,
        likelihood=round(p, 6),
        rei=round(r, 6),
        risk_factor=round(rf, 6),
        formula=RF_FORMULA,
        provenance=dict(provenance or {}),
        facility_id=facility_id,
        notes=collected_notes,
    )
    logger.info(
        "orchestrator.risk.rf_calculated facility=%s p=%.6f rei=%.6f rf=%.6f",
        facility_id, p, r, rf,
    )
    return result


def assess_network_risk(
    rei_by_facility: Dict[str, Optional[float]],
    likelihood_by_facility: Dict[str, Optional[float]],
    *,
    default_likelihood: Optional[float] = None,
    network_id: Optional[str] = None,
    data_version: Optional[str] = None,
    rei_provenance: str = "netgravity_rei",
    likelihood_provenance: str = "external_signal",
) -> RiskAssessment:
    """
    Compute RF across every facility with both inputs available.

    Facilities missing either input are SKIPPED with a warning rather than
    defaulted to zero — a fabricated 0.0 would silently read as "no risk", which
    is a materially different claim from "not assessed".

    Args:
        rei_by_facility:        facility_id → REI (None when unavailable).
        likelihood_by_facility: facility_id → P (None when unavailable).
        default_likelihood:     Applied where no per-facility P exists. Only use
                                when a single event covers the whole network.

    Returns:
        RiskAssessment, ranked implicitly via `max_risk_factor`.
    """
    results: List[RiskFactorResult] = []
    uncomputable: List[RiskFactorResult] = []
    warnings: List[str] = []

    for facility_id in sorted(rei_by_facility):
        rei = rei_by_facility.get(facility_id)
        p = likelihood_by_facility.get(facility_id, default_likelihood)

        provenance = {
            "likelihood": likelihood_provenance,
            "rei": f"{rei_provenance}:{network_id or 'unknown'}@{data_version or 'unknown'}",
        }

        try:
            outcome = compute_risk_factor(
                likelihood=p, rei=rei, facility_id=facility_id, provenance=provenance,
            )
        except ValidationFailureError as exc:
            outcome = not_computable(
                RFNotComputableReason.INVALID_INPUT,
                likelihood=p if isinstance(p, (int, float)) else None,
                rei=rei if isinstance(rei, (int, float)) else None,
                facility_id=facility_id, provenance=provenance, note=exc.message,
            )

        if outcome.is_computed:
            results.append(outcome)
        else:
            uncomputable.append(outcome)
            reason = outcome.not_computable_reason
            warnings.append(
                f"{facility_id}: RF NOT_COMPUTABLE ({reason.value if reason else 'UNKNOWN'})"
            )

    max_rf: Optional[float] = None
    top: Optional[str] = None
    if results:
        best = max(results, key=lambda r: r.risk_factor or 0.0)
        max_rf = best.risk_factor
        top = best.facility_id

    results.sort(key=lambda r: (-(r.risk_factor or 0.0), r.facility_id or ""))
    uncomputable.sort(key=lambda r: r.facility_id or "")

    return RiskAssessment(
        results=results,
        not_computable=uncomputable,
        max_risk_factor=max_rf,
        highest_risk_entity=top,
        network_id=network_id,
        data_version=data_version,
        warnings=warnings,
    )
