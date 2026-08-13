# NetGravity V1.0 — Audit Report
## Produced for V1.1 Hardening | Date: 2026-08-12

---

## Audit Methodology

Full source code review of all netgravity modules. Every constraint, formula, variable, and output field was inspected against the mathematical specification.
63 existing tests were run and confirmed passing before any changes.

---

## 1. What Is CORRECT in V1.0 (Retained Unchanged)

| Component | Status | Notes |
|---|---|---|
| Modular architecture | ✅ CORRECT | Clean separation; retained |
| Pydantic-typed canonical data model | ✅ CORRECT | `CanonicalNetwork`, `FacilityRecord`, etc. |
| MILP uses binary y_i for facility decisions | ✅ CORRECT | Correct formulation |
| Flow variables x_ijvk >= 0 are continuous | ✅ CORRECT | |
| C2 Facility capacity: Σ outbound ≤ CAP × y_i | ✅ CORRECT | |
| C4 Flow conservation at DCs | ✅ CORRECT | inbound = outbound |
| C5 Mandatory facility: y_i = 1 | ✅ CORRECT | |
| Lane eligibility via feasible-arc construction | ✅ CORRECT | Not millions of constraints |
| Product-facility eligibility (C8) | ✅ CORRECT | |
| SLA transit-time filter (single-arc) | ✅ CORRECT | |
| Carbon formula: dist × weight × ef / 1000 | ✅ CORRECT | |
| Scenario engine: deep-copy, no base mutation | ✅ CORRECT | |
| Sensitivity: one-way, two-way, tornado | ✅ CORRECT | |
| CoG / Weiszfeld as screening-only | ✅ CORRECT | |
| Baseline evaluator (fix EXISTING, solve LP) | ✅ CORRECT | |
| 63 tests pass | ✅ CORRECT | |

---

## 2. What Is INCORRECT in V1.0 (Fixed in V1.1)

### CRITICAL — Mathematical Bugs

#### BUG-001: Demand Balance with Empty Inbound Set + Shortage
**File**: `optimization/milp.py`, lines 325–337  
**Problem**: When `allow_shortage=True` AND `inbound_keys=[]` (no arcs reach this market), no constraint is written at all. The shortage variable `u_mk` is created but the demand balance equation `u_mk = D_mk` is never enforced. The solver is free to set `u_mk` to any value that minimizes cost. While the penalty term drives `u_mk` toward `D_mk`, the constraint is absent from the model.
**Mathematical consequence**: Demand balance is not a hard constraint — it is only enforced via the penalty. This violates the stated formulation `Σ x_imvk + u_mk = D_mk`.
**Fix V1.1**: Always write the demand balance constraint, even when `inbound_keys=[]`.

#### BUG-002: Inventory Unit Mismatch
**File**: `inventory/module.py`, line 139  
**Problem**: Safety stock formula `SS = z × σ_agg × sqrt(LT)` mixes:
- `σ_agg` = aggregate monthly demand standard deviation (units/month)
- `LT` = replenishment lead time in days

Result: `z × (units/month) × sqrt(days)` — dimensionally inconsistent.
The correct formula when `σ` is monthly and `LT` is in days requires:
`σ_daily = σ_monthly / sqrt(days_per_period)`
`SS = z × σ_daily × sqrt(LT_days)`

**Magnitude of error**: With `days_per_period=30, LT=3`:  
- Old formula gives: `z × σ_monthly × 1.732`  
- Correct formula gives: `z × σ_monthly × sqrt(3/30) = z × σ_monthly × 0.316`  
- Old formula overestimates safety stock by factor ~5.5x.
**Fix V1.1**: Use `SS = z × σ_monthly × sqrt(LT_days / days_per_period)`.

#### BUG-003: Inventory Cost Not in MILP Objective
**File**: `optimization/milp.py`, lines 217–221, 532–545  
**Problem**: Inventory cost is computed POST-SOLVE based on actual flow assignments (correct methodology), but it is **not included in the MILP objective**. The optimizer therefore ignores inventory costs when choosing which facilities to open. A high-variability, long-lead-time facility can be selected over a better-inventory-economics alternative because inventory does not influence the decision.
**Mathematical consequence**: Result is cost-suboptimal when inventory costs are material relative to fixed/transport costs.
**Fix V1.1**: Iterative solve — each iteration uses the previous solve's inventory assignment to compute linear inventory coefficients, adds them to the objective, and re-solves. Converges when open-facility set is stable AND objective change < tolerance.

