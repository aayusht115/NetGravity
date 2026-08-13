"""
NetGravity — Center of Gravity / Weiszfeld Screener
=====================================================
GEOGRAPHIC SCREENING TOOL ONLY — NOT AN OPTIMIZATION DECISION.

Purpose:
    Identify promising geographic regions for candidate DC placement
    based on demand-weighted distance minimization.

IMPORTANT (Assumption A-011):
    The CoG / Weiszfeld output is a SCREENING OUTPUT.
    It does NOT account for:
      - Fixed facility costs
      - Capacity constraints
      - Service requirements
      - Actual road network distances
      - Candidate site availability

    The final location decision comes from the MILP network optimizer
    after these real constraints are applied.

Pipeline:
    Demand geography
        ↓
    Weiszfeld (geographic sweet-spot)
        ↓
    Candidate location enumeration (human / data-driven)
        ↓
    MILP Network Optimization (authoritative decision)

Algorithm:
    Given demand points pⱼ = (xⱼ, yⱼ) with weights wⱼ,
    minimize: f(x*, y*) = Σⱼ wⱼ · ‖(x*, y*) − pⱼ‖₂

    Weiszfeld update:
        x*_(t+1) = [Σⱼ wⱼ·xⱼ/dⱼ(t)] / [Σⱼ wⱼ/dⱼ(t)]
        y*_(t+1) = [Σⱼ wⱼ·yⱼ/dⱼ(t)] / [Σⱼ wⱼ/dⱼ(t)]

    Initialized at demand-weighted centroid.

Source: Chopra & Meindl §5.2; Weiszfeld (1937)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CogResult:
    """
    Result of a single Weiszfeld CoG run.
    Labelled as SCREENING OUTPUT to prevent misuse.
    """
    x:                     float
    y:                     float
    converged:             bool
    iterations:            int
    total_weighted_dist:   float
    convergence_history:   List[dict] = field(default_factory=list)
    disclaimer:            str = (
        "SCREENING OUTPUT ONLY — Geographic sweet-spot, not an optimized facility location. "
        "See Assumption A-011 and docs/model_foundation.md §1.1"
    )


@dataclass
class MultiCogResult:
    """Result of multi-facility Weiszfeld (k-CoG)."""
    n_facilities:        int
    cog_locations:       List[CogResult]
    converged:           bool
    outer_iterations:    int
    disclaimer:          str = (
        "SCREENING OUTPUT ONLY — Geographic sweet-spots, not optimized facility locations. "
        "These coordinates must be mapped to real candidate sites before MILP solve. "
        "See Assumption A-011."
    )


# ---------------------------------------------------------------------------
# Weiszfeld single-facility CoG
# ---------------------------------------------------------------------------

def weiszfeld_cog(
    demand_points: Sequence[Tuple[float, float]],
    weights:       Sequence[float],
    max_iter:      int   = 500,
    tol:           float = 1e-8,
) -> CogResult:
    """
    Run Weiszfeld algorithm to find the single demand-weighted CoG.

    Args:
        demand_points: Sequence of (x, y) — lat/lon or schematic coords
        weights:       Demand weights per point (must be non-negative)
        max_iter:      Maximum iterations
        tol:           Convergence tolerance (shift in location)

    Returns:
        CogResult with optimal (x, y) and convergence diagnostics.
        Result is labeled as SCREENING OUTPUT.
    """
    n = len(demand_points)

    if n == 0:
        raise ValueError("No demand points provided for CoG calculation")
    if n != len(weights):
        raise ValueError("demand_points and weights must have the same length")

    pts = [(float(p[0]), float(p[1])) for p in demand_points]
    w   = [float(wi) for wi in weights]
    total_w = sum(w)

    if total_w <= 0:
        raise ValueError("Total demand weight must be > 0")

    # Initialize at demand-weighted centroid
    x = sum(w[j] * pts[j][0] for j in range(n)) / total_w
    y = sum(w[j] * pts[j][1] for j in range(n)) / total_w

    if n == 1:
        return CogResult(
            x=pts[0][0], y=pts[0][1],
            converged=True, iterations=0, total_weighted_dist=0.0,
        )

    history = []
    converged = False

    for iteration in range(max_iter):
        dists = [
            max(math.sqrt((x - pts[j][0])**2 + (y - pts[j][1])**2), 1e-10)
            for j in range(n)
        ]

        num_x = sum(w[j] * pts[j][0] / dists[j] for j in range(n))
        num_y = sum(w[j] * pts[j][1] / dists[j] for j in range(n))
        denom  = sum(w[j] / dists[j] for j in range(n))

        new_x = num_x / denom
        new_y = num_y / denom

        shift = math.sqrt((x - new_x)**2 + (y - new_y)**2)
        obj   = sum(w[j] * dists[j] for j in range(n))

        history.append({
            "iteration": iteration + 1,
            "x": round(new_x, 8),
            "y": round(new_y, 8),
            "objective": round(obj, 6),
            "shift": round(shift, 10),
        })

        x, y = new_x, new_y

        if shift < tol:
            converged = True
            break

    final_dists = [math.sqrt((x - pts[j][0])**2 + (y - pts[j][1])**2) for j in range(n)]
    total_wd = sum(w[j] * final_dists[j] for j in range(n))

    return CogResult(
        x=round(x, 8),
        y=round(y, 8),
        converged=converged,
        iterations=len(history),
        total_weighted_dist=round(total_wd, 6),
        convergence_history=history,
    )


# ---------------------------------------------------------------------------
# Multi-facility CoG (k-CoG via Lloyd's algorithm + Weiszfeld)
# ---------------------------------------------------------------------------

def multi_cog(
    demand_points: Sequence[Tuple[float, float]],
    weights:       Sequence[float],
    n_facilities:  int,
    seeds:         Optional[Sequence[Tuple[float, float]]] = None,
    max_outer:     int   = 100,
    max_inner:     int   = 500,
    tol:           float = 1e-8,
) -> MultiCogResult:
    """
    Multi-facility CoG using iterative assignment + Weiszfeld.

    Algorithm (Lloyd's-style):
      1. Initialize n_facilities center estimates (seeds or spread evenly)
      2. Assign each demand point to nearest center
      3. Run Weiszfeld in each cluster to update center
      4. Repeat until center positions converge

    Args:
        demand_points:  Demand point coordinates
        weights:        Demand weights per point
        n_facilities:   Number of CoG locations to identify
        seeds:          Initial center positions (optional)
        max_outer:      Maximum outer (assignment) iterations
        max_inner:      Maximum inner (Weiszfeld) iterations per cluster
        tol:            Convergence tolerance for center shift

    Returns:
        MultiCogResult with all CoG locations labeled as SCREENING OUTPUT.
    """
    if n_facilities <= 0:
        raise ValueError("n_facilities must be >= 1")

    n   = len(demand_points)
    pts = [(float(p[0]), float(p[1])) for p in demand_points]
    w   = [float(wi) for wi in weights]

    # Initialize centers
    if seeds is not None:
        centers = [(float(s[0]), float(s[1])) for s in seeds[:n_facilities]]
        # Pad if fewer seeds than facilities
        while len(centers) < n_facilities:
            centers.append(pts[len(centers) % n] if pts else (0.0, 0.0))
    else:
        # Spread seeds across demand points
        step = max(1, n // n_facilities)
        centers = [pts[i * step % n] for i in range(n_facilities)]

    converged = False

    for outer_iter in range(max_outer):
        # Step 1: Assign each point to nearest center
        assignments = []
        for j in range(n):
            dists_to_centers = [
                math.sqrt((pts[j][0] - cx)**2 + (pts[j][1] - cy)**2)
                for cx, cy in centers
            ]
            assignments.append(min(range(n_facilities), key=lambda k: dists_to_centers[k]))

        # Step 2: Run Weiszfeld in each cluster
        new_centers = []
        for k in range(n_facilities):
            cluster_pts = [pts[j] for j in range(n) if assignments[j] == k]
            cluster_w   = [w[j]   for j in range(n) if assignments[j] == k]

            if not cluster_pts:
                new_centers.append(centers[k])
                continue

            result = weiszfeld_cog(cluster_pts, cluster_w, max_iter=max_inner, tol=tol)
            new_centers.append((result.x, result.y))

        # Check convergence
        max_shift = max(
            math.sqrt((centers[k][0] - new_centers[k][0])**2
                     + (centers[k][1] - new_centers[k][1])**2)
            for k in range(n_facilities)
        )

        centers = new_centers
        if max_shift < tol:
            converged = True
            break

    # Build individual CogResult per facility
    cog_locations = []
    for k, (cx, cy) in enumerate(centers):
        cluster_pts = [pts[j] for j in range(n) if assignments[j] == k]
        cluster_w   = [w[j]   for j in range(n) if assignments[j] == k]
        wd = sum(
            cluster_w[i] * math.sqrt((cluster_pts[i][0] - cx)**2 + (cluster_pts[i][1] - cy)**2)
            for i in range(len(cluster_pts))
        ) if cluster_pts else 0.0
        cog_locations.append(CogResult(
            x=round(cx, 8), y=round(cy, 8),
            converged=converged,
            iterations=outer_iter + 1,
            total_weighted_dist=round(wd, 6),
        ))

    return MultiCogResult(
        n_facilities     = n_facilities,
        cog_locations    = cog_locations,
        converged        = converged,
        outer_iterations = outer_iter + 1,
    )
