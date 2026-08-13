# NetGravity — Mathematical Model Specification
## Version: 1.0.0 | Date: 2026-08-11

---

## 1. Sets

| Symbol | Description | Notes |
|---|---|---|
| N | All network nodes | N = S ∪ P ∪ W ∪ M |
| S | Supplier nodes | Optional layer |
| P | Plant / manufacturing nodes | Optional layer |
| W | Warehouse / DC nodes | Core layer |
| M | Market / customer nodes | Core layer |
| K | Products / SKUs | {k₁, k₂, ..., k_n} |
| V | Transport modes | e.g., {ROAD, RAIL, AIR} |
| A | Feasible arcs | A ⊆ N × N × V × K |
| T | Planning periods | {1, ..., τ}; τ=1 in baseline |
| Ω | Scenario set | Handled externally by ScenarioEngine |
| W_E | Existing (open) facilities | W_E ⊆ W |
| W_C | Candidate facilities | W_C ⊆ W, W_E ∩ W_C = ∅ |
| W_CL | Closable existing facilities | W_CL ⊆ W_E |

---

## 2. Parameters

### 2.1 Facility Parameters

| Symbol | Description | Unit |
|---|---|---|
| f_i | Fixed annual cost of opening/maintaining facility i | currency/year |
| h_i | Handling cost per unit throughput at facility i | currency/unit |
| CAP_i | Throughput capacity of facility i | units/period |
| CAP_i^min | Minimum throughput if facility i is open | units/period |
| E_{ik} | Product-facility eligibility: 1 if facility i can process product k | binary |
| CAPEX_i | Capital expenditure to open facility i (one-time) | currency |
| CLOS_i | Closure cost of facility i | currency |

### 2.2 Demand Parameters

| Symbol | Description | Unit |
|---|---|---|
| D_{mk} | Demand at market m for product k | units/period |
| σ_{mk} | Standard deviation of demand at market m for product k | units/period |
| SLA_m | Maximum acceptable lead time at market m | days |
| α_m | Required cycle service level at market m | fraction [0,1] |

### 2.3 Transportation Parameters

| Symbol | Description | Unit |
|---|---|---|
| c_{ijvk} | Unit transport cost on arc (i,j) via mode v for product k | currency/unit |
| dist_{ij} | Distance on lane (i,j) | km |
| LT_{ijv} | Lead time on arc (i,j) via mode v | days |
| LC_{ijv} | Lane capacity (max flow per period) | units/period |
| ef_{v} | Emission factor for mode v | kg CO₂ / (tonne·km) |
| wt_k | Unit weight of product k | kg/unit |

### 2.4 Inventory Parameters

| Symbol | Description | Unit |
|---|---|---|
| z_α | z-score for service level α | dimensionless |
| r_h | Annual holding cost rate | fraction of unit value |
| p_k | Unit value / price of product k | currency/unit |
| LT_{replen,i} | Replenishment lead time for facility i | days |

### 2.5 Objective Configuration

| Symbol | Description |
|---|---|
| λ_C | Carbon price (cost per kg CO₂) — optional |
| pen | Unit shortage penalty — optional |
| w_CO₂ | Weight on CO₂ in weighted objective |
| Budget | CapEx budget (optional constraint) |
| N_max | Maximum number of open facilities (optional) |

---

## 3. Decision Variables

### 3.1 Primary Variables

| Symbol | Domain | Description |
|---|---|---|
| y_i | {0, 1} | 1 if facility i is open in planning period |
| x_{ijvkt} | ℝ⁺ | Flow on arc (i,j) via mode v for product k in period t |

### 3.2 Optional / Extensional Variables

| Symbol | Domain | Description |
|---|---|---|
| u_{mkt} | ℝ⁺ | Unmet demand (shortage) at market m for product k in period t |
| I_{ikt} | ℝ⁺ | Inventory at facility i for product k at end of period t |
| z_{imk} | {0,1} | 1 if market m is exclusively assigned to facility i for product k (single-source) |
| e_{iqt} | {0,1} | 1 if facility i selects capacity option q in period t |

---

## 4. Objective Function

### Mode A — Total Cost Minimization (Default)

