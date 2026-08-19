"""
NetGravity — Baseline Evaluator
================================
Evaluate KPIs for the CURRENT-STATE network without optimization.

Purpose:
    "What does the network cost TODAY?"
    The baseline represents what the client operates — not what is optimal.

Method:
    - Fix y_i = 1 for all EXISTING facilities
    - Fix y_i = 0 for all CANDIDATE facilities  
    - Solve the resulting LP (all y_i are fixed → LP, not MIP)
    - Extract KPIs

The baseline result is the comparison anchor for all scenarios.

IMPORTANT: Baseline ≠ Optimal
The optimizer may find a better configuration.
The baseline is not necessarily cost-efficient.
"""

from __future__ import annotations

import copy
import uuid
from typing import Optional

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityStatus,
    NodeRole,
    OptimizationConfig,
)
from netgravity.schemas.results import OptimizationResult
from netgravity.optimization.milp import solve as milp_solve


def evaluate_baseline(
    network:  CanonicalNetwork,
    config:   Optional[OptimizationConfig] = None,
) -> OptimizationResult:
    """
    Evaluate the current-state network as-is (no optimization).

    All EXISTING facilities are forced open (y_i = 1).
    All CANDIDATE facilities are forced closed (y_i = 0, i.e., not eligible).
    Then the LP is solved to find the optimal FLOW ALLOCATION for the fixed network.

    Args:
        network: Assembled network
        config:  Optimization config (defaults to network.config)

    Returns:
        OptimizationResult tagged with scenario_id="BASELINE"
    """
    if config is None:
        config = network.config

    # Deep copy to avoid mutating the base network
    # Force all EXISTING facilities to mandatory (y=1)
    # Force all CANDIDATE facilities to non-eligible (remove them from the network)
    modified_facilities = []
    for fac in network.facilities:
        fac_copy = fac.model_copy(deep=True)
        if fac.role != NodeRole.MARKET:
            if fac.status == FacilityStatus.EXISTING:
                fac_copy.is_mandatory = True
                fac_copy.is_closable  = False
            elif fac.status == FacilityStatus.CANDIDATE:
                # Exclude candidates from baseline: set capacity to 0
                # so no flow routes through them, and y will be 0
                fac_copy.capacity_units_per_period = 0.0
                fac_copy.is_mandatory              = False
                fac_copy.is_closable               = True
        modified_facilities.append(fac_copy)

    baseline_network = network.model_copy(update={"facilities": modified_facilities})

    result = milp_solve(
        network     = baseline_network,
        config      = config,
        scenario_id = "BASELINE",
    )

    return result
