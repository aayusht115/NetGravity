"""
NetGravity — Service Module
============================
Translates customer service requirements into:
  1. Lane eligibility filters (TRANSIT_TIME mode)
  2. MILP constraints (CSL / fill-rate mode)
  3. Shortage penalty coefficients (PENALTY mode)

Service is NOT a single arbitrary score.
It is a measurable operational constraint.

Supported service metrics (Assumption A-006):
  TRANSIT_TIME  — filter lanes where lead_time > SLA
  CSL           — cycle service level constraint
  FILL_RATE     — fraction of demand met per period
  PENALTY       — no hard constraint; use penalty cost for late delivery

Source: Chopra & Meindl, SCM 5th Ed., §12.1–12.3
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from netgravity.schemas.network import (
    DemandRecord,
    FacilityRecord,
    LaneRecord,
    ServiceMetric,
)


# ---------------------------------------------------------------------------
# Service violation record
# ---------------------------------------------------------------------------

@dataclass
class ServiceViolation:
    """
    A lane that cannot meet a customer's service requirement.
    Used to build the feasible arc set A.
    """
    lane_origin:       str
    lane_destination:  str
    mode:              str
    lead_time_days:    float
    sla_days:          float
    customer_id:       str
    violation_type:    str   # TRANSIT_TIME / CSL / FILL_RATE


# ---------------------------------------------------------------------------
# Service module
# ---------------------------------------------------------------------------

class ServiceModule:
    """
    Manages service-level constraints and lane eligibility.

    The optimizer calls:
      - get_eligible_lanes() to filter A during network build
      - get_penalty_map() to get shortage penalty per market
      - check_service_compliance() to validate a solution
    """

    def __init__(self, metric: ServiceMetric = ServiceMetric.TRANSIT_TIME):
        self.metric = metric

    def get_eligible_lanes(
        self,
        market:           FacilityRecord,
        demand:           DemandRecord,
        all_lanes:        List[LaneRecord],
        enforce_sla:      bool = True,
    ) -> List[LaneRecord]:
        """
        Filter lanes to only those that can meet the market's SLA.

        Args:
            market:      The destination market node
            demand:      Demand record (contains SLA requirement)
            all_lanes:   All lanes that terminate at this market
            enforce_sla: If False, return all lanes regardless of SLA

        Returns:
            List of eligible LaneRecords
        """
        if not enforce_sla or demand.sla_days is None:
            return [ln for ln in all_lanes if ln.destination_id == market.id]

        if self.metric == ServiceMetric.TRANSIT_TIME:
            return [
                ln for ln in all_lanes
                if ln.destination_id == market.id
                and ln.lead_time_days <= demand.sla_days
            ]
        else:
            # For other metrics, lane filtering is less restrictive
            # (handled via constraints or penalties in MILP)
            return [ln for ln in all_lanes if ln.destination_id == market.id]

    def get_service_violations(
        self,
        demands:   List[DemandRecord],
        lanes:     List[LaneRecord],
    ) -> List[ServiceViolation]:
        """
        Identify all lanes that violate SLA for their destination market.
        Used for diagnostics and reporting — not for optimization.
        """
        demand_sla: Dict[str, float] = {}
        for d in demands:
            if d.sla_days is not None:
                demand_sla[d.market_id] = d.sla_days

        violations = []
        for ln in lanes:
            sla = demand_sla.get(ln.destination_id)
            if sla is not None and ln.lead_time_days > sla:
                violations.append(ServiceViolation(
                    lane_origin      = ln.origin_id,
                    lane_destination = ln.destination_id,
                    mode             = ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode),
                    lead_time_days   = ln.lead_time_days,
                    sla_days         = sla,
                    customer_id      = ln.destination_id,
                    violation_type   = "TRANSIT_TIME",
                ))
        return violations

    def check_service_compliance(
        self,
        flows:   List[Dict],   # list of {lane, demand_record, flow_units}
        demands: List[DemandRecord],
    ) -> Dict[str, float]:
        """
        Post-solve: compute fraction of demand served within SLA for each market.

        Returns:
            Dict mapping market_id → fraction of demand in SLA (0.0 – 1.0)
        """
        served_in_sla:  Dict[str, float] = {}
        total_demand:   Dict[str, float] = {}

        # Build total demand map
        for d in demands:
            key = d.market_id
            total_demand[key] = total_demand.get(key, 0.0) + d.quantity

        # Check each flow
        for item in flows:
            lane:  LaneRecord    = item["lane"]
            dem:   DemandRecord  = item.get("demand_record")
            flow:  float         = item["flow_units"]

            dest = lane.destination_id
            sla  = dem.sla_days if dem is not None else None

            if sla is None or lane.lead_time_days <= sla:
                served_in_sla[dest] = served_in_sla.get(dest, 0.0) + flow

        # Compute compliance fraction
        compliance = {}
        for market_id, total in total_demand.items():
            served = served_in_sla.get(market_id, 0.0)
            compliance[market_id] = min(1.0, served / total) if total > 0 else 1.0

        return compliance

    def compute_network_service_level(
        self,
        compliance: Dict[str, float],
        demands:    List[DemandRecord],
    ) -> float:
        """
        Demand-weighted average service level across all markets.

        Returns: float in [0, 1]
        """
        total_demand  = sum(d.quantity for d in demands)
        weighted_service = 0.0
        demand_by_market: Dict[str, float] = {}
        for d in demands:
            demand_by_market[d.market_id] = demand_by_market.get(d.market_id, 0.0) + d.quantity

        for market_id, comp in compliance.items():
            weight = demand_by_market.get(market_id, 0.0)
            weighted_service += comp * weight

        return weighted_service / total_demand if total_demand > 0 else 1.0