```
min  Z =  Σ_i     f_i · y_i                                     [facility fixed cost]
        + Σ_{(i,j,v,k,t)∈A×T}  c_{ijvk} · x_{ijvkt}           [transport cost]
        + Σ_{i∈W, k∈K, t∈T}  h_i · Σ_{(j,v)} x_{ijvkt}        [handling cost]
        + Σ_{i∈W, k∈K}  INV_COST(i, k, y_i)                    [inventory cost]
        + Σ_{m∈M, k∈K, t∈T}  pen · u_{mkt}                     [shortage penalty, if enabled]
        + Σ_{(i,j,v,k,t)∈A×T}  λ_C · CO2(i,j,v,k) · x_{ijvkt} [carbon cost, if enabled]
```

### Mode B — Cost subject to Service Constraint
```
min  Z_cost (from Mode A without carbon term)
s.t. Σ_{(i,v): LT_{ijv} ≤ SLA_m} x_{ijvk} ≥ (1 − ε) · D_{mk}   ∀m, k
```

### Mode C — Cost subject to Carbon Cap
```
min  Z_cost
s.t. Σ_{(i,j,v,k,t)} CO2(i,j,v,k) · x_{ijvkt} ≤ CARBON_CAP
```

### Mode D — Weighted Cost + Carbon
```
min  Z_cost + w_CO₂ · Σ_{(i,j,v,k,t)} CO2(i,j,v,k) · x_{ijvkt}
```

---

## 5. Constraints

### C1 — Demand Fulfillment
```
Σ_{i∈W, v: (i,m,v,k)∈A} x_{imvkt} + u_{mkt} = D_{mk}    ∀m∈M, k∈K, t∈T
```
where u_{mkt} = 0 if shortage is not permitted (default).

### C2 — Facility Capacity
```
Σ_{j∈N, v, k: (i,j,v,k)∈A} x_{ijvkt} ≤ CAP_i · y_i    ∀i∈W, t∈T
```

### C3 — Minimum Throughput (if configured)
```
Σ_{j∈N, v, k: (i,j,v,k)∈A} x_{ijvkt} ≥ CAP_i^min · y_i    ∀i∈W, t∈T
```

### C4 — Flow Conservation at Through-Nodes (Warehouses/DCs)
```
Σ_{j: (j,i,v,k)∈A} x_{jivkt} = Σ_{j: (i,j,v,k)∈A} x_{ijvkt}    ∀i∈W, k∈K, v∈V, t∈T
```
(Applies only to pure transshipment nodes; production nodes may have inequality.)

### C5 — Existing Mandatory Facility
```
y_i = 1    ∀i∈W_E \ W_CL    (existing, non-closable)
```

### C6 — Existing Closable Facility
```
y_i ∈ {0, 1}    ∀i∈W_CL    (may close)
```

### C7 — Lane Eligibility
```
x_{ijvkt} = 0    if (i,j,v,k) ∉ A
```
(Enforced through variable bounds, not explicit constraints.)

### C8 — Product-Facility Eligibility
```
Σ_{j,v,t} x_{ijvkt} = 0    if E_{ik} = 0    ∀i∈W, k∈K
```

### C9 — Lane Capacity
```
Σ_k x_{ijvkt} ≤ LC_{ijv}    ∀(i,j,v)∈A_lanes, t∈T
```

### C10 — Supply Limit (Suppliers/Plants)
```
Σ_{j,v,k} x_{ijvkt} ≤ SUP_i    ∀i∈S∪P, t∈T
```

### C11 — Maximum Number of Open Facilities (optional)
```
Σ_{i∈W_C} y_i ≤ N_max
```

### C12 — Budget Constraint (optional)
```
Σ_{i∈W_C} CAPEX_i · y_i ≤ Budget
```

### C13 — Single Sourcing (optional, per market-product pair)
```
Σ_{i∈W} z_{imk} = 1    ∀m∈M, k∈K
x_{imvkt} ≤ D_{mk} · z_{imk}    ∀i∈W, m∈M, v∈V, k∈K, t∈T
z_{imk} ≤ y_i    ∀i∈W, m∈M, k∈K
z_{imk} ∈ {0,1}
```

### C14 — Service / Transit Time (lane filter approach)
```
x_{imvkt} = 0    if LT_{imv} > SLA_m    ∀(i,m,v,k,t)
```
(Lane is removed from A during network build if SLA violated, or penalized.)

### C15 — Non-negativity
```
x_{ijvkt} ≥ 0    ∀(i,j,v,k,t)∈A×T
u_{mkt} ≥ 0      ∀m,k,t
y_i ∈ {0,1}      ∀i∈W
```

---

## 6. Inventory Cost Formulation

### V1 — Safety Stock (Default, Modular)

**Assumption A-001**: Demand ~ Normal(μ_{mk}, σ_{mk})

At facility i serving a set of markets M_i:

