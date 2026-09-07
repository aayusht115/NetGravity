"""
NetGravity — Transshipment Model with Maximal Flow Constraints
=============================================================
Extended network: Sources → Transshipment DCs → Destinations

Mathematical Formulation:
    Sets:
        S  = {s1..s3}   supply nodes (plants)
        T  = {t1..t5}   transshipment nodes (candidate DCs)
        D  = {d1..d10}  demand nodes (customer zones)
    Variables:
        x_s[i][k] ≥ 0   units shipped from source i to DC k
        x_d[k][j] ≥ 0   units shipped from DC k to destination j
    Objective:
        min  Σᵢ Σₖ c_s[i][k]·x_s[i][k]  +  Σₖ Σⱼ c_d[k][j]·x_d[k][j]
             +  Σₖ h[k]·(Σⱼ x_d[k][j])   (handling cost at active DCs)
    Subject to:
        (Supply)    Σₖ x_s[i][k] ≤ supply[i]                  ∀ i ∈ S
        (Demand)    Σₖ x_d[k][j] = demand[j]                  ∀ j ∈ D
        (Balance)   Σᵢ x_s[i][k] = Σⱼ x_d[k][j]             ∀ k ∈ T  (flow conservation at DC)
        (Cap S→T)   x_s[i][k] ≤ cap_s[i][k]                  ∀ i,k   (arc capacity/maximal flow)
        (Cap T→D)   x_d[k][j] ≤ cap_d[k][j]                  ∀ k,j
        (DC Cap)    Σⱼ x_d[k][j] ≤ dc_capacity[k]·open[k]   ∀ k ∈ T
        (Non-neg)   x_s[i][k], x_d[k][j] ≥ 0

Solved using scipy.optimize.linprog (HiGHS backend).
"""

import numpy as np
from scipy.optimize import linprog


