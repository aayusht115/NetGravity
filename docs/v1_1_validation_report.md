# NetGravity V1.1 — Validation & Benchmark Report
**Date:** August 2026  
**Status:** APPROVED FOR PRODUCTION DEPLOYMENT  
**Target Engine:** NetGravity Mathematical Optimization Engine V1.1  
**Test Suite Coverage:** 106 unit, integration, formulation, stress, and diagnostic tests (100% Pass Rate)

---

## 1. Executive Summary

This report documents the verification, validation, and benchmarking of **NetGravity Optimization Engine V1.1**.

NetGravity V1.1 is the production-grade release of the mathematical optimization core for supply chain network design. All major mathematical defects, unit inconsistencies, deprecation warnings, and diagnostic gaps identified during the V1.0 audit have been systematically resolved.

### Key Validation Outcomes:
1. **Mathematical Correctness:** 100% of mathematical formulation tests pass, including demand balance constraints, plant supply capacity limits, candidate opening costs, and unit-corrected safety stock calculations.
2. **Iterative Inventory Convergence:** The iterative inventory solver converges cleanly in 3 iterations on the Case 16 reference network, achieving optimal cost distribution while resolving the non-linear interaction between facility selection and safety stock.
3. **PuLP 4.0 Compatibility:** Zero deprecation warnings across all variable and constraint initializations.
4. **Test Suite Expansion:** Expanded test coverage from 63 tests (V1.0) to **106 tests** (V1.1), introducing formal formulation correctness, stress/adversarial testing, and structural infeasibility diagnostics.
5. **Deterministic Verification:** Proven identical solution reproducibility across repeated solves.

---

## 2. Audit Resolution Matrix (V1.0 vs V1.1)

| Audit Item ID | Vulnerability / Issue Description in V1.0 | Status in V1.1 | Verification Method & Results |
| :--- | :--- | :--- | :--- |
| **BUG-001** | Demand balance constraint skipped when no inbound arcs exist | **FIXED** | Constraint $C1$ always written; returns `INFEASIBLE` or 100% shortage. Tested in `TestDemandBalanceAlwaysWritten`. |
| **BUG-002** | Inventory safety stock unit mismatch ($\sigma_{monthly} \cdot \sqrt{LT_{days}}$) | **FIXED** | Unit-corrected formula $SS = z \cdot \sigma_{period} \cdot \sqrt{LT_{days} / DaysPerPeriod}$. Tested in `TestInventoryUnitFormula`. |
| **BUG-003** | Plant supply capacity enforced conditionally with $y_i$ multiplier | **FIXED** | Plant supply constraint $C10$ enforced unconditionally ($SUP_p$). Tested in `TestPlantSupplyCapacity`. |
| **BUG-004** | Facility opening cost ignored in objective function | **FIXED** | Candidate facility opening cost $\sum o_i y_i$ added to objective. Tested in `TestOpeningCostInObjective`. |
| **BUG-005** | Forced facility closure handled via `capacity=0` hack | **FIXED** | Explicit binary constraint $C5b$ ($y_i = 0$) enforced via `is_forced_closed`. Tested in `TestForcedCloseConstraint`. |
| **WARN-001** | 1,628 PuLP deprecation warnings due to `LpVariable` constructor | **FIXED** | Updated to PuLP 4.0 `prob.add_variable()` and clean `PULP_CBC_CMD` wrapper. 0 warnings emitted. |
| **FEAT-001** | Non-convergent inventory cost attribution | **FIXED** | Iterative fixed-point MILP solver with configurable stopping tolerance ($\epsilon = 0.001$). Tested in `TestIterativeInventorySolve`. |
| **FEAT-002** | Missing structural infeasibility diagnostics | **FIXED** | `InfeasibilityDiagnostic` pre-solve engine added with 5 diagnostic check types. Tested in `TestInfeasibilityDiagnostics`. |
| **FEAT-003** | Lack of cumulative lead time tracking | **FIXED** | Multi-echelon Dijkstra backward-pass module added (`service/cumulative.py`). Tested in `TestMultiEchelon`. |

---

## 3. Case 16 Benchmark Results (V1.1 Reference Run)

The reference Case 16 dataset (2 plants, 3 existing DCs, 2 candidate DCs, 8 customer markets, 1 product) was solved using NetGravity V1.1.

### Benchmark Output Summary:
- **Solver Status:** `OPTIMAL`
- **Objective Value:** `$812,321.38`
- **MIP Optimality Gap:** `0.001` (0.10%)
- **Optimality Statement:** `"Best feasible solution within 0.10% of optimal."`
- **Inventory Iterations to Convergence:** 3
- **Open Facility Configuration:**
  - `PLANT_NORTH` (Plant)
  - `PLANT_SOUTH` (Plant)
  - `DC_EAST` (DC)
  - `DC_WEST` (DC)
- **Closed Facility Configuration:**
  - `DC_CENTRAL` (Closed - economically suboptimal)
  - `DC_CANDIDATE_1` (Candidate - unopened)
  - `DC_CANDIDATE_2` (Candidate - unopened)

### Cost Breakdown Audit:
- **Facility Fixed Operating Cost:** `$285,000.00`
- **Candidate Opening Cost:** `$0.00`
- **Freight Transportation Cost:** `$412,450.00`
- **Facility Handling Cost:** `$48,220.00`
- **Safety Stock & Cycle Inventory Cost:** `$66,651.38`
- **Shortage Penalty Cost:** `$0.00` (100.0% demand fill rate)

---

## 4. Test Suite Execution Summary

```
============================= test session starts =============================
platform win32 -- Python 3.14.3, pytest-8.4.2, pluggy-1.6.0
collected 106 items

netgravity/tests/test_inventory.py ........                              [  7%]
netgravity/tests/test_milp_core.py .............................        [ 34%]
netgravity/tests/test_scenarios.py ..........                            [ 44%]
netgravity/tests/test_formulation.py ....................              [ 63%]
netgravity/tests/test_stress.py .........................                [ 86%]
netgravity/tests/test_infeasibility.py ..............                     [100%]

============================== 106 passed in 31.57s ==============================
```

---

## 5. Deployment Sign-off & Guidelines

### Operational Directives:
1. **Deterministic Core:** The MILP engine in `netgravity/optimization/` is strictly deterministic and data-agnostic. No heuristic rules or external AI logic mutate the formulation during solving.
2. **Solver Choice:** HiGHS is the default solver. If HiGHS binary is unavailable on client environment, the system gracefully falls back to bundled CBC without code modification.
3. **Audit Readiness:** All client reports generated by NetGravity V1.1 carry input SHA-256 data hashes, exact solver parameters, and non-overclaiming optimality statements.

**Sign-off:** Lead Operations Research Engineer — Approved for Client Deployment.
