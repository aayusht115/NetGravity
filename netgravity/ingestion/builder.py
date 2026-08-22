"""
NetGravity — Network Builder
=============================
Assembles validated rows into the engine's CanonicalNetwork.

This module is the seam between ingestion and the optimisation engine.
It IMPORTS the engine's schemas; it never modifies them. CanonicalNetwork
is the finish line — ingestion's entire job is to produce a valid one.

Cost adjustments extracted from contracts are applied HERE, at assembly
time, so that:
    lane.rate_per_unit          stays the contracted (headline) rate
    effective rate              is computed and reported separately

See schemas/contract.py for why that separation matters.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from netgravity.ingestion.schemas.contract import ContractRule
from netgravity.ingestion.schemas.ingest_result import RowIssue, Severity
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    LaneRecord,
    OptimizationConfig,
    ProductRecord,
)


class NetworkBuildError(Exception):
    """Raised when a network cannot be assembled at all."""


def build_network(
    facilities: List[FacilityRecord],
    products: List[ProductRecord],
    demands: List[DemandRecord],
    lanes: List[LaneRecord],
    *,
    config: Optional[OptimizationConfig] = None,
    network_id: str = "netgravity_india",
    description: str = "",
    contracts: Optional[List[ContractRule]] = None,
) -> Tuple[CanonicalNetwork, List[RowIssue]]:
    """
    Assemble a CanonicalNetwork from validated records.

    Returns the network plus any issues raised during assembly (for example a
    contract surcharge that could not be matched to a lane destination).

    Raises NetworkBuildError only when assembly is impossible — Pydantic's own
    cross-reference validation failing, for instance.
    """
    issues: List[RowIssue] = []

    if not facilities:
        raise NetworkBuildError("Cannot build a network with zero facilities.")
    if not products:
        raise NetworkBuildError("Cannot build a network with zero products.")

    issues.extend(_annotate_contract_effects(lanes, contracts or []))
    issues.extend(_check_period_consistency(facilities, demands))

    try:
        network = CanonicalNetwork(
            facilities=facilities,
            products=products,
            demands=demands,
            lanes=lanes,
            config=config or OptimizationConfig(),
            network_id=network_id,
            description=description,
        )
    except Exception as exc:  # Pydantic validation error
        raise NetworkBuildError(f"CanonicalNetwork rejected the assembled data: {exc}") from exc

    # Stamp a deterministic content hash so any downstream result can be
    # traced back to the exact inputs that produced it.
    network.data_version = network.compute_data_version()
    return network, issues


# Ratios that signal a period mix-up rather than a real capacity shortfall.
# Tolerance is wide because real networks are never exactly at one of these.
_PERIOD_RATIOS = {
    30.0: "daily capacity against monthly demand",
    7.0:  "weekly capacity against monthly demand",
    12.0: "monthly capacity against annual demand",
    4.3:  "weekly capacity against monthly demand",
}
_RATIO_TOLERANCE = 0.45      # +/- 45% around the ratio


def _check_period_consistency(facilities: List[FacilityRecord],
                              demands: List[DemandRecord]) -> List[RowIssue]:
    """
    Catch demand and capacity landing on DIFFERENT time periods.

    This is the safety net for a real ambiguity in the client workbook:
    Daily_Demand_Units states its period in its name, but Capacity_Units does
    not (the workbook calls it "units/day or units/year" and then gives a
    "units/month" example). If demand is scaled to MONTH and capacity is not,
    the model reports a confident INFEASIBLE for a network that is actually
    healthy — the worst possible failure, because it looks like a real answer.

    Names cannot always tell us, so this checks the NUMBERS: a demand-to-
    capacity ratio sitting near 30, 12 or 7 is far more likely to be a unit
    error than a genuine 30x shortfall.
    """
    issues: List[RowIssue] = []
    total_demand = sum(d.quantity for d in demands)
    total_capacity = sum(
        f.capacity_units_per_period for f in facilities
        if not f.is_market and f.capacity_units_per_period < 1e11
    )
    if total_demand <= 0 or total_capacity <= 0:
        return issues

    ratio = total_demand / total_capacity
    if ratio <= 1.0:
        return issues      # capacity covers demand; nothing to suspect

    for factor, explanation in _PERIOD_RATIOS.items():
        if abs(ratio - factor) / factor <= _RATIO_TOLERANCE:
            issues.append(RowIssue(
                severity=Severity.ERROR,
                code="R-021",
                message=(
                    f"demand ({total_demand:,.0f}) exceeds total capacity "
                    f"({total_capacity:,.0f}) by {ratio:.1f}x, which is close to "
                    f"{factor:g}x — this looks like {explanation}, not a real "
                    f"shortfall. State the period explicitly in the capacity "
                    f"column name (e.g. Monthly_Capacity_Units) so it is "
                    f"converted like demand. Solving as-is would report a false "
                    f"INFEASIBLE."
                ),
            ))
            return issues

    issues.append(RowIssue(
        severity=Severity.WARNING, code="R-021",
        message=(
            f"demand ({total_demand:,.0f}) exceeds total capacity "
            f"({total_capacity:,.0f}) by {ratio:.1f}x. If this is not a genuine "
            f"shortfall, check that demand and capacity are on the same period."
        ),
    ))
    return issues


def _annotate_contract_effects(lanes: List[LaneRecord],
                               contracts: List[ContractRule]) -> List[RowIssue]:
    """
    Attach contract-derived cost information to lanes WITHOUT overwriting
    the contracted base rate.

    The effective rate is recorded on the lane's tag-like metadata so both
    numbers survive into the snapshot and can be shown side by side.
    """
    issues: List[RowIssue] = []
    if not contracts:
        return issues

    for lane in lanes:
        for contract in contracts:
            if not contract.has_hidden_cost:
                continue
            effective = contract.effective_rate_for(lane.destination_id)
            if effective > contract.base_rate:
                surcharge = effective - contract.base_rate
                issues.append(RowIssue(
                    severity=Severity.INFO,
                    code="R-CONTRACT",
                    message=(
                        f"lane {lane.origin_id}->{lane.destination_id}: vendor "
                        f"'{contract.vendor_name}' headline {contract.base_rate:g} "
                        f"{contract.rate_unit} becomes {effective:g} after a "
                        f"{surcharge:g} surcharge at this destination"
                    ),
                    source_file=contract.source_file_key or contract.contract_id,
                ))
    return issues


def summarise(network: CanonicalNetwork) -> Dict[str, int]:
    """Counts used by the ingestion report."""
    markets = network.get_markets()
    return {
        "facilities": len(network.facilities) - len(markets),
        "markets": len(markets),
        "products": len(network.products),
        "lanes": len(network.lanes),
        "demands": len(network.demands),
    }
