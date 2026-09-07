"""
NetGravity — Center of Gravity (CoG) Solver
============================================
Implements the Weiszfeld iterative algorithm to find the
optimal continuous-space location that minimizes total
weighted distance to all demand points.

Mathematical Formulation:
    Given demand points p_j = (x_j, y_j) with weights w_j,
    minimize:
        f(x*, y*) = Σⱼ w_j · d(x*, p_j)
                  = Σⱼ w_j · √[(x* - x_j)² + (y* - y_j)²]

    Weiszfeld Update Rule:
        x*_(t+1) = [ Σⱼ w_j·x_j / d_j(t) ] / [ Σⱼ w_j / d_j(t) ]
        y*_(t+1) = [ Σⱼ w_j·y_j / d_j(t) ] / [ Σⱼ w_j / d_j(t) ]
    where d_j(t) = ‖(x*(t), y*(t)) − p_j‖₂  (current iteration distance)

    Initialize at the demand-weighted centroid.
    Converges geometrically; typically < 50 iterations to 1e-6 tolerance.

Usage:
    - Multi-DC: Run CoG independently for each cluster of destinations,
      where cluster assignment is done by closest DC seed.
    - The CoG result seeds the MILP facility location problem.
"""

import math
from typing import Sequence


def _euclidean(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.sqrt((x1 - x2) ** 2 + (y1 - y2) ** 2)


def weiszfeld_cog(
    demand_points: Sequence[tuple[float, float]],   # list of (x, y) positions
    weights: Sequence[float],                        # demand / weight per point
    max_iter: int = 500,
    tol: float = 1e-6,
) -> dict:
    """
    Run the Weiszfeld algorithm to find the single optimal CoG location.

    Returns:
        dict with optimal (x, y), convergence info, per-point distances
    """
    n = len(demand_points)
    if n == 0:
        return {"error": "No demand points provided"}
    if n == 1:
        return {
            "x": demand_points[0][0], "y": demand_points[0][1],
            "converged": True, "iterations": 0,
            "total_weighted_distance": 0.0,
            "convergence_history": [],
        }

    w = [float(wi) for wi in weights]
    pts = [(float(p[0]), float(p[1])) for p in demand_points]
    total_w = sum(w)

    # --- Initialize at weighted centroid ---
    x = sum(w[j] * pts[j][0] for j in range(n)) / total_w
    y = sum(w[j] * pts[j][1] for j in range(n)) / total_w

    history = []

    for iteration in range(max_iter):
        # Compute distances from current estimate to each demand point
        distances = [
            max(_euclidean(x, y, pts[j][0], pts[j][1]), 1e-9)
            for j in range(n)
        ]

        # Weiszfeld numerators and denominators
        num_x = sum(w[j] * pts[j][0] / distances[j] for j in range(n))
        num_y = sum(w[j] * pts[j][1] / distances[j] for j in range(n))
        denom  = sum(w[j] / distances[j] for j in range(n))

        new_x = num_x / denom
        new_y = num_y / denom

        shift = _euclidean(x, y, new_x, new_y)
        obj   = sum(w[j] * distances[j] for j in range(n))  # objective value

        history.append({
            "iteration": iteration + 1,
            "x": round(new_x, 6),
            "y": round(new_y, 6),
            "objective": round(obj, 4),
            "shift": round(shift, 8),
        })

        x, y = new_x, new_y

        if shift < tol:
            break

    # Final diagnostics
    final_distances = [_euclidean(x, y, pts[j][0], pts[j][1]) for j in range(n)]
    total_weighted_dist = sum(w[j] * final_distances[j] for j in range(n))

    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "converged": history[-1]["shift"] < tol if history else True,
        "iterations": len(history),
        "total_weighted_distance": round(total_weighted_dist, 4),
        "convergence_history": history,
        "per_point_distances": [
            {"point_idx": j, "distance": round(final_distances[j], 4), "weight": w[j]}
            for j in range(n)
        ],
    }


def multi_cog(
    demand_points: Sequence[tuple[float, float]],
    weights: Sequence[float],
    n_dcs: int,
    dc_seeds: Sequence[tuple[float, float]] | None = None,
    max_iter: int = 200,
    tol: float = 1e-6,
) -> dict:
    """
    Run multi-facility CoG using iterative assignment (Lloyd's-style k-CoG).

    Clusters demand points, runs Weiszfeld in each cluster,
    reassigns points to nearest CoG, repeats until stable.

    Args:
        n_dcs: number of distribution centers to locate
        dc_seeds: initial DC positions; if None, evenly spaced from demand points
    """
    n = len(demand_points)
    pts = [(float(p[0]), float(p[1])) for p in demand_points]
    w   = [float(wi) for wi in weights]

    # --- Initialize DC positions ---
    if dc_seeds is not None:
        centers = [(float(s[0]), float(s[1])) for s in dc_seeds[:n_dcs]]
    else:
        # Spread seeds across demand points
        step = max(1, n // n_dcs)
        centers = [pts[i * step] for i in range(n_dcs)]
        # Pad if not enough seeds
        while len(centers) < n_dcs:
            centers.append(pts[-1])

    global_history = []

    for outer_iter in range(max_iter):
        # Step 1: Assign each demand point to nearest center
        assignments = []
        for j in range(n):
            dists = [_euclidean(pts[j][0], pts[j][1], cx, cy)
                     for cx, cy in centers]
            assignments.append(int(min(range(n_dcs), key=lambda k: dists[k])))

        # Step 2: Run Weiszfeld in each cluster
        new_centers = []
        cluster_results = []
        for k in range(n_dcs):
            cluster_pts = [pts[j] for j in range(n) if assignments[j] == k]
            cluster_w   = [w[j]   for j in range(n) if assignments[j] == k]

            if not cluster_pts:
                # Empty cluster — keep old center
                new_centers.append(centers[k])
                cluster_results.append({"dc_index": k, "n_points": 0,
                                         "x": centers[k][0], "y": centers[k][1]})
                continue

            res = weiszfeld_cog(cluster_pts, cluster_w, tol=tol)
            new_centers.append((res["x"], res["y"]))
            cluster_results.append({
                "dc_index": k,
                "n_points": len(cluster_pts),
                "x": res["x"],
                "y": res["y"],
                "total_weighted_dist": res["total_weighted_distance"],
                "converged": res["converged"],
            })

        # Check convergence
        max_shift = max(
            _euclidean(centers[k][0], centers[k][1], new_centers[k][0], new_centers[k][1])
            for k in range(n_dcs)
        )
        global_history.append({"outer_iter": outer_iter + 1, "max_shift": round(max_shift, 8)})
        centers = new_centers

        if max_shift < tol:
            break

    return {
        "n_dcs": n_dcs,
        "converged": global_history[-1]["max_shift"] < tol if global_history else True,
        "outer_iterations": len(global_history),
        "dc_locations": [
            {"dc_index": k, "x": round(centers[k][0], 6), "y": round(centers[k][1], 6)}
            for k in range(n_dcs)
        ],
        "cluster_details": cluster_results,
        "convergence_history": global_history,
    }
