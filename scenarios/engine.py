"""
NetGravity — Scenario Engine
==============================
Applies scenario overrides to a CanonicalNetwork and re-solves.

Design principle (Faculty guidance #25, #26):
  - Scenarios modify PARAMETERS, not the optimization model
  - The same optimizer is called every time
  - The base network is NEVER mutated
  - Scenarios are parameterized via Scenario schema objects

Supported scenario types:
  CLOSE_FACILITY        → force y_i = 0 (set capacity = 0, is_mandatory=False)
  OPEN_FACILITY         → add new facility or activate candidate
  CHANGE_CAPACITY       → override CAP_i
  CHANGE_DEMAND         → scale D_{mk}
  CHANGE_TRANSPORT_COST → scale c_{ijvk}
  LANE_DISRUPTION       → remove arc from A (set is_active=False)
  FACILITY_DISRUPTION   → set CAP_i ≈ 0 (disruption, no flow)
  CARBON_FACTOR_CHANGE  → update emission factors
  SERVICE_TARGET_CHANGE → update SLA_m
  CUSTOM                → apply ParameterOverride list
"""

from __future__ import annotations

import copy
from typing import Dict, List, Optional

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
)
from netgravity.schemas.scenario import (
    CostChange,
    DemandChange,
    FacilityChange,
    LaneChange,
    Scenario,
    ScenarioType,
)
from netgravity.schemas.results import OptimizationResult
from netgravity.optimization.milp import solve as milp_solve
from netgravity.metrics.kpis import compute_kpis, compute_flow_analytics, compare_scenarios


# ---------------------------------------------------------------------------
# Scenario engine
# ---------------------------------------------------------------------------

