"""
NetGravity — Business Network Cost
===================================
Version: 1.0.0

Derives BUSINESS NETWORK COST from an OptimizationResult.

This module exists to make one distinction explicit and auditable:

    Solver Objective
        What the MILP mathematically minimises. It may contain artificial
        devices — notably the shortage penalty (default 1e6 per unmet unit) and,
        under ObjectiveMode.WEIGHTED_COST_CARBON, a carbon preference weight.
        These exist to steer the optimiser, not to state what the business pays.

    Business Network Cost
        The economic cost the business actually incurs on the network:

            C_business = C_facility + C_opening + C_transport
                       + C_handling + C_inventory + C_carbon*

        (*carbon only when genuinely priced — see below.)

    Shortage Penalty
        A penalty on unmet demand. NOT automatically a financial cost. Excluded
        by default and reported separately, because including it would invent a
        monetary value for lost demand. Unmet demand remains an important
        resilience diagnostic and is reported alongside, never folded in.

Why this matters
────────────────
Under disruption the resilience assessment enables shortage so unmet demand can
be measured. The penalty then dominates the objective by orders of magnitude
(a 5,300-unit shortfall at 1e6/unit is 5.3bn against ~61k of real network cost).
Differencing raw solver objectives would therefore measure the penalty, not the
business impact. Performance Impact must be computed from business cost.

Reuse, not a second cost model
──────────────────────────────
Component values come from `costs.reconciliation.reconcile_costs`, which
independently evaluates each component from the raw decision vectors
(y_i, x_ijvk, a_ij) and canonical network parameters. This module only SELECTS
which of those components constitute business cost. There is no parallel cost
arithmetic here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from netgravity.costs.reconciliation import CostReconciliation, reconcile_costs
from netgravity.schemas.network import CanonicalNetwork, NodeRole, ObjectiveMode, OptimizationConfig
from netgravity.schemas.resilience import ResilienceCostBasis
from netgravity.schemas.results import OptimizationResult

logger = logging.getLogger(__name__)


# Components that may constitute business network cost, in reporting order.
BUSINESS_COST_COMPONENTS = (
    "facility_cost",
    "opening_cost",
    "transport_cost",
    "handling_cost",
    "inventory_cost",
    "carbon_cost",
)

# Components that are mathematical devices, never business cost by default.
NON_BUSINESS_COMPONENTS = ("shortage_cost",)


class BusinessCostError(ValueError):
    """Raised when business network cost cannot be computed from a result."""


@dataclass
class BusinessNetworkCost:
    """
    Business network cost for one optimization result, with a full audit trail
    of what was included, what was excluded, and why.
    """
    total: float

    # Included components and their values (sums exactly to `total`).
    components: Dict[str, float]
    included_components: List[str]

    # Components deliberately left out of `total`, with their values.
    excluded_components: Dict[str, float]

    # The raw mathematical objective, retained so the separation stays visible.
    solver_objective: float

    # Artificial penalty diagnostics (retained, never folded into `total`).
    shortage_penalty_cost: float = 0.0
    unserved_demand: float = 0.0

    # Reconciliation health of the underlying result (solver objective vs the
    # independent sum of ALL components, including excluded ones).
    reconciliation_absolute_difference: float = 0.0
    reconciliation_is_reconciled: bool = True

    notes: List[str] = field(default_factory=list)

    @property
    def excluded_total(self) -> float:
        return round(sum(self.excluded_components.values()), 4)


def compute_business_network_cost(
    result:     OptimizationResult,
    network:    CanonicalNetwork,
    config:     Optional[OptimizationConfig] = None,
    cost_basis: Optional[ResilienceCostBasis] = None,
) -> BusinessNetworkCost:
    """
    Compute business network cost from an optimization result.

    Args:
        result:     A solved OptimizationResult.
        network:    The CanonicalNetwork that produced it.
        config:     Optional OptimizationConfig (uses network.config if None).
        cost_basis: Which components constitute business cost. Defaults to
                    ResilienceCostBasis() — everything except shortage penalty,
                    with carbon gated on a genuine carbon price.

    Returns:
        BusinessNetworkCost with the total, the included/excluded breakdown, and
        the retained artificial-penalty diagnostics.

    Raises:
        BusinessCostError: if the result is not solved (no decisions to cost).
    """
    if config is None:
        config = network.config
    if cost_basis is None:
        cost_basis = ResilienceCostBasis()

    if not result.is_solved:
        raise BusinessCostError(
            f"Cannot compute business network cost from an unsolved result "
            f"(solver status: {result.solver.status.value}). Handle infeasibility "
            f"explicitly rather than costing a non-existent solution."
        )

    # Independent component evaluation from raw decision vectors — reused, not
    # reimplemented. See costs/reconciliation.py.
    rec: CostReconciliation = reconcile_costs(result, network, config=config)
    independent = rec.independent_component_costs

    notes: List[str] = []

    # ---- Carbon gating -----------------------------------------------------
    # Carbon counts as business cost only when it is genuinely part of the
    # configured business objective, i.e. a real monetary price is applied.
    carbon_is_priced = bool(config.enable_carbon_cost)
    obj_mode = config.objective_mode
    obj_mode_str = obj_mode.value if hasattr(obj_mode, "value") else str(obj_mode)
    weighted_carbon_mode = obj_mode_str == ObjectiveMode.WEIGHTED_COST_CARBON.value

    include_flags = {
        "facility_cost":  cost_basis.include_facility_cost,
        "opening_cost":   cost_basis.include_opening_cost,
        "transport_cost": cost_basis.include_transport_cost,
        "handling_cost":  cost_basis.include_handling_cost,
        "inventory_cost": cost_basis.include_inventory_cost,
        "carbon_cost":    cost_basis.include_carbon_cost and carbon_is_priced,
    }

    if cost_basis.include_carbon_cost and not carbon_is_priced:
        notes.append(
            "carbon_cost excluded from business cost: enable_carbon_cost is False, "
            "so no monetary carbon price is part of the configured business objective."
        )
    if weighted_carbon_mode:
        notes.append(
            "objective_mode is WEIGHTED_COST_CARBON: the carbon_weight term is a "
            "modelling preference weight, not a financial cost. It contributes to the "
            "solver objective but is not business cost. When enable_carbon_cost is also "
            "True, the reported carbon_cost component contains both terms and the "
            "business-cost carbon figure is therefore an upper bound."
        )

    # ---- Shortage penalty handling ----------------------------------------
    shortage_cost = round(independent.get("shortage_cost", 0.0), 4)
    include_flags["shortage_cost"] = bool(cost_basis.include_shortage_penalty)

    if cost_basis.include_shortage_penalty:
        notes.append(
            "shortage penalty INCLUDED in business cost by explicit configuration "
            "(include_shortage_penalty=True). This asserts the configured "
            "shortage_penalty is a validated business cost of lost demand."
        )
    elif shortage_cost > 0.0:
        notes.append(
            f"shortage penalty of {shortage_cost:,.2f} excluded from business cost: it is "
            f"a mathematical penalty, not a validated financial cost of lost demand. "
            f"Unmet demand is reported separately as a resilience diagnostic."
        )

    # ---- Assemble ----------------------------------------------------------
    components: Dict[str, float] = {}
    excluded:   Dict[str, float] = {}
    ordered = list(BUSINESS_COST_COMPONENTS) + list(NON_BUSINESS_COMPONENTS)

    for name in ordered:
        value = round(independent.get(name, 0.0), 4)
        if include_flags.get(name, False):
            components[name] = value
        elif value != 0.0 or name in NON_BUSINESS_COMPONENTS:
            excluded[name] = value

    total = round(sum(components.values()), 4)

    # ---- Unmet demand (retained diagnostic) --------------------------------
    market_roles = {NodeRole.MARKET, NodeRole.CUSTOMER}
    fac_map = {f.id: f for f in network.facilities}
    total_demand = sum(d.quantity for d in network.demands)
    total_served = sum(
        fl.flow_units for fl in result.flow_decisions
        if fac_map.get(fl.destination_id) and fac_map[fl.destination_id].role in market_roles
    )
    unserved = round(max(0.0, total_demand - total_served), 4)

    if not rec.is_reconciled:
        notes.append(
            f"underlying cost reconciliation did not close: solver objective "
            f"{rec.solver_objective:,.4f} vs independent total "
            f"{rec.independently_calculated_total:,.4f} "
            f"(absolute difference {rec.absolute_difference:,.4f}). "
            f"Business cost components are still independently evaluated, but the "
            f"result should be investigated."
        )
        logger.warning(
            "business_cost.reconciliation_gap run_id=%s abs_diff=%.4f rel_diff=%.6f",
            result.run_id, rec.absolute_difference, rec.relative_difference,
        )

    return BusinessNetworkCost(
        total                              = total,
        components                         = components,
        included_components                = [n for n in ordered if include_flags.get(n, False)],
        excluded_components                = excluded,
        solver_objective                   = round(result.solver.objective_value or 0.0, 4),
        shortage_penalty_cost              = shortage_cost,
        unserved_demand                    = unserved,
        reconciliation_absolute_difference = rec.absolute_difference,
        reconciliation_is_reconciled       = rec.is_reconciled,
        notes                              = notes,
    )