```
μ_i^{agg}  = Σ_{m∈M_i, k} D_{mk}
σ_i^{agg}  = √(Σ_{m∈M_i, k} σ_{mk}²)    [under demand independence assumption]
SS_i       = z_α · σ_i^{agg} · √(LT_{replen,i})
INV_COST_i = (SS_i + CycleSS_i) · r_h · p̄    [p̄ = average unit value]
```

**Note**: This is a pre-computed parameter (not a decision variable) fed into the
MILP as a cost coefficient per open facility. It is recomputed for each scenario
based on which markets are assigned to which facility.

**Future V2**: Nonlinear exact formulation → piecewise-linear approximation for MILP.

---

## 7. Carbon Calculation

```
CO2_{ijvk}(x) = dist_{ij} × wt_k × ef_v × x_{ijvk} / 1000
```
Units: km × kg/unit × kg_CO₂/(tonne·km) × units = kg_CO₂

**Total network CO₂**:
```
TOTAL_CO₂ = Σ_{(i,j,v,k,t)∈A×T} CO2_{ijvk} · x_{ijvkt}    [kg CO₂/period]
```

---

## 8. Key Performance Indicators (KPIs)

| KPI | Formula | Unit |
|---|---|---|
| Total Network Cost | Z (objective value) | currency/period |
| Facility Cost | Σ_i f_i · y_i | currency/year |
| Transport Cost | Σ c_{ijvk} · x_{ijvk} | currency/period |
| Handling Cost | Σ h_i · throughput_i | currency/period |
| Inventory Cost | Σ INV_COST_i · y_i | currency/period |
| Average Distance (demand-weighted) | Σ_{m,k} dist_{i(m)m} · D_{mk} / Σ D_{mk} | km |
| Facility Utilization_i | throughput_i / CAP_i | fraction |
| Service Level | Σ demand met on time / Σ demand | fraction |
| Total CO₂ | Σ CO2_{ijvk} · x_{ijvk} | kg CO₂/period |
| N Facilities Open | Σ y_i | integer |
| Unmet Demand | Σ u_{mk} | units/period |

---

## 9. Scenario Transformations

A scenario Ω is a set of parameter overrides applied to the base CanonicalNetwork.

| Scenario Type | Transformation |
|---|---|
| Close facility i | Force y_i = 0; remove from W |
| Open candidate i | Add to W_C; set y_i ∈ {0,1} |
| Capacity change | CAP_i ← CAP_i × (1 + δ_cap) |
| Demand change | D_{mk} ← D_{mk} × (1 + δ_dem) |
| Cost change | c_{ijvk} ← c_{ijvk} × (1 + δ_cost) |
| Lane disruption | Remove arc (i,j,v,k) from A |
| Facility disruption | CAP_i ← 0 (or small ε) |
| Carbon factor change | ef_v ← ef_v × (1 + δ_ef) |
| Service target change | SLA_m ← SLA_m - Δ |

All transformations are applied to a **copy** of the canonical network.
The base network is never mutated.

---

## 10. Model Versioning

Every optimization run records:
```
model_version:    "1.0.0"
data_version:     hash of input data
config_version:   hash of OptimizationConfig
scenario_id:      scenario identifier
solver:           "HiGHS" / "CBC" / "Gurobi"
solver_version:   from solver metadata
timestamp:        ISO 8601
```

This ensures reproducibility: the same inputs + config must produce the same result.

---

## 11. Assumptions Registry (Summary)

Full registry: `assumptions/registry.py`

| ID | Statement | Default | Confidence |
|---|---|---|---|
| A-001 | Demand distribution is Normal | NORMAL | MEDIUM |
| A-002 | Planning horizon is 1 period (single-period) | T=1 | HIGH |
| A-003 | Transport cost is linear in flow volume | LINEAR | HIGH |
| A-004 | Facility available at full capacity once opened | FULL | HIGH |
| A-005 | Carbon accounting covers transport flows only | TRANSPORT | HIGH |
| A-006 | Inbound = Outbound at DC (no inventory in model) | FLOW_THROUGH | HIGH |
| A-007 | Safety stock z-score (95% CSL) = 1.645 | z=1.645 | HIGH |
| A-008 | Default: no unmet demand allowed | NO_SHORTAGE | MEDIUM |
| A-009 | All demand points receive service from exactly one period model | SINGLE_PERIOD | HIGH |
| A-010 | Emission factor per mode is homogeneous across all lanes | HOMOGENEOUS | MEDIUM |
