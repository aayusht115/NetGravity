"""
NetGravity — Cumulative Lead Time Module V1.1
===============================================
Computes cumulative (end-to-end) lead times across the supply chain network.

Purpose:
    In multi-echelon networks (Plant → DC → Market), the transit time
    from plant to market is the SUM of arc lead times along the path.
    V1.1's MILP only checks direct-arc SLA (the last arc before the market).
    This module computes the MINIMUM cumulative lead time from any upstream
    node to each market, and raises warnings when the cumulative path
    might exceed the SLA.

    IMPORTANT: This is for DIAGNOSTIC and WARNING purposes only in V1.1.
    Full path-dependent SLA optimization (selecting arcs based on cumulative
    lead time) is documented as a future extension.

    Why not full path-SLA in V1.1?
    - Requires O(|N|²) constraints or big-M formulations
    - Significantly increases model complexity for marginal benefit in
      single-echelon networks (the common case in Case-16)
    - The direct-arc SLA filter correctly handles the last-mile constraint,
      which is typically the binding one

Algorithm: Dijkstra's shortest-path on directed arc graph with lead time as weight.
           "Shortest" path = minimum cumulative lead time.

Usage:
    from netgravity.service.cumulative import compute_min_cumulative_lead_times
    lt_map = compute_min_cumulative_lead_times(network)
    # lt_map: {market_id -> {origin_id -> min_cumulative_lt_days}}
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
)


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_min_cumulative_lead_times(
    network: CanonicalNetwork,
) -> Dict[str, Dict[str, float]]:
    """
    Compute minimum cumulative lead time from each node to each market.

    Uses Dijkstra's algorithm on the directed arc graph (with lead_time_days as edge weights).
    For each market, finds the minimum total lead time from every reachable upstream node.

    Args:
        network: CanonicalNetwork with facilities and lanes

    Returns:
        Dict[market_id -> Dict[upstream_node_id -> min_cumulative_lead_time_days]]

    Example:
        result["MARKET_A"]["PLANT_1"] = 5.0 means:
        The fastest path from PLANT_1 to MARKET_A has a total transit time of 5 days.
    """
    market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
    markets = [f for f in network.facilities if f.role in market_roles]
    facility_ids = {f.id for f in network.facilities}

    # Build adjacency list: origin_id -> [(dest_id, lead_time_days)]
    adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    for ln in network.lanes:
        if ln.is_active_baseline:
            adj[ln.origin_id].append((ln.destination_id, ln.lead_time_days))

    # For each market, run backwards Dijkstra from the market node
    # (or equivalently, run forward Dijkstra on the reversed graph)
    all_results: Dict[str, Dict[str, float]] = {}

    for market in markets:
        # Build reversed adjacency for backward Dijkstra from market
        rev_adj: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
        for ln in network.lanes:
            if ln.is_active_baseline:
                rev_adj[ln.destination_id].append((ln.origin_id, ln.lead_time_days))

        # Dijkstra from market (backward)
        dist: Dict[str, float] = {market.id: 0.0}
        heap = [(0.0, market.id)]   # (cumulative_lt, node_id)

        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, float("inf")):
                continue
            for neighbor, lt in rev_adj.get(node, []):
                new_d = d + lt
                if new_d < dist.get(neighbor, float("inf")):
                    dist[neighbor] = new_d
                    heapq.heappush(heap, (new_d, neighbor))

        # dist now maps: {node_id -> min cumulative LT from that node to market}
        all_results[market.id] = {k: v for k, v in dist.items() if k != market.id}

    return all_results


def check_cumulative_sla(
    network: CanonicalNetwork,
    config:  Optional[OptimizationConfig] = None,
) -> List[str]:
    """
    Check whether any demand market has no feasible path within its SLA
    when considering CUMULATIVE lead times (multi-echelon paths).

    Returns a list of warning strings. Empty list = no cumulative SLA issues.

    V1.1 LIMITATION NOTE:
        The MILP only enforces direct-arc SLA (last arc before market).
        This function checks cumulative LT and warns if end-to-end paths
        exceed the SLA — but this does NOT cause infeasibility in the MILP.
        Use these warnings to inform scenario analysis and network design.
    """
    if config is None:
        config = network.config

    if not config.enforce_sla:
        return []

    cumulative_lt = compute_min_cumulative_lead_times(network)
    demand_sla: Dict[str, Optional[float]] = {}
    for d in network.demands:
        if d.sla_days is not None:
            existing = demand_sla.get(d.market_id)
            if existing is None or d.sla_days < existing:
                demand_sla[d.market_id] = d.sla_days

    warnings = []
    for market_id, sla in demand_sla.items():
        if sla is None:
            continue
        lt_map = cumulative_lt.get(market_id, {})
        # Find the minimum cumulative LT from any supply node
        supply_node_roles = {NodeRole.PLANT, NodeRole.SUPPLIER}
        supply_nodes_in_network = {
            f.id for f in network.facilities if f.role in supply_node_roles
        }
        feasible_paths = [
            lt for node_id, lt in lt_map.items()
            if lt <= sla and node_id not in {NodeRole.MARKET.value, NodeRole.CUSTOMER.value}
        ]
        if not feasible_paths:
            min_lt = min(lt_map.values()) if lt_map else float("inf")
            warnings.append(
                f"[W-CLT-001] Market '{market_id}': No path from any upstream node "
                f"meets cumulative SLA of {sla:.1f} days. "
                f"Minimum reachable cumulative lead time: {min_lt:.1f} days. "
                f"NOTE: MILP only enforces direct-arc SLA; this is a warning only."
            )

    return warnings


def get_cumulative_lt_summary(network: CanonicalNetwork) -> List[Dict]:
    """
    Compute a summary of cumulative lead times for each market.
    Used for reporting and dashboard output.

    Returns:
        List of dicts with keys:
            market_id, min_lt_days, max_lt_days, n_reachable_nodes
    """
    cumulative_lt = compute_min_cumulative_lead_times(network)
    market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}

    summary = []
    for market_id, lt_map in cumulative_lt.items():
        if not lt_map:
            summary.append({
                "market_id": market_id,
                "min_lt_days": None,
                "max_lt_days": None,
                "n_reachable_nodes": 0,
            })
        else:
            lts = list(lt_map.values())
            summary.append({
                "market_id": market_id,
                "min_lt_days": round(min(lts), 2),
                "max_lt_days": round(max(lts), 2),
                "n_reachable_nodes": len(lt_map),
            })

    return sorted(summary, key=lambda x: x["market_id"])
