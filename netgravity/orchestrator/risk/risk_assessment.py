"""
NetGravity — Phase 1 deterministic risk assessment.

Ties the three mathematical components together WITHOUT duplicating any of
their mathematics:

    Network Snapshot → Baseline MILP → REI Batch → REI Registry
                                                        ↓
                          External Event Probability P  ↓
                                                   ↘    ↓
                                                  RF Calculator
                                                        ↓
                                                  Risk Assessment

Responsibilities kept strictly separate:

    MILP   decides the optimal network and its cost   (optimization/milp.py)
    REI    measures economic exposure to node failure (resilience/rei.py)
    RF     combines P with REI                        (risk/risk_factor.py)
    HERE   maps an event to a node, looks up that node's REI, validates the
           snapshot, and asks the RF calculator for an answer

This module contains no optimization, no REI arithmetic and no RF arithmetic.
It resolves *which* numbers are eligible to be combined, and refuses clearly
when they are not.

Two refusals matter most:

  * An event that cannot be tied to an eligible node yields
    NODE_MAPPING_UNAVAILABLE. RF is never computed against an arbitrary node.
  * A REI computed on a different network snapshot yields STALE_REI. Mixing
    exposure from snapshot V17 with a network at V18 would be a silent
    correctness failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from netgravity.orchestrator.risk.risk_factor import compute_risk_factor, not_computable
from netgravity.orchestrator.schemas.requests import ExternalSignal
from netgravity.orchestrator.schemas.risk import (
    RFNotComputableReason,
    RFStatus,
    RiskAssessment,
    RiskFactorResult,
)
from netgravity.schemas.results import (
    CalculationStatus,
    FacilityResilienceRegistry,
    FacilityResilienceResult,
)

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Node mapping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeMapping:
    """
    Resolution of an external event onto network nodes.

    `resolved` are node ids that genuinely exist in the REI registry.
    `unresolved` are identifiers the event named that do NOT — recorded rather
    than dropped, so a misspelled or unknown location is visible instead of
    silently producing no risk row.
    """
    resolved: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    method: str = "explicit"     # "explicit" | "location_match" | "none"

    @property
    def has_mapping(self) -> bool:
        return bool(self.resolved)


def map_event_to_nodes(
    signal: ExternalSignal,
    registry: FacilityResilienceRegistry,
) -> NodeMapping:
    """
    Resolve which assessed nodes an external event affects.

    Resolution is EXPLICIT by preference: `affected_entity_ids` naming real
    assessed nodes. Only when the event names nothing does it fall back to
    matching `location` against a node id, and even then the match must be
    exact-or-token — never a guess.

    An event that resolves to nothing yields an empty mapping, which the caller
    turns into NODE_MAPPING_UNAVAILABLE. It must never silently broadcast
    across the whole network: a flood in Delhi says nothing about Mumbai's
    probability.
    """
    assessed = {r.facility_id for r in registry.results}

    if signal.affected_entity_ids:
        resolved = [fid for fid in signal.affected_entity_ids if fid in assessed]
        unresolved = [fid for fid in signal.affected_entity_ids if fid not in assessed]
        if resolved:
            return NodeMapping(resolved=sorted(resolved), unresolved=sorted(unresolved),
                               method="explicit")
        return NodeMapping(resolved=[], unresolved=sorted(unresolved), method="explicit")

    # No explicit entity: try the location string against node identities.
    location = (signal.location or "").strip()
    if location:
        exact = [fid for fid in assessed if fid.lower() == location.lower()]
        if exact:
            return NodeMapping(resolved=sorted(exact), method="location_match")
        contained = [fid for fid in assessed if location.lower() in fid.lower()]
        if contained:
            return NodeMapping(resolved=sorted(contained), method="location_match")
        return NodeMapping(resolved=[], unresolved=[location], method="location_match")

    return NodeMapping(resolved=[], unresolved=[], method="none")


# ---------------------------------------------------------------------------
# REI lookup with snapshot validation
# ---------------------------------------------------------------------------

@dataclass
class REILookupResult:
    """Outcome of resolving one node's exposure from the registry."""
    node_id: str
    rei: Optional[float] = None
    row: Optional[FacilityResilienceResult] = None
    unavailable_reason: Optional[RFNotComputableReason] = None
    detail: str = ""

    @property
    def is_usable(self) -> bool:
        return self.rei is not None and self.unavailable_reason is None


def lookup_rei(
    node_id: str,
    registry: FacilityResilienceRegistry,
    *,
    expected_snapshot_id: Optional[str] = None,
) -> REILookupResult:
    """
    Fetch a node's REI, refusing anything that is not safe to combine.

    Refuses when:
      * the node is not in the registry            → NO_REI
      * the row exists but carries no REI          → NO_REI
        (infeasible, time-limited or errored — all of which deliberately
         produce `rei = None` rather than a fabricated value)
      * the registry was built on a different snapshot → STALE_REI
    """
    if expected_snapshot_id is not None and registry.network_snapshot_id is not None:
        if registry.network_snapshot_id != expected_snapshot_id:
            return REILookupResult(
                node_id=node_id,
                unavailable_reason=RFNotComputableReason.STALE_REI,
                detail=(
                    f"REI registry was computed against snapshot "
                    f"'{registry.network_snapshot_id}' but the current snapshot is "
                    f"'{expected_snapshot_id}'. Recalculate REI before combining."
                ),
            )

    row = registry.get(node_id)
    if row is None:
        return REILookupResult(
            node_id=node_id,
            unavailable_reason=RFNotComputableReason.NO_REI,
            detail=f"'{node_id}' was not assessed in batch '{registry.batch_id}'.",
        )

    if row.rei is None:
        return REILookupResult(
            node_id=node_id, row=row,
            unavailable_reason=RFNotComputableReason.NO_REI,
            detail=(
                f"'{node_id}' has no REI: calculation_status="
                f"{row.calculation_status.value}, solver_status="
                f"{row.solver_status.value}."
                + (f" {row.failure_reason}" if row.failure_reason else "")
            ),
        )

    return REILookupResult(node_id=node_id, rei=row.rei, row=row)