def solve_transshipment(
    source_dc_cost: list[list[float]],
    dc_dest_cost: list[list[float]],
    supply: list[float],
    demand: list[float],
    dc_capacity: list[float],
    dc_handling_cost: list[float],
    arc_cap_source_dc: list[list[float]],
    arc_cap_dc_dest: list[list[float]],
    active_dcs: list[str] | None = None,      # e.g. ["T1","T3","T5"]; None = all
    demand_multiplier: float = 1.0,
    transport_cost_multiplier: float = 1.0,
) -> dict:
    """
    Solve the Transshipment Problem with maximal flow arc capacities.

    Args:
        active_dcs: if set, only the specified DCs are available (others forced to 0)
    """
    n_s = len(supply)
    n_t = len(dc_capacity)
    n_d = len(demand)

    # DC open/closed mask (for scenario analysis)
    if active_dcs is None:
        open_mask = [1] * n_t
    else:
        dc_ids = [f"T{k+1}" for k in range(n_t)]
        open_mask = [1 if dc_ids[k] in active_dcs else 0 for k in range(n_t)]

    # Apply multipliers
    c_sd  = np.array(source_dc_cost, dtype=float) * transport_cost_multiplier
    c_dd  = np.array(dc_dest_cost,   dtype=float) * transport_cost_multiplier
    h     = np.array(dc_handling_cost, dtype=float)
    sup   = np.array(supply, dtype=float)
    dem   = np.array(demand, dtype=float) * demand_multiplier
    dc_cap = np.array(dc_capacity, dtype=float)
    cap_sd = np.array(arc_cap_source_dc, dtype=float)
    cap_dd = np.array(arc_cap_dc_dest,   dtype=float)

    # ---------------------------------------------------------------
    # Decision variables layout:
    #   x_sd: (n_s × n_t) — source-to-DC flows   [0 .. n_s*n_t - 1]
    #   x_dd: (n_t × n_d) — DC-to-dest flows      [n_s*n_t .. n_s*n_t + n_t*n_d - 1]
    # ---------------------------------------------------------------
    n_x_sd = n_s * n_t
    n_x_dd = n_t * n_d
    n_vars = n_x_sd + n_x_dd

    def sd_idx(i, k):  # source i → DC k
        return i * n_t + k

    def dd_idx(k, j):  # DC k → dest j
        return n_x_sd + k * n_d + j

    # ---------------------------------------------------------------
    # Objective: minimize total transport + handling cost
    # ---------------------------------------------------------------
    c_obj = np.zeros(n_vars, dtype=float)
    for i in range(n_s):
        for k in range(n_t):
            c_obj[sd_idx(i, k)] = c_sd[i, k] * open_mask[k]
    for k in range(n_t):
        for j in range(n_d):
            c_obj[dd_idx(k, j)] = (c_dd[k, j] + h[k]) * open_mask[k]

    # ---------------------------------------------------------------
    # Equality constraints:
    # 1. Flow balance at each DC: inflow = outflow
    #    Σᵢ x_s[i][k]  - Σⱼ x_d[k][j] = 0   ∀ k
    # 2. Demand satisfaction:
    #    Σₖ x_d[k][j] = demand[j]              ∀ j
    # ---------------------------------------------------------------
    n_eq = n_t + n_d
    A_eq = np.zeros((n_eq, n_vars), dtype=float)
    b_eq = np.zeros(n_eq, dtype=float)

    # DC balance (rows 0..n_t-1)
    for k in range(n_t):
        for i in range(n_s):
            A_eq[k, sd_idx(i, k)] = 1.0   # inflow
        for j in range(n_d):
            A_eq[k, dd_idx(k, j)] = -1.0  # outflow
        b_eq[k] = 0.0

    # Demand equality (rows n_t..n_t+n_d-1)
    for j in range(n_d):
        for k in range(n_t):
            A_eq[n_t + j, dd_idx(k, j)] = 1.0
        b_eq[n_t + j] = dem[j]

    # ---------------------------------------------------------------
    # Inequality constraints (≤):
    # 1. Supply limits:   Σₖ x_s[i][k] ≤ supply[i]
    # 2. DC capacity:     Σⱼ x_d[k][j] ≤ dc_cap[k] * open_mask[k]
    # ---------------------------------------------------------------
    n_ineq = n_s + n_t
    A_ub = np.zeros((n_ineq, n_vars), dtype=float)
    b_ub = np.zeros(n_ineq, dtype=float)

    for i in range(n_s):
        for k in range(n_t):
            A_ub[i, sd_idx(i, k)] = 1.0
        b_ub[i] = sup[i]

    for k in range(n_t):
        for j in range(n_d):
            A_ub[n_s + k, dd_idx(k, j)] = 1.0
        b_ub[n_s + k] = dc_cap[k] * open_mask[k]

    # ---------------------------------------------------------------
    # Arc-level bounds (upper bounds encode maximal flow constraints)
    # Closed-DC arcs are forced to 0 (ub=0)
    # ---------------------------------------------------------------
    bounds = []
    for i in range(n_s):
        for k in range(n_t):
            ub = float(cap_sd[i, k]) if open_mask[k] else 0.0
            bounds.append((0.0, ub))
    for k in range(n_t):
        for j in range(n_d):
            ub = float(cap_dd[k, j]) if open_mask[k] else 0.0
            bounds.append((0.0, ub))

    # Solve
    result = linprog(c_obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                     bounds=bounds, method="highs")

    if result.status != 0:
        return {
            "status": "infeasible",
            "message": result.message,
            "total_cost": None,
            "flows_sd": None,
            "flows_dd": None,
        }

    x_sd_flat = result.x[:n_x_sd].reshape(n_s, n_t)
    x_dd_flat = result.x[n_x_sd:].reshape(n_t, n_d)

    total_cost = float(result.fun)

    # Build flow lists for frontend
    flows_sd, flows_dd = [], []
    for i in range(n_s):
        for k in range(n_t):
            qty = round(float(x_sd_flat[i, k]), 2)
            if qty > 0.01:
                flows_sd.append({
                    "from": f"S{i+1}", "to": f"T{k+1}",
                    "flow": qty, "cost": round(float(c_sd[i, k]), 2),
                    "total_cost": round(float(qty * c_sd[i, k]), 2),
                    "arc_utilization_pct": round(float(qty / cap_sd[i, k] * 100), 1)
                                           if cap_sd[i, k] > 0 else 100.0,
                })

    for k in range(n_t):
        for j in range(n_d):
            qty = round(float(x_dd_flat[k, j]), 2)
            if qty > 0.01:
                flows_dd.append({
                    "from": f"T{k+1}", "to": f"D{j+1}",
                    "flow": qty, "cost": round(float(c_dd[k, j] + h[k]), 2),
                    "total_cost": round(float(qty * (c_dd[k, j] + h[k])), 2),
                    "arc_utilization_pct": round(float(qty / cap_dd[k, j] * 100), 1)
                                           if cap_dd[k, j] > 0 else 100.0,
                })

    # DC-level stats
    dc_throughput = x_dd_flat.sum(axis=1)
    dc_utilization = [
        round(float(dc_throughput[k]) / float(dc_cap[k]) * 100, 1) if (dc_cap[k] > 0 and open_mask[k]) else 0.0
        for k in range(n_t)
    ]

    total_units = float(dem.sum())
    avg_cost_per_unit = total_cost / total_units if total_units > 0 else 0

    return {
        "status": "optimal",
        "model": "transshipment",
        "total_cost": round(total_cost, 2),
        "total_units": round(total_units, 2),
        "avg_cost_per_unit": round(avg_cost_per_unit, 4),
        "flows_source_dc": flows_sd,
        "flows_dc_dest": flows_dd,
        "dc_throughput": [round(float(v), 2) for v in dc_throughput.tolist()],
        "dc_utilization_pct": dc_utilization,
        "active_dcs": active_dcs,
        "open_mask": open_mask,
        "supply_used": [round(float(x_sd_flat[i, :].sum()), 2) for i in range(n_s)],
        "supply_remaining": [round(float(sup[i] - x_sd_flat[i, :].sum()), 2) for i in range(n_s)],
    }
