"""
NetGravity — Infeasibility Diagnostics V1.1
=============================================
Pre-solve and post-solve diagnostic checks that explain WHY the model
is infeasible when the solver returns INFEASIBLE.

Purpose:
    When the optimizer returns INFEASIBLE, this module provides structured
    diagnostic information to help the analyst:
      1. Identify which markets have no supply arcs
      2. Estimate whether total capacity is sufficient for total demand
      3. Identify SLA-blocked markets (no arc meets service requirement)
      4. Identify markets blocked by forced-close facility constraints
      5. Estimate the minimum infeasibility gap

This module does NOT modify the network or attempt to repair it.
Principle: "Fail loudly with explanation."

Usage:
    from netgravity.diagnostics.infeasibility import diagnose_infeasibility
    diag = diagnose_infeasibility(network, config)
    if diag.has_issues:
        diag.print_report()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    NodeRole,
    OptimizationConfig,
    ServiceMetric,
)


# ---------------------------------------------------------------------------
# Diagnostic data classes
# ---------------------------------------------------------------------------

@dataclass
class InfeasibilityDiagnostic:
    """
    Structured explanation of model infeasibility.

    All fields are computed from the network data BEFORE solving.
    Used to provide actionable feedback when the solver returns INFEASIBLE.
    """
    # Was the pre-solve check able to find issues?
    has_issues: bool

    # Markets with no inbound arcs at all
    # Cause: No lane from any facility to this market exists
    # Fix: Add at least one transportation lane to this market
    markets_with_no_arcs: List[str] = field(default_factory=list)

    # Markets with arcs but none within SLA
    # Cause: All lanes to this market exceed d.sla_days
    # Fix: Add faster lanes, relax SLA, or disable SLA enforcement
    markets_with_no_sla_arcs: List[str] = field(default_factory=list)

    # Markets blocked by forced-close cascades
    # Cause: The only facilities serving this market are all forced-closed
    # Fix: Keep at least one facility open that can serve this market
    markets_blocked_by_forced_close: List[str] = field(default_factory=list)

    # Total capacity vs total demand
    # Negative gap means total capacity < total demand
    # Note: This is a necessary but NOT sufficient condition for feasibility
    capacity_gap: float = 0.0          # total_capacity - total_demand
    total_demand:  float = 0.0
    total_capacity: float = 0.0

    # Specific market-level demand coverage estimates
    # market_id -> max_supply_available (sum of open facility capacities reaching it)
    market_supply_coverage: Dict[str, float] = field(default_factory=dict)

    # Narrative summary (human-readable)
    summary: List[str] = field(default_factory=list)

    def print_report(self) -> None:
        """Print a formatted diagnostic report to stdout."""
        print(f"\n{'='*65}")
        print(f"INFEASIBILITY DIAGNOSTIC REPORT")
        print(f"{'='*65}")

        if not self.has_issues:
            print("  ✓ No obvious pre-solve infeasibility causes detected.")
            print("  → The model structure appears correct. Check solver logs.")
            return

        if self.markets_with_no_arcs:
            print(f"\n  ✗ MARKETS WITH NO INBOUND ARCS ({len(self.markets_with_no_arcs)}):")
            for m in self.markets_with_no_arcs:
                print(f"      - {m}")
            print("    Fix: Add at least one transportation lane to each market.")

        if self.markets_with_no_sla_arcs:
            print(f"\n  ✗ MARKETS BLOCKED BY SLA ({len(self.markets_with_no_sla_arcs)}):")
            for m in self.markets_with_no_sla_arcs:
                print(f"      - {m}")
            print("    Fix: Add faster lanes, extend SLA requirement, or set enforce_sla=False.")

        if self.markets_blocked_by_forced_close:
            print(f"\n  ✗ MARKETS BLOCKED BY FORCED CLOSURE ({len(self.markets_blocked_by_forced_close)}):")
            for m in self.markets_blocked_by_forced_close:
                print(f"      - {m}")
            print("    Fix: Keep at least one facility that serves each market open.")

        if self.capacity_gap < 0:
            print(f"\n  ✗ INSUFFICIENT TOTAL CAPACITY:")
            print(f"      Total demand:   {self.total_demand:,.0f} units/period")
            print(f"      Total capacity: {self.total_capacity:,.0f} units/period")
            print(f"      Shortfall:      {-self.capacity_gap:,.0f} units/period")
            print("    Fix: Expand facility capacity or enable allow_shortage.")

        print(f"\n  NARRATIVE:")
        for line in self.summary:
            print(f"    → {line}")

        print(f"{'='*65}\n")


# ---------------------------------------------------------------------------
# Diagnostic function
# ---------------------------------------------------------------------------

def diagnose_infeasibility(
    network: CanonicalNetwork,
    config:  Optional[OptimizationConfig] = None,
) -> InfeasibilityDiagnostic:
    """
    Run pre-solve infeasibility checks on a CanonicalNetwork.

    This should be called when the optimizer returns INFEASIBLE to
    provide actionable feedback.

    Args:
        network: The CanonicalNetwork that was passed to the optimizer
        config:  OptimizationConfig (defaults to network.config)

    Returns:
        InfeasibilityDiagnostic with structured findings
    """
    if config is None:
        config = network.config

    market_roles    = {NodeRole.MARKET, NodeRole.CUSTOMER}
    markets:        List[FacilityRecord] = [f for f in network.facilities if f.role in market_roles]
    non_market_facs = [f for f in network.facilities if f.role not in market_roles]
    facility_map    = {f.id: f for f in network.facilities}

    diag = InfeasibilityDiagnostic(has_issues=False)
    issues_found = False

    # ── Check 1: Markets with no inbound arcs ─────────────────────────────
    market_ids: Set[str] = {m.id for m in markets}
    inbound_origins: Dict[str, Set[str]] = {m.id: set() for m in markets}

    for ln in network.lanes:
        if ln.destination_id in market_ids and ln.is_active_baseline:
            inbound_origins[ln.destination_id].add(ln.origin_id)

    for market in markets:
        if not inbound_origins[market.id]:
            diag.markets_with_no_arcs.append(market.id)
            issues_found = True

    # ── Check 2: Markets blocked by SLA ───────────────────────────────────
    if config.enforce_sla and config.service_metric == ServiceMetric.TRANSIT_TIME:
        demand_sla: Dict[str, Optional[float]] = {}
        for d in network.demands:
            if d.sla_days is not None:
                # Use the strictest (tightest) SLA for each market
                existing = demand_sla.get(d.market_id)
                if existing is None or d.sla_days < existing:
                    demand_sla[d.market_id] = d.sla_days

        for market_id, sla in demand_sla.items():
            if market_id in diag.markets_with_no_arcs:
                continue  # Already flagged
            eligible = [
                ln for ln in network.lanes
                if ln.destination_id == market_id
                and ln.is_active_baseline
                and (sla is None or ln.lead_time_days <= sla)
            ]
            if not eligible:
                diag.markets_with_no_sla_arcs.append(market_id)
                issues_found = True

    # ── Check 3: Markets blocked by forced-close cascade ──────────────────
    forced_closed_ids: Set[str] = {
        f.id for f in non_market_facs if f.is_forced_closed
    }
    # Also consider capacity=0 facilities as effectively closed (backward compat)
    capacity_zero_ids: Set[str] = {
        f.id for f in non_market_facs
        if f.capacity_units_per_period <= 0 and not f.is_mandatory
    }
    effectively_closed = forced_closed_ids | capacity_zero_ids

    if effectively_closed:
        for market in markets:
            if market.id in diag.markets_with_no_arcs:
                continue
            # Find all arcs reaching this market from non-closed facilities
            viable_origins = [
                ln.origin_id for ln in network.lanes
                if ln.destination_id == market.id
                and ln.is_active_baseline
                and ln.origin_id not in effectively_closed
            ]
            if not viable_origins:
                # All origins are forced closed
                diag.markets_blocked_by_forced_close.append(market.id)
                issues_found = True

    # ── Check 4: Total capacity vs total demand ────────────────────────────
    total_demand = sum(d.quantity for d in network.demands)
    total_capacity = sum(
        f.capacity_units_per_period
        for f in non_market_facs
        if f.capacity_units_per_period < 1e11
        and not f.is_forced_closed
        and f.capacity_units_per_period > 0
    )
    capacity_gap = total_capacity - total_demand

    diag.total_demand   = total_demand
    diag.total_capacity = total_capacity
    diag.capacity_gap   = capacity_gap

    if capacity_gap < 0 and not config.allow_shortage:
        issues_found = True

    # ── Check 5: Per-market supply coverage ───────────────────────────────
    for market in markets:
        reachable_origin_ids = {
            ln.origin_id for ln in network.lanes
            if ln.destination_id == market.id and ln.is_active_baseline
            and ln.origin_id not in effectively_closed
        }
        max_supply = sum(
            min(f.capacity_units_per_period, f.production_capacity_units_per_period)
            for f in non_market_facs
            if f.id in reachable_origin_ids
            and f.capacity_units_per_period < 1e11
        )
        diag.market_supply_coverage[market.id] = max_supply

    # ── Build narrative summary ───────────────────────────────────────────
    summary = []
    if diag.markets_with_no_arcs:
        summary.append(
            f"{len(diag.markets_with_no_arcs)} market(s) have no inbound transportation lanes. "
            f"These markets can never be served."
        )
    if diag.markets_with_no_sla_arcs:
        summary.append(
            f"{len(diag.markets_with_no_sla_arcs)} market(s) have inbound lanes but none "
            f"meet the SLA requirement. Set enforce_sla=False or add faster lanes."
        )
    if diag.markets_blocked_by_forced_close:
        summary.append(
            f"{len(diag.markets_blocked_by_forced_close)} market(s) are unreachable because "
            f"all supplying facilities are forced-closed or have zero capacity."
        )
    if capacity_gap < 0 and not config.allow_shortage:
        summary.append(
            f"Total capacity ({total_capacity:,.0f}) is less than total demand "
            f"({total_demand:,.0f}). Enable allow_shortage or increase capacity."
        )
    if not issues_found:
        summary.append(
            "No obvious structural causes detected. Infeasibility may be due to "
            "combined constraints (e.g., simultaneous SLA + capacity + sourcing)."
        )

    diag.summary = summary
    diag.has_issues = issues_found

    return diag
