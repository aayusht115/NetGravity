from __future__ import annotations

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
  CLOSE_FACILITY        -> force y_i = 0 (set capacity = 0, is_mandatory=False)
  OPEN_FACILITY         -> add new facility or activate candidate
  CHANGE_CAPACITY       -> override CAP_i
  CHANGE_DEMAND         -> scale D_{mk}
  CHANGE_TRANSPORT_COST -> scale c_{ijvk}
  LANE_DISRUPTION       -> remove arc from A (set is_active=False)
  FACILITY_DISRUPTION   -> set CAP_i ≈ 0 (disruption, no flow)
  CARBON_FACTOR_CHANGE  -> update emission factors
  SERVICE_TARGET_CHANGE -> update SLA_m
  CUSTOM                -> apply ParameterOverride list
"""

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    TransportMode,
)
from netgravity.schemas.scenario import (
    CostChange,
    DemandChange,
    FacilityChange,
    LaneChange,
    ParameterOverride,
    Scenario,
    ScenarioType,
)
from netgravity.schemas.results import OptimizationResult
from netgravity.optimization.milp import solve as milp_solve
from netgravity.metrics.kpis import compute_kpis, compute_flow_analytics, compare_scenarios


# ---------------------------------------------------------------------------
# Geodesic Distance Helper
# ---------------------------------------------------------------------------

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate geodesic distance between two coordinate pairs in kilometers using Haversine formula.
    """
    R = 6371.0  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2.0) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


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

        # Build modified network and change manifest
        modified, manifest = self._apply_overrides_with_manifest(base_network, scenario)

        # Solve
        result = milp_solve(
            network     = modified,
            config      = config,
            scenario_id = scenario.scenario_id,
        )

        # Attach KPIs, Flow Analytics, and Scenario Audit Manifest
        result.kpis           = compute_kpis(result, modified)
        result.flow_analytics = compute_flow_analytics(result, modified)
        result.scenario_audit_metadata = {
            "scenario_id": scenario.scenario_id,
            "scenario_name": scenario.scenario_name,
            "change_manifest": manifest,
            "total_changes_applied": len(manifest),
        }

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
        net, _ = self._apply_overrides_with_manifest(network, scenario)
        return net

    def _apply_overrides_with_manifest(
        self,
        network:  CanonicalNetwork,
        scenario: Scenario,
    ) -> Tuple[CanonicalNetwork, List[Dict[str, Any]]]:
        """Apply all scenario overrides to a deep copy of the network and build a change manifest."""

        facilities = [f.model_copy(deep=True) for f in network.facilities]
        demands    = [d.model_copy(deep=True) for d in network.demands]
        lanes      = [ln.model_copy(deep=True) for ln in network.lanes]

        fac_map  = {f.id: f for f in facilities}
        manifest: List[Dict[str, Any]] = []

        # --- Apply facility changes ---
        for fc in scenario.facility_changes:
            self._apply_facility_change(fc, fac_map, facilities, lanes)
            manifest.append({
                "type": "FACILITY_CHANGE",
                "action": str(fc.action),
                "facility_id": fc.facility_id,
                "details": fc.model_dump(exclude_none=True),
            })

        # --- Apply demand changes ---
        for dc in scenario.demand_changes:
            self._apply_demand_change(dc, demands)
            manifest.append({
                "type": "DEMAND_CHANGE",
                "market_id": dc.market_id,
                "product_id": dc.product_id,
                "details": dc.model_dump(exclude_none=True),
            })

        # --- Apply cost changes ---
        for cc in scenario.cost_changes:
            self._apply_cost_change(cc, lanes)
            manifest.append({
                "type": "COST_CHANGE",
                "origin_id": cc.origin_id,
                "destination_id": cc.destination_id,
                "mode": cc.mode,
                "details": cc.model_dump(exclude_none=True),
            })

        # --- Apply lane changes ---
        for lc in scenario.lane_changes:
            self._apply_lane_change(lc, lanes, network)
            manifest.append({
                "type": "LANE_CHANGE",
                "action": str(lc.action),
                "origin_id": lc.origin_id,
                "destination_id": lc.destination_id,
                "mode": lc.mode,
                "details": lc.model_dump(exclude_none=True),
            })

        # --- Apply parameter overrides ---
        for po in scenario.parameter_overrides:
            self._apply_parameter_override(po, fac_map, facilities, demands, lanes, network)
            manifest.append({
                "type": "PARAMETER_OVERRIDE",
                "path": po.path,
                "operation": str(po.operation),
                "value": po.value,
            })

        mod_net = network.model_copy(update={
            "facilities": facilities,
            "demands":    demands,
            "lanes":      lanes,
            "network_id": f"{network.network_id}_{scenario.scenario_id}",
        })
        return mod_net, manifest

    def _apply_facility_change(
        self,
        fc:         FacilityChange,
        fac_map:    Dict[str, FacilityRecord],
        facilities: List[FacilityRecord],
        lanes:      List[LaneRecord],
    ) -> None:
        if fc.action == "ADD_FACILITY":
            if fc.new_facility is None:
                raise ValueError(f"ADD_FACILITY action requires `new_facility` object for facility '{fc.facility_id}'.")
            new_f = fc.new_facility.model_copy(deep=True)
            if new_f.id in fac_map:
                idx = next(i for i, f in enumerate(facilities) if f.id == new_f.id)
                facilities[idx] = new_f
                fac_map[new_f.id] = new_f
            else:
                facilities.append(new_f)
                fac_map[new_f.id] = new_f

            if fc.new_lanes:
                for ln in fc.new_lanes:
                    lanes.append(ln.model_copy(deep=True))
            elif new_f.latitude is not None and new_f.longitude is not None:
                res = self._auto_connect_facility(new_f, fac_map, facilities, lanes)
                if not res.get("eligible"):
                    raise ValueError(f"ADD_FACILITY '{new_f.id}' failed auto-connection: {res.get('reason_if_ineligible')}")
            else:
                raise ValueError(f"ADD_FACILITY '{new_f.id}' requires explicit connecting lanes or valid latitude/longitude coordinates.")
            return

        fac = fac_map.get(fc.facility_id)
        if fac is None:
            raise ValueError(f"Facility '{fc.facility_id}' not found in canonical network. Cannot perform action '{fc.action}'.")

        if fc.action in ("MOVE", "SET_LOCATION", "MOVE_FACILITY"):
            lat = fc.latitude if fc.latitude is not None else fac.latitude
            lon = fc.longitude if fc.longitude is not None else fac.longitude
            if lat is None or lon is None:
                raise ValueError(f"MOVE facility '{fc.facility_id}' requires valid latitude and longitude coordinates.")
            orig_lat = fac.latitude
            orig_lon = fac.longitude
            fac.latitude = float(lat)
            fac.longitude = float(lon)
            self._recalculate_facility_lanes(fac, orig_lat, orig_lon, fac_map, lanes)

        elif fc.action == "CLOSE":
            fac.is_forced_closed = True
            fac.is_mandatory     = False
            fac.is_closable      = True
            fac.status           = FacilityStatus.CLOSED

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

    def _recalculate_facility_lanes(
        self,
        facility: FacilityRecord,
        orig_lat: Optional[float],
        orig_lon: Optional[float],
        fac_map:  Dict[str, FacilityRecord],
        lanes:    List[LaneRecord],
    ) -> None:
        """
        Recalculate distance, transport rate, lead time, and carbon inputs for all lanes
        connected to a facility whose location has changed using business-correct relocation logic.
        """
        for ln in lanes:
            if ln.origin_id == facility.id or ln.destination_id == facility.id:
                f_orig = fac_map.get(ln.origin_id)
                f_dest = fac_map.get(ln.destination_id)
                if (f_orig and f_dest and
                    f_orig.latitude is not None and f_orig.longitude is not None and
                    f_dest.latitude is not None and f_dest.longitude is not None):

                    # Anchor baseline initial values on first move
                    if getattr(ln, "_base_distance_km", None) is None:
                        setattr(ln, "_base_distance_km", ln.distance_km)
                        setattr(ln, "_base_rate_per_unit", ln.rate_per_unit)
                        setattr(ln, "_base_lead_time_days", ln.lead_time_days)
                        o_lat = orig_lat if ln.origin_id == facility.id and orig_lat is not None else f_orig.latitude
                        o_lon = orig_lon if ln.origin_id == facility.id and orig_lon is not None else f_orig.longitude
                        d_lat = orig_lat if ln.destination_id == facility.id and orig_lat is not None else f_dest.latitude
                        d_lon = orig_lon if ln.destination_id == facility.id and orig_lon is not None else f_dest.longitude
                        setattr(ln, "_base_orig_lat", o_lat)
                        setattr(ln, "_base_orig_lon", o_lon)
                        setattr(ln, "_base_dest_lat", d_lat)
                        setattr(ln, "_base_dest_lon", d_lon)

                    base_dist = getattr(ln, "_base_distance_km")
                    base_rate = getattr(ln, "_base_rate_per_unit")
                    base_lt   = getattr(ln, "_base_lead_time_days")

                    base_o_lat = getattr(ln, "_base_orig_lat")
                    base_o_lon = getattr(ln, "_base_orig_lon")
                    base_d_lat = getattr(ln, "_base_dest_lat")
                    base_d_lon = getattr(ln, "_base_dest_lon")

                    # Check if moved back to baseline coordinates (within 0.001 deg)
                    is_back_to_base = (
                        abs(f_orig.latitude - base_o_lat) < 1e-3 and
                        abs(f_orig.longitude - base_o_lon) < 1e-3 and
                        abs(f_dest.latitude - base_d_lat) < 1e-3 and
                        abs(f_dest.longitude - base_d_lon) < 1e-3
                    )

                    if is_back_to_base:
                        ln.distance_km = base_dist
                        if ln.network_distance_km is not None:
                            ln.network_distance_km = base_dist
                        ln.rate_per_unit = base_rate
                        ln.lead_time_days = base_lt
                    else:
                        new_dist = haversine_distance(
                            f_orig.latitude, f_orig.longitude,
                            f_dest.latitude, f_dest.longitude,
                        )
                        ln.distance_km = round(new_dist, 2)
                        if ln.network_distance_km is not None:
                            ln.network_distance_km = round(new_dist, 2)

                        # Rule 1 & 2 & 3: Transport Rate Recalculation
                        if ln.rate_per_km is not None:
                            fixed_cost = ln.fixed_leg_cost if ln.fixed_leg_cost is not None else 0.0
                            ln.rate_per_unit = round(ln.rate_per_km * new_dist + fixed_cost, 4)
                        elif ln.tariff_requires_user_input:
                            ln.rate_per_unit = base_rate
                        elif base_dist > 1e-3 and base_rate > 0:
                            ln.rate_per_unit = max(0.01, round((base_rate / base_dist) * new_dist, 4))

                        # Rule 2: Lead Time Recalculation
                        if ln.speed_km_per_day is not None and ln.speed_km_per_day > 0:
                            term_time = ln.terminal_time_days if ln.terminal_time_days is not None else 0.0
                            ln.lead_time_days = max(0.1, round(new_dist / ln.speed_km_per_day + term_time, 2))
                        elif base_dist > 1e-3 and base_lt > 0:
                            ln.lead_time_days = max(0.1, round((base_lt / base_dist) * new_dist, 2))

    def _auto_connect_facility(
        self,
        new_fac:    FacilityRecord,
        fac_map:    Dict[str, FacilityRecord],
        facilities: List[FacilityRecord],
        lanes:      List[LaneRecord],
    ) -> Dict[str, Any]:
        """
        Automatically build inbound (from plants) and outbound (to markets) candidate lanes
        for a newly added facility using canonical transport tariffs and network relationships.
        Supports virtual/source plants without coordinates (F-13).
        """
        plant_roles  = {NodeRole.PLANT, NodeRole.SUPPLIER}
        market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}

        lanes_created = 0

        # Calculate network baseline average rate_per_km for ROAD mode across existing active lanes
        road_lanes = [
            l for l in lanes
            if l.mode == TransportMode.ROAD and l.distance_km > 0 and l.rate_per_unit > 0
        ]
        if road_lanes:
            base_rate_per_km = sum(l.rate_per_unit / l.distance_km for l in road_lanes) / len(road_lanes)
        else:
            base_rate_per_km = 0.025

        for f in facilities:
            if f.id == new_fac.id:
                continue

            # Inbound supply connection (from plants/suppliers)
            if f.role in plant_roles:
                if f.latitude is not None and f.longitude is not None and new_fac.latitude is not None and new_fac.longitude is not None:
                    dist = haversine_distance(f.latitude, f.longitude, new_fac.latitude, new_fac.longitude)
                else:
                    # F-13 Rule 3: Support virtual/source node without coordinates using existing supply relationships
                    existing_inbound = [l.distance_km for l in lanes if l.origin_id == f.id and l.distance_km > 0]
                    dist = sum(existing_inbound) / len(existing_inbound) if existing_inbound else 100.0

                rate = max(0.01, round(dist * base_rate_per_km, 4))
                lt   = max(0.1, round(dist / 500.0, 2))

                lanes.append(LaneRecord(
                    origin_id      = f.id,
                    destination_id = new_fac.id,
                    mode           = TransportMode.ROAD,
                    rate_per_unit  = rate,
                    distance_km    = round(dist, 2),
                    lead_time_days = lt,
                    rate_per_km    = base_rate_per_km,
                ))
                lanes_created += 1

            # Outbound demand connection (to markets)
            if f.role in market_roles:
                if new_fac.latitude is not None and new_fac.longitude is not None and f.latitude is not None and f.longitude is not None:
                    dist = haversine_distance(new_fac.latitude, new_fac.longitude, f.latitude, f.longitude)
                    rate = max(0.01, round(dist * base_rate_per_km, 4))
                    lt   = max(0.1, round(dist / 500.0, 2))

                    lanes.append(LaneRecord(
                        origin_id      = new_fac.id,
                        destination_id = f.id,
                        mode           = TransportMode.ROAD,
                        rate_per_unit  = rate,
                        distance_km    = round(dist, 2),
                        lead_time_days = lt,
                        rate_per_km    = base_rate_per_km,
                    ))
                    lanes_created += 1

        return {
            "facility_added": True,
            "eligible": lanes_created > 0,
            "lanes_created": lanes_created,
            "reason_if_ineligible": None if lanes_created > 0 else "Insufficient data to establish valid inbound/outbound connectivity",
        }

    def _apply_demand_change(
        self,
        dc:      DemandChange,
        demands: List,
    ) -> None:
        matched = 0
        for d in demands:
            if dc.market_id not in (None, "*") and d.market_id != dc.market_id:
                continue
            if dc.product_id not in (None, "*") and d.product_id != dc.product_id:
                continue
            if dc.period is not None and d.period != dc.period:
                continue

            matched += 1
            if dc.quantity_override is not None:
                d.quantity = dc.quantity_override
            elif dc.quantity_multiplier is not None:
                d.quantity = max(0.0, d.quantity * dc.quantity_multiplier)

            if dc.std_dev_multiplier is not None:
                d.std_dev = max(0.0, d.std_dev * dc.std_dev_multiplier)

        if matched == 0:
            raise ValueError(f"DemandChange ({dc}) matched 0 demand records in network.")

    def _apply_cost_change(
        self,
        cc:    CostChange,
        lanes: List[LaneRecord],
    ) -> None:
        matched = 0
        for ln in lanes:
            if cc.origin_id not in (None, "*") and ln.origin_id != cc.origin_id:
                continue
            if cc.destination_id not in (None, "*") and ln.destination_id != cc.destination_id:
                continue
            if cc.mode not in (None, "*"):
                ln_mode = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                if ln_mode != cc.mode:
                    continue

            matched += 1
            if cc.rate_override is not None:
                ln.rate_per_unit = cc.rate_override
            elif cc.rate_multiplier is not None:
                ln.rate_per_unit = max(0.0, ln.rate_per_unit * cc.rate_multiplier)

        if matched == 0:
            raise ValueError(f"CostChange ({cc}) matched 0 lane records in network.")

    def _apply_lane_change(
        self,
        lc:      LaneChange,
        lanes:   List[LaneRecord],
        network: CanonicalNetwork,
    ) -> None:
        if lc.action == "REMOVE":
            orig_len = len(lanes)
            lanes[:] = [
                ln for ln in lanes
                if not (ln.origin_id == lc.origin_id
                        and ln.destination_id == lc.destination_id
                        and (ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)) == lc.mode)
            ]
            if len(lanes) == orig_len:
                raise ValueError(f"LaneChange REMOVE ({lc}) matched 0 active lanes.")

        elif lc.action == "ADD":
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
            matched = 0
            for ln in lanes:
                ln_mode = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                if (ln.origin_id == lc.origin_id
                        and ln.destination_id == lc.destination_id
                        and ln_mode == lc.mode):
                    matched += 1
                    if lc.rate_per_unit   is not None: ln.rate_per_unit  = lc.rate_per_unit
                    if lc.distance_km     is not None: ln.distance_km    = lc.distance_km
                    if lc.lead_time_days  is not None: ln.lead_time_days = lc.lead_time_days
                    if lc.lane_capacity   is not None: ln.lane_capacity  = lc.lane_capacity
                    if lc.is_active       is not None: ln.is_active_baseline = lc.is_active
            if matched == 0:
                raise ValueError(f"LaneChange MODIFY ({lc}) matched 0 active lanes.")

    def _apply_parameter_override(
        self,
        po:         ParameterOverride,
        fac_map:    Dict[str, FacilityRecord],
        facilities: List[FacilityRecord],
        demands:    List,
        lanes:      List[LaneRecord],
        network:    CanonicalNetwork,
    ) -> None:
        """
        Parse and apply typed dot-path parameter override (`ParameterOverride`).
        Supported paths:
          facilities.<id>.capacity_units_per_period
          facilities.<id>.fixed_cost_per_year
          facilities.<id>.handling_cost_per_unit
          facilities.<id>.status
          facilities.<id>.latitude
          facilities.<id>.longitude
          demands.<market_id>.<product_id>.quantity
          demands.<market_id>.<product_id>.sla_days
          demands.<market_id>.<product_id>.service_level
          lanes.<origin_id>.<destination_id>.<mode>.rate_per_unit
          lanes.<origin_id>.<destination_id>.<mode>.distance_km
          lanes.<origin_id>.<destination_id>.<mode>.emission_factor_override
          config.carbon_price
          config.enable_carbon_cost
          config.enforce_sla
          config.inventory_z_score
        """
        parts = po.path.split(".")
        if parts[0] == "facilities" and len(parts) >= 3:
            fac_id = parts[1]
            attr   = parts[2]
            fac = fac_map.get(fac_id)
            if not fac:
                raise ValueError(f"ParameterOverride error: Facility '{fac_id}' not found.")

            val = po.value
            if attr in ("capacity_units_per_period", "capacity"):
                if po.operation == "SET": fac.capacity_units_per_period = float(val)
                elif po.operation == "MULTIPLY": fac.capacity_units_per_period *= float(val)
                elif po.operation == "ADD": fac.capacity_units_per_period += float(val)
            elif attr in ("fixed_cost_per_year", "fixed_cost"):
                if po.operation == "SET": fac.fixed_cost_per_year = float(val)
                elif po.operation == "MULTIPLY": fac.fixed_cost_per_year *= float(val)
                elif po.operation == "ADD": fac.fixed_cost_per_year += float(val)
            elif attr in ("handling_cost_per_unit", "handling_cost"):
                if po.operation == "SET": fac.handling_cost_per_unit = float(val)
                elif po.operation == "MULTIPLY": fac.handling_cost_per_unit *= float(val)
                elif po.operation == "ADD": fac.handling_cost_per_unit += float(val)
            elif attr == "status":
                fac.status = FacilityStatus(val)
            elif attr == "latitude":
                orig_lat = fac.latitude
                orig_lon = fac.longitude
                fac.latitude = float(val)
                self._recalculate_facility_lanes(fac, orig_lat, orig_lon, fac_map, lanes)
            elif attr == "longitude":
                orig_lat = fac.latitude
                orig_lon = fac.longitude
                fac.longitude = float(val)
                self._recalculate_facility_lanes(fac, orig_lat, orig_lon, fac_map, lanes)
            else:
                raise ValueError(f"Unsupported facility parameter override attribute: '{attr}'")

        elif parts[0] == "demands" and len(parts) >= 3:
            market_id  = parts[1]
            product_id = parts[2] if len(parts) >= 4 else None
            attr       = parts[-1]

            matched = False
            for d in demands:
                if d.market_id == market_id and (product_id is None or d.product_id == product_id):
                    matched = True
                    val = po.value
                    if attr == "quantity":
                        if po.operation == "SET": d.quantity = float(val)
                        elif po.operation == "MULTIPLY": d.quantity *= float(val)
                        elif po.operation == "ADD": d.quantity += float(val)
                    elif attr == "sla_days":
                        d.sla_days = float(val) if val is not None else None
                    elif attr == "service_level":
                        d.service_level = float(val)
            if not matched:
                raise ValueError(f"ParameterOverride error: Demand for market '{market_id}' not found.")

        elif parts[0] == "lanes" and len(parts) >= 4:
            orig_id = parts[1]
            dest_id = parts[2]
            if len(parts) >= 5:
                mode_str = parts[3]
                attr = parts[4]
            else:
                mode_str = None
                attr = parts[3]

            matched = False
            for ln in lanes:
                ln_mode = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)
                if (ln.origin_id == orig_id and ln.destination_id == dest_id and
                    (mode_str is None or ln_mode == mode_str)):
                    matched = True
                    val = po.value
                    if attr == "rate_per_unit":
                        if po.operation == "SET": ln.rate_per_unit = float(val)
                        elif po.operation == "MULTIPLY": ln.rate_per_unit *= float(val)
                        elif po.operation == "ADD": ln.rate_per_unit += float(val)
                    elif attr == "distance_km":
                        if po.operation == "SET": ln.distance_km = float(val)
                        elif po.operation == "MULTIPLY": ln.distance_km *= float(val)
                        elif po.operation == "ADD": ln.distance_km += float(val)
                    elif attr in ("emission_factor_override", "emission_factor"):
                        ln.emission_factor_override = float(val) if val is not None else None
            if not matched:
                raise ValueError(f"ParameterOverride error: Lane '{orig_id}'->'{dest_id}' not found.")

        elif parts[0] == "config" and len(parts) >= 2:
            attr = parts[1]
            if hasattr(network.config, attr):
                setattr(network.config, attr, po.value)
            else:
                raise ValueError(f"Unsupported config parameter override attribute: '{attr}'")
        else:
            raise ValueError(f"Unsupported parameter override path: '{po.path}'")

