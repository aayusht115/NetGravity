"""
NetGravity — Core MILP Optimization Engine (Direct Inventory Formulation)
========================================================================
Formulation Reference:
- Direct MILP Facility Location, Multi-Commodity Flow & Inventory Optimization.
- Precomputed safety stock coefficients IC_{ij} are integrated into the MILP objective
  using binary facility-market assignment variables a_{ij} ∈ {0,1}.
- Single-pass exact optimization (eliminates iterative fixed-point inventory loops).
- Multi-period: flow, demand, capacity and stock are indexed by period t.

Mathematical Formulation:
─────────────────────────
Indices:
    i ∈ F : Facilities (Plants, DCs)
    j ∈ M : Markets / Customers
    v ∈ V : Modes
    k ∈ K : Products
    t ∈ T : Planning periods, in the order the demand table states them

Decision Variables:
    y_i ∈ {0,1}       : Facility open status — ONE decision across the horizon
    a_{ij} ∈ {0,1}    : Binary market assignment (facility i serves market j)
    x_{ijvkt} ≥ 0     : Flow volume on arc (i,j,v,k) in period t
    I_{ikt} ≥ 0       : Stock held at facility i of product k at the END of t
    u_{jkt} ≥ 0       : Unmet demand / shortage in period t

Objective Function (Cost Minimization):
    Min Z = |T| Σ f_i y_i + Σ o_i y_i + Σ z_i (1 − y_i)
          + Σ c_{ijvk} x_{ijvkt} + Σ h_i x_{ijvkt}
          + Σ IC_{ij} x_{ijvkt} + Σ g_{ik} I_{ikt} + Σ p u_{jkt}

    Fixed and handling costs recur in every period the facility is open;
    opening, closure and capex are one-time and are charged once.

Constraints:
    (C1)  Demand Balance:      Σ_{i,v} x_{ijvkt} + u_{jkt} = D_{jkt}   ∀j,k,t
    (C2)  Capacity Bounds:     Σ_{j,v,k} x_{ijvkt} ≤ Cap_i y_i         ∀i,t
    (C3)  Min Throughput:      Σ_{j,v,k} x_{ijvkt} ≥ MinThru_i y_i     ∀i,t
    (C4)  Stock Balance:       I_{ik,t−1} + Σ_in x = Σ_out x + I_{ikt} ∀i ∈ DCs, k, t
    (C5a) Mandatory Open:      y_i = 1                                 ∀i ∈ Mandatory
    (C5b) Forced Closed:       y_i = 0                                 ∀i ∈ Closed
    (C6)  Assign Open Link:    a_{ij} ≤ y_i                            ∀i,j
    (C7)  Flow Assign Link:    Σ_{v,k,t} x_{ijvkt} ≤ D_j a_{ij}        ∀i,j
    (C8)  Single Sourcing:     Σ_i a_{ij} = 1                          ∀j (if enabled)
    (C9)  Storage Bound:       Σ_k I_{ikt} ≤ S_i y_i                   ∀i,t

Single-period networks
----------------------
A network stating one demand period produces exactly the model it always did:
|T| = 1 makes the period multipliers 1, C4 degenerates to the flow conservation
it was, and C9 binds nothing because there is no next period to carry into.
Variable and constraint names are unchanged in that case, so a single-period
solve is not perturbed by the horizon machinery existing.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import pulp

from netgravity.inventory.coefficient_engine import InventoryCoefficientEngine
from netgravity.inventory.module import NormalSafetyStockModule, ZeroInventoryModule
from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    FacilityRecord,
    LaneRecord,
    NodeRole,
    ObjectiveMode,
    OptimizationConfig,
    ProductRecord,
    SLAMode,
    SourcingPolicy,
    TransportMode,
)
from netgravity.schemas.results import (
    AssignmentDecision,
    FacilityDecision,
    FlowDecision,
    InventoryDecision,
    OptimizationResult,
    SolverMetadata,
    SolverStatus,
)

logger = logging.getLogger(__name__)

# Type aliases
BaseArcKey      = Tuple[str, str, str, str]         # (origin, dest, mode, product)
ArcKey          = Tuple[str, str, str, str, Any]    # + period
InvCoefficients = Dict[str, float]                  # fac_id -> inventory_cost_per_period

#: Days in a year, for converting a product's ANNUAL holding rate to the cost of
#: holding one unit for one planning period.
_DAYS_PER_YEAR = 365.0


def solve(
    network: CanonicalNetwork,
    config: Optional[OptimizationConfig] = None,
    scenario_id: Optional[str] = None,
) -> OptimizationResult:
    return milp_solve(network=network, config=config, scenario_id=scenario_id)


def milp_solve(
    network: CanonicalNetwork,
    config: Optional[OptimizationConfig] = None,
    scenario_id: Optional[str] = None,
) -> OptimizationResult:
    """
    Solve the network design problem using the V1.2 Direct MILP formulation.

    Precomputes inventory coefficients IC[i,j] and incorporates binary
    assignment variables a[i,j] directly into a SINGLE MILP solve.

    Args:
        network:     CanonicalNetwork input.
        config:      Optional config override. Uses network.config if None.
        scenario_id: Optional scenario identifier for result metadata.

    Returns:
        OptimizationResult containing decision vectors, objective components,
        and solver metadata.
    """
    if config is None:
        config = network.config

    # V1.4: Centralised mode preparation. Fixes decision variables and lane
    # availability for the selected mode BEFORE the formulation is built, so the
    # MILP itself stays single-sourced. The default mode is a strict no-op.
    from netgravity.optimization.modes import get_mode_policy, prepare_network_for_mode
    mode_policy = get_mode_policy(config.optimization_mode)
    mode_network = prepare_network_for_mode(network, mode_policy.mode)

    # Decide which periods this solve carries variables for.
    #
    # A no-op for a network stating one period. For one stating several,
    # FULL_HORIZON (the default) models every period with stock carried between
    # them, and the collapse policies reduce it to one representative period
    # and say so on the result.
    from netgravity.optimization.periods import resolve_horizon
    mode_network, periods, period_report = resolve_horizon(
        mode_network, getattr(config, "multi_period_policy", "FULL_HORIZON"))

    # F-05: Ensure single resolved configuration attached to effective_network
    effective_network = mode_network.model_copy(update={"config": config})

    # Fast-fail network topology validation check
    from netgravity.validation.checks import validate_network
    report = validate_network(effective_network)
    if not report.is_valid and not config.allow_shortage:
        critical_codes = {"V-014", "V-007", "V-008", "V-010"}
        if any(i.code in critical_codes for i in report.errors):
            err_msg = "; ".join([f"[{i.code}] {i.description}" for i in report.errors if i.code in critical_codes])
            solver_meta = SolverMetadata(
                solver_name=config.solver_name,
                status=SolverStatus.INFEASIBLE,
                objective_value=0.0,
                optimality_label="INFEASIBLE",
                warnings=[err_msg],
                scenario_id=scenario_id,
            )
            return OptimizationResult(
                run_id=str(uuid.uuid4())[:8],
                scenario_id=scenario_id,
                network_id=effective_network.network_id,
                data_version=effective_network.data_version,
                result_type="BASELINE" if scenario_id is None else "SCENARIO",
                solver=solver_meta,
                facility_decisions=[],
                flow_decisions=[],
                assignment_decisions=[],
                evaluated_total_cost=None,
            )

    result = _solve_milp(
        network=effective_network,
        config=config,
        scenario_id=scenario_id,
        iteration=0,
        periods=periods,
    )
    # Travels with the result so a screen can state what the figures cover.
    result.period_report = period_report
    # A reader of the solver metadata alone must still be told, whether the
    # horizon was collapsed OR modelled — costs that are horizon totals read as
    # a tenfold cost increase to anyone who assumes one period.
    if period_report.get("n_periods", 1) > 1:
        result.solver.warnings.append(period_report["note"])
    return result


# ---------------------------------------------------------------------------
# Core MILP Builder & Solver (V1.2 Direct Formulation)
# ---------------------------------------------------------------------------

def _solve_milp(
    network: CanonicalNetwork,
    config: OptimizationConfig,
    scenario_id: Optional[str],
    iteration: int = 0,
    periods: Optional[List[Any]] = None,
) -> OptimizationResult:
    """
    Construct and solve the single-pass direct MILP optimization model.

    `periods` is the horizon to carry variables for, in order. When it is None
    the periods are read off the demand table, so calling this directly still
    produces a correct model.
    """
    run_id = str(uuid.uuid4())[:8]

    # ---- Horizon -------------------------------------------------------
    if periods is None:
        seen_periods = sorted({d.period for d in network.demands},
                              key=lambda p: (isinstance(p, str), p))
        periods = seen_periods or [1]
    n_periods = len(periods)
    multi_period = n_periods > 1
    period_index = {p: i for i, p in enumerate(periods)}

    # Resolve the mode policy locally so this builder is self-contained. The
    # network reaching here has already been prepared by milp_solve(); the policy
    # is needed again to decide which ECONOMIC terms apply (closure, contracts).
    from netgravity.optimization.modes import get_mode_policy
    mode_policy = get_mode_policy(config.optimization_mode)

    # Precompute deterministic inventory coefficients IC[i,j] for direct objective integration
    inv_coeffs = InventoryCoefficientEngine.compute_coefficients(network, config)

    # 1. Classify network nodes
    market_roles    = {NodeRole.MARKET, NodeRole.CUSTOMER}
    non_market_facs = [f for f in network.facilities if f.role not in market_roles]
    markets         = [f for f in network.facilities if f.role in market_roles]

    facility_map = {f.id: f for f in network.facilities}
    product_map  = {p.id: p for p in network.products}

    # 2. Build candidate arcs (i, j, v, k)
    arcs: List[Tuple[ArcKey, LaneRecord, ProductRecord]] = []
    arc_unit_cost: Dict[ArcKey, float] = {}
    arc_unit_co2:  Dict[ArcKey, float] = {}

    from netgravity.carbon.module import CarbonModule
    carbon_mod = CarbonModule(config.emission_factor_table, config.emission_methodology)

    # Track SLA-driven arc exclusions so the service report can state exactly
    # what transit-time feasibility removed (V1.4).
    sla_excluded_lanes: List[Dict[str, object]] = []
    n_lanes_evaluated = 0

    seen_keys: Set[ArcKey] = set()
    for lane in network.lanes:
        orig = facility_map.get(lane.origin_id)
        dest = facility_map.get(lane.destination_id)
        if not orig or not dest:
            continue

        if orig.status.name == "CLOSED" or dest.status.name == "CLOSED":
            continue

        sla_mode_val = getattr(config, "sla_mode", "LAST_MILE")
        sla_mode_str = sla_mode_val.value if hasattr(sla_mode_val, "value") else str(sla_mode_val)

        if config.enforce_sla and orig.role not in market_roles and dest.role in market_roles:
            sla_feasible = True
            n_lanes_evaluated += 1
            binding_sla: Optional[float] = None
            binding_lt: float = lane.lead_time_days

            for d in network.demands:
                if d.market_id == dest.id and d.sla_days is not None and d.sla_days > 0:
                    if sla_mode_str == "END_TO_END":
                        plant_roles = {NodeRole.PLANT, NodeRole.SUPPLIER}
                        inbound_lanes = [
                            ln for ln in network.lanes
                            if ln.destination_id == orig.id and facility_map.get(ln.origin_id) and facility_map.get(ln.origin_id).role in plant_roles
                        ]
                        dc_lt = getattr(orig, "replenishment_lead_time_days", 0.0) or 0.0
                        if inbound_lanes:
                            feasible_paths = [
                                ln.lead_time_days + dc_lt + lane.lead_time_days
                                for ln in inbound_lanes
                                if (ln.lead_time_days + dc_lt + lane.lead_time_days) <= d.sla_days
                            ]
                            if not feasible_paths:
                                sla_feasible = False
                                binding_sla = d.sla_days
                                binding_lt = min(
                                    ln.lead_time_days + dc_lt + lane.lead_time_days
                                    for ln in inbound_lanes
                                )
                                break
                        else:
                            total_lt = dc_lt + lane.lead_time_days
                            if total_lt > d.sla_days:
                                sla_feasible = False
                                binding_sla = d.sla_days
                                binding_lt = total_lt
                                break
                    else:
                        # LAST_MILE (default)
                        if lane.lead_time_days > d.sla_days:
                            sla_feasible = False
                            binding_sla = d.sla_days
                            binding_lt = lane.lead_time_days
                            break

            if not sla_feasible:
                # Record the exclusion so the service report can state exactly
                # which arcs transit-time feasibility removed, and why.
                sla_excluded_lanes.append({
                    "origin_id":      lane.origin_id,
                    "destination_id": lane.destination_id,
                    "mode":           lane.mode.value if hasattr(lane.mode, "value") else str(lane.mode),
                    "lead_time_days": round(binding_lt, 4),
                    "sla_days":       round(binding_sla or 0.0, 4),
                    "excess_days":    round(binding_lt - (binding_sla or 0.0), 4),
                })
                continue

        mode_str = lane.mode.value if hasattr(lane.mode, "value") else str(lane.mode)

        for prod in network.products:
            if orig.eligible_product_ids and prod.id not in orig.eligible_product_ids:
                continue
            if dest.eligible_product_ids and prod.id not in dest.eligible_product_ids:
                continue

            base_key: BaseArcKey = (lane.origin_id, lane.destination_id, mode_str, prod.id)
            if base_key in seen_keys:
                continue
            seen_keys.add(base_key)

            rate = lane.rate_per_unit
            co2  = carbon_mod.compute_unit_co2(lane, prod)

            # One arc per period. Rates, distances and emission factors do not
            # vary by period in the schema, so every period shares the lane's
            # own figures — but the FLOW on each is a separate decision, which
            # is what makes a seasonal plan expressible.
            for period in periods:
                key: ArcKey = base_key + (period,)
                arcs.append((key, lane, prod))
                arc_unit_cost[key] = rate
                arc_unit_co2[key]  = co2

    arc_set: Set[ArcKey] = {item[0] for item in arcs}

    # 3. Build PuLP model
    prob = pulp.LpProblem(f"NetGravity_V1_2_{run_id}", pulp.LpMinimize)

    # Decision variables: y_i ∈ {0,1}
    y: Dict[str, pulp.LpVariable] = {}
    for fac in non_market_facs:
        y[fac.id] = _make_var(prob, f"y_{fac.id}", lowBound=0, upBound=1, cat="Binary")

    # Decision variables: a_{ij} ∈ {0,1} (Facility-to-Market assignment)
    a: Dict[Tuple[str, str], pulp.LpVariable] = {}
    if config.sourcing_policy in (SourcingPolicy.SINGLE, SourcingPolicy.DUAL):
        for fac in non_market_facs:
            for mkt in markets:
                a[(fac.id, mkt.id)] = _make_var(
                    prob,
                    f"a_{fac.id}_{mkt.id}",
                    lowBound=0,
                    upBound=1,
                    cat="Binary",
                )

    # Decision variables: x_{ijvkt} ≥ 0
    #
    # The period suffix is added only when there IS more than one period, so a
    # single-period model produces exactly the variable names it always did.
    x: Dict[ArcKey, pulp.LpVariable] = {}
    for (key, ln, prod) in arcs:
        # A lane capacity is stated PER PERIOD, so it bounds each period's own
        # flow variable — not the horizon total.
        ub = (ln.lane_capacity if (ln.lane_capacity is not None and ln.lane_capacity > 0) else None)
        x[key] = _make_var(
            prob,
            f"x_{key[0]}_{key[1]}_{key[2]}_{key[3]}" + (f"_t{key[4]}" if multi_period else ""),
            lowBound=0,
            upBound=ub,
            cat="Continuous",
        )

    # Arc lookups, built once.
    #
    # Every constraint family below used to scan the whole arc set, which is
    # O(|F|·|arcs|) per family. Multiplying the arc set by the number of periods
    # turns that from slow into the difference between building a model and
    # appearing to hang, so the scans are replaced by indexes built in one pass.
    arcs_by_dest_prod_period:   Dict[Tuple[str, str, Any], List[ArcKey]] = {}
    arcs_by_origin_period:      Dict[Tuple[str, Any], List[ArcKey]] = {}
    arcs_by_origin_prod_period: Dict[Tuple[str, str, Any], List[ArcKey]] = {}
    arcs_by_corridor_period:    Dict[Tuple[str, str, Any], List[ArcKey]] = {}
    arcs_by_origin:             Dict[str, List[ArcKey]] = {}
    arcs_by_origin_dest:        Dict[Tuple[str, str], List[ArcKey]] = {}
    for key in arc_set:
        if key not in x:
            continue
        origin, dest, _mode, prod_id, period = key
        arcs_by_dest_prod_period.setdefault((dest, prod_id, period), []).append(key)
        arcs_by_origin_period.setdefault((origin, period), []).append(key)
        arcs_by_origin_prod_period.setdefault((origin, prod_id, period), []).append(key)
        arcs_by_corridor_period.setdefault((origin, dest, period), []).append(key)
        arcs_by_origin.setdefault(origin, []).append(key)
        arcs_by_origin_dest.setdefault((origin, dest), []).append(key)

    # Demand aggregated per (market, product, period).
    #
    # Aggregated rather than iterated row by row because two rows CAN share a
    # market, product and period — different service levels on the same month
    # is an ordinary shape — and one constraint per row produced two constraints
    # with one name, which is the PulpError this whole area exists to stop. The
    # SLA filter above already applies the strictest service level on the lane,
    # so the quantities are what the balance needs.
    demand_by_key: Dict[Tuple[str, str, Any], float] = {}
    for d in network.demands:
        dk = (d.market_id, d.product_id, d.period)
        demand_by_key[dk] = demand_by_key.get(dk, 0.0) + float(d.quantity)

    # Decision variables: u_{jkt} ≥ 0 (Shortage)
    u: Dict[Tuple[str, str, Any], pulp.LpVariable] = {}
    if config.allow_shortage:
        for (mkt_id, prod_id, period) in demand_by_key:
            key_u = (mkt_id, prod_id, period)
            if key_u not in u:
                u[key_u] = _make_var(
                    prob,
                    f"u_{mkt_id}_{prod_id}" + (f"_t{period}" if multi_period else ""),
                    lowBound=0,
                    cat="Continuous",
                )

    # Decision variables: I_{ikt} ≥ 0 (stock carried into the next period)
    #
    # Only built for a genuine horizon. A single-period model has no next period
    # to carry stock into, so creating the variables would add columns that can
    # only ever be zero and would change nothing but the solve time.
    #
    # Only at facilities that can actually hold stock:
    #   * markets are demand sinks, not warehouses;
    #   * a CROSS_DOCK by definition does not store — allowing it to would let
    #     the model solve a seasonality problem with a building that cannot;
    #   * plants are excluded because in this formulation a plant's outbound
    #     flow IS its production. Separating "made in March" from "shipped in
    #     June" needs a production variable distinct from the shipment
    #     variable, which this model does not have. Pre-build therefore happens
    #     downstream, at the DC, which is where inbound and outbound genuinely
    #     are two different flows.
    storage_roles = {NodeRole.DC, NodeRole.DEPOT, NodeRole.WAREHOUSE, NodeRole.DARKSTORE}
    stock_facs = [f for f in non_market_facs if f.role in storage_roles] if multi_period else []

    # Cost of holding one unit of product k for one period, from the product's
    # own stated value and annual holding rate. Nothing is assumed: a product
    # with no unit_value carries a holding cost of zero, and that is reported
    # rather than replaced with a plausible rate.
    holding_cost_per_unit: Dict[str, float] = {}
    for prod in network.products:
        unit_value = float(getattr(prod, "unit_value", 0.0) or 0.0)
        holding_rate = float(getattr(prod, "holding_rate", 0.0) or 0.0)
        holding_cost_per_unit[prod.id] = (
            unit_value * holding_rate * (config.days_per_period / _DAYS_PER_YEAR)
        )

    # The largest quantity it could ever be useful to hold: everything the
    # horizon asks for. Used to bound stock at a facility that states no storage
    # capacity, and to force stock to zero at a facility the model closes.
    horizon_demand_total = sum(demand_by_key.values())

    # Only products this facility actually moves. A stock variable for a product
    # with no arc at either end is connected to no balance constraint, so it
    # would be free to take any value the storage bound allows whenever holding
    # is unpriced — stock appearing in a plan out of nothing.
    products_touched: Dict[str, Set[str]] = {}
    for (o_id, d_id, _m, p_id, _t) in arc_set:
        products_touched.setdefault(o_id, set()).add(p_id)
        products_touched.setdefault(d_id, set()).add(p_id)

    I: Dict[Tuple[str, str, Any], pulp.LpVariable] = {}
    for fac in stock_facs:
        stated = getattr(fac, "storage_capacity_units", None)
        bound = float(stated) if (stated is not None and stated > 0) else horizon_demand_total
        touched = products_touched.get(fac.id, set())
        for prod in network.products:
            if prod.id not in touched:
                continue
            if fac.eligible_product_ids and prod.id not in fac.eligible_product_ids:
                continue
            for period in periods:
                I[(fac.id, prod.id, period)] = _make_var(
                    prob,
                    f"I_{fac.id}_{prod.id}_t{period}",
                    lowBound=0,
                    upBound=bound if bound > 0 else 0.0,
                    cat="Continuous",
                )

    # 4. Objective Terms
    #
    # A fixed cost is stated PER PERIOD, so over a horizon of |T| periods an
    # open facility is charged |T| times. Charging it once would make a
    # twelve-month plan look like it rents a warehouse for one month, and would
    # tilt every siting decision towards more facilities than the money
    # supports. Opening, closure and capex are one-time and stay one-time.
    facility_cost_term = pulp.lpSum(
        fac.get_fixed_cost_for_period(config.cost_period) * n_periods * y[fac.id]
        for fac in non_market_facs
    )

    opening_cost_term = pulp.lpSum(
        fac.opening_cost * y[fac.id]
        for fac in non_market_facs
        if fac.is_candidate and fac.opening_cost > 0
    )

    # Closure cost (V1.4): one-time cost charged when an EXISTING facility
    # transitions open → closed.
    #
    #     Σ_{i ∈ closure-eligible} closure_cost_i · (1 − y_i)
    #
    # Linear in y, so the formulation stays a MILP. Eligibility is decided by
    # FacilityRecord.closure_cost_applies(), which keys on the OBSERVED baseline
    # status (not the possibly scenario-mutated `status`) and excludes disruption
    # targets. Whether closure economics apply at all is a mode decision.
    closure_eligible_facs = (
        [
            fac for fac in non_market_facs
            # closure_cost_applies() is evaluated for the closed case (y_i = 0)
            if fac.closure_cost_applies(is_open=False)
        ]
        if (config.enable_closure_cost and mode_policy.apply_closure_cost)
        else []
    )

    closure_cost_term = pulp.lpSum(
        fac.closure_cost * (1 - y[fac.id]) for fac in closure_eligible_facs
    ) if closure_eligible_facs else 0

    transport_cost_term = pulp.lpSum(
        arc_unit_cost[key] * x[key]
        for key in arc_set
        if key in x
    )

    # Handling is charged per unit shipped, in every period it is shipped in.
    handling_cost_term = pulp.lpSum(
        fac.handling_cost_per_unit * pulp.lpSum(
            x[key] for key in arcs_by_origin.get(fac.id, [])
        )
        for fac in non_market_facs
        if fac.handling_cost_per_unit > 0
    )

    # Direct Volume-Responsive Inventory Cost Term: Σ unit_inv_cost[i,j,k] * x[i,j,v,k]
    if config.enable_inventory:
        inventory_cost_term = pulp.lpSum(
            inv_coeffs[(key[0], key[1])].unit_inv_cost_by_product.get(key[3], 0.0) * x[key]
            for key in arc_set
            if key in x
            and (key[0], key[1]) in inv_coeffs
            and inv_coeffs[(key[0], key[1])].unit_inv_cost_by_product
            and inv_coeffs[(key[0], key[1])].unit_inv_cost_by_product.get(key[3], 0.0) > 0
        )
    else:
        inventory_cost_term = 0

    # Stock held between periods costs money, or the model would smooth every
    # peak for free. Zero for a product with no stated unit value — which is
    # reported on the result, not papered over with an assumed rate.
    holding_cost_term = (
        pulp.lpSum(
            holding_cost_per_unit[prod_id] * var
            for (fac_id, prod_id, period), var in I.items()
            if holding_cost_per_unit.get(prod_id, 0.0) > 0
        )
        if I
        else 0
    )

    # Priority is a property of the market and product, not of one month of it.
    # The highest priority stated across the periods governs, so a market never
    # loses its priority because one row omitted it.
    demand_priority_map: Dict[Tuple[str, str], int] = {}
    for d in network.demands:
        pk = (d.market_id, d.product_id)
        demand_priority_map[pk] = max(demand_priority_map.get(pk, 1),
                                      getattr(d, "priority", 1) or 1)

    shortage_cost_term = (
        pulp.lpSum(
            config.shortage_penalty
            * (1.0 + (demand_priority_map.get((key_u[0], key_u[1]), 1) - 1) * 0.5)
            * u[key_u]
            for key_u in u
        )
        if config.allow_shortage and u
        else 0
    )

    carbon_cost_term = (
        pulp.lpSum(
            config.carbon_price * arc_unit_co2[key] * x[key]
            for key in arc_set
            if key in x
        )
        if config.enable_carbon_cost
        else 0
    )

    carbon_objective_term = (
        pulp.lpSum(
            config.carbon_weight * arc_unit_co2[key] * x[key]
            for key in arc_set
            if key in x
        )
        if (hasattr(config, "objective_mode") and
            (config.objective_mode.value if hasattr(config.objective_mode, "value") else str(config.objective_mode)) == "WEIGHTED_COST_CARBON")
        else 0
    )

    prob += (
        facility_cost_term
        + opening_cost_term
        + closure_cost_term
        + transport_cost_term
        + handling_cost_term
        + inventory_cost_term
        + holding_cost_term
        + shortage_cost_term
        + carbon_cost_term
        + carbon_objective_term
    ), "TotalCost"

    # 5. Constraints

    # (F-19) Carbon Cap Constraint: Σ (arc_unit_co2 · x) <= carbon_cap_kg
    #
    # Stated as a total, so it binds the HORIZON total. A cap meant per period
    # would have to say so; reading a single number as per-period would let a
    # twelve-month plan emit twelve times the stated cap.
    if config.carbon_cap_kg is not None and config.carbon_cap_kg >= 0:
        prob += (
            pulp.lpSum(arc_unit_co2[key] * x[key] for key in arc_set if key in x) <= config.carbon_cap_kg,
            "C_carbon_cap",
        )

    # (F-03) Aggregate Corridor Capacity: Σ_{v,k} x_{i,j,v,k,t} <= corridor_capacity ∀t
    # A corridor capacity is a per-period throughput, so it binds each period.
    corridor_caps: Dict[Tuple[str, str], float] = {}
    for lane in network.lanes:
        if lane.lane_capacity is not None and lane.lane_capacity > 0:
            corr_key = (lane.origin_id, lane.destination_id)
            if corr_key not in corridor_caps or lane.lane_capacity < corridor_caps[corr_key]:
                corridor_caps[corr_key] = lane.lane_capacity

    for (orig_id, dest_id), cap_val in corridor_caps.items():
        for period in periods:
            corr_arcs = arcs_by_corridor_period.get((orig_id, dest_id, period), [])
            if corr_arcs:
                prob += (
                    pulp.lpSum(x[k] for k in corr_arcs) <= cap_val,
                    f"corridor_cap_{orig_id}_{dest_id}"
                    + (f"_t{period}" if multi_period else ""),
                )

    # (C1) Demand Fulfillment, per market, product AND period
    for (mkt_id, prod_id, period), quantity in demand_by_key.items():
        inbound_keys = arcs_by_dest_prod_period.get((mkt_id, prod_id, period), [])
        key_u = (mkt_id, prod_id, period)
        suffix = f"_p{period}"

        if inbound_keys and config.allow_shortage:
            prob += (
                pulp.lpSum(x[k] for k in inbound_keys) + u[key_u] == quantity,
                f"demand_{mkt_id}_{prod_id}{suffix}",
            )
        elif inbound_keys:
            prob += (
                pulp.lpSum(x[k] for k in inbound_keys) == quantity,
                f"demand_{mkt_id}_{prod_id}{suffix}",
            )
        else:
            if config.allow_shortage:
                prob += (
                    u[key_u] == quantity,
                    f"demand_unreachable_{mkt_id}_{prod_id}{suffix}",
                )
            else:
                dummy_infeas = _make_var(prob, f"infeas_dummy_{mkt_id}_{prod_id}{suffix}", lowBound=0, upBound=0)
                prob += (
                    dummy_infeas == quantity,
                    f"demand_unreachable_force_infeasible_{mkt_id}_{prod_id}{suffix}",
                )

    # (C2 & C3) Capacity and Min Throughput — PER PERIOD.
    #
    # Both are stated per period, so both bind per period. Applying them to the
    # horizon total instead would let a facility ship a whole year's capacity in
    # one month, which is exactly the seasonality error this model exists to
    # stop making.
    for fac in non_market_facs:
        outbound_all = arcs_by_origin.get(fac.id, [])
        if not outbound_all:
            continue
        outbound_total = pulp.lpSum(x[key] for key in outbound_all)

        # Throughput capacity, plus the SUPPLY-side limit where one applies.
        #
        # `production_capacity_units_per_period` is documented on
        # `FacilityRecord` as a PLANT/SUPPLIER limit, and
        # `effective_supply_capacity` already encodes that reading. This took
        # `min(throughput, production)` for every non-market facility instead,
        # so a DC carrying a production capacity of zero — which is the literal
        # truth about a DC, and what several code paths set — became a facility
        # that may ship nothing at all, with no diagnostic anywhere: it still
        # reports its throughput capacity in every KPI while the model holds it
        # at zero.
        cap_vals = [c for c in (fac.capacity_units_per_period,
                                fac.effective_supply_capacity)
                    if c is not None]
        eff_cap = min(cap_vals) if cap_vals else None
        min_thru = fac.min_throughput_per_period

        for period in periods:
            period_keys = arcs_by_origin_period.get((fac.id, period), [])
            if not period_keys:
                continue
            outbound_sum = pulp.lpSum(x[key] for key in period_keys)
            suffix = f"_t{period}" if multi_period else ""
            if eff_cap is not None:
                prob += outbound_sum <= eff_cap * y[fac.id], f"cap_{fac.id}{suffix}"
            if config.minimum_throughput_enabled and min_thru is not None and min_thru > 0:
                prob += outbound_sum >= min_thru * y[fac.id], f"min_thru_{fac.id}{suffix}"

        # (F-20) Unpin binary y_i when facility carries 0 fixed cost and 0
        # throughput. Applied to the horizon total: a facility that ships in any
        # period is open, and one that never ships need not be.
        if not fac.is_mandatory:
            prob += y[fac.id] <= outbound_total * 1e5, f"unpin_zero_thru_{fac.id}"

    # (C4) Stock balance at intermediate facilities.
    #
    #     I_{i,k,t−1} + Σ_in x_{·,i,·,k,t} = Σ_out x_{i,·,·,k,t} + I_{i,k,t}
    #
    # With one period and no stock variables this is exactly the flow
    # conservation it replaces: 0 + inbound = outbound + 0. With several, it is
    # the single line that makes seasonality answerable — a DC may take delivery
    # in a quiet month and ship it in a busy one, which is what a planner
    # actually does and what the previous model could not express at all.
    #
    # Opening stock is zero. Nothing in the schema states what is on hand at the
    # start of the horizon, and inventing an opening balance would hand the plan
    # free goods.
    intermediate_roles = {NodeRole.DC, NodeRole.DEPOT, NodeRole.WAREHOUSE, NodeRole.CROSS_DOCK, NodeRole.DARKSTORE}
    intermediate_facs = [f for f in non_market_facs if f.role in intermediate_roles]
    for fac in intermediate_facs:
        for prod in network.products:
            for period in periods:
                inbound = arcs_by_dest_prod_period.get((fac.id, prod.id, period), [])
                outbound = arcs_by_origin_prod_period.get((fac.id, prod.id, period), [])
                if not outbound and not (inbound and multi_period):
                    continue
                idx = period_index[period]
                opening = I.get((fac.id, prod.id, periods[idx - 1])) if idx > 0 else None
                closing = I.get((fac.id, prod.id, period))
                lhs = pulp.lpSum(x[k] for k in inbound)
                if opening is not None:
                    lhs = lhs + opening
                rhs = pulp.lpSum(x[k] for k in outbound)
                if closing is not None:
                    rhs = rhs + closing
                prob += (
                    lhs == rhs,
                    f"flow_bal_{fac.id}_{prod.id}" + (f"_t{period}" if multi_period else ""),
                )

    # (C9) Storage bound, and no stock at a facility the model closes.
    #
    # Without the y_i link a closed DC could still take delivery and sit on the
    # goods for ever: C2 limits its OUTBOUND flow to zero, not its inbound, so
    # the balance above would be satisfied by parking everything in stock.
    for fac in stock_facs:
        stated = getattr(fac, "storage_capacity_units", None)
        bound = float(stated) if (stated is not None and stated > 0) else horizon_demand_total
        for period in periods:
            held = [I[(fac.id, p.id, period)] for p in network.products
                    if (fac.id, p.id, period) in I]
            if held:
                prob += (
                    pulp.lpSum(held) <= bound * y[fac.id],
                    f"storage_cap_{fac.id}_t{period}",
                )

    # (C5a & C5b) Mandatory and Forced Closed
    for fac in non_market_facs:
        if fac.is_forced_closed:
            prob += y[fac.id] == 0, f"forced_closed_{fac.id}"
        elif fac.is_mandatory:
            prob += y[fac.id] == 1, f"mandatory_open_{fac.id}"

    # (C5c) Contractual Open (V1.4): a facility under an ACTIVE contract that
    # does not permit early closure must remain open.
    #
    #     y_i = 1   ∀ i with contract_status = ACTIVE ∧ ¬contract_allows_early_closure
    #
    # Disruption targets are exempt (see FacilityRecord.contract_prohibits_closure):
    # an involuntary outage does not breach a voluntary-closure clause.
    #
    # If a scenario ALSO forces the facility closed (C5b), the two constraints
    # conflict and the model is INFEASIBLE — the correct, honest signal that the
    # scenario violates a contractual commitment. Validation check V-015 names
    # the conflict explicitly so the diagnostic is readable. To close such a
    # facility legitimately, the scenario must explicitly relax the contract
    # (contract_status → EXPIRED, or contract_allows_early_closure → True).
    if config.enforce_contracts and mode_policy.enforce_contracts:
        for fac in non_market_facs:
            if fac.contract_prohibits_closure:
                prob += y[fac.id] == 1, f"contract_open_{fac.id}"

    # (C6-C8) Assignment and Sourcing Constraints (ONLY when SINGLE or DUAL sourcing)
    if config.sourcing_policy in (SourcingPolicy.SINGLE, SourcingPolicy.DUAL):
        # (C6) Assignment Open Link: a_{ij} ≤ y_i
        for fac in non_market_facs:
            for mkt in markets:
                prob += a[(fac.id, mkt.id)] <= y[fac.id], f"link_assign_open_{fac.id}_{mkt.id}"

        # (C7) Bidirectional Flow Assignment Linking.
        #
        # An assignment is a horizon decision, so it links the horizon's flow:
        # the point of single sourcing is that a market has ONE source, not a
        # different one each month.
        market_demand_total: Dict[str, float] = {}
        for d in network.demands:
            market_demand_total[d.market_id] = (
                market_demand_total.get(d.market_id, 0.0) + float(d.quantity))
        for fac in non_market_facs:
            for mkt in markets:
                mkt_demand = market_demand_total.get(mkt.id, 0.0)
                out_arcs = arcs_by_origin_dest.get((fac.id, mkt.id), [])
                if out_arcs and mkt_demand > 0:
                    # (C7a) Lower bound: Flow > 0 forces a_{ij} = 1
                    prob += (
                        pulp.lpSum(x[key] for key in out_arcs) <= mkt_demand * a[(fac.id, mkt.id)],
                        f"link_flow_assign_{fac.id}_{mkt.id}",
                    )
                    # (C7b) Upper bound: a_{ij} = 1 forces Flow > 0 (zero flow forces a_{ij} = 0)
                    big_m = max(1000.0, 1.0 / max(mkt_demand, 1e-4))
                    prob += (
                        a[(fac.id, mkt.id)] <= big_m * pulp.lpSum(x[key] for key in out_arcs),
                        f"link_assign_flow_ub_{fac.id}_{mkt.id}",
                    )
                else:
                    prob += a[(fac.id, mkt.id)] == 0, f"link_assign_zero_{fac.id}_{mkt.id}"

        # (C8) Sourcing Policy Constraint (SINGLE or DUAL)
        if config.sourcing_policy == SourcingPolicy.SINGLE:
            for mkt in markets:
                prob += (
                    pulp.lpSum(a[(fac.id, mkt.id)] for fac in non_market_facs if (fac.id, mkt.id) in a) == 1,
                    f"single_sourcing_{mkt.id}",
                )
        elif config.sourcing_policy == SourcingPolicy.DUAL:
            for mkt in markets:
                eligible_facs = [fac for fac in non_market_facs if (fac.id, mkt.id) in a]
                if len(eligible_facs) >= 2:
                    prob += (
                        pulp.lpSum(a[(fac.id, mkt.id)] for fac in eligible_facs) == 2,
                        f"dual_sourcing_{mkt.id}",
                    )
                else:
                    # Require exactly two sources; if unavailable force infeasible
                    prob += (
                        pulp.lpSum(a[(fac.id, mkt.id)] for fac in eligible_facs) == 2,
                        f"dual_sourcing_insufficient_{mkt.id}",
                    )

    # (C12) CapEx Budget Constraint: Σ capex_i * y_i <= budget_capex
    if config.budget_capex is not None:
        capex_terms = [
            (fac, fac.capex) for fac in non_market_facs if fac.capex and fac.capex > 0
        ]
        if capex_terms:
            prob += (
                pulp.lpSum(capex * y[fac.id] for fac, capex in capex_terms) <= config.budget_capex,
                "C_capex_budget",
            )

    # Max facilities constraint
    if config.max_facilities is not None:
        dc_open_vars = [y[f.id] for f in non_market_facs if f.role == NodeRole.DC and f.id in y]
        if dc_open_vars:
            prob += pulp.lpSum(dc_open_vars) <= config.max_facilities, "max_facilities"

    # 6. Solve PuLP Model
    solver_meta = _run_pulp_solver(prob, config)

    # 7. Extract Decision Vectors
    facility_decisions: List[FacilityDecision] = []
    assignment_decisions: List[AssignmentDecision] = []
    flow_decisions: List[FlowDecision] = []
    inventory_decisions: List[InventoryDecision] = []
    obj_components: Dict[str, float] = {}
    evaluated_total: Optional[float] = None

    total_fc = 0.0
    total_oc = 0.0
    total_hc = 0.0
    total_cc = 0.0   # closure cost (V1.4)
    total_hold = 0.0  # cost of stock carried between periods
    closure_eligible_ids = {fac.id for fac in closure_eligible_facs}

    def lane_unit_cost(ln, prod, cfg):
        return ln.rate_per_unit

    if solver_meta.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE):
        # Stock first: a facility's holding cost is part of what it costs.
        held_by_facility: Dict[str, float] = {}
        for (fac_id, prod_id, period), var in I.items():
            units = pulp.value(var) or 0.0
            if units <= 1e-6:
                continue
            fac = facility_map.get(fac_id)
            stated = getattr(fac, "storage_capacity_units", None) if fac else None
            cost = holding_cost_per_unit.get(prod_id, 0.0) * units
            total_hold += cost
            held_by_facility[fac_id] = held_by_facility.get(fac_id, 0.0) + cost
            inventory_decisions.append(InventoryDecision(
                facility_id    = fac_id,
                facility_name  = fac.name if fac else fac_id,
                product_id     = prod_id,
                period         = int(period) if isinstance(period, int) else period,
                units          = round(units, 4),
                holding_cost   = round(cost, 4),
                headroom_units = (round(float(stated) - units, 4)
                                  if (stated is not None and stated > 0) else None),
            ))
        inventory_decisions.sort(key=lambda d: (d.facility_id, d.product_id, d.period))

        # Per-period throughput, so utilization can be reported against the
        # per-period capacity it is actually measured against.
        throughput_by_fac_period: Dict[Tuple[str, Any], float] = {}
        for key in arc_set:
            if key not in x:
                continue
            val = pulp.value(x[key]) or 0.0
            if val > 1e-6:
                fp = (key[0], key[4])
                throughput_by_fac_period[fp] = throughput_by_fac_period.get(fp, 0.0) + val

        for fac in non_market_facs:
            y_val = pulp.value(y[fac.id]) or 0.0
            is_open = y_val > 0.5
            # Charged in every period the facility is open, mirroring the
            # objective term exactly.
            fixed_c = (fac.get_fixed_cost_for_period(config.cost_period) * n_periods
                       if is_open else 0.0)
            open_c  = fac.opening_cost if (is_open and fac.is_candidate) else 0.0

            by_period = {p: throughput_by_fac_period.get((fac.id, p), 0.0) for p in periods}
            outbound_flow = sum(by_period.values())
            hand_c = fac.handling_cost_per_unit * outbound_flow if is_open else 0.0
            hold_c = held_by_facility.get(fac.id, 0.0) if is_open else 0.0

            # Mirror the closure term added to the objective exactly: charged
            # once, only for eligible facilities that ended up closed.
            close_c = (
                fac.closure_cost
                if (not is_open and fac.id in closure_eligible_ids)
                else 0.0
            )

            total_fc += fixed_c
            total_oc += open_c
            total_hc += hand_c
            total_cc += close_c

            # `capacity_units` is the HORIZON capacity, so it stays the honest
            # denominator of the horizon throughput beside it. The single worst
            # period is reported separately, because that is the number that
            # decides whether the footprint works and the one an average hides:
            # a DC at 50% for eleven months and 140% in December is not a DC at
            # 57%.
            per_period_cap = fac.capacity_units_per_period
            horizon_cap = (per_period_cap * n_periods
                           if (per_period_cap and per_period_cap > 0) else per_period_cap)
            util_pct = (outbound_flow / horizon_cap * 100.0) if (is_open and horizon_cap and horizon_cap > 0) else 0.0
            peak_util = (max(by_period.values()) / per_period_cap * 100.0
                         if (is_open and per_period_cap and per_period_cap > 0 and by_period)
                         else 0.0)

            markets_served = sum(
                1 for mkt in markets
                if any((pulp.value(x[k]) or 0.0) > 1e-6
                       for k in arcs_by_origin_dest.get((fac.id, mkt.id), []))
            )

            facility_decisions.append(FacilityDecision(
                facility_id             = fac.id,
                facility_name           = fac.name,
                role                    = fac.role,
                status                  = (fac.status.value if hasattr(fac.status, "value")
                                           else str(fac.status)),
                is_open                 = is_open,
                throughput_units        = round(outbound_flow, 4),
                capacity_units          = horizon_cap,
                utilization_pct         = round(util_pct, 2),
                peak_utilization_pct    = round(peak_util, 2),
                n_periods               = n_periods,
                throughput_by_period    = {str(p): round(v, 4) for p, v in by_period.items()},
                fixed_cost              = round(fixed_c, 4),
                opening_cost            = round(open_c, 4),
                closure_cost            = round(close_c, 4),
                handling_cost           = round(hand_c, 4),
                holding_cost            = round(hold_c, 4),
                total_facility_cost     = round(fixed_c + open_c + close_c + hand_c + hold_c, 4),
                n_markets_served        = markets_served,
                latitude                = fac.latitude,
                longitude               = fac.longitude,
            ))

        for fac in non_market_facs:
            for mkt in markets:
                if (fac.id, mkt.id) in a:
                    a_val = pulp.value(a[(fac.id, mkt.id)]) or 0.0
                    is_assigned = a_val > 0.5
                else:
                    is_assigned = any((pulp.value(x[k]) or 0.0) > 1e-6
                                      for k in arcs_by_origin_dest.get((fac.id, mkt.id), []))

                inv_cost_pair = 0.0
                if is_assigned and config.enable_inventory:
                    coeff = inv_coeffs.get((fac.id, mkt.id))
                    if coeff:
                        inv_cost_pair = coeff.total_inventory_cost

                assignment_decisions.append(AssignmentDecision(
                    facility_id    = fac.id,
                    market_id      = mkt.id,
                    is_assigned    = is_assigned,
                    inventory_cost = round(inv_cost_pair, 4),
                ))

        total_tc = 0.0
        total_co2 = 0.0

        for (key, ln, prod) in arcs:
            flow_val = pulp.value(x[key]) or 0.0
            if flow_val > 1e-6:
                tc = lane_unit_cost(ln, prod, config) * flow_val
                co2 = carbon_mod.compute_unit_co2(ln, prod) * flow_val
                total_tc += tc
                total_co2 += co2

                cap_lane = ln.lane_capacity
                util_pct_arc = (flow_val / cap_lane * 100.0) if (cap_lane and cap_lane > 0) else 0.0

                flow_decisions.append(FlowDecision(
                    origin_id           = key[0],
                    destination_id      = key[1],
                    mode                = ln.mode,
                    product_id          = key[3],
                    # The period this flow actually moves in. It was hardcoded
                    # to 1, which was true only because nothing else existed.
                    period              = key[4],
                    flow_units          = round(flow_val, 4),
                    distance_km         = ln.distance_km,
                    lead_time_days      = ln.lead_time_days,
                    rate_per_unit       = ln.rate_per_unit,
                    transport_cost      = round(tc, 4),
                    carbon_kg           = round(co2, 6),
                    lane_capacity       = ln.lane_capacity,
                    arc_utilization_pct = util_pct_arc,
                ))

        total_shortage_cost = 0.0
        if config.allow_shortage:
            # Must mirror shortage_cost_term above, including the demand-priority
            # multiplier. Without it, objective_components would not sum to the
            # solver objective whenever a priority > 1 market goes short.
            for key_u, var in u.items():
                uval = pulp.value(var) or 0.0
                priority_mult = 1.0 + (demand_priority_map.get((key_u[0], key_u[1]), 1) - 1) * 0.5
                total_shortage_cost += config.shortage_penalty * priority_mult * uval

        total_ic = 0.0
        if config.enable_inventory:
            # A pair with no per-product coefficient carries a per-PAIR figure,
            # so it is charged once for the pair. Charging it per flow decision
            # meant a horizon of twelve periods paid it twelve times for the
            # same pair, against an objective that charges it once.
            pair_charged: Set[Tuple[str, str]] = set()
            for fl in flow_decisions:
                pair = (fl.origin_id, fl.destination_id)
                coeff = inv_coeffs.get(pair)
                if coeff and coeff.unit_inv_cost_by_product:
                    unit_ic = coeff.unit_inv_cost_by_product.get(fl.product_id, 0.0)
                    total_ic += unit_ic * fl.flow_units
                elif coeff and pair not in pair_charged:
                    pair_charged.add(pair)
                    total_ic += coeff.total_inventory_cost

        # Carbon cost must mirror the objective terms actually added to the solver
        # (carbon_cost_term + carbon_objective_term are ADDITIVE, not multiplied).
        carbon_cost = 0.0
        if config.enable_carbon_cost:
            carbon_cost += total_co2 * config.carbon_price
        is_weighted_carbon_mode = (
            hasattr(config, "objective_mode") and
            (config.objective_mode.value if hasattr(config.objective_mode, "value") else str(config.objective_mode)) == "WEIGHTED_COST_CARBON"
        )
        if is_weighted_carbon_mode:
            carbon_cost += total_co2 * config.carbon_weight
        carbon_cost = round(carbon_cost, 4)

        obj_components = {
            "facility_cost":  round(total_fc, 4),
            "opening_cost":   round(total_oc, 4),
            "closure_cost":   round(total_cc, 4),
            "transport_cost": round(total_tc, 4),
            "handling_cost":  round(total_hc, 4),
            "inventory_cost": round(total_ic, 4),
            "holding_cost":   round(total_hold, 4),
            "shortage_cost":  round(total_shortage_cost, 4),
            "carbon_cost":    carbon_cost,
            "carbon_kg":      round(total_co2, 6),
        }

        raw_obj_val = solver_meta.objective_value if solver_meta and solver_meta.objective_value is not None else (
            total_fc + total_oc + total_cc + total_tc + total_hc + total_ic
            + total_hold + total_shortage_cost + carbon_cost
        )
        evaluated_total = round(raw_obj_val, 4)

    res = OptimizationResult(
        run_id                        = run_id,
        scenario_id                   = scenario_id,
        network_id                    = network.network_id,
        data_version                  = network.data_version,
        solver                        = solver_meta,
        facility_decisions            = facility_decisions,
        flow_decisions                = flow_decisions,
        assignment_decisions          = assignment_decisions,
        inventory_decisions           = inventory_decisions,
        objective_components          = obj_components,
        evaluated_total_cost          = evaluated_total,
        result_type                   = "OPTIMIZED" if scenario_id != "BASELINE" else "BASELINE",
        optimization_mode             = mode_policy.mode.value,
        is_hypothetical               = mode_policy.is_hypothetical,
        inventory_method              = "DIRECT_MILP",
        inventory_optimization_status = "INTEGRATED",
        inventory_iterations          = 1,
    )

    from netgravity.metrics.kpis import compute_kpis, compute_flow_analytics
    res.kpis = compute_kpis(res, network)
    res.flow_analytics = compute_flow_analytics(res, network)
    res.service_report = _build_service_report(
        res, network, config, sla_excluded_lanes, n_lanes_evaluated,
    )
    # Surface declared-but-inert settings on the solver metadata too, so a
    # caller inspecting only the solver block still sees them.
    for feature in res.service_report.unsupported_features:
        if feature not in solver_meta.warnings:
            solver_meta.warnings.append(feature)

    # Carrying stock between periods is only priced where the client stated
    # what the goods are worth. Where they did not, the model can smooth a peak
    # for nothing — which is a real property of the plan and has to be said, not
    # patched over with an assumed carrying rate.
    if multi_period:
        unpriced = [p.id for p in network.products
                    if holding_cost_per_unit.get(p.id, 0.0) <= 0]
        if unpriced and stock_facs:
            solver_meta.warnings.append(
                f"{len(unpriced)} product(s) state no unit value, so holding stock "
                f"between periods costs this model nothing: "
                f"{', '.join(unpriced[:5])}"
                f"{'…' if len(unpriced) > 5 else ''}. The plan may pre-build ahead "
                f"of a peak more freely than the real network would. No carrying "
                f"rate has been assumed on their behalf."
            )
    return res


# ---------------------------------------------------------------------------
# Service methodology reporting (V1.4)
# ---------------------------------------------------------------------------

def _build_service_report(
    result:            OptimizationResult,
    network:           CanonicalNetwork,
    config:            OptimizationConfig,
    sla_excluded:      List[Dict[str, object]],
    n_lanes_evaluated: int,
) -> "ServiceReport":
    """
    State explicitly how service was enforced, and name anything that was
    declared but NOT enforced.

    The V1 methodology is transit-time SLA feasibility and nothing else. This
    function exists so a result can never imply CSL / fill-rate / OTIF /
    service-penalty optimization that the MILP does not perform.
    """
    from netgravity.schemas.network import (
        V1_SUPPORTED_OBJECTIVE_MODES,
        V1_SUPPORTED_SERVICE_METRICS,
    )
    from netgravity.schemas.results import ServiceReport, ServiceViolationRecord

    metric_val = config.service_metric
    metric_str = metric_val.value if hasattr(metric_val, "value") else str(metric_val)
    obj_val = config.objective_mode
    obj_str = obj_val.value if hasattr(obj_val, "value") else str(obj_val)
    sla_mode_val = getattr(config, "sla_mode", "LAST_MILE")
    sla_mode_str = sla_mode_val.value if hasattr(sla_mode_val, "value") else str(sla_mode_val)

    unsupported: List[str] = []
    metric_supported = metric_str in V1_SUPPORTED_SERVICE_METRICS
    if not metric_supported:
        unsupported.append(
            f"service_metric={metric_str} is declared but NOT implemented as an "
            f"optimization constraint in V1. Service feasibility was enforced via "
            f"transit-time SLA only; no {metric_str} constraint or objective term "
            f"was added."
        )
    if obj_str not in V1_SUPPORTED_OBJECTIVE_MODES:
        unsupported.append(
            f"objective_mode={obj_str} is declared but NOT implemented in V1. No "
            f"corresponding constraint or objective term was added; the solve "
            f"behaved as COST_MIN plus any explicitly configured carbon terms."
        )

    kpis = result.kpis
    total_demand = kpis.total_demand if kpis else sum(d.quantity for d in network.demands)
    served = kpis.total_served if kpis else 0.0
    unserved = kpis.unmet_demand if kpis else 0.0
    pct_sla = kpis.pct_demand_in_sla if kpis else 0.0

    violations: List[ServiceViolationRecord] = []
    if config.verbose:
        # Diagnostics-only: the excluded arcs never carried flow (they were
        # removed pre-solve), so flow_units stays 0.
        for rec in sla_excluded:
            violations.append(ServiceViolationRecord(
                origin_id          = str(rec["origin_id"]),
                destination_id     = str(rec["destination_id"]),
                mode               = str(rec["mode"]),
                lead_time_days     = float(rec["lead_time_days"]),
                sla_days           = float(rec["sla_days"]),
                excess_days        = float(rec["excess_days"]),
                flow_units         = 0.0,
                excluded_pre_solve = True,
            ))

    return ServiceReport(
        methodology              = "TRANSIT_TIME_SLA_FEASIBILITY",
        sla_enforced             = bool(config.enforce_sla),
        sla_mode                 = sla_mode_str,
        service_metric           = metric_str,
        service_metric_supported = metric_supported,
        unsupported_features     = unsupported,
        total_demand             = round(total_demand, 4),
        served_demand            = round(served, 4),
        unserved_demand          = round(unserved, 4),
        demand_within_sla        = round(total_demand * pct_sla / 100.0, 4),
        pct_demand_in_sla        = round(pct_sla, 4),
        n_lanes_evaluated        = n_lanes_evaluated,
        n_lanes_sla_excluded     = len(sla_excluded),
        violations               = violations,
    )


# ---------------------------------------------------------------------------
# PuLP Solver Invocation Helper
# ---------------------------------------------------------------------------

def _run_pulp_solver(prob: pulp.LpProblem, config: OptimizationConfig) -> SolverMetadata:
    """Invoke specified PuLP solver and construct SolverMetadata."""
    import time
    from datetime import datetime

    solver_name = config.solver_name.upper()
    start_time = time.perf_counter()

    n_vars = len(prob.variables())
    n_cons = len(prob.constraints)
    n_bin = sum(1 for v in prob.variables() if getattr(v, "cat", None) in ("Binary", pulp.LpBinary) or str(getattr(v, "cat", "")) == "Binary")
    warnings_list: List[str] = []

    # An absolute tolerance, when the caller set one. A solver stops as soon as
    # EITHER tolerance is met, so the relative gap is tightened here to let the
    # absolute one govern — otherwise setting it changes nothing.
    gap_abs = getattr(config, "mip_gap_abs", None)
    gap_rel = 1e-9 if gap_abs is not None else config.mip_gap
    abs_kw = {"gapAbs": gap_abs} if gap_abs is not None else {}

    try:
        if solver_name == "HIGHS":
            solver = pulp.HiGHS(
                timeLimit=config.time_limit_seconds,
                gapRel=gap_rel,
                msg=config.verbose,
                **abs_kw,
            )
        elif solver_name == "GUROBI":
            solver = pulp.GUROBI(
                timeLimit=config.time_limit_seconds,
                mipGap=gap_rel,
                msg=config.verbose,
                **({"mipGapAbs": gap_abs} if gap_abs is not None else {}),
            )
        elif solver_name == "CPLEX":
            solver = pulp.CPLEX_CMD(
                timeLimit=config.time_limit_seconds,
                gapRel=gap_rel,
                msg=config.verbose,
                **abs_kw,
            )
        else:
            solver = pulp.PULP_CBC_CMD(
                timeLimit=config.time_limit_seconds,
                gapRel=gap_rel,
                msg=config.verbose,
                **abs_kw,
            )
    except Exception as exc:
        warn_msg = f"Could not initialize solver {solver_name}, falling back to CBC: {exc}"
        logger.warning(warn_msg)
        warnings_list.append(warn_msg)
        solver = pulp.PULP_CBC_CMD(
            timeLimit=config.time_limit_seconds,
            gapRel=gap_rel,
            msg=config.verbose,
            **abs_kw,
        )

    try:
        prob.solve(solver)
    except Exception as exc:
        logger.error(f"Solver invocation failed: {exc}")
        runtime_sec = round(time.perf_counter() - start_time, 4)
        return SolverMetadata(
            solver_name=solver_name,
            solver_version=getattr(solver, "version", "1.0"),
            status=SolverStatus.ERROR,
            runtime_seconds=runtime_sec,
            n_variables=n_vars,
            n_constraints=n_cons,
            n_binary=n_bin,
            warnings=[f"Solver exception: {exc}"],
        )

    runtime_sec = round(time.perf_counter() - start_time, 4)

    status_str = pulp.LpStatus[prob.status].upper()
    status_enum = SolverStatus.ERROR
    if status_str == "OPTIMAL":
        status_enum = SolverStatus.OPTIMAL
    elif status_str in ("INFEASIBLE", "UNBOUNDED"):
        status_enum = SolverStatus.INFEASIBLE
    elif status_str == "NOT SOLVED":
        status_enum = SolverStatus.TIME_LIMIT

    raw_obj = pulp.value(prob.objective)
    obj_val = float(raw_obj) if raw_obj is not None else None

    best_bnd = getattr(prob, "bestBound", None)
    if best_bnd is None:
        best_bnd = obj_val

    achieved_gap = config.mip_gap if status_enum == SolverStatus.OPTIMAL else None

    meta = SolverMetadata(
        solver_name=solver_name,
        solver_version="1.0.0",
        status=status_enum,
        objective_value=obj_val,
        best_bound=best_bnd,
        mip_gap=achieved_gap,
        runtime_seconds=runtime_sec,
        n_variables=n_vars,
        n_constraints=n_cons,
        n_binary=n_bin,
        warnings=warnings_list,
        timestamp=datetime.now().isoformat(),
    )
    meta.optimality_label = meta.get_optimality_label()
    return meta


def _make_var(
    prob: pulp.LpProblem,
    name: str,
    lowBound: Optional[float] = 0,
    upBound: Optional[float] = None,
    cat: str = "Continuous",
) -> pulp.LpVariable:
    var = pulp.LpVariable(name, lowBound=lowBound, upBound=upBound, cat=cat)
    prob.addVariable(var)
    return var
