# NetGravity V1.1 — Mathematical Model Reference Document
**Version:** 1.1.0  
**Date:** August 2026  
**Author:** NetGravity Core Engineering / Operations Research  
**Theoretical Alignment:** Chopra & Meindl, *Supply Chain Management* (5th ed., Ch. 5 & 12); Daskin, *Network and Discrete Location* (2nd ed., 2013)

---

## 1. Executive Summary & Purpose

NetGravity V1.1 is a production-grade Mixed-Integer Linear Programming (MILP) engine for multi-echelon, multi-product, capacitated supply chain network design and facility location optimization.

This document serves as the authoritative mathematical specification for the engine, detailing:
- Sets, indices, and parameters
- Decision variables and domain definitions
- Objective function formulations across four optimization modes
- Exact constraint equations and mathematical rationale
- Iterative safety-stock inventory allocation mechanics
- Solver interface abstraction and optimality status semantics

---

## 2. Core Sets & Indices

| Symbol | Description | Canonical Schema Entity |
| :--- | :--- | :--- |
| $N$ | Set of all nodes in the network ($N = F \cup M$) | `FacilityRecord` |
| $F \subset N$ | Set of facility nodes (plants, DCs, warehouses, depots) | `FacilityRecord` (where `is_facility == True`) |
| $F_{plant} \subseteq F$ | Set of production plants and raw material suppliers | `NodeRole.PLANT`, `NodeRole.SUPPLIER` |
| $F_{thru} \subseteq F$ | Set of intermediate transshipment nodes (DCs, warehouses, depots, cross-docks) | `NodeRole.DC`, `NodeRole.WAREHOUSE`, `NodeRole.DEPOT`, `NodeRole.CROSS_DOCK` |
| $M \subset N$ | Set of market demand aggregation nodes | `FacilityRecord` (where `role ∈ {MARKET, CUSTOMER}`) |
| $K$ | Set of products / SKUs | `ProductRecord` |
| $V$ | Set of available transportation modes (ROAD, RAIL, AIR, SEA, INTERMODAL) | `TransportMode` |
| $A \subseteq N \times N \times V \times K$ | Set of feasible directed transportation arcs $(i, j, v, k)$ | `LaneRecord` |
| $T$ | Planning period set (single-period static model in V1.1: $T = \{1\}$) | `DemandRecord.period` |

---

## 3. Input Parameters

### 3.1 Facility Parameters
- $f_i \ge 0$: Fixed operating cost of facility $i \in F$ per planning period (`fixed_cost_per_year / 12` or per-period).
- $o_i \ge 0$: One-time opening cost for candidate facility $i \in F_{candidate}$ (`opening_cost`).
- $h_i \ge 0$: Variable handling cost per unit throughput at facility $i \in F$ (`handling_cost_per_unit`).
- $CAP_i \ge 0$: Outbound throughput capacity limit at facility $i \in F$ (`capacity_units_per_period`).
- $SUP_i \ge 0$: Production / supply capacity limit for plant/supplier $i \in F_{plant}$ (`production_capacity_units_per_period`).
- $MIN_i \ge 0$: Minimum throughput requirement if facility $i \in F$ is open (`min_throughput_per_period`).
- $LT_{replen, i} \ge 0$: Replenishment lead time for facility $i \in F$ in days (`replenishment_lead_time_days`).
- $y_i^{forced} \in \{0, 1\}$: Forced closure flag ($1 = \text{forced closed}$) (`is_forced_closed`).
- $y_i^{mand} \in \{0, 1\}$: Mandatory opening flag ($1 = \text{must remain open}$) (`is_mandatory`).

### 3.2 Demand & Product Parameters
- $D_{mk} \ge 0$: Mean demand for product $k \in K$ at market $m \in M$ per planning period (`quantity`).
- $\sigma_{mk} \ge 0$: Standard deviation of demand for product $k \in K$ at market $m \in M$ per period (`std_dev`).
- $SLA_{mk} \ge 0$: Maximum allowable lead time in days for demand $(m, k)$ (`sla_days`).
- $w_k \ge 0$: Physical weight of product $k \in K$ in kg (`weight_kg`).
- $p_k \ge 0$: Monetary unit value of product $k \in K$ (`unit_value`).
- $r_k \ge 0$: Annual inventory holding cost rate as fraction of unit value (`holding_rate`).

