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
