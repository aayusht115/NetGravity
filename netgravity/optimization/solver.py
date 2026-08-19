"""
NetGravity — Solver Interface V1.1
=====================================
Abstract solver interface with implementations for:
  - HiGHS (preferred, free, production-grade, via PuLP)
  - CBC   (bundled with PuLP, auto-resolved by PuLP)
  - Gurobi (commercial, optional)

V1.1 Changes:
  - Simplified solver detection: try HiGHS, fall back to PuLP's bundled CBC
  - Suppresses PuLP PULP_CBC_CMD deprecation warnings at call site
  - Added best_bound extraction (multi-solver attempt)
  - Added optimality_label population via get_optimality_label()
  - Precise mip_gap computation when best_bound is available

Design principle: The MILP builder (milp.py) calls SolverInterface.solve()
without knowing which solver is used underneath.
"""

from __future__ import annotations

import time
import warnings
from abc import ABC, abstractmethod
from typing import Optional

try:
    import pulp
    PULP_AVAILABLE = True
except ImportError:
    PULP_AVAILABLE = False

from netgravity.schemas.results import SolverMetadata, SolverStatus


# ---------------------------------------------------------------------------
# Abstract solver interface
# ---------------------------------------------------------------------------

class SolverInterface(ABC):
    """Abstract interface that every solver implementation must satisfy."""

    @abstractmethod
    def solve(
        self,
        prob:          "pulp.LpProblem",
        time_limit:    int   = 300,
        mip_gap:       float = 0.001,
        threads:       int   = 0,
        verbose:       bool  = False,
    ) -> SolverMetadata:
        """
        Solve a PuLP LpProblem.

        Args:
            prob:       The PuLP model (already built)
            time_limit: Maximum solve time in seconds
            mip_gap:    MIP optimality gap (fraction)
            threads:    CPU threads (0 = auto)
            verbose:    Print solver log

        Returns:
            SolverMetadata with status, objective, best_bound, gap, runtime
        """
        ...


# ---------------------------------------------------------------------------
# Solver availability checks
# ---------------------------------------------------------------------------