### 3.3 Transportation Arc Parameters
- $c_{ijvk} \ge 0$: Freight cost per unit of product $k$ transported on arc $(i,j)$ via mode $v$ (`rate_per_unit`).
- $d_{ij} \ge 0$: Distance in km along arc $(i,j)$ (`distance_km` or `network_distance_km`).
- $LT_{ijv} \ge 0$: Transit lead time in days on arc $(i,j)$ via mode $v$ (`lead_time_days`).
- $CAP_{ijv} \ge 0$: Capacity limit on lane $(i,j,v)$ (`lane_capacity`).
- $ef_{ijv} \ge 0$: Carbon emission factor in $\text{kg CO}_2 / (\text{tonne} \cdot \text{km})$ (`emission_factor`).

---

## 4. Decision Variables

$$y_i \in \{0, 1\} \quad \forall i \in F \quad \text{(Facility binary decision: 1 if open, 0 if closed)}$$

$$x_{ijvk} \ge 0 \quad \forall (i, j, v, k) \in A \quad \text{(Flow quantity of product } k \text{ on arc } (i,j) \text{ via mode } v\text{)}$$

$$u_{mk} \ge 0 \quad \forall m \in M, k \in K \quad \text{(Shortage / unmet demand, instantiated when } \texttt{allow\_shortage} = \text{True)}$$

---

## 5. Objective Function Formulations

NetGravity V1.1 supports four objective modes configured via `OptimizationConfig.objective_mode`.

### 5.1 Mode A: Total Cost Minimization (`COST_MIN`)

$$\min Z = \sum_{i \in F} f_i y_i + \sum_{i \in F_{candidate}} o_i y_i + \sum_{(i,j,v,k) \in A} c_{ijvk} x_{ijvk} + \sum_{i \in F} h_i \left( \sum_{(i,j,v,k) \in A} x_{ijvk} \right) + \sum_{i \in F} IC_i^{(k)} y_i + P_{short} \sum_{m \in M, k \in K} u_{mk}$$

Where:
- $\sum f_i y_i$: Facility fixed operating costs
- $\sum o_i y_i$: Candidate facility opening costs
- $\sum c_{ijvk} x_{ijvk}$: Transportation freight costs
- $\sum h_i (\dots)$: Facility throughput handling costs
- $\sum IC_i^{(k)} y_i$: Iteratively updated safety-stock inventory cost
- $P_{short} \sum u_{mk}$: Shortage penalty cost (if `allow_shortage` = True)

### 5.2 Mode B: Cost s.t. Service Constraint (`COST_SERVICE`)
Minimizes Mode A total cost subject to a hard service level constraint ($CB$) requiring a target fraction $\gamma$ of demand to be fulfilled within $SLA_{mk}$.

### 5.3 Mode C: Cost s.t. Carbon Cap (`COST_CARBON`)
Minimizes Mode A total cost subject to a hard network emissions cap $C_{max}$ ($CC$).

### 5.4 Mode D: Weighted Cost + Carbon Pricing (`WEIGHTED_COST_CARBON`)

$$\min Z_{Mode D} = Z_{Mode A} + \lambda_{CO2} \cdot E_{total} + w_{CO2} \cdot E_{total}$$

Where $E_{total} = \sum_{(i,j,v,k) \in A} \frac{w_k \cdot d_{ij} \cdot ef_{ijv}}{1000} x_{ijvk}$ is total network emissions in $\text{kg CO}_2$.

---

## 6. Constraints Formulation

### C1: Demand Fulfillment Constraint (Corrected V1.1)

$$\sum_{(i,m,v,k) \in A} x_{imvk} + u_{mk} = D_{mk} \quad \forall m \in M, k \in K$$

> **V1.1 Critical Fix:** This constraint is written for **all** $(m, k) \in M \times K$, even when the set of inbound arcs to market $m$ is empty ($\emptyset$). If no arcs reach market $m$:
> - If `allow_shortage` = False: $0 = D_{mk} \implies$ Model is strictly **INFEASIBLE**.
> - If `allow_shortage` = True: $u_{mk} = D_{mk} \implies$ All demand is explicitly recorded as shortage.

### C2: Facility Throughput Capacity Constraint

$$\sum_{(i,j,v,k) \in A} x_{ijvk} \le CAP_i \cdot y_i \quad \forall i \in F \setminus M$$

### C3: Minimum Throughput Constraint (Configurable V1.1)

$$\sum_{(i,j,v,k) \in A} x_{ijvk} \ge MIN_i \cdot y_i \quad \forall i \in F \text{ where } MIN_i > 0$$

> Controlled by `config.minimum_throughput_enabled`. Can be toggled off for datasets without minimum volume requirements.

### C4: Flow Conservation at Through-Nodes