class ScenarioEngine:
    """
    Apply a Scenario to a CanonicalNetwork and solve.

    The engine:
    1. Deep-copies the base network
    2. Applies all overrides in order
    3. Calls the MILP solver
    4. Returns the result + comparison vs baseline
    """

    def run(
        self,
        base_network:    CanonicalNetwork,
        scenario:        Scenario,
        config:          Optional[OptimizationConfig] = None,
        baseline_result: Optional[OptimizationResult] = None,
    ) -> OptimizationResult:
        """
        Apply scenario to network and optimize.

        Args:
            base_network:    The canonical (immutable) base network
            scenario:        Scenario with overrides
            config:          Optional config override
            baseline_result: If provided, comparison is computed

        Returns:
            OptimizationResult with scenario_id set
        """
        if config is None:
            config = base_network.config

        # Apply config overrides
        if scenario.config_overrides:
            config = config.model_copy(update=scenario.config_overrides)

        # Build modified network
        modified = self._apply_overrides(base_network, scenario)

        # Solve
        result = milp_solve(
            network     = modified,
            config      = config,
            scenario_id = scenario.scenario_id,
        )

        # Attach KPIs
        result.kpis         = compute_kpis(result, modified)
        result.flow_analytics = compute_flow_analytics(result, modified)

        return result

    def run_library(
        self,
        base_network: CanonicalNetwork,
        scenarios:    List[Scenario],
        config:       Optional[OptimizationConfig] = None,
    ) -> Dict[str, OptimizationResult]:
        """
        Run multiple scenarios in sequence. Returns dict {scenario_id: result}.
        """
        results = {}
        for scenario in scenarios:
            results[scenario.scenario_id] = self.run(
                base_network = base_network,
                scenario     = scenario,
                config       = config,
            )
        return results

    # ------------------------------------------------------------------
    # Internal: Apply all overrides to a copy of the network
    # ------------------------------------------------------------------

    def _apply_overrides(
        self,
        network:  CanonicalNetwork,
        scenario: Scenario,
    ) -> CanonicalNetwork:
        """Apply all scenario overrides to a deep copy of the network."""

        facilities = [f.model_copy(deep=True) for f in network.facilities]
        demands    = [d.model_copy(deep=True) for d in network.demands]
        lanes      = [ln.model_copy(deep=True) for ln in network.lanes]

        fac_map  = {f.id: f for f in facilities}
        lane_map = {}
        for ln in lanes:
            key = (ln.origin_id, ln.destination_id, ln.mode.value
                   if hasattr(ln.mode, "value") else str(ln.mode))
            lane_map[key] = ln

        # --- Apply facility changes ---
        for fc in scenario.facility_changes:
            self._apply_facility_change(fc, fac_map, facilities)

        # --- Apply demand changes ---
        for dc in scenario.demand_changes:
            self._apply_demand_change(dc, demands)

        # --- Apply cost changes ---
        for cc in scenario.cost_changes:
            self._apply_cost_change(cc, lanes)

        # --- Apply lane changes ---
        for lc in scenario.lane_changes:
            self._apply_lane_change(lc, lanes, network)

        return network.model_copy(update={
            "facilities": facilities,
            "demands":    demands,
            "lanes":      lanes,
            "network_id": f"{network.network_id}_{scenario.scenario_id}",
        })

    def _apply_facility_change(
        self,
        fc:        FacilityChange,
        fac_map:   Dict[str, FacilityRecord],
        facilities: List[FacilityRecord],
    ) -> None:
        fac = fac_map.get(fc.facility_id)
        if fac is None:
            # Ignore unknown facility (warn in production; don't crash)
            return

        if fc.action == "CLOSE":
            fac.is_forced_closed = True
            fac.is_mandatory     = False
            fac.is_closable      = True
            fac.status           = FacilityStatus.CLOSED
            # Preserves original capacity parameter while forcing y_i = 0 via C5b constraint

        elif fc.action == "FORCE_OPEN":
            fac.is_mandatory = True
            fac.is_closable  = False

        elif fc.action == "OPEN":
            fac.is_mandatory = False
            fac.is_closable  = True
            if fac.status == FacilityStatus.CLOSED:
                fac.status = FacilityStatus.CANDIDATE

        elif fc.action == "SET_CAPACITY":
            if fc.capacity_override is not None:
                fac.capacity_units_per_period = fc.capacity_override
            if fc.capacity_multiplier is not None:
                fac.capacity_units_per_period *= fc.capacity_multiplier

        elif fc.action == "SET_FIXED_COST":
            if fc.fixed_cost_override is not None:
                fac.fixed_cost_per_year = fc.fixed_cost_override

    def _apply_demand_change(
        self,
        dc:      DemandChange,
        demands: List,
    ) -> None:
        for d in demands:
            if dc.market_id is not None and d.market_id != dc.market_id:
                continue
            if dc.product_id is not None and d.product_id != dc.product_id:
                continue
            if dc.period is not None and d.period != dc.period:
                continue

            if dc.quantity_override is not None:
                d.quantity = dc.quantity_override
            elif dc.quantity_multiplier is not None:
                d.quantity = max(0.0, d.quantity * dc.quantity_multiplier)

            if dc.std_dev_multiplier is not None:
                d.std_dev = max(0.0, d.std_dev * dc.std_dev_multiplier)

    def _apply_cost_change(
        self,
        cc:    CostChange,
        lanes: List[LaneRecord],
    ) -> None:
        for ln in lanes:
            if cc.origin_id is not None and ln.origin_id != cc.origin_id:
                continue
            if cc.destination_id is not None and ln.destination_id != cc.destination_id:
                continue
            if cc.mode is not None:
                ln_mode = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                if ln_mode != cc.mode:
                    continue

            if cc.rate_override is not None:
                ln.rate_per_unit = cc.rate_override
            elif cc.rate_multiplier is not None:
                ln.rate_per_unit = max(0.0, ln.rate_per_unit * cc.rate_multiplier)

    def _apply_lane_change(
        self,
        lc:      LaneChange,
        lanes:   List[LaneRecord],
        network: CanonicalNetwork,
    ) -> None:
        if lc.action == "REMOVE":
            lanes[:] = [
                ln for ln in lanes
                if not (ln.origin_id == lc.origin_id
                        and ln.destination_id == lc.destination_id
                        and (ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)) == lc.mode)
            ]

        elif lc.action == "ADD":
            # Build and append new lane
            from netgravity.schemas.network import TransportMode
            new_lane = LaneRecord(
                origin_id       = lc.origin_id,
                destination_id  = lc.destination_id,
                mode            = TransportMode(lc.mode),
                rate_per_unit   = lc.rate_per_unit or 0.0,
                distance_km     = lc.distance_km or 0.0,
                lead_time_days  = lc.lead_time_days or 1.0,
                lane_capacity   = lc.lane_capacity,
            )
            lanes.append(new_lane)

        elif lc.action == "MODIFY":
            for ln in lanes:
                ln_mode = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                if (ln.origin_id == lc.origin_id
                        and ln.destination_id == lc.destination_id
                        and ln_mode == lc.mode):
                    if lc.rate_per_unit   is not None: ln.rate_per_unit  = lc.rate_per_unit
                    if lc.distance_km     is not None: ln.distance_km    = lc.distance_km
                    if lc.lead_time_days  is not None: ln.lead_time_days = lc.lead_time_days
                    if lc.lane_capacity   is not None: ln.lane_capacity  = lc.lane_capacity
                    if lc.is_active       is not None: ln.is_active_baseline = lc.is_active
