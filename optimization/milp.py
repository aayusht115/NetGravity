"""
NetGravity V1.2 — Core MILP Optimization Engine (Direct Inventory Formulation)
=============================================================================
Formulation Reference:
- Direct MILP Facility Location, Multi-Commodity Flow & Inventory Optimization.
- Precomputed safety stock coefficients IC_{ij} are integrated into the MILP objective
  using binary facility-market assignment variables a_{ij} ∈ {0,1}.
- Single-pass exact optimization (eliminates iterative fixed-point inventory loops).

Mathematical Formulation:
─────────────────────────
Indices:
    i ∈ F : Facilities (Plants, DCs)
    j ∈ M : Markets / Customers
    v ∈ V : Modes
    k ∈ K : Products

Decision Variables:
    y_i ∈ {0,1}       : Facility open status
    a_{ij} ∈ {0,1}    : Binary market assignment (facility i serves market j)
    x_{ijvk} ≥ 0      : Flow volume on arc (i,j,v,k)
    u_{jk} ≥ 0        : Unmet demand / shortage

Objective Function (Cost Minimization):
    Min Z = Σ f_i y_i + Σ o_i y_i + Σ c_{ijvk} x_{ijvk} + Σ h_i x_{ijvk}
          + Σ IC_{ij} a_{ij} + Σ p_k u_{jk}

Constraints:
    (C1)  Demand Balance:      Σ_{i,v} x_{ijvk} + u_{jk} = D_{jk}  ∀j,k
    (C2)  Capacity Bounds:     Σ_{j,v,k} x_{ijvk} ≤ Cap_i y_i      ∀i
    (C3)  Min Throughput:      Σ_{j,v,k} x_{ijvk} ≥ MinThru_i y_i  ∀i
    (C4)  Flow Conservation:   Σ_{in} x_{in} = Σ_{out} x_{out}     ∀i ∈ DCs
    (C5a) Mandatory Open:      y_i = 1                             ∀i ∈ Mandatory
    (C5b) Forced Closed:       y_i = 0                             ∀i ∈ Closed
    (C6)  Assign Open Link:    a_{ij} ≤ y_i                        ∀i,j
    (C7)  Flow Assign Link:    Σ_{v,k} x_{ijvk} ≤ D_j a_{ij}       ∀i,j
    (C8)  Single Sourcing:     Σ_i a_{ij} = 1                      ∀j (if enabled)
"""

from __future__ import annotations

import logging
import uuid
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

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
    SourcingPolicy,
    TransportMode,
)
from netgravity.schemas.results import (
    AssignmentDecision,
    FacilityDecision,
    FlowDecision,
    OptimizationResult,
    SolverMetadata,
    SolverStatus,
)

logger = logging.getLogger(__name__)

# Type aliases
ArcKey          = Tuple[str, str, str, str]    # (origin, dest, mode, product)
InvCoefficients = Dict[str, float]             # fac_id -> inventory_cost_per_period


def solve(
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

    # Fast-fail network topology validation check
    from netgravity.validation.checks import validate_network
    report = validate_network(network)
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
                network_id=network.network_id,
                data_version=network.data_version,
                result_type="BASELINE" if scenario_id is None else "SCENARIO",
                solver=solver_meta,
                facility_decisions=[],
                flow_decisions=[],
                assignment_decisions=[],
                evaluated_total_cost=None,
            )

    return _solve_milp(
        network=network,
        config=config,
        scenario_id=scenario_id,
        iteration=0,
    )


# ---------------------------------------------------------------------------
# Core MILP Builder & Solver (V1.2 Direct Formulation)
# ---------------------------------------------------------------------------