def _is_highs_available() -> bool:
    """Check if HiGHS binary is executable via PuLP's HiGHS_CMD."""
    try:
        import subprocess
        solver = pulp.HiGHS_CMD(msg=0)
        result = subprocess.run(
            [solver.path, "--version"],
            capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


_HIGHS_AVAILABLE: bool = _is_highs_available()


# ---------------------------------------------------------------------------
# CBC solver command factory (handles PuLP version differences)
# ---------------------------------------------------------------------------

def _make_cbc_solver(verbose: bool, time_limit: int, mip_gap: float):
    """
    Return a working CBC solver command.

    Uses PULP_CBC_CMD which ships PuLP's bundled CBC binary at an absolute path.
    The DeprecationWarning about PuLP 4.0 is suppressed — we cannot use
    COIN_CMD as a replacement since it requires a separately installed 'cbc'
    binary in PATH ('pip install pulp[cbc]'), which is not guaranteed.

    This maintains compatibility with all PuLP environments while suppressing
    the deprecation noise.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return pulp.PULP_CBC_CMD(
            msg       = 1 if verbose else 0,
            timeLimit = time_limit,
            gapRel    = mip_gap,
        )


# ---------------------------------------------------------------------------
# HiGHS solver (via PuLP) — Default
# ---------------------------------------------------------------------------

class HiGHSSolver(SolverInterface):
    """
    HiGHS solver via PuLP, with automatic CBC fallback.

    Attempts HiGHS_CMD first; if not available or fails, falls back to PuLP's
    bundled CBC solver (COIN_CMD / PULP_CBC_CMD). This covers all deployment
    environments without requiring additional setup.

    License: MIT (free, open-source)
    Reference: Huangfu & Hall (2018), Mathematical Programming Computation
    """

    def solve(
        self,
        prob:       "pulp.LpProblem",
        time_limit: int   = 300,
        mip_gap:    float = 0.001,
        threads:    int   = 0,
        verbose:    bool  = False,
    ) -> SolverMetadata:

        start = time.perf_counter()
        solver_name = "CBC"

        if _HIGHS_AVAILABLE:
            try:
                highs_solver = pulp.HiGHS_CMD(
                    msg       = 1 if verbose else 0,
                    timeLimit = time_limit,
                    gapRel    = mip_gap,
                    threads   = threads if threads > 0 else None,
                )
                prob.solve(highs_solver)
                solver_name = "HiGHS"
            except Exception:
                # HiGHS binary found but failed — fall back to CBC
                solver_name = "CBC (HiGHS fallback)"
                prob.solve(_make_cbc_solver(verbose, time_limit, mip_gap))
        else:
            # HiGHS not installed; use bundled CBC
            prob.solve(_make_cbc_solver(verbose, time_limit, mip_gap))

        runtime = time.perf_counter() - start
        return _extract_metadata(prob, solver_name, runtime, configured_mip_gap=mip_gap)


# ---------------------------------------------------------------------------
# CBC solver (explicit, always available via PuLP bundled binary)
# ---------------------------------------------------------------------------

class CBCSolver(SolverInterface):
    """
    COIN-OR CBC solver via PuLP's bundled binary.
    Tries COIN_CMD (PuLP 4.0) first; falls back to PULP_CBC_CMD (PuLP 3.x).
    License: Eclipse Public License (free, open-source)
    """

    def solve(
        self,
        prob:       "pulp.LpProblem",
        time_limit: int   = 300,
        mip_gap:    float = 0.001,
        threads:    int   = 0,
        verbose:    bool  = False,
    ) -> SolverMetadata:

        start = time.perf_counter()
        prob.solve(_make_cbc_solver(verbose, time_limit, mip_gap))
        runtime = time.perf_counter() - start
        return _extract_metadata(prob, "CBC", runtime, configured_mip_gap=mip_gap)


# ---------------------------------------------------------------------------
# Gurobi solver (commercial, optional)
# ---------------------------------------------------------------------------

class GurobiSolver(SolverInterface):
    """
    Gurobi solver via PuLP.
    Requires a valid Gurobi license.
    License: Commercial
    """

    def solve(
        self,
        prob:       "pulp.LpProblem",
        time_limit: int   = 300,
        mip_gap:    float = 0.001,
        threads:    int   = 0,
        verbose:    bool  = False,
    ) -> SolverMetadata:

        try:
            solver = pulp.GUROBI_CMD(
                msg       = 1 if verbose else 0,
                timeLimit = time_limit,
                MIPGap    = mip_gap,
                Threads   = threads if threads > 0 else 0,
            )
        except Exception as e:
            raise RuntimeError(
                f"Gurobi solver not available: {e}. "
                "Ensure Gurobi is installed and licensed."
            )

        start = time.perf_counter()
        prob.solve(solver)
        runtime = time.perf_counter() - start
        return _extract_metadata(prob, "Gurobi", runtime, configured_mip_gap=mip_gap)


# ---------------------------------------------------------------------------
# Metadata extraction helper
# ---------------------------------------------------------------------------

def _extract_metadata(
    prob:               "pulp.LpProblem",
    solver_name:        str,
    runtime:            float,
    configured_mip_gap: float = 0.001,
) -> SolverMetadata:
    """
    Extract solver status and diagnostics from a solved PuLP problem.

    V1.1: Also extracts best_bound where available, computes reported
    mip_gap, and populates optimality_label via SolverMetadata.get_optimality_label().

    Optimality language (precise, non-overclaiming):
      - "Proven optimal" ONLY when gap is confirmed zero.
      - Otherwise: "Best feasible within X% of optimal."
    """
    pulp_status = pulp.LpStatus.get(prob.status, "Unknown")

    status_map = {
        "Optimal":    SolverStatus.OPTIMAL,
        "Infeasible": SolverStatus.INFEASIBLE,
        "Unbounded":  SolverStatus.UNBOUNDED,
        "Undefined":  SolverStatus.NO_SOLUTION,
        "Not Solved": SolverStatus.NO_SOLUTION,
    }
    ng_status = status_map.get(pulp_status, SolverStatus.FEASIBLE)

    obj_val    = pulp.value(prob.objective)
    best_bound = None
    mip_gap    = None

    # Try to extract best_bound from solver model (solver-specific attributes)
    if obj_val is not None:
        for attr in ("ObjBound", "bestBound", "BestBound"):
            try:
                best_bound = float(getattr(prob.solverModel, attr))
                break
            except Exception:
                pass

        if best_bound is not None and abs(obj_val) > 1e-9:
            mip_gap = abs(obj_val - best_bound) / abs(obj_val)
        elif ng_status == SolverStatus.OPTIMAL and configured_mip_gap == 0.0:
            mip_gap = 0.0
        else:
            # Report the configured tolerance as the upper bound on the gap
            mip_gap = configured_mip_gap if ng_status == SolverStatus.OPTIMAL else None

    # Count variables and constraints (PuLP 3.x and 4.x compatible)
    n_vars   = len(prob.variables())
    n_binary = sum(1 for v in prob.variables() if v.cat == "Binary")
    try:
        n_constr = len(list(prob.constraints()))
    except TypeError:
        n_constr = len(prob.constraints)  # type: ignore[arg-type]

    meta = SolverMetadata(
        solver_name      = solver_name,
        solver_version   = None,
        status           = ng_status,
        objective_value  = round(float(obj_val), 6) if obj_val is not None else None,
        best_bound       = round(float(best_bound), 6) if best_bound is not None else None,
        mip_gap          = round(mip_gap, 6) if mip_gap is not None else None,
        optimality_label = "",   # populated below
        runtime_seconds  = round(runtime, 4),
        n_variables      = n_vars,
        n_constraints    = n_constr,
        n_binary         = n_binary,
        warnings         = [],
    )
    meta.optimality_label = meta.get_optimality_label()
    return meta


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_solver(solver_name: str = "HiGHS") -> SolverInterface:
    """
    Return the appropriate solver implementation.

    Args:
        solver_name: "HiGHS" | "CBC" | "Gurobi"

    Returns:
        SolverInterface instance
    """
    name = solver_name.upper()
    if name == "HIGHS":
        return HiGHSSolver()
    elif name == "CBC":
        return CBCSolver()
    elif name in ("GUROBI", "GUROBIPY"):
        return GurobiSolver()
    else:
        raise ValueError(
            f"Unknown solver '{solver_name}'. "
            f"Available: HiGHS, CBC, Gurobi"
        )
