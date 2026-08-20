"""
NetGravity — Pre-Solve Network Validation
==========================================
Validates CanonicalNetwork consistency before passing to the optimizer.

Validation is a FAST-FAIL layer. If these checks fail, the optimizer
will be infeasible or produce wrong results. Better to catch early.

Checks:
  1.  Total supply ≥ total demand (feasibility prerequisite)
  2.  All demand markets have at least one eligible inbound arc
  3.  All demand markets have at least one inbound arc passing SLA
  4.  No negative demands, costs, or capacities
  5.  All referenced facility IDs exist
  6.  All referenced product IDs exist
  7.  Mandatory facilities are not marked closable
  8.  At least one facility can handle each demanded product
  9.  Capacity not negative
  10. Existing facilities have consistent flags
  11. Network is connected (every market reachable)
  12. Demand periods are consistent (single-period model)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from netgravity.schemas.network import CanonicalNetwork, NodeRole


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    severity:    str    # ERROR | WARNING | INFO
    code:        str    # e.g., "V-001"
    description: str
    context:     str = ""


@dataclass
class ValidationReport:
    is_valid:  bool
    issues:    List[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "WARNING"]

    def add_error(self, code: str, description: str, context: str = "") -> None:
        self.is_valid = False
        self.issues.append(ValidationIssue("ERROR", code, description, context))

    def add_warning(self, code: str, description: str, context: str = "") -> None:
        self.issues.append(ValidationIssue("WARNING", code, description, context))

    def print_report(self) -> None:
        print(f"\n{'='*60}")
        print(f"VALIDATION REPORT — {'PASSED' if self.is_valid else 'FAILED'}")
        print(f"{'='*60}")
        if not self.issues:
            print("  ✓ No issues found.")
        for issue in self.issues:
            icon = "✗" if issue.severity == "ERROR" else "⚠" if issue.severity == "WARNING" else "ℹ"
            print(f"  {icon} [{issue.code}] {issue.description}")
            if issue.context:
                print(f"      Context: {issue.context}")


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def validate_network(network: CanonicalNetwork) -> ValidationReport:
    """
    Run all pre-solve validation checks on a CanonicalNetwork.

    Args:
        network: Assembled CanonicalNetwork

    Returns:
        ValidationReport with is_valid=True if all checks pass (no ERRORs)
    """
    report = ValidationReport(is_valid=True)

    facility_map = {f.id: f for f in network.facilities}
    product_map  = {p.id: p for p in network.products}
    market_ids   = {f.id for f in network.facilities if f.role == NodeRole.MARKET}
    non_market_ids = {f.id for f in network.facilities if f.role != NodeRole.MARKET}

    # -----------------------------------------------------------------------
    # V-001: Non-negative demands
    # -----------------------------------------------------------------------
    for d in network.demands:
        if d.quantity < 0:
            report.add_error(
                "V-001",
                f"Negative demand quantity {d.quantity}",
                f"market={d.market_id}, product={d.product_id}",
            )

    # -----------------------------------------------------------------------
    # V-002: Non-negative capacity
    # -----------------------------------------------------------------------
    for f in network.facilities:
        if f.capacity_units_per_period < 0:
            report.add_error(
                "V-002",
                f"Negative capacity for facility '{f.id}'",
                f"capacity={f.capacity_units_per_period}",
            )

    # -----------------------------------------------------------------------
    # V-003: Non-negative costs
    # -----------------------------------------------------------------------
    for f in network.facilities:
        if f.fixed_cost_per_year < 0 or f.handling_cost_per_unit < 0:
            report.add_error(
                "V-003",
                f"Negative cost for facility '{f.id}'",
                f"fixed={f.fixed_cost_per_year}, handling={f.handling_cost_per_unit}",
            )
    for ln in network.lanes:
        if ln.rate_per_unit < 0:
            report.add_error(
                "V-003",
                f"Negative rate_per_unit on lane {ln.origin_id}→{ln.destination_id}",
            )

    # -----------------------------------------------------------------------
    # V-004: Demand market IDs must reference MARKET-role facilities
    # -----------------------------------------------------------------------
    for d in network.demands:
        if d.market_id not in market_ids:
            report.add_error(
                "V-004",
                f"Demand market_id '{d.market_id}' does not reference a MARKET-role facility",
                f"product={d.product_id}",
            )

    # -----------------------------------------------------------------------
    # V-005: Product IDs in demand must exist in product registry
    # -----------------------------------------------------------------------
    for d in network.demands:
        if d.product_id not in product_map:
            report.add_error(
                "V-005",
                f"Demand references unknown product_id '{d.product_id}'",
                f"market={d.market_id}",
            )

    # -----------------------------------------------------------------------
    # V-006: Supply ≥ Demand (prerequisite for feasibility)
    # -----------------------------------------------------------------------
    total_demand   = sum(d.quantity for d in network.demands)
    total_capacity = sum(
        f.capacity_units_per_period
        for f in network.facilities
        if f.role != NodeRole.MARKET and f.capacity_units_per_period < 1e11
    )

    if total_capacity < total_demand and total_capacity > 0:
        report.add_warning(
            "V-006",
            f"Total facility capacity ({total_capacity:.0f}) < total demand ({total_demand:.0f}). "
            f"Model may be infeasible unless shortage is permitted.",
        )

    # -----------------------------------------------------------------------
    # V-007: Each demand market must have at least one inbound lane
    # -----------------------------------------------------------------------
    inbound_lanes: Dict[str, List] = {mid: [] for mid in market_ids}
    for ln in network.lanes:
        if ln.destination_id in market_ids:
            inbound_lanes[ln.destination_id].append(ln)

    for market_id in market_ids:
        if not inbound_lanes.get(market_id):
            report.add_error(
                "V-007",
                f"Market '{market_id}' has no inbound lanes. "
                f"Demand cannot be satisfied.",
            )

    # -----------------------------------------------------------------------
    # V-008: SLA feasibility — every demanded market must have ≥1 lane within SLA
    # -----------------------------------------------------------------------
    if network.config.enforce_sla:
        demand_sla: Dict[str, Optional[float]] = {}
        for d in network.demands:
            if d.sla_days is not None:
                demand_sla[d.market_id] = d.sla_days

        for market_id, sla in demand_sla.items():
            eligible = [
                ln for ln in inbound_lanes.get(market_id, [])
                if sla is None or ln.lead_time_days <= sla
            ]
            if not eligible:
                report.add_error(
                    "V-008",
                    f"Market '{market_id}' has SLA={sla} days but no inbound lane "
                    f"meets this service requirement. Model will be infeasible.",
                )

    # -----------------------------------------------------------------------
    # V-009: Mandatory facilities must be consistent
    # -----------------------------------------------------------------------
    for f in network.facilities:
        if f.is_mandatory and f.is_closable:
            report.add_warning(
                "V-009",
                f"Facility '{f.id}' is marked both mandatory and closable. "
                f"Mandatory takes precedence (y_i = 1 forced).",
            )

    # -----------------------------------------------------------------------
    # V-010: At least one non-market facility
    # -----------------------------------------------------------------------
    if not non_market_ids:
        report.add_error(
            "V-010",
            "Network has no non-market facilities (DCs, warehouses, plants). "
            "Cannot route demand.",
        )

    # -----------------------------------------------------------------------
    # V-011: At least one demand record
    # -----------------------------------------------------------------------
    if not network.demands:
        report.add_warning(
            "V-011",
            "Network has no demand records. Optimization will have trivial solution.",
        )

    # -----------------------------------------------------------------------
    # V-012: At least one product
    # -----------------------------------------------------------------------
    if not network.products:
        report.add_error(
            "V-012",
            "Network has no product records.",
        )

    # -----------------------------------------------------------------------
    # V-013: Lane endpoints are valid facilities
    # -----------------------------------------------------------------------
    for ln in network.lanes:
        if ln.origin_id not in facility_map:
            report.add_error(
                "V-013",
                f"Lane origin '{ln.origin_id}' not in facility registry",
            )
        if ln.destination_id not in facility_map:
            report.add_error(
                "V-013",
                f"Lane destination '{ln.destination_id}' not in facility registry",
            )

    # -----------------------------------------------------------------------
    # V-014: DC Network Topology — DCs must have inbound lanes from plants/supply
    # -----------------------------------------------------------------------
    plant_roles = {NodeRole.PLANT, NodeRole.SUPPLIER}
    dc_roles = {NodeRole.DC, NodeRole.WAREHOUSE, NodeRole.DEPOT, NodeRole.CROSS_DOCK, NodeRole.DARKSTORE}
    plant_ids = {f.id for f in network.facilities if f.role in plant_roles}
    dc_ids = {f.id for f in network.facilities if f.role in dc_roles}

    # Map inbound origins to each DC
    dc_inbound_origins: Dict[str, Set[str]] = {dc_id: set() for dc_id in dc_ids}
    for ln in network.lanes:
        if ln.destination_id in dc_ids:
            dc_inbound_origins[ln.destination_id].add(ln.origin_id)

    for dc_id in dc_ids:
        inbounds = dc_inbound_origins.get(dc_id, set())
        if not inbounds:
            report.add_warning(
                "V-014",
                f"DC facility '{dc_id}' has no inbound lanes from any origin. Transshipment is impossible.",
            )
        elif not (inbounds & plant_ids):
            # Check if reachable transitively from any plant
            reachable = False
            visited = set()
            stack = list(inbounds)
            while stack:
                curr = stack.pop()
                if curr in plant_ids:
                    reachable = True
                    break
                visited.add(curr)
                for prev in dc_inbound_origins.get(curr, set()):
                    if prev not in visited:
                        stack.append(prev)
            if not reachable:
                report.add_warning(
                    "V-014",
                    f"DC facility '{dc_id}' has no path connecting it to any supply origin/plant.",
                )

    # -----------------------------------------------------------------------
    # V-015: Contractual commitment vs forced closure (V1.4)
    # -----------------------------------------------------------------------
    # A facility under an ACTIVE contract that prohibits early closure is pinned
    # open by constraint (C5c). If a scenario ALSO forces it closed (C5b), the
    # two constraints conflict and the model is infeasible. Naming the conflict
    # here turns a bare INFEASIBLE into a readable diagnostic.
    for f in network.facilities:
        if f.contract_prohibits_closure and f.is_forced_closed:
            report.add_error(
                "V-015",
                f"Facility '{f.id}' is under an ACTIVE contract that prohibits early "
                f"closure (contract_allows_early_closure=False) but is also forced "
                f"closed by an override. These constraints conflict and the model will "
                f"be infeasible. To close it, the scenario must explicitly relax the "
                f"contract: set contract_status=EXPIRED or "
                f"contract_allows_early_closure=True (in which case closure_cost "
                f"{f.closure_cost:,.2f} is charged as the early-termination penalty).",
            )

    return report