def _solve_milp(
    network: CanonicalNetwork,
    config: OptimizationConfig,
    scenario_id: Optional[str],
    iteration: int = 0,
) -> OptimizationResult:
    """Construct and solve the single-pass direct MILP optimization model."""
    run_id = str(uuid.uuid4())[:8]

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

    seen_keys: Set[ArcKey] = set()
    for lane in network.lanes:
        orig = facility_map.get(lane.origin_id)
        dest = facility_map.get(lane.destination_id)
        if not orig or not dest:
            continue

        if orig.status.name == "CLOSED" or dest.status.name == "CLOSED":
            continue

        mode_str = lane.mode.value if hasattr(lane.mode, "value") else str(lane.mode)

        if config.enforce_sla and orig.role not in market_roles and dest.role in market_roles:
            sla_mode_val = getattr(config, "sla_mode", "LAST_MILE")
            sla_mode_str = sla_mode_val.value if hasattr(sla_mode_val, "value") else str(sla_mode_val)
            sla_feasible = True

            for d in network.demands:
                if d.market_id == dest.id and d.sla_days is not None:
                    if sla_mode_str == "END_TO_END":
                        # Compute minimum inbound lead time to origin (DC) from any plant
                        plant_roles = {NodeRole.PLANT, NodeRole.SUPPLIER}
                        inbound_lts = [
                            ln.lead_time_days for ln in network.lanes
                            if ln.destination_id == orig.id and facility_map.get(ln.origin_id) and facility_map.get(ln.origin_id).role in plant_roles
                        ]
                        min_inbound_lt = min(inbound_lts) if inbound_lts else float("inf")
                        total_lt = min_inbound_lt + lane.lead_time_days
                        if total_lt > d.sla_days:
                            sla_feasible = False
                            break
                    else:
                        # LAST_MILE (default)
                        if lane.lead_time_days > d.sla_days:
                            sla_feasible = False
                            break

            if not sla_feasible:
                continue

        for prod in network.products:
            if orig.eligible_product_ids and prod.id not in orig.eligible_product_ids:
                continue
            if dest.eligible_product_ids and prod.id not in dest.eligible_product_ids:
                continue

            key: ArcKey = (lane.origin_id, lane.destination_id, mode_str, prod.id)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            rate = lane.rate_per_unit
            co2  = carbon_mod.compute_unit_co2(lane, prod)

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
    for fac in non_market_facs:
        for mkt in markets:
            a[(fac.id, mkt.id)] = _make_var(
                prob,
                f"a_{fac.id}_{mkt.id}",
                lowBound=0,
                upBound=1,
                cat="Binary",
            )

    # Decision variables: x_{ijvk} ≥ 0
    x: Dict[ArcKey, pulp.LpVariable] = {}
    for key, ln, prod in arcs:
        ub = (ln.lane_capacity
              if (ln.lane_capacity is not None and ln.lane_capacity > 0)
              else None)
        x[key] = _make_var(
            prob,
            f"x_{key[0]}_{key[1]}_{key[2]}_{key[3]}",
            lowBound=0,
            upBound=ub,
            cat="Continuous",
        )

    # Shortage variables u_{mk} ≥ 0
    u: Dict[Tuple[str, str], pulp.LpVariable] = {}
    if config.allow_shortage:
        for d in network.demands:
            key_u = (d.market_id, d.product_id)
            if key_u not in u:
                u[key_u] = _make_var(
                    prob,
                    f"u_{d.market_id}_{d.product_id}",
                    lowBound=0,
                    cat="Continuous",
                )

    # 4. Objective Terms
    facility_cost_term = pulp.lpSum(
        fac.get_fixed_cost_for_period(config.cost_period) * y[fac.id]
        for fac in non_market_facs
    )

    opening_cost_term = pulp.lpSum(
        fac.opening_cost * y[fac.id]
        for fac in non_market_facs
        if fac.is_candidate and fac.opening_cost > 0
    )

    transport_cost_term = pulp.lpSum(
        arc_unit_cost[key] * x[key]
        for key in arc_set
        if key in x
    )

    handling_cost_term = pulp.lpSum(
        fac.handling_cost_per_unit * pulp.lpSum(
            x[key]
            for key in arc_set
            if key[0] == fac.id and key in x
        )
        for fac in non_market_facs
        if fac.handling_cost_per_unit > 0
    )

    # Direct Inventory Cost Term: Σ IC[i,j] * a[i,j]
    if config.enable_inventory:
        inventory_cost_term = pulp.lpSum(
            inv_coeffs[(fac.id, mkt.id)].total_inventory_cost * a[(fac.id, mkt.id)]
            for fac in non_market_facs
            for mkt in markets
            if (fac.id, mkt.id) in inv_coeffs and inv_coeffs[(fac.id, mkt.id)].total_inventory_cost > 0
        )
    else:
        inventory_cost_term = 0

    shortage_cost_term = (
        pulp.lpSum(config.shortage_penalty * u[key_u] for key_u in u)
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

    if config.objective_mode == ObjectiveMode.WEIGHTED_COST_CARBON:
        carbon_objective_term = config.carbon_weight * pulp.lpSum(
            arc_unit_co2[key] * x[key]
            for key in arc_set
            if key in x
        )
    else:
        carbon_objective_term = 0

    prob += (
        facility_cost_term
        + opening_cost_term
        + transport_cost_term
        + handling_cost_term
        + inventory_cost_term
        + shortage_cost_term
        + carbon_cost_term
        + carbon_objective_term
    ), "TotalCost"

    # 5. Constraints

    # (C1) Demand Fulfillment
    for d in network.demands:
        inbound_keys = [
            key for key in arc_set
            if key[1] == d.market_id and key[3] == d.product_id and key in x
        ]

        if inbound_keys and config.allow_shortage:
            key_u = (d.market_id, d.product_id)
            prob += (
                pulp.lpSum(x[k] for k in inbound_keys) + u[key_u] == d.quantity,
                f"demand_{d.market_id}_{d.product_id}",
            )
        elif inbound_keys:
            prob += (
                pulp.lpSum(x[k] for k in inbound_keys) == d.quantity,
                f"demand_{d.market_id}_{d.product_id}",
            )
        else:
            if config.allow_shortage:
                key_u = (d.market_id, d.product_id)
                prob += (
                    u[key_u] == d.quantity,
                    f"demand_unreachable_{d.market_id}_{d.product_id}",
                )
            else:
                dummy_infeas = _make_var(prob, f"infeas_dummy_{d.market_id}_{d.product_id}", lowBound=0, upBound=0)
                prob += (
                    dummy_infeas == d.quantity,
                    f"demand_unreachable_force_infeasible_{d.market_id}_{d.product_id}",
                )

    # (C2 & C3) Capacity and Min Throughput
    for fac in non_market_facs:
        outbound_keys = [key for key in arc_set if key[0] == fac.id and key in x]
        if outbound_keys:
            outbound_sum = pulp.lpSum(x[key] for key in outbound_keys)
            cap_vals = [
                c for c in (fac.capacity_units_per_period, fac.production_capacity_units_per_period)
                if c is not None
            ]
            if cap_vals:
                eff_cap = min(cap_vals)
                prob += outbound_sum <= eff_cap * y[fac.id], f"cap_{fac.id}"

            min_thru = fac.min_throughput_per_period
            if config.minimum_throughput_enabled and min_thru is not None and min_thru > 0:
                prob += outbound_sum >= min_thru * y[fac.id], f"min_thru_{fac.id}"

    # (C4) Flow Conservation at Intermediate Facilities (DCs, Depots, Warehouses, Cross-docks)
    intermediate_roles = {NodeRole.DC, NodeRole.DEPOT, NodeRole.WAREHOUSE, NodeRole.CROSS_DOCK, NodeRole.DARKSTORE}
    intermediate_facs = [f for f in non_market_facs if f.role in intermediate_roles]
    for fac in intermediate_facs:
        for prod in network.products:
            inbound = [key for key in arc_set if key[1] == fac.id and key[3] == prod.id and key in x]
            outbound = [key for key in arc_set if key[0] == fac.id and key[3] == prod.id and key in x]
            # Flow conservation applies strictly to ALL transshipment facilities.
            # If inbound is empty, outbound flow must equal 0 (no phantom supply).
            if outbound:
                prob += (
                    pulp.lpSum(x[k] for k in inbound) == pulp.lpSum(x[k] for k in outbound),
                    f"flow_bal_{fac.id}_{prod.id}",
                )

    # (C5a & C5b) Mandatory and Forced Closed
    for fac in non_market_facs:
        if fac.is_forced_closed:
            prob += y[fac.id] == 0, f"forced_closed_{fac.id}"
        elif fac.is_mandatory:
            prob += y[fac.id] == 1, f"mandatory_open_{fac.id}"

    # (C6) Assignment Open Link: a_{ij} ≤ y_i
    for fac in non_market_facs:
        for mkt in markets:
            prob += a[(fac.id, mkt.id)] <= y[fac.id], f"link_assign_open_{fac.id}_{mkt.id}"

    # (C7) Bidirectional Flow Assignment Linking
    for fac in non_market_facs:
        for mkt in markets:
            mkt_demand = sum(d.quantity for d in network.demands if d.market_id == mkt.id)
            out_arcs = [key for key in arc_set if key[0] == fac.id and key[1] == mkt.id and key in x]
            if out_arcs and mkt_demand > 0:
                # (C7a) Lower bound: Flow > 0 forces a_{ij} = 1
                prob += (
                    pulp.lpSum(x[key] for key in out_arcs) <= mkt_demand * a[(fac.id, mkt.id)],
                    f"link_flow_assign_{fac.id}_{mkt.id}",
                )
                # (C7b) Upper bound: a_{ij} = 1 forces Flow > 0 (zero flow forces a_{ij} = 0)
                prob += (
                    a[(fac.id, mkt.id)] <= 1e8 * pulp.lpSum(x[key] for key in out_arcs),
                    f"link_assign_flow_ub_{fac.id}_{mkt.id}",
                )
            else:
                # If no outbound arcs exist or demand is 0, assignment MUST be 0
                prob += a[(fac.id, mkt.id)] == 0, f"link_assign_zero_{fac.id}_{mkt.id}"

    # (C8) Single Sourcing Constraint (if policy == SINGLE)
    if config.sourcing_policy == SourcingPolicy.SINGLE:
        for mkt in markets:
            prob += (
                pulp.lpSum(a[(fac.id, mkt.id)] for fac in non_market_facs) == 1,
                f"single_sourcing_{mkt.id}",
            )

    # Max facilities constraint
    if config.max_facilities is not None:
        dc_open_vars = [y[f.id] for f in non_market_facs if f.role == NodeRole.DC and f.id in y]
        if dc_open_vars:
            prob += pulp.lpSum(dc_open_vars) <= config.max_facilities, "max_facilities"

    # 6. Solve Problem
    solver_meta = _run_pulp_solver(prob, config)

    facility_decisions: List[FacilityDecision]   = []
    flow_decisions:     List[FlowDecision]       = []
    assignment_decisions: List[AssignmentDecision] = []
    obj_components:     Dict[str, float]         = {}
    evaluated_total:    Optional[float]          = None

    if solver_meta.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE, SolverStatus.TIME_LIMIT):

        # Facility decisions & throughput calculation
        fac_throughput: Dict[str, float] = {}
        for (key, ln, prod) in arcs:
            if key in x:
                val = pulp.value(x[key]) or 0.0
                if val > 1e-6:
                    fac_throughput[key[0]] = fac_throughput.get(key[0], 0.0) + val

        # Assignment decisions collection & assigned inventory cost calculation
        assigned_inv_cost_by_fac: Dict[str, float] = {}
        for (fac_id, mkt_id), var in a.items():
            is_ass = (pulp.value(var) or 0.0) > 0.5
            coeff = inv_coeffs.get((fac_id, mkt_id))
            ss_units = coeff.total_safety_stock_units if (is_ass and coeff) else 0.0
            ic_cost  = coeff.total_inventory_cost if (is_ass and coeff) else 0.0

            if is_ass:
                assigned_inv_cost_by_fac[fac_id] = assigned_inv_cost_by_fac.get(fac_id, 0.0) + ic_cost

            assignment_decisions.append(AssignmentDecision(
                facility_id=fac_id,
                market_id=mkt_id,
                is_assigned=is_ass,
                safety_stock_units=round(ss_units, 4),
                inventory_cost=round(ic_cost, 4),
            ))

        total_fc = 0.0
        total_oc = 0.0
        total_hc = 0.0
        for fac in non_market_facs:
            y_val = pulp.value(y[fac.id]) or 0.0
            is_open = y_val > 0.5

            fc = fac.get_fixed_cost_for_period(config.cost_period) if is_open else 0.0
            oc = fac.opening_cost if (is_open and fac.is_candidate) else 0.0
            throughput = fac_throughput.get(fac.id, 0.0)
            hc = fac.handling_cost_per_unit * throughput if is_open else 0.0
            fac_ic = assigned_inv_cost_by_fac.get(fac.id, 0.0) if is_open else 0.0

            total_fc += fc
            total_oc += oc
            total_hc += hc

            cap = fac.capacity_units_per_period or 1e12
            util_pct = (throughput / cap * 100.0) if cap > 0 and cap < 1e11 else 0.0

            facility_decisions.append(FacilityDecision(
                facility_id         = fac.id,
                facility_name       = fac.name,
                role                = fac.role.value,
                is_open             = is_open,
                throughput_units    = round(throughput, 4),
                capacity_units      = cap if cap < 1e11 else 0.0,
                utilization_pct     = round(util_pct, 2),
                fixed_cost          = round(fc, 4),
                handling_cost       = round(hc, 4),
                opening_cost        = round(oc, 4),
                inventory_cost      = round(fac_ic, 4),
                total_facility_cost = round(fc + hc + oc + fac_ic, 4),
            ))

        # Flow decisions
        total_tc  = 0.0
        total_co2 = 0.0
        for (key, ln, prod) in arcs:
            if key not in x:
                continue
            flow_val = pulp.value(x[key]) or 0.0
            if flow_val < 1e-6:
                continue

            tc  = arc_unit_cost[key] * flow_val
            co2 = arc_unit_co2[key]  * flow_val
            total_tc  += tc
            total_co2 += co2

            util_pct_arc = None
            if ln.lane_capacity and ln.lane_capacity > 0:
                util_pct_arc = round(flow_val / ln.lane_capacity * 100, 2)

            flow_decisions.append(FlowDecision(
                origin_id           = key[0],
                destination_id      = key[1],
                mode                = key[2],
                product_id          = key[3],
                period              = 1,
                flow_units          = round(flow_val, 4),
                distance_km         = ln.distance_km,
                lead_time_days      = ln.lead_time_days,
                rate_per_unit       = ln.rate_per_unit,
                transport_cost      = round(tc, 4),
                carbon_kg           = round(co2, 6),
                lane_capacity       = ln.lane_capacity,
                arc_utilization_pct = util_pct_arc,
            ))

        # Shortage
        total_shortage_cost = 0.0
        if config.allow_shortage:
            for key_u, var in u.items():
                uval = pulp.value(var) or 0.0
                total_shortage_cost += config.shortage_penalty * uval

        total_ic = sum(ad.inventory_cost for ad in assignment_decisions if ad.is_assigned)

        obj_components = {
            "facility_cost":  round(total_fc, 4),
            "opening_cost":   round(total_oc, 4),
            "transport_cost": round(total_tc, 4),
            "handling_cost":  round(total_hc, 4),
            "inventory_cost": round(total_ic, 4),
            "shortage_cost":  round(total_shortage_cost, 4),
            "carbon_kg":      round(total_co2, 6),
        }

        evaluated_total = round(
            total_fc + total_oc + total_tc + total_hc + total_ic + total_shortage_cost, 4
        )

    res = OptimizationResult(
        run_id                        = run_id,
        scenario_id                   = scenario_id,
        network_id                    = network.network_id,
        data_version                  = network.data_version,
        solver                        = solver_meta,
        facility_decisions            = facility_decisions,
        flow_decisions                = flow_decisions,
        assignment_decisions          = assignment_decisions,
        objective_components          = obj_components,
        evaluated_total_cost          = evaluated_total,
        result_type                   = "OPTIMIZED" if scenario_id != "BASELINE" else "BASELINE",
        inventory_method              = "DIRECT_MILP",
        inventory_optimization_status = "INTEGRATED",
        inventory_iterations          = 1,
    )

    from netgravity.metrics.kpis import compute_kpis, compute_flow_analytics
    res.kpis = compute_kpis(res, network)
    res.flow_analytics = compute_flow_analytics(res, network)
    return res