$$\sum_{(p,i,v,k) \in A} x_{pivk} = \sum_{(i,j,v',k) \in A} x_{ijv'k} \quad \forall i \in F_{thru}, k \in K$$

### C5: Mandatory Facilities Constraint

$$y_i = 1 \quad \forall i \in F \text{ where } y_i^{mand} = 1$$

### C5b: Forced-Closed Facilities Constraint (V1.1 Addition)

$$y_i = 0 \quad \forall i \in F \text{ where } y_i^{forced} = 1$$

### C10: Supply Capacity for Plant/Supplier Nodes (Unconditional V1.1)

$$\sum_{(p,j,v,k) \in A} x_{pjvk} \le SUP_p \quad \forall p \in F_{plant}$$

> Enforces production capacity limits independently of $y_p$. Plants/suppliers operate without binary gate multipliers.

### C11: Maximum Number of Open Facilities (Optional)

$$\sum_{i \in F_{candidate}} y_i \le K_{max}$$

### C12: Budget Constraint (Optional)

$$\sum_{i \in F} Capex_i \cdot y_i \le B_{max}$$

---

## 7. Iterative Inventory Optimization Engine

### 7.1 Mathematical Formulation of Safety Stock (V1.1 Unit Correction)

Safety stock for facility $i$ serving assigned market demands $M_i$:

$$SS_i = z_\alpha \cdot \sigma_{daily, i} \cdot \sqrt{LT_{replen, i}}$$

To maintain dimensional unit consistency when periodic demand std dev $\sigma_{period, i}$ is given per planning period (e.g. 30 days):

$$\sigma_{daily, i} = \frac{\sigma_{period, i}}{\sqrt{DaysPerPeriod}}$$

$$\implies SS_i = z_\alpha \cdot \sigma_{period, i} \cdot \sqrt{\frac{LT_{replen, i}}{DaysPerPeriod}}$$

Where:
- $\sigma_{period, i} = \sqrt{\sum_{m \in M_i, k \in K} \sigma_{mk}^2}$ (Demand independence assumption A-010)
- $CS_i = \frac{\sum_{m \in M_i, k \in K} D_{mk}}{2}$ (Cycle stock approximation)
- $IC_i = (SS_i + CS_i) \cdot r_h \cdot \bar{p}$

### 7.2 Fixed-Point Iterative Algorithm

Because $IC_i$ depends nonlinearly on market flow assignments $M_i$, V1.1 solves the MILP iteratively:

1. **Iteration $k=0$:** Solve MILP with $IC_i^{(0)} = 0$.
2. **Post-Solve Attribution:** Assign markets to facilities based on flow decisions $x_{ijvk}^{(k)}$, compute $IC_i^{(k)}$.
3. **Iteration $k+1$:** Update objective coefficient $IC_i^{(k)} \cdot y_i$ and re-solve.
4. **Convergence Check:** Stop when open facility set $F_{open}^{(k+1)} == F_{open}^{(k)}$ AND $\frac{|Z^{(k+1)} - Z^{(k)}|}{Z^{(k)}} < \epsilon_{tol}$ (default $\epsilon_{tol} = 0.001$).

---

## 8. Solver Interface & Optimality Diagnostics

### 8.1 Solver Architecture & Fallback
NetGravity interacts with solvers via an abstract wrapper `SolverInterface`:
- **HiGHS** (Default: production-grade open-source MIP solver)
- **CBC** (Bundled PuLP solver via `PULP_CBC_CMD`, auto-resolved)
- **Gurobi** (Commercial solver integration)

### 8.2 Auditability & Non-Overclaiming Optimality Statements
NetGravity V1.1 strictly enforces precise optimality labeling via `SolverMetadata.get_optimality_label()`:
- `"Proven optimal solution."` — Only returned when status is `OPTIMAL` and `mip_gap == 0.0` or proven by `best_bound`.
- `"Best feasible solution within X.XX% of optimal."` — Returned when solved within a non-zero MIP gap tolerance.
- `"Feasible solution found (optimality not proven)."` — Returned for time-limited or heuristic bounds.
- `"No feasible solution exists with current constraints."` — Returned for `INFEASIBLE`.

---

## 9. Verification & Auditability Standards

Every optimization run generates a fully auditable `OptimizationResult` carrying:
1. `data_version`: Deterministic SHA-256 hash of all input schemas.
2. `solver`: Complete solver metadata including runtime, node count, variable/constraint counts, and `best_bound`.
3. `objective_components`: Explicit breakdown of fixed, opening, freight, handling, inventory, shortage, and carbon costs.
4. `inventory_iterations`: Total iterations executed by the iterative inventory engine.