### HIGH — Formulation Problems

#### BUG-004: CLOSE Scenario Action Uses Capacity=0 Hack
**File**: `scenarios/engine.py`, lines 182–187  
**Problem**: The CLOSE action sets `capacity_units_per_period = 0.0`. This causes the optimizer to implicitly force y_i = 0 because the capacity constraint becomes `Σ outbound ≤ 0`. However:
- This does not explicitly set y_i = 0; the solver infers it from the capacity constraint
- For facilities with no outbound flow anyway (trivial case), y_i could still be 1 (open but empty) without violating the capacity constraint
- The C3 minimum throughput constraint would then be: `Σ outbound ≥ min_throughput × y_i` — if y_i = 1 but outbound = 0 and min_throughput = 0, this is satisfied, but the facility is "open" with zero throughput
**Fix V1.1**: Add `is_forced_closed: bool` field to `FacilityRecord`. MILP enforces `y_i = 0` via explicit equality constraint.

#### BUG-005: No Infeasibility Diagnostics
**File**: `optimization/milp.py` (extraction phase)  
**Problem**: When the solver returns INFEASIBLE, the result contains only `SolverStatus.INFEASIBLE` with no diagnostic information. The user cannot determine whether infeasibility is caused by: missing supply arcs, insufficient capacity, SLA constraints, or budget constraints.
**Fix V1.1**: Add `diagnostics/infeasibility.py` with `InfeasibilityDiagnostic` that pre-computes likely causes before the solver is called.

### MEDIUM — Missing or Incomplete Features

#### GAP-001: MIP Gap Not Extracted from Solver
**File**: `optimization/solver.py`, lines 241–245  
**Problem**: `mip_gap = prob.solverModel.MIPGap` inside `try/except` that silently fails. CBC and HiGHS via PuLP do not easily expose the final gap through this interface. `mip_gap` remains `None` in most runs. The `optimality_label` (proven optimal vs feasible) cannot be correctly derived.
**Fix V1.1**: Use configured `mip_gap` as an upper bound; add `optimality_label` field derived from solver status + configured gap.

#### GAP-002: No Opening Cost in MILP Objective
**File**: `optimization/milp.py` (objective construction)  
**Problem**: `FacilityRecord.capex` exists but is not in the MILP objective. `FacilityRecord.opening_cost` does not exist. Opening a candidate facility has no one-time cost signal in the optimizer beyond fixed annual cost.
**Fix V1.1**: Add `opening_cost` field; include `opening_cost × y_i` in objective for candidate facilities.

#### GAP-003: Minimum Throughput Has No Global Enable Flag
**File**: `optimization/milp.py`, lines 353–362  
**Problem**: C3 applies whenever `fac.min_throughput_per_period > 0`. There is no config flag to globally disable C3 without zeroing all individual `min_throughput_per_period` fields.
**Fix V1.1**: Add `minimum_throughput_enabled: bool = True` to `OptimizationConfig`.

#### GAP-004: Carbon Factors Have No Version/Methodology Metadata in Results
**File**: `carbon/module.py`, `config/defaults.py`  
**Problem**: GLEC v2.0 factors are used but the methodology version is not recorded in the result. If factors are overridden, the override is not traceable in outputs.
**Fix V1.1**: Add `emission_methodology`, `emission_factor_table` to `OptimizationConfig`. `CarbonResult` includes methodology version.

#### GAP-005: Sensitivity Does Not Track Facility Configuration Changes
**File**: `sensitivity/engine.py`, `schemas/results.py`  
**Problem**: `SensitivityPoint` records objective/cost/carbon/distance/fill_rate but not which facilities are open. Cannot determine if network configuration changed between sensitivity points.
**Fix V1.1**: Add `facility_ids_open: List[str]` and `configuration_stable: bool` to `SensitivityPoint`.

