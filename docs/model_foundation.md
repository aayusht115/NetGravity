# NetGravity — Model Foundation
## Grounded in Chopra & Meindl, Supply Chain Management (5th Ed.), Chapter 5

---

## 1. Source Concepts from Chopra & Meindl Chapter 5

### 1.1 Gravity Models / Weiszfeld Algorithm
- **Source**: C&M §5.2 — "Gravity Location Models"
- **Description**: Given demand points pⱼ = (xⱼ, yⱼ) with weights wⱼ, minimize the
  total weighted Euclidean distance to a single facility.
  Solved iteratively via Weiszfeld's algorithm (also called Iteratively Re-Weighted
  Least Squares applied to L1 regression in 2D space).
- **Limitation explicitly stated in C&M**: The gravity model finds a geographic
  optimum but ignores capacity, fixed costs, service requirements and actual road
  networks. It is a *screening tool*, not a decision tool.

### 1.2 Capacitated Plant Location Model (CPLM)
- **Source**: C&M §5.3 — "Network Optimization Models"
- **Description**: Binary facility-opening decisions combined with continuous
  allocation decisions, subject to capacity and demand satisfaction constraints.
- **Formulation adopted**: Chopra's two-index formulation extended to multi-product,
  multi-mode, multi-echelon by NetGravity.

### 1.3 Gravity + MILP Integration
- **Source**: C&M §5.4 — "Making Network Design Decisions in Practice"
- **Key insight adopted**: Run gravity model to identify promising geographic regions,
  enumerate candidate sites within those regions, then solve the MILP. The MILP is
  the authoritative decision engine.

### 1.4 Multi-Echelon Network Design
- **Source**: C&M §5.3 — Network-flow formulations with multiple facility layers
- **Description**: Suppliers → Plants → Warehouses/DCs → Markets. Each layer is
  explicitly modeled with flow-conservation at intermediate nodes.

### 1.5 Inventory Cost Integration
- **Source**: C&M §5.3 — Total landed cost including inventory carrying cost
- **Safety stock formula (adopted, V1)**:
  ```
  SS = z_α × σ_D × √(LT)
  ```
  where z_α is the service-level z-score, σ_D is demand standard deviation per
  period, and LT is replenishment lead time in periods.
- **Assumption documented**: Demand follows Normal distribution (relaxable).
- **C&M guidance**: Inventory cost increases as network fragments (more DCs = more
  safety stock due to demand fragmentation / lost pooling benefit).

### 1.6 Service Level Constraints
- **Source**: C&M §5.3 — Response time, customer service level
- **Types adopted**:
  - Transit-time constraint: lane lead_time_days ≤ customer SLA
  - Cycle Service Level (CSL): probability of no stockout in a replenishment cycle
  - Fill Rate: fraction of demand met without backorder

### 1.7 Transportation Cost
- **Source**: C&M §5.3 — Transport cost as function of distance, volume, mode
- **Formula (V1)**:
  ```
  TransportCost(i→j, v, k) = rate_per_unit_{ijv} × flow_{ijvk}
  ```
- **Architecture**: Abstracted through `CostEngine` so it can support future
  fixed-trip-cost, fuel surcharge, and toll models without changing the optimizer.

---

## 2. Adopted Concepts in NetGravity

| C&M Concept | NetGravity Implementation | Status |
|---|---|---|
| Gravity / Weiszfeld | `cog/screener.py` — screening only | ✅ Implemented |
| Capacitated Plant Location MILP | `optimization/milp.py` — core MILP | ✅ Implemented |
| Binary facility-open variable | `y_i ∈ {0,1}` in MILP | ✅ Implemented |
| Continuous flow variables | `x_{ijvk} ≥ 0` on feasible arcs | ✅ Implemented |
| Demand satisfaction constraint | `Σ_i x_{im} = D_m` (or ≥ with shortage) | ✅ Implemented |
| Facility capacity constraint | `Σ flow ≤ CAP_i × y_i` | ✅ Implemented |
| Flow conservation at DCs | `Σ inbound = Σ outbound` | ✅ Implemented |
| Multi-echelon (S→W→M) | Node-set architecture, all layers modeled | ✅ Implemented |
| Inventory (safety stock) | `inventory/module.py` — modular, pluggable | ✅ Implemented |
| Transportation cost abstraction | `costs/engine.py` | ✅ Implemented |
| Service level constraints | `service/module.py` — SLA / CSL / fill rate | ✅ Implemented |
| Carbon accounting | `carbon/module.py` — CO2 = dist × weight × ef | ✅ Implemented |
| Scenario analysis | `scenarios/engine.py` — parameter overrides | ✅ Implemented |
| Sensitivity analysis | `sensitivity/engine.py` — one/two-way sweeps | ✅ Implemented |

