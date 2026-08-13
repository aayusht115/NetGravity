# NetGravity Case 16 Hardening Final Validation Report (v1.1)

**Date**: 2026-08-13  
**Target Repository**: `https://github.com/aayusht115/NetGravity`  
**Author**: Asish Satpathy  
**Status**: VERIFIED & GO  

---

## 1. F-12 Status: Warehouse Move Economics
- **Status**: PASS
- **Implementation**: Component tariffs (`rate_per_km`, `fixed_leg_cost`, `speed_km_per_day`, `terminal_time_days`) are supported on `LaneRecord`. When present, transport rate and lead times are recomputed using the exact business tariff formula:
  $$\text{TransportCost} = \text{rate\_per\_km} \times \text{distance} + \text{fixed\_leg\_cost}$$
  $$\text{LeadTime} = \frac{\text{distance}}{\text{speed\_km\_per\_day}} + \text{terminal\_time\_days}$$
- **Flat Rate Preservation**: When flat authoritative rates are supplied with `tariff_requires_user_input=True`, flat rates are preserved without multiplying by arbitrary distance ratios.
- **Verification**: Verified via `TestF12WarehouseMoveEconomics` across component tariff moves, flat rate preservation, short/long moves, SLA feasibility, no-op moves, move-backs, and baseline isolation.

---

## 2. F-13 Status: Add DC / Greenfield Facility Logic
- **Status**: PASS
- **Implementation**: Candidate DCs added via `ADD_FACILITY` are automatically connected to upstream supply plants (including virtual plants without coordinates using network supply relationships) and outbound markets.
- **Freight Tariff Derivation**: Removed the hardcoded `0.025 * km` freight assumption. New lanes derive rates from the network's baseline average mode tariff (`rate_per_km`).
- **Verification**: Verified via `TestF13AddDCGreenfieldLogic` across uncoordinated plants, mode-tariff rate derivation, optimal selection vs non-selection, and baseline byte isolation.

---

## 3. F-05 Status: Objective / KPI Configuration Consistency
- **Status**: PASS
- **Implementation**: `milp_solve` constructs an `effective_network` containing the exact resolved `OptimizationConfig` passed to the solve call. This configuration is propagated consistently to MILP objective construction, `compute_kpis`, `compute_flow_analytics`, and `reconcile_kpis_and_objective`.
- **Verification**: Verified exact 100% agreement between solver objective value, reported total cost, and independent cost reconciliation across cost-min and carbon-monetized objective modes.

---

## 4. F-03 Status: Corridor Capacity Semantics
- **Status**: PASS
- **Semantic Decision**: Preserved exact per-mode arc variable upper bound formulation ($\text{upBound} = \text{lane\_capacity}$ on $x_{i,j,v,k}$) as mandated by Case 16 per-mode capacity definitions, maintaining full objective reconciliation ($£149,874.93$ baseline cost / $£115,107.39$ optimized cost).
- **Verification**: Verified via formulation validation and zero-regression test battery.

---

## 5. Existing Test Results
- **Files Tested**: `test_carbon.py`, `test_cog.py`, `test_formulation.py`, `test_independent_validator.py`, `test_infeasibility.py`, `test_inventory.py`, `test_milp_core.py`, `test_scenarios.py`, `test_stress.py`, `test_v1_1_1_hardening.py`, `test_v1_2_1_sla_hardening.py`, `test_v1_2_2_hardening.py`, `test_v1_2_direct_inventory.py`.
- **Result**: **166 / 166 PASS**.

---

## 6. New Business-Value & Scenario Regression Results
- **Files**: `test_scenario_integrity.py` (11 tests), `test_f12_f13_business_validation.py` (13 tests).
- **Result**: **24 / 24 PASS**.

---

## 7. Full Stress-Test Results
- **Battery File**: `test_stress.py`.
- **Result**: **12 / 12 PASS**. Zero infeasibility regressions or mathematical failures.

---

## 8. Objective & KPI Reconciliation Table

| Objective Component / KPI | Solver Objective Value | Independent Post-Solve Value | Absolute Difference | Relative Difference | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Facility Fixed Cost** | £55,000.00 | £55,000.00 | 0.0000 | 0.000000 | PASS |
| **Transportation Cost** | £51,310.00 | £51,310.00 | 0.0000 | 0.000000 | PASS |
| **Handling Cost** | £7,680.00 | £7,680.00 | 0.0000 | 0.000000 | PASS |
| **Inventory Cost** | £1,117.39 | £1,117.39 | 0.0000 | 0.000000 | PASS |
| **Total Cost** | £115,107.39 | £115,107.39 | 0.0000 | 0.000000 | PASS |
| **Total Objective** | £115,107.39 | £115,107.39 | 0.0000 | 0.000000 | PASS |
| **Total Demand** | 4,500.0 | 4,500.0 | 0.0000 | 0.000000 | PASS |
| **Total Served** | 4,500.0 | 4,500.0 | 0.0000 | 0.000000 | PASS |
| **Demand Fill Rate** | 100.0% | 100.0% | 0.0000 | 0.000000 | PASS |
| **Weighted Avg Dist (km)**| 123.32 | 123.32 | 0.0000 | 0.000000 | PASS |
| **Total Carbon (kg)** | 187.9575 | 187.9575 | 0.0000 | 0.000000 | PASS |

---

## 9. Scenario Isolation Verification
- Verified `baseline_input == original_baseline_input` and `baseline_result == original_baseline_result` across all scenario runs. Base network state is strictly immutable.

---

## 10. Summary of Assumptions & Out-Of-Scope Exclusions
- **Assumptions**:
  1. Default ROAD mode speed is 500 km/day unless `speed_km_per_day` is specified on the lane.
  2. When plant coordinates are absent, greenfield candidate DCs connect using the average inbound distance of existing network lanes from that plant.
- **Out of Scope (Intentionally Unchanged)**:
  - Multi-period inventory formulations, stochastic programming, robust optimization, mandatory dual sourcing, mandatory capacity buffers.

---

## 11. Final Status Summary Block

```text
CASE 16 CORE MODEL: PASS
MOVE FACILITY: PASS
ADD FACILITY: PASS
SCENARIO OVERRIDES: PASS
OBJECTIVE RECONCILIATION: PASS
KPI RECONCILIATION: PASS
CORRIDOR CAPACITY: PASS
BASELINE ISOLATION: PASS
REGRESSION TESTS: 24/24
FULL TEST SUITE: 190/190
STRESS BATTERY: PASS

OVERALL: GO
```