#### GAP-006: NodeRole Missing DEPOT and CUSTOMER
**File**: `schemas/network.py`  
**Problem**: `NodeRole` includes `DARKSTORE` but is missing `DEPOT` and `CUSTOMER` (equivalent to MARKET). Industry usage expects these roles.
**Fix V1.1**: Add `DEPOT` and `CUSTOMER` to `NodeRole`.

#### GAP-007: PuLP 3.x Deprecation Warnings (1,628 warnings in test run)
**File**: All optimization files  
**Problem**: PuLP is deprecating: `LpVariable(name, ...)` constructor, `PULP_CBC_CMD`, and `prob.constraints` dict access. These generate 1,628 warnings in the test suite, masking real issues.
**Fix V1.1**: Migrate to PuLP 4.0 compatible API throughout.

#### GAP-008: Inventory Unit Documentation
**File**: `inventory/module.py`, `schemas/network.py`  
**Problem**: `DemandRecord.std_dev` documented as "demand standard deviation" without explicit unit (units/period vs units/day). The unit is critical for the safety stock formula.
**Fix V1.1**: Add explicit unit comment `# units/period (same unit as quantity)`.

#### GAP-009: Weighted Average Distance Formula Undefined
**File**: `metrics/kpis.py`  
**Problem**: `avg_distance_km` is computed but the formula (simple average vs demand-weighted vs flow-weighted) is not explicit in the result schema.
**Fix V1.1**: Add `weighted_avg_distance_km` (demand-weighted: Σdist×flow / Σflow) distinctly from simple average.

---

## 3. What Is INCOMPLETE (Architecture Exists, Not Yet Fully Implemented)

| Feature | Status | Notes |
|---|---|---|
| Multi-echelon service (cumulative LT) | ARCH READY | V1.1 adds warning; full optimization deferred |
| Production variables (BOM, multi-period) | DEFERRED | Architecture supports; not implemented |
| Multi-period optimization | DEFERRED | T=1 only |
| Multi-echelon inventory | DEFERRED | Normal SS is V1 approximation |
| Dark store / last-mile | ARCH READY | DARKSTORE role exists |
| Supplier layer | ARCH READY | SUPPLIER role exists |
| Real road-network distances | ARCH READY | `network_distance_km` field added V1.1 |
| Gurobi / CPLEX best_bound extraction | PARTIAL | Gurobi added; CPLEX not yet |
| Inventory pooling | DEFERRED | |
| Stochastic optimization | DEFERRED | |

---

## 4. What Is Being Changed in V1.1

| V1.0 | V1.1 |
|---|---|
| Demand balance skipped when no inbound arcs + shortage enabled | Always write demand balance constraint |
| `SS = z × σ_monthly × sqrt(LT_days)` (wrong units) | `SS = z × σ_monthly × sqrt(LT_days / 30)` (correct) |
| Inventory cost not in MILP objective | Iterative solve: inventory cost in objective with convergence check |
| CLOSE scenario sets capacity=0 (hack) | `is_forced_closed` field + explicit `y_i=0` constraint |
| No infeasibility diagnostics | `InfeasibilityDiagnostic` module |
| No opening cost in objective | `opening_cost × y_i` for candidate facilities |
| No global min-throughput flag | `minimum_throughput_enabled` config flag |
| Carbon factors not versioned in results | `emission_methodology` in config + results |
| PuLP 3.x API (1,628 warnings) | PuLP 4.0 compatible API |
| `SensitivityPoint` has no facility list | `facility_ids_open`, `configuration_stable` added |
| `model_version = "1.0.0"` | `model_version = "1.1.0"` |

---

## 5. References

- Chopra & Meindl, Supply Chain Management, 5th Ed., Ch. 5 (Network Design) and Ch. 12 (Inventory)
- Daskin (2013), Network and Discrete Location
- Melo, Nickel & Saldanha-da-Gama (2009), Facility location and supply chain management — A review
- GLEC Framework v2.0 (emission factors — configurable, version-tracked)
- Huangfu & Hall (2018), Parallelizing the dual revised simplex method (HiGHS)
