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

import math
from typing import Dict, List, Optional, Set, Tuple

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
            self._apply_facility_change(fc, fac_map, facilities, lanes)

        # --- Apply demand changes ---
        for dc in scenario.demand_changes:
            self._apply_demand_change(dc, demands)

        # --- Apply cost changes ---
        for cc in scenario.cost_changes:
            self._apply_cost_change(cc, lanes)

        # --- Apply lane changes ---
        for lc in scenario.lane_changes:
            self._apply_lane_change(lc, lanes, network)

        # --- Apply parameter overrides ---
        for po in scenario.parameter_overrides:
            self._apply_parameter_override(po, fac_map, facilities, demands, lanes, network)

        return network.model_copy(update={
            "facilities": facilities,
            "demands":    demands,
            "lanes":      lanes,
            "network_id": f"{network.network_id}_{scenario.scenario_id}",
        })

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
                self._auto_connect_facility(new_f, fac_map, facilities, lanes)
            return

        fac = fac_map.get(fc.facility_id)
        if fac is None:
            raise ValueError(f"Facility '{fc.facility_id}' not found in canonical network. Cannot perform action '{fc.action}'.")

        if fc.action in ("MOVE", "SET_LOCATION", "MOVE_FACILITY"):
            lat = fc.latitude if fc.latitude is not None else fac.latitude
            lon = fc.longitude if fc.longitude is not None else fac.longitude
            if lat is None or lon is None:
                raise ValueError(f"MOVE facility '{fc.facility_id}' requires valid latitude and longitude.")
            orig_lat = fac.latitude
            orig_lon = fac.longitude
            fac.latitude = lat
            fac.longitude = lon
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
        connected to a facility whose location has changed using relative coordinate scaling.
        """
        for ln in lanes:
            if ln.origin_id == facility.id or ln.destination_id == facility.id:
                f_orig = fac_map.get(ln.origin_id)
                f_dest = fac_map.get(ln.destination_id)
                if (f_orig and f_dest and
                    f_orig.latitude is not None and f_orig.longitude is not None and
                    f_dest.latitude is not None and f_dest.longitude is not None):

                    # Coordinates of origin and destination BEFORE this move
                    o_lat = orig_lat if ln.origin_id == facility.id and orig_lat is not None else f_orig.latitude
                    o_lon = orig_lon if ln.origin_id == facility.id and orig_lon is not None else f_orig.longitude
                    d_lat = orig_lat if ln.destination_id == facility.id and orig_lat is not None else f_dest.latitude
                    d_lon = orig_lon if ln.destination_id == facility.id and orig_lon is not None else f_dest.longitude

                    init_dist = haversine_distance(o_lat, o_lon, d_lat, d_lon)
                    new_dist  = haversine_distance(f_orig.latitude, f_orig.longitude, f_dest.latitude, f_dest.longitude)

                    if init_dist > 1e-3:
                        scale_factor = new_dist / init_dist
                        ln.distance_km = round(ln.distance_km * scale_factor, 2)
                        if ln.network_distance_km is not None and ln.network_distance_km > 0:
                            ln.network_distance_km = round(ln.network_distance_km * scale_factor, 2)
                        if ln.lead_time_days > 0:
                            ln.lead_time_days = max(0.1, round(ln.lead_time_days * scale_factor, 2))
                        if ln.rate_per_unit > 0:
                            ln.rate_per_unit = max(0.01, round(ln.rate_per_unit * scale_factor, 4))
                    else:
                        ln.distance_km = round(new_dist, 2)
                        ln.lead_time_days = max(0.1, round(new_dist / 500.0, 2))
                        ln.rate_per_unit = max(0.01, round(new_dist * 0.025, 4))

    def _auto_connect_facility(
        self,
        new_fac:    FacilityRecord,
        fac_map:    Dict[str, FacilityRecord],
        facilities: List[FacilityRecord],
        lanes:      List[LaneRecord],
    ) -> None:
        """
        Automatically build inbound (from plants) and outbound (to markets) candidate lanes
        for a newly added facility using geodesic coordinates.
        """
        plant_roles  = {NodeRole.PLANT, NodeRole.SUPPLIER}
        market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}

        for f in facilities:
            if f.id == new_fac.id:
                continue
            if f.latitude is None or f.longitude is None:
                continue

            dist = haversine_distance(new_fac.latitude, new_fac.longitude, f.latitude, f.longitude)
            rate = max(0.5, round(dist * 0.025, 4))
            lt   = max(0.5, round(dist / 500.0, 2))

            # If f is a plant/supplier, build inbound lane: f -> new_fac
            if f.role in plant_roles:
                lanes.append(LaneRecord(
                    origin_id      = f.id,
                    destination_id = new_fac.id,
                    mode           = TransportMode.ROAD,
                    rate_per_unit  = rate,
                    distance_km    = round(dist, 2),
                    lead_time_days = lt,
                ))

            # If f is a market, build outbound lane: new_fac -> f
            if f.role in market_roles:
                lanes.append(LaneRecord(
                    origin_id      = new_fac.id,
                    destination_id = f.id,
                    mode           = TransportMode.ROAD,
                    rate_per_unit  = rate,
                    distance_km    = round(dist, 2),
                    lead_time_days = lt,
                ))

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

