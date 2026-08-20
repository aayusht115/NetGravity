"""
NetGravity — Optimization Mode Policies
========================================
Version: 1.4.0

Centralises every mode-specific decision-variable fixing and economic-term
selection in ONE place.

Design rule
───────────
A mode NEVER changes the mathematics. There is exactly one MILP formulation
(`optimization/milp.py`); a mode only:

  1. fixes facility open/close decision variables (via facility flags), and
  2. selects which lanes are available, and
  3. declares which economic terms apply (closure cost, contracts).

The MILP reads these declarations; it does not branch on the mode itself. That
keeps the formulation single-sourced and makes mode behaviour auditable from one
table (`MODE_POLICIES` below).

Backward compatibility
──────────────────────
BROWNFIELD_SCENARIO_OPTIMIZATION is the default and is a strict NO-OP: it
honours facility flags exactly as supplied and leaves every lane available,
reproducing NetGravity's pre-V1.4 behaviour byte-for-byte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    NodeRole,
    OptimizationMode,
)

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}
SUPPLY_ROLES = {NodeRole.PLANT, NodeRole.SUPPLIER}


@dataclass(frozen=True)
class ModePolicy:
    """
    The complete, declarative description of one optimization mode.

    Frozen so a mode's semantics cannot be mutated at runtime.
    """
    mode: OptimizationMode

    # --- Decision-variable fixing ---
    # Pin every EXISTING non-market facility open (y_i = 1) and make it
    # non-closable, so the footprint cannot change.
    pin_existing_open: bool
    # Remove CANDIDATE facilities from consideration (capacity → 0, y_i → 0).
    exclude_candidates: bool
    # Release the existing footprint: clear mandatory/forced-closed pins on
    # non-supply facilities so the optimizer may choose the footprint freely.
    release_existing_footprint: bool

    # --- Lane availability ---
    # Restrict arcs to lanes flagged `is_active_baseline`, i.e. the observed
    # lane set rather than every technically possible lane.
    restrict_to_baseline_lanes: bool

    # --- Economic terms ---
    # Charge closure_cost when an EXISTING facility transitions open → closed.
    apply_closure_cost: bool
    # Enforce contractual must-remain-open commitments (constraint C5c).
    enforce_contracts: bool

    # --- Result semantics ---
    # True when the result describes a hypothetical network rather than the
    # observed one. Consumed by the deterministic result contract so an
    # optimized state can never be mistaken for observed state.
    is_hypothetical: bool

    description: str

    @property
    def locks_facility_decisions(self) -> bool:
        """True when open/close decisions are fixed rather than optimized."""
        return self.pin_existing_open


# ---------------------------------------------------------------------------
# The mode table — single source of truth for mode behaviour
# ---------------------------------------------------------------------------

MODE_POLICIES: Dict[OptimizationMode, ModePolicy] = {

    OptimizationMode.ACTUAL_AS_IS_EVALUATION: ModePolicy(
        mode                       = OptimizationMode.ACTUAL_AS_IS_EVALUATION,
        pin_existing_open          = True,
        exclude_candidates         = True,
        release_existing_footprint = False,
        restrict_to_baseline_lanes = True,
        # Nothing is being decided, so nothing is being closed.
        apply_closure_cost         = False,
        # Redundant (everything is pinned open) but harmless and consistent.
        enforce_contracts          = True,
        is_hypothetical            = False,
        description = (
            "Observed network evaluated as-is: existing footprint pinned open, "
            "candidates excluded, only baseline-active lanes available. No "
            "network redesign. NOTE: V1 has no observed-flow input, so the "
            "allocation within this fixed footprint and lane set is cost-minimal "
            "rather than a replay of recorded shipment volumes."
        ),
    ),

    OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION: ModePolicy(
        mode                       = OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION,
        pin_existing_open          = True,
        exclude_candidates         = True,
        release_existing_footprint = False,
        # Every lane available — this is what distinguishes it from as-is.
        restrict_to_baseline_lanes = False,
        # Footprint is locked, so no closure decision can occur.
        apply_closure_cost         = False,
        enforce_contracts          = True,
        is_hypothetical            = True,
        description = (
            "Routing, allocation and sourcing optimized while the facility "
            "footprint stays fixed: existing facilities remain open, candidates "
            "excluded, open/close decisions locked, all lanes available."
        ),
    ),

    OptimizationMode.GREENFIELD_OPTIMIZATION: ModePolicy(
        mode                       = OptimizationMode.GREENFIELD_OPTIMIZATION,
        pin_existing_open          = False,
        exclude_candidates         = False,
        release_existing_footprint = True,
        restrict_to_baseline_lanes = False,
        # A facility absent from a greenfield design was never "closed" —
        # charging a closure cost would price a decision nobody made.
        apply_closure_cost         = False,
        # Greenfield deliberately ignores the incumbent footprint's obligations.
        enforce_contracts          = False,
        is_hypothetical            = True,
        description = (
            "Footprint optimized from candidate locations with the existing "
            "footprint released. Candidate-location optimization only — not "
            "arbitrary continuous geographic siting. No closure economics."
        ),
    ),

    OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION: ModePolicy(
        mode                       = OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION,
        # NO-OP transform: facility flags are honoured exactly as supplied.
        pin_existing_open          = False,
        exclude_candidates         = False,
        release_existing_footprint = False,
        restrict_to_baseline_lanes = False,
        apply_closure_cost         = True,
        enforce_contracts          = True,
        is_hypothetical            = True,
        description = (
            "Existing network optimized under explicit scenario overrides. "
            "Facility flags honoured as supplied; closure economics and "
            "contractual constraints both apply."
        ),
    ),

    OptimizationMode.DISRUPTION_RESILIENCE_OPTIMIZATION: ModePolicy(
        mode                       = OptimizationMode.DISRUPTION_RESILIENCE_OPTIMIZATION,
        pin_existing_open          = False,
        exclude_candidates         = False,
        release_existing_footprint = False,
        restrict_to_baseline_lanes = False,
        # Applies to voluntary closures the re-optimization chooses; facilities
        # flagged `is_disruption_target` are exempt at the facility level.
        apply_closure_cost         = True,
        enforce_contracts          = True,
        is_hypothetical            = True,
        description = (
            "Network re-optimized after an explicit disruption override. "
            "Disruption targets are exempt from closure cost and from "
            "contractual must-remain-open constraints: an involuntary outage is "
            "not a voluntary closure decision."
        ),
    ),
}


def get_mode_policy(mode: OptimizationMode | str) -> ModePolicy:
    """
    Resolve a mode to its policy.

    Raises:
        ValueError: for an unknown mode, naming the valid ones.
    """
    if isinstance(mode, str):
        try:
            mode = OptimizationMode(mode)
        except ValueError as exc:
            valid = ", ".join(m.value for m in OptimizationMode)
            raise ValueError(
                f"Unknown optimization mode '{mode}'. Valid modes: {valid}"
            ) from exc

    policy = MODE_POLICIES.get(mode)
    if policy is None:
        valid = ", ".join(m.value for m in OptimizationMode)
        raise ValueError(
            f"No policy registered for optimization mode '{mode}'. Valid modes: {valid}"
        )
    return policy


# ---------------------------------------------------------------------------
# Network preparation
# ---------------------------------------------------------------------------

def prepare_network_for_mode(
    network: CanonicalNetwork,
    mode:    OptimizationMode | str,
) -> CanonicalNetwork:
    """
    Return a COPY of the network with decision variables fixed for the mode.

    Never mutates the input — observed baseline state must survive every
    hypothetical evaluation intact.

    For BROWNFIELD_SCENARIO_OPTIMIZATION (the default) this is a no-op and the
    original network object is returned unchanged.

    Args:
        network: The canonical network.
        mode:    Optimization mode or its string value.

    Returns:
        A mode-prepared CanonicalNetwork.
    """
    policy = get_mode_policy(mode)

    needs_facility_work = (
        policy.pin_existing_open
        or policy.exclude_candidates
        or policy.release_existing_footprint
    )
    if not needs_facility_work and not policy.restrict_to_baseline_lanes:
        # Pure no-op mode — hand the network straight back.
        return network

    facilities: List[FacilityRecord] = []
    for fac in network.facilities:
        if fac.role in MARKET_ROLES:
            facilities.append(fac)
            continue

        updates: Dict[str, object] = {}

        if policy.pin_existing_open and fac.effective_baseline_status == FacilityStatus.EXISTING:
            # Footprint locked open. A disruption target is never re-opened by a
            # mode policy — the outage is the whole point of the run.
            if not fac.is_disruption_target:
                updates["is_mandatory"]     = True
                updates["is_closable"]      = False
                updates["is_forced_closed"] = False

        if policy.exclude_candidates and fac.effective_baseline_status == FacilityStatus.CANDIDATE:
            # Not part of the observed footprint: remove it from consideration.
            updates["capacity_units_per_period"]            = 0.0
            updates["production_capacity_units_per_period"] = 0.0
            updates["min_throughput_per_period"]            = 0.0
            updates["is_mandatory"]                         = False
            updates["is_closable"]                          = True

        if policy.release_existing_footprint and fac.role not in SUPPLY_ROLES:
            # Greenfield: the optimizer chooses the footprint. Supply nodes keep
            # their flags — a plant is a source of product, not a siting choice.
            if not fac.is_disruption_target:
                updates["is_mandatory"]     = False
                updates["is_closable"]      = True
                updates["is_forced_closed"] = False

        facilities.append(fac.model_copy(update=updates) if updates else fac)

    updates_net: Dict[str, object] = {"facilities": facilities}

    if policy.restrict_to_baseline_lanes:
        active = [ln for ln in network.lanes if ln.is_active_baseline]
        dropped = len(network.lanes) - len(active)
        if dropped:
            logger.info(
                "modes.lanes_restricted mode=%s dropped=%d retained=%d",
                policy.mode.value, dropped, len(active),
            )
        updates_net["lanes"] = active

    logger.info(
        "modes.network_prepared mode=%s pin_existing=%s exclude_candidates=%s "
        "release_footprint=%s baseline_lanes_only=%s closure_cost=%s contracts=%s",
        policy.mode.value, policy.pin_existing_open, policy.exclude_candidates,
        policy.release_existing_footprint, policy.restrict_to_baseline_lanes,
        policy.apply_closure_cost, policy.enforce_contracts,
    )

    return network.model_copy(update=updates_net)