# ---------------------------------------------------------------------------
# Event risk assessment
# ---------------------------------------------------------------------------

def assess_event_risk(
    signal: ExternalSignal,
    registry: FacilityResilienceRegistry,
    *,
    expected_snapshot_id: Optional[str] = None,
) -> RiskAssessment:
    """
    Combine an external event with network exposure into RF, per node.

    Args:
        signal:               External evidence. Supplies P, and only P — never
                              severity or confidence in its place.
        registry:             The REI batch to look exposure up in.
        expected_snapshot_id: When given, the registry must match it or every
                              result is STALE_REI.

    Returns:
        RiskAssessment. Computable nodes appear in `results`; everything else
        appears in `not_computable` with an explicit reason. Nothing is ever
        defaulted to zero, and no node is assessed that the event did not
        actually map to.
    """
    computed: List[RiskFactorResult] = []
    uncomputable: List[RiskFactorResult] = []
    warnings: List[str] = []

    probability = signal.event_probability
    provenance_base = {
        "likelihood": (
            f"external_signal:{signal.source}"
            f"|basis={signal.probability_basis or 'none'}"
        ),
        "rei": (
            f"rei_registry:{registry.batch_id or 'unknown'}"
            f"@{registry.network_snapshot_id or registry.data_version or 'unknown'}"
        ),
    }

    mapping = map_event_to_nodes(signal, registry)

    logger.info(
        "risk.assess_event.started event=%s source=%s p=%s mapped=%s unresolved=%s "
        "batch_id=%s snapshot=%s",
        signal.event_type, signal.source, probability,
        mapping.resolved, mapping.unresolved,
        registry.batch_id, registry.network_snapshot_id,
    )

    # ---- No usable node mapping ------------------------------------------
    if not mapping.has_mapping:
        named = mapping.unresolved or [signal.location or "(nothing named)"]
        detail = (
            f"Event '{signal.event_type}' named {named} but none correspond to a node "
            f"assessed in REI batch '{registry.batch_id}'. RF is not computed against "
            f"an arbitrary node."
        )
        warnings.append(detail)
        uncomputable.append(not_computable(
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE,
            likelihood=probability,
            facility_id=None,
            provenance=provenance_base,
            note=detail,
        ))
        logger.warning("risk.assess_event.no_node_mapping event=%s named=%s",
                       signal.event_type, named)
        return _finalise(computed, uncomputable, registry, warnings)

    if mapping.unresolved:
        warnings.append(
            f"Event named {mapping.unresolved}, which are not assessed nodes; they "
            f"were ignored. Mapped nodes: {mapping.resolved}."
        )

    # ---- Per-node assessment ---------------------------------------------
    for node_id in mapping.resolved:
        lookup = lookup_rei(node_id, registry, expected_snapshot_id=expected_snapshot_id)

        if not lookup.is_usable:
            uncomputable.append(not_computable(
                lookup.unavailable_reason or RFNotComputableReason.NO_REI,
                likelihood=probability,
                facility_id=node_id,
                provenance=provenance_base,
                note=lookup.detail,
            ))
            warnings.append(f"{node_id}: {lookup.detail}")
            logger.warning(
                "risk.assess_event.rei_unusable node=%s reason=%s",
                node_id, (lookup.unavailable_reason or RFNotComputableReason.NO_REI).value,
            )
            continue

        # `compute_risk_factor` returns NOT_COMPUTABLE (never a fabricated
        # value) when P is missing, so the missing-probability case is handled
        # by the calculator rather than duplicated here.
        result = compute_risk_factor(
            likelihood=probability,
            rei=lookup.rei,
            facility_id=node_id,
            provenance=provenance_base,
        )
        if result.is_computed:
            computed.append(result)
        else:
            uncomputable.append(result)
            warnings.append(
                f"{node_id}: RF NOT_COMPUTABLE "
                f"({result.not_computable_reason.value if result.not_computable_reason else 'UNKNOWN'})"
            )

    return _finalise(computed, uncomputable, registry, warnings)


def _finalise(
    computed: List[RiskFactorResult],
    uncomputable: List[RiskFactorResult],
    registry: FacilityResilienceRegistry,
    warnings: List[str],
) -> RiskAssessment:
    """Rank computable results and assemble the assessment."""
    max_rf: Optional[float] = None
    top: Optional[str] = None
    if computed:
        best = max(computed, key=lambda r: r.risk_factor or 0.0)
        max_rf = best.risk_factor
        top = best.facility_id

    computed.sort(key=lambda r: (-(r.risk_factor or 0.0), r.facility_id or ""))
    uncomputable.sort(key=lambda r: r.facility_id or "")

    logger.info(
        "risk.assess_event.completed computed=%d not_computable=%d max_rf=%s top=%s",
        len(computed), len(uncomputable),
        f"{max_rf:.6f}" if max_rf is not None else "None", top,
    )

    return RiskAssessment(
        results=computed,
        not_computable=uncomputable,
        max_risk_factor=max_rf,
        highest_risk_entity=top,
        network_id=registry.network_id,
        data_version=registry.data_version,
        warnings=warnings,
    )