---

## 3. Modified Concepts

| C&M Concept | NetGravity Modification | Rationale |
|---|---|---|
| Single-period model | Extended to multi-period skeleton (t=1..T activatable) | Future roadmap; single-period is default |
| Single-product flow | Extended to multi-product K | Case 16 may involve SKUs |
| Single transport mode | Extended to multi-mode V | Road/rail/air meaningful for real clients |
| Fixed facility cost annualized | Config-driven unit (annual / daily / monthly) | Avoid hard-coded unit conversions |
| Linking constraint `x_{ij} ≤ y_j` | Replaced with big-M capacity constraint | More tractable for large problems |

---

## 4. Intentionally Deferred Concepts

| Concept | Rationale | Future Integration Point |
|---|---|---|
| Stochastic demand (full) | Adds MINLP complexity; use safety stock V1 | `inventory/module.py` → replace `SafetyStockModule` |
| Multi-period MILP | Adds T×|W| binary variables; deferred | Activate `T` dimension in schemas and MILP |
| Robust optimization | Scenario-based approximation used instead | `resilience/engine.py` → Benders decomposition |
| Nonlinear inventory | Piecewise-linear approximation instead | `inventory/module.py` → PWL cost curves |
| Benders decomposition | Not needed at current scale | Add if |W|×|M|×|K| > 10,000 variables |
| Dark store / last-mile | Node-role extension, schema ready | Add `NodeRole.DARKSTORE` flows |
| Manufacturing decisions | Plant capacity / make-or-buy | Add production variables in MILP |
| Supplier network | Source-layer nodes | Already in schema; activate `S` set |

---

## 5. Model Assumptions (Summary)

Full registry is in `assumptions/registry.py`. Key assumptions:

| ID | Assumption | Default | Overridable |
|---|---|---|---|
| A-001 | Demand distribution = Normal | NORMAL | Yes |
| A-002 | Planning horizon = 1 year | 365 days | Yes |
| A-003 | Transport cost is linear in volume | TRUE | No (architecture allows non-linear) |
| A-004 | Facility is fully available once opened | TRUE | Yes (ramp-up configurable) |
| A-005 | Carbon accounting = transport flows only | TRANSPORT_ONLY | Yes |
| A-006 | Service metric = transit time (lane filter) | TRANSIT_TIME | Yes |
| A-007 | Safety stock z-score (95% CSL) = 1.645 | 1.645 | Yes |
| A-008 | All demand must be met (no shortage default) | TRUE | Yes (enable shortage penalty) |
| A-009 | Single period (T=1) | T=1 | Yes |
| A-010 | Flow through DCs (not direct) | THROUGH | Yes (direct lanes allowed) |

---

## 6. Rationale for Solver Choice

**Selected**: PuLP 3.x with HiGHS backend  
**Reason**: HiGHS is a state-of-the-art open-source LP/MIP solver (Huangfu & Hall, 2018).
It is free, has no licensing restrictions, ships with SciPy, and consistently benchmarks
competitive with commercial solvers on medium-scale MIPs. PuLP provides a clean
Python modeling interface with solver abstraction.

**Future**: Gurobi, CPLEX, GLPK accessible through the same `SolverInterface` without
changing the MILP builder.

---

## 7. References

1. Chopra, S. & Meindl, P. (2016). *Supply Chain Management: Strategy, Planning,
   and Operation* (5th ed.). Pearson. Chapter 5: Network Design in the Supply Chain.
2. Weiszfeld, E. (1937). Sur le point pour lequel la somme des distances de n points
   donnés est minimum. *Tôhoku Mathematical Journal*, 43, 355–386.
3. Huangfu, Q. & Hall, J. A. J. (2018). Parallelizing the dual revised simplex method.
   *Mathematical Programming Computation*, 10(1), 119–142. [HiGHS solver]
4. Daskin, M. S. (2013). *Network and Discrete Location* (2nd ed.). Wiley.
5. Melo, M. T., Nickel, S., & Saldanha-da-Gama, F. (2009). Facility location and
   supply chain management — A review. *European Journal of Operational Research*,
   196(2), 401–412.
