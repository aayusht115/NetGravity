"""
NetGravity — Resilience Engine
================================
Evaluates network behavior under disruption scenarios.

Disruption types:
  FACILITY_FAILURE   — facility becomes unavailable (CAP = 0)
  LANE_FAILURE       — specific lane is removed from A
  CAPACITY_LOSS      — facility capacity drops by a fraction
  DEMAND_SURGE       — demand increases beyond expected levels

Output metrics (all derived from MILP, no heuristic scores):
  cost_delta         — change in total cost
  service_delta      — change in demand fill rate
  unmet_demand       — units that cannot be served
  rerouted_volume    — volume that changes routing
  carbon_delta_kg    — change in CO₂

Faculty guidance #29:
  "Resilience should eventually be represented through explicit disruption
   scenarios rather than arbitrary resilience scores."

This module returns ResilienceResult objects — measurable, verifiable metrics.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig
from netgravity.schemas.results import OptimizationResult, ResilienceResult, SolverStatus
from netgravity.optimization.milp import solve as milp_solve
from netgravity.metrics.kpis import compute_kpis


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def shortage_config(
    network: CanonicalNetwork, config: OptimizationConfig,
) -> OptimizationConfig:
    """
    A disruption config: unmet demand permitted, optimality judged in money.

    Every disruption solve here permits shortage — that is the point, because a
    disrupted network's answer is "how much can still be served", not
    "infeasible". But permitting it adds `shortage_penalty x unserved` to the
    objective, and at the default 1e6 per unit that term is two orders of
    magnitude larger than the spend. The RELATIVE `mip_gap` is then a tolerance
    of millions of rupees of real cost, while every resilience metric in this
    module is a DIFFERENCE of two such costs — so the difference can be
    dominated entirely by how far each solve happened to stop short of optimal.

    That is not hypothetical: it is what produces a NEGATIVE performance impact
    for a facility whose closure obviously cannot save money.

    `mip_gap_abs` makes the tolerance a money figure, so the two sides of every
    difference are optimal to the same, small, absolute amount.
    """
    market_roles = {"MARKET", "CUSTOMER"}
    fixed_total = 0.0
    for facility in network.facilities:
        role = getattr(facility.role, "value", str(facility.role))
        if role in market_roles:
            continue
        try:
            fixed_total += float(
                facility.get_fixed_cost_for_period(config.cost_period))
        except Exception:  # noqa: BLE001
            continue
    return config.model_copy(update={
        "allow_shortage": True,
        "mip_gap_abs": max(1_000.0, 0.001 * fixed_total),
    })


def compute_rerouted_volume(
    pre_result:  OptimizationResult,
    post_result: OptimizationResult,
) -> float:
    """
    Volume whose routing changed between two solutions.

    Sums the absolute per-arc flow change and halves it: a unit that moves from
    one arc to another shows up as a decrease on one arc and an increase on
    another, so the raw sum double-counts it.

        rerouted = ½ · Σ_a |x_a^post − x_a^pre|

    Shared by ResilienceEngine and the REI assessment so both report the same
    quantity from one definition.
    """
    pre_flows = {
        (f.origin_id, f.destination_id, f.product_id): f.flow_units
        for f in pre_result.flow_decisions
    }
    post_flows = {
        (f.origin_id, f.destination_id, f.product_id): f.flow_units
        for f in post_result.flow_decisions
    }
    all_keys = set(pre_flows.keys()) | set(post_flows.keys())
    return sum(
        abs(post_flows.get(k, 0.0) - pre_flows.get(k, 0.0))
        for k in all_keys
    ) / 2.0


# ---------------------------------------------------------------------------
# Resilience engine
# ---------------------------------------------------------------------------

class ResilienceEngine:
    """
    Evaluate network performance under disruption scenarios.
    Results are derived from MILP solutions — not heuristic scores.
    """

    def facility_failure(
        self,
        network:    CanonicalNetwork,
        facility_id: str,
        config:     Optional[OptimizationConfig] = None,
    ) -> ResilienceResult:
        """
        Simulate failure of a single facility.
        Sets capacity = 0 and re-solves with allow_shortage=True.

        Args:
            network:     Base network
            facility_id: ID of facility to disrupt
            config:      Optimization config

        Returns:
            ResilienceResult with measurable impact metrics
        """
        if config is None:
            config = network.config

        # Enable shortage to measure unmet demand rather than infeasibility
        disruption_config = shortage_config(network, config)

        # Pre-disruption baseline
        pre_result = milp_solve(network=network, config=config, scenario_id="PRE_DISRUPTION")
        pre_kpis   = compute_kpis(pre_result, network)

        # Apply disruption: set facility capacity = 0
        new_facilities = []
        for f in network.facilities:
            if f.id == facility_id:
                new_facilities.append(f.model_copy(update={
                    "capacity_units_per_period": 0.0,
                    "is_mandatory": False,
                    "is_closable":  True,
                }))
            else:
                new_facilities.append(f)

        disrupted_network = network.model_copy(update={"facilities": new_facilities})

        # Post-disruption solve
        post_result = milp_solve(
            network=disrupted_network, config=disruption_config,
            scenario_id=f"DISRUPT_FAC_{facility_id}"
        )
        post_kpis = compute_kpis(post_result, disrupted_network)

        return self._build_result(
            disruption_type = "FACILITY_FAILURE",
            affected_ids    = [facility_id],
            pre_result      = pre_result,
            pre_kpis        = pre_kpis,
            post_result     = post_result,
            post_kpis       = post_kpis,
            network         = network,
        )

    def lane_failure(
        self,
        network:    CanonicalNetwork,
        origin_id:  str,
        dest_id:    str,
        mode:       str = "ROAD",
        config:     Optional[OptimizationConfig] = None,
    ) -> ResilienceResult:
        """
        Simulate failure of a network lane (arc removal).

        Args:
            network:   Base network
            origin_id: Lane origin
            dest_id:   Lane destination
            mode:      Transport mode
            config:    Optimization config

        Returns:
            ResilienceResult
        """
        if config is None:
            config = network.config

        disruption_config = shortage_config(network, config)

        pre_result = milp_solve(network=network, config=config, scenario_id="PRE_DISRUPTION")
        pre_kpis   = compute_kpis(pre_result, network)

        # Remove the specified lane
        new_lanes = [
            ln for ln in network.lanes
            if not (ln.origin_id == origin_id
                    and ln.destination_id == dest_id
                    and (ln.mode.value if hasattr(ln.mode, "value") else str(ln.mode)) == mode)
        ]
        disrupted_network = network.model_copy(update={"lanes": new_lanes})

        post_result = milp_solve(
            network=disrupted_network, config=disruption_config,
            scenario_id=f"DISRUPT_LANE_{origin_id}_{dest_id}"
        )
        post_kpis = compute_kpis(post_result, disrupted_network)

        return self._build_result(
            disruption_type = "LANE_FAILURE",
            affected_ids    = [f"{origin_id}->{dest_id}({mode})"],
            pre_result      = pre_result,
            pre_kpis        = pre_kpis,
            post_result     = post_result,
            post_kpis       = post_kpis,
            network         = network,
        )

    def capacity_loss(
        self,
        network:        CanonicalNetwork,
        facility_id:    str,
        capacity_fraction_remaining: float = 0.5,
        config:         Optional[OptimizationConfig] = None,
    ) -> ResilienceResult:
        """
        Simulate partial capacity loss at a facility.

        Args:
            network:                       Base network
            facility_id:                   Affected facility
            capacity_fraction_remaining:   Fraction of original capacity remaining (e.g., 0.5 = 50%)
            config:                        Optimization config

        Returns:
            ResilienceResult
        """
        if config is None:
            config = network.config

        disruption_config = shortage_config(network, config)

        pre_result = milp_solve(network=network, config=config, scenario_id="PRE_DISRUPTION")
        pre_kpis   = compute_kpis(pre_result, network)

        new_facilities = []
        for f in network.facilities:
            if f.id == facility_id:
                new_cap = f.capacity_units_per_period * capacity_fraction_remaining
                new_facilities.append(f.model_copy(update={"capacity_units_per_period": new_cap}))
            else:
                new_facilities.append(f)

        disrupted_network = network.model_copy(update={"facilities": new_facilities})

        post_result = milp_solve(
            network=disrupted_network, config=disruption_config,
            scenario_id=f"DISRUPT_CAP_{facility_id}"
        )
        post_kpis = compute_kpis(post_result, disrupted_network)

        return self._build_result(
            disruption_type = "CAPACITY_LOSS",
            affected_ids    = [facility_id],
            pre_result      = pre_result,
            pre_kpis        = pre_kpis,
            post_result     = post_result,
            post_kpis       = post_kpis,
            network         = network,
        )

    def demand_surge(
        self,
        network:           CanonicalNetwork,
        demand_multiplier: float = 1.3,
        config:            Optional[OptimizationConfig] = None,
    ) -> ResilienceResult:
        """
        Simulate demand surge (e.g., +30% demand).

        Args:
            network:           Base network
            demand_multiplier: Demand scale factor (e.g., 1.3 = +30%)
            config:            Optimization config

        Returns:
            ResilienceResult
        """
        if config is None:
            config = network.config

        disruption_config = shortage_config(network, config)

        pre_result = milp_solve(network=network, config=config, scenario_id="PRE_DISRUPTION")
        pre_kpis   = compute_kpis(pre_result, network)

        new_demands = [
            d.model_copy(update={"quantity": d.quantity * demand_multiplier})
            for d in network.demands
        ]
        surged_network = network.model_copy(update={"demands": new_demands})

        post_result = milp_solve(
            network=surged_network, config=disruption_config,
            scenario_id=f"DISRUPT_SURGE_{demand_multiplier:.2f}x"
        )
        post_kpis = compute_kpis(post_result, surged_network)

        return self._build_result(
            disruption_type = "DEMAND_SURGE",
            affected_ids    = [f"ALL_MARKETS x{demand_multiplier:.2f}"],
            pre_result      = pre_result,
            pre_kpis        = pre_kpis,
            post_result     = post_result,
            post_kpis       = post_kpis,
            network         = network,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_result(
        self,
        disruption_type: str,
        affected_ids:    List[str],
        pre_result:      OptimizationResult,
        pre_kpis,
        post_result:     OptimizationResult,
        post_kpis,
        network:         CanonicalNetwork,
    ) -> ResilienceResult:
        """Build ResilienceResult from pre/post optimization results."""

        pre_cost    = pre_result.solver.objective_value  or 0.0
        post_cost   = post_result.solver.objective_value or 0.0
        pre_served  = pre_kpis.total_served
        post_served = post_kpis.total_served
        pre_carbon  = pre_kpis.total_carbon_kg
        post_carbon = post_kpis.total_carbon_kg

        unmet = pre_kpis.total_demand - post_served
        service_delta = (post_served - pre_served) / pre_served if pre_served > 0 else 0.0

        # Rerouted volume: flows that changed origin or destination
        rerouted = compute_rerouted_volume(pre_result, post_result)

        return ResilienceResult(
            scenario_id     = post_result.scenario_id or disruption_type,
            disruption_type = disruption_type,
            pre_cost        = round(pre_cost, 2),
            pre_served      = round(pre_served, 2),
            pre_carbon_kg   = round(pre_carbon, 4),
            post_cost       = round(post_cost, 2),
            post_served     = round(post_served, 2),
            post_carbon_kg  = round(post_carbon, 4),
            post_status     = post_result.solver.status,
            cost_delta      = round(post_cost - pre_cost, 2),
            service_delta   = round(service_delta, 6),
            unmet_demand    = round(max(0.0, unmet), 2),
            carbon_delta_kg = round(post_carbon - pre_carbon, 4),
            rerouted_volume = round(rerouted, 2),
            affected_ids    = affected_ids,
        )
