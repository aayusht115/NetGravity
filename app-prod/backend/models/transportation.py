"""
NetGravity — Transportation Problem Solver
==========================================
Classic Balanced/Unbalanced Transportation LP
Model: 3 sources (plants) → 10 destinations (customer zones) DIRECT

Mathematical Formulation:
    Sets:
        S = {s1..s3}   supply nodes
        D = {d1..d10}  demand nodes
    Variables:
        x[i][j] ≥ 0   units shipped from source i to destination j
    Objective:
        min  Σᵢ Σⱼ  c[i][j] · x[i][j]
    Subject to:
        (Supply)   Σⱼ x[i][j] ≤ supply[i]   ∀ i ∈ S
        (Demand)   Σᵢ x[i][j] ≥ demand[j]   ∀ j ∈ D
        (Non-neg)  x[i][j] ≥ 0               ∀ i,j

Solved using scipy.optimize.linprog (Revised Simplex / HiGHS).
"""

import numpy as np
from scipy.optimize import linprog


def solve_transportation(
    cost_matrix: list[list[float]],
    supply: list[float],
    demand: list[float],
    demand_multiplier: float = 1.0,
    transport_cost_multiplier: float = 1.0,
) -> dict:
    """
    Solve the Transportation Problem.

    Args:
        cost_matrix: (n_sources × n_dests) unit transport costs
        supply: list of supply capacities per source
        demand: list of demand requirements per destination
        demand_multiplier: scale demand (for scenario analysis)
        transport_cost_multiplier: scale costs (for scenario analysis)

    Returns:
        dict with optimal solution, total cost, flow matrix, status
    """
    n_s = len(supply)    # number of sources
    n_d = len(demand)    # number of destinations

    # Apply scenario multipliers
    cost_arr   = np.array(cost_matrix, dtype=float) * transport_cost_multiplier
    supply_arr = np.array(supply, dtype=float)
    demand_arr = np.array(demand, dtype=float) * demand_multiplier

    # Flatten cost matrix into objective vector (row-major)
    # Variables: x = [x_00, x_01, ..., x_0(n_d-1), x_10, ..., x_(n_s-1)(n_d-1)]
    c_flat = cost_arr.flatten()          # shape: (n_s * n_d,)
    n_vars = n_s * n_d

    # ---------------------------------------------------------------
    # Inequality constraints: supply constraints
    # Σⱼ x[i][j] ≤ supply[i]  for each source i
    # ---------------------------------------------------------------
    A_ub = np.zeros((n_s, n_vars), dtype=float)
    for i in range(n_s):
        for j in range(n_d):
            A_ub[i, i * n_d + j] = 1.0
    b_ub = supply_arr

    # ---------------------------------------------------------------
    # Equality constraints: demand constraints
    # Σᵢ x[i][j] = demand[j]  for each destination j
    # NOTE: Using equality to ensure all demand is exactly met.
    # ---------------------------------------------------------------
    A_eq = np.zeros((n_d, n_vars), dtype=float)
    for j in range(n_d):
        for i in range(n_s):
            A_eq[j, i * n_d + j] = 1.0
    b_eq = demand_arr

    # Bounds: x[i][j] >= 0 (no upper bound)
    bounds = [(0, None)] * n_vars

    # Solve LP
    result = linprog(
        c_flat,
        A_ub=A_ub, b_ub=b_ub,
        A_eq=A_eq, b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )

    if result.status != 0:
        return {
            "status": "infeasible",
            "message": result.message,
            "total_cost": None,
            "flow_matrix": None,
            "supply_used": None,
            "demand_met": None,
            "utilization": None,
        }

    # Reshape solution
    flow_matrix = result.x.reshape(n_s, n_d)

    # Compute diagnostics
    supply_used   = flow_matrix.sum(axis=1)                             # per source
    demand_met    = flow_matrix.sum(axis=0)                             # per destination
    utilization   = (supply_used / supply_arr * 100).tolist()           # %
    total_cost    = float(result.fun)                                   # objective value

    # Build arc-level flows for frontend rendering
    flows = []
    for i in range(n_s):
        for j in range(n_d):
            qty = round(float(flow_matrix[i, j]), 2)
            if qty > 0.01:
                flows.append({
                    "from":   f"S{i+1}",
                    "to":     f"D{j+1}",
                    "flow":   qty,
                    "cost":   round(cost_arr[i, j], 2),
                    "total_cost": round(qty * cost_arr[i, j], 2),
                })

    # Average distance proxy: total cost / total units
    total_units = float(demand_arr.sum())
    avg_cost_per_unit = total_cost / total_units if total_units > 0 else 0

    return {
        "status": "optimal",
        "model": "transportation",
        "total_cost": round(total_cost, 2),
        "total_units": round(total_units, 2),
        "avg_cost_per_unit": round(avg_cost_per_unit, 4),
        "flow_matrix": flow_matrix.tolist(),
        "flows": flows,
        "supply_used": [round(v, 2) for v in supply_used.tolist()],
        "demand_met": [round(v, 2) for v in demand_met.tolist()],
        "utilization_pct": [round(v, 2) for v in utilization],
        "iterations": getattr(result, "nit", None),
        "shadow_prices": {
            "supply": [round(v, 4) for v in result.ineqlin.marginals.tolist()]
                       if hasattr(result, "ineqlin") and result.ineqlin is not None else [],
            "demand": [round(v, 4) for v in result.eqlin.marginals.tolist()]
                       if hasattr(result, "eqlin") and result.eqlin is not None else [],
        },
    }
