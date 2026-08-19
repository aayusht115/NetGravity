"""
NetGravity — Facility Location Problem (MILP)
=============================================
Uncapacitated / Capacitated Facility Location using PuLP.

Mathematical Formulation (Capacitated):
    Sets:
        J = {j: 1..n_dcs}   candidate facility locations
        I = {i: 1..n_dests} demand nodes
    Parameters:
        f[j]    fixed annual cost of opening facility j
        c[i][j] unit transport cost from facility j to demand i
        d[i]    annual demand at node i
        M[j]    maximum throughput capacity of facility j
    Decision Variables:
        y[j] ∈ {0,1}     1 if facility j is opened
        x[i][j] ∈ [0,1]  fraction of demand i served by facility j
    Objective:
        min  Σⱼ f[j]·y[j]  +  Σᵢ Σⱼ c[i][j]·d[i]·x[i][j]
    Subject to:
        (Coverage)   Σⱼ x[i][j] = 1           ∀ i          (all demand covered)
        (Linking)    x[i][j] ≤ y[j]            ∀ i,j        (serve only from open facility)
        (Capacity)   Σᵢ d[i]·x[i][j] ≤ M[j]·y[j]  ∀ j     (throughput limit)
        (Binary)     y[j] ∈ {0,1}              ∀ j
        (Non-neg)    x[i][j] ≥ 0               ∀ i,j
"""

import warnings
import pulp

warnings.filterwarnings("ignore", category=DeprecationWarning)


def solve_facility_location(
    fixed_costs: list[float],       # f[j] ₹ lakh/year
    transport_costs: list[list[float]],  # c[i][j] cost per unit from DC j to demand i
    demand: list[float],            # d[i] units/day
    capacities: list[float],        # M[j] max throughput/day
    dc_ids: list[str] | None = None,
    dest_ids: list[str] | None = None,
    min_open: int = 1,              # minimum DCs that must be opened
    max_open: int | None = None,    # maximum DCs allowed
    time_limit_sec: int = 60,
) -> dict:
    """
    Solve the Capacitated Facility Location Problem using PuLP (CBC solver).
    """
    n_j = len(fixed_costs)       # number of candidate DCs
    n_i = len(demand)            # number of demand nodes

    dc_names   = dc_ids   if dc_ids   else [f"T{j+1}" for j in range(n_j)]
    dest_names = dest_ids if dest_ids else [f"D{i+1}" for i in range(n_i)]

    prob = pulp.LpProblem("Capacitated_Facility_Location", pulp.LpMinimize)

    # ---------------------------------------------------------------
    # Decision variables
    # ---------------------------------------------------------------
    y = {name: pulp.LpVariable(f"open_{name}", cat="Binary") for name in dc_names}
    x = {
        (dest_names[i], dc_names[j]): pulp.LpVariable(
            f"serve_{dest_names[i]}_{dc_names[j]}", lowBound=0, upBound=1, cat="Continuous"
        )
        for i in range(n_i) for j in range(n_j)
    }

    # ---------------------------------------------------------------
    # Objective
    # ---------------------------------------------------------------
    # Fixed cost term (annualized, converted to daily by /365 for comparability)
    annual_to_daily = 1 / 365
    prob += (
        pulp.lpSum(fixed_costs[j] * annual_to_daily * y[dc_names[j]] for j in range(n_j))
        + pulp.lpSum(
            transport_costs[j][i] * demand[i] * x[(dest_names[i], dc_names[j])]
            for i in range(n_i) for j in range(n_j)
        )
    ), "Total_Cost"

    # ---------------------------------------------------------------
    # Constraints
    # ---------------------------------------------------------------
    # 1. Coverage: all demand must be fully served
    for i in range(n_i):
        prob += (
            pulp.lpSum(x[(dest_names[i], dc_names[j])] for j in range(n_j)) == 1.0,
            f"Coverage_{dest_names[i]}"
        )

    # 2. Linking: can only serve from open facility
    for i in range(n_i):
        for j in range(n_j):
            prob += (
                x[(dest_names[i], dc_names[j])] <= y[dc_names[j]],
                f"Link_{dest_names[i]}_{dc_names[j]}"
            )

    # 3. Capacity: throughput ≤ capacity of open DC
    for j in range(n_j):
        prob += (
            pulp.lpSum(demand[i] * x[(dest_names[i], dc_names[j])] for i in range(n_i))
            <= capacities[j] * y[dc_names[j]],
            f"Capacity_{dc_names[j]}"
        )

    # 4. Min/max open DCs
    prob += (
        pulp.lpSum(y[dc_names[j]] for j in range(n_j)) >= min_open,
        "Min_Open"
    )
    if max_open is not None:
        prob += (
            pulp.lpSum(y[dc_names[j]] for j in range(n_j)) <= max_open,
            "Max_Open"
        )

    # ---------------------------------------------------------------
    # Solve
    # ---------------------------------------------------------------
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit_sec)
    status = prob.solve(solver)

    if pulp.LpStatus[prob.status] not in ("Optimal", "Not Solved"):
        return {
            "status": "infeasible",
            "message": pulp.LpStatus[prob.status],
            "open_dcs": [],
            "total_cost": None,
        }

    # ---------------------------------------------------------------
    # Extract results
    # ---------------------------------------------------------------
    open_dcs = [dc_names[j] for j in range(n_j) if pulp.value(y[dc_names[j]]) > 0.5]
    total_cost = pulp.value(prob.objective)

    # Assignment matrix
    assignment = {}
    for i in range(n_i):
        for j in range(n_j):
            val = pulp.value(x[(dest_names[i], dc_names[j])]) or 0.0
            if val > 0.01:
                assignment[dest_names[i]] = {
                    "dc": dc_names[j],
                    "fraction": round(val, 4),
                    "daily_units": round(val * demand[i], 2),
                }

    # Throughput per DC
    dc_throughput = {}
    for j in range(n_j):
        if dc_names[j] in open_dcs:
            tp = sum(
                demand[i] * (pulp.value(x[(dest_names[i], dc_names[j])]) or 0.0)
                for i in range(n_i)
            )
            dc_throughput[dc_names[j]] = {
                "throughput": round(tp, 2),
                "capacity": capacities[j],
                "utilization_pct": round(tp / capacities[j] * 100, 1) if capacities[j] > 0 else 0,
            }

    # Fixed cost savings vs. opening all DCs
    max_fixed_cost = sum(fixed_costs)
    actual_fixed   = sum(fixed_costs[j] for j in range(n_j) if dc_names[j] in open_dcs)
    fixed_savings  = max_fixed_cost - actual_fixed

    return {
        "status": pulp.LpStatus[prob.status],
        "model": "facility_location",
        "open_dcs": open_dcs,
        "n_open": len(open_dcs),
        "total_cost_daily": round(float(total_cost), 4),
        "fixed_cost_annual_lakh": round(actual_fixed, 2),
        "fixed_cost_savings_lakh": round(fixed_savings, 2),
        "assignment": assignment,
        "dc_throughput": dc_throughput,
        "solver_status": pulp.LpStatus[prob.status],
    }