# ---------------------------------------------------------------------------
# PuLP Solver Invocation Helper
# ---------------------------------------------------------------------------

def _run_pulp_solver(prob: pulp.LpProblem, config: OptimizationConfig) -> SolverMetadata:
    """Invoke specified PuLP solver and construct SolverMetadata."""
    solver_name = config.solver_name.upper()

    try:
        if solver_name == "HIGHS":
            solver = pulp.HiGHS(
                timeLimit=config.time_limit_seconds,
                gapRel=config.mip_gap,
                msg=config.verbose,
            )
        elif solver_name == "GUROBI":
            solver = pulp.GUROBI(
                timeLimit=config.time_limit_seconds,
                mipGap=config.mip_gap,
                msg=config.verbose,
            )
        elif solver_name == "CPLEX":
            solver = pulp.CPLEX_CMD(
                timeLimit=config.time_limit_seconds,
                gapRel=config.mip_gap,
                msg=config.verbose,
            )
        else:
            solver = pulp.PULP_CBC_CMD(
                timeLimit=config.time_limit_seconds,
                gapRel=config.mip_gap,
                msg=config.verbose,
            )
    except Exception as exc:
        logger.warning(f"Could not initialize solver {solver_name}, falling back to CBC: {exc}")
        solver = pulp.PULP_CBC_CMD(
            timeLimit=config.time_limit_seconds,
            gapRel=config.mip_gap,
            msg=config.verbose,
        )

    try:
        prob.solve(solver)
    except Exception as exc:
        logger.error(f"Solver invocation failed: {exc}")
        return SolverMetadata(
            solver_name=solver_name,
            status=SolverStatus.ERROR,
            warnings=[f"Solver exception: {exc}"],
        )

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

    opt_label = "PROVEN_OPTIMAL" if status_enum == SolverStatus.OPTIMAL else status_str
    if config.mip_gap > 0 and status_enum == SolverStatus.OPTIMAL:
        opt_label = f"FEASIBLE_GAP_{config.mip_gap*100:.1f}%"

    return SolverMetadata(
        solver_name=solver_name,
        status=status_enum,
        objective_value=obj_val,
        optimality_label=opt_label,
        warnings=[],
    )


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
