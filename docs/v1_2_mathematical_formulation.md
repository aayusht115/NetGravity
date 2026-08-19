# NetGravity V1.2 — Direct MILP Mathematical Formulation

## Executive Summary
NetGravity V1.2 replaces the legacy iterative fixed-point inventory loop with a **Direct MILP Inventory Formulation**. By precomputing deterministic safety stock coefficients $IC_{ij}$ prior to model construction and introducing binary facility-market assignment decision variables $a_{ij} \in \{0, 1\}$, safety stock costs are optimized directly inside a single PuLP solve. This eliminates inventory iteration loops, cycle detection, damping heuristics, and false-positive non-convergence issues.

---

## 1. Index Sets & Indices

| Set Notation | Index | Description |
| :--- | :--- | :--- |
| $i \in F$ | $i$ | Network facilities (Plants, Distribution Centres, Depots, Warehouses) |
| $j \in M$ | $j$ | Demand nodes (Markets, Customers) |
| $v \in V$ | $v$ | Transportation modes (ROAD, RAIL, AIR, SEA, INTERMODAL) |
| $k \in K$ | $k$ | Product SKUs |

---

## 2. Precomputed Safety Stock & Inventory Cost Coefficients

Prior to MILP construction, `InventoryCoefficientEngine` precomputes the deterministic safety stock and associated holding cost for every facility-market pair $(i, j)$ and product $k$:

$$\text{SS}_{ij,k} = z_{j,k} \cdot \sigma_{j,k} \cdot \sqrt{\frac{\text{LT}_{ij}}{\text{DaysPerPlanningPeriod}}}$$

$$\text{IC}_{ij,k} = \text{SS}_{ij,k} \cdot \text{unit\_value}_{j,k} \cdot \text{holding\_rate\_period}_{j,k}$$

$$\text{IC}_{ij} = \sum_{k \in K} \text{IC}_{ij,k}$$

Where:
- $z_{j,k}$: Standard normal inverse CDF quantile for target Cycle Service Level ($CSL$).
- $\sigma_{j,k}$: Standard deviation of demand for product $k$ at market $j$ per planning period.
- $\text{LT}_{ij} = \text{LeadTime}_{\text{facility}, i} + \text{LeadTime}_{\text{lane}, ij}$: Total replenishment and transportation lead time in days.
- $\text{DaysPerPlanningPeriod}$: Days per planning period (e.g. 30 days/month).
- $\text{holding\_rate\_period}_{j,k}$: Annual holding rate normalized to the cost period (e.g. $\text{holding\_rate} / 12$ for monthly).

---

## 3. Decision Variables

| Variable | Type | Bounds | Description |
| :--- | :--- | :--- | :--- |
| $y_i$ | Binary | $\{0, 1\}$ | $y_i = 1$ if facility $i$ is open, $0$ otherwise. |
| $a_{ij}$ | Binary | $\{0, 1\}$ | $a_{ij} = 1$ if facility $i$ is assigned to serve market $j$, $0$ otherwise. |
| $x_{ijvk}$ | Continuous | $\ge 0$ | Flow volume of product $k$ shipped from origin $i$ to destination $j$ via mode $v$. |
| $u_{jk}$ | Continuous | $\ge 0$ | Shortage / unmet demand for product $k$ at market $j$. |

---

## 4. Objective Function (Total Cost Minimization)

$$\min Z_{\text{MILP}} = Z_{\text{Facility}} + Z_{\text{Opening}} + Z_{\text{Transport}} + Z_{\text{Handling}} + Z_{\text{Inventory}} + Z_{\text{Shortage}} + Z_{\text{Carbon}}$$

Where:

$$Z_{\text{Facility}} = \sum_{i \in F} f_i \cdot y_i \quad \text{(Fixed facility operating cost normalized to period)}$$

$$Z_{\text{Opening}} = \sum_{i \in F_{\text{candidate}}} o_i \cdot y_i \quad \text{(One-time opening cost for candidate facilities)}$$

$$Z_{\text{Transport}} = \sum_{(i,j,v,k) \in A} c_{ijvk} \cdot x_{ijvk} \quad \text{(Freight transportation cost)}$$

$$Z_{\text{Handling}} = \sum_{i \in F} h_i \cdot \left( \sum_{(i,j,v,k) \in A} x_{ijvk} \right) \quad \text{(Variable throughput handling cost)}$$

$$Z_{\text{Inventory}} = \sum_{i \in F} \sum_{j \in M} \text{IC}_{ij} \cdot a_{ij} \quad \text{(Direct deterministic safety stock holding cost)}$$

$$Z_{\text{Shortage}} = \sum_{j \in M} \sum_{k \in K} p_k \cdot u_{jk} \quad \text{(Shortage penalty if allowed)}$$

$$Z_{\text{Carbon}} = \sum_{(i,j,v,k) \in A} p_{\text{CO}_2} \cdot \text{CO}_{2, ijvk} \cdot x_{ijvk} \quad \text{(Carbon emissions cost if enabled)}$$

---

## 5. Mathematical Constraints

### (C1) Demand Fulfillment & Shortage Balance
$$\sum_{i \in F} \sum_{v \in V} x_{ijvk} + u_{jk} = D_{jk} \quad \forall j \in M, k \in K$$

### (C2) Effective Facility Capacity
$$\sum_{j \in M} \sum_{v \in V} \sum_{k \in K} x_{ijvk} \le \text{Cap}_i \cdot y_i \quad \forall i \in F$$

### (C3) Minimum Throughput Requirement
$$\sum_{j \in M} \sum_{v \in V} \sum_{k \in K} x_{ijvk} \ge \text{MinThru}_i \cdot y_i \quad \forall i \in F \quad \text{(if enabled)}$$

### (C4) Intermediate Node Flow Conservation
$$\sum_{\text{inbound}} x_{\text{in}} = \sum_{\text{outbound}} x_{\text{out}} \quad \forall i \in F_{\text{intermediate}}, k \in K$$

### (C5a & C5b) Facility Status Control
$$y_i = 1 \quad \forall i \in F_{\text{mandatory}}$$
$$y_i = 0 \quad \forall i \in F_{\text{forced\_closed}}$$

### (C6) Closed Facility Assignment Prevention Link
$$a_{ij} \le y_i \quad \forall i \in F, j \in M$$

### (C7) Flow-Assignment Linking
$$\sum_{v \in V} \sum_{k \in K} x_{ijvk} \le D_j \cdot a_{ij} \quad \forall i \in F, j \in M \quad \left(D_j = \sum_{k} D_{jk}\right)$$

### (C8) Sourcing Policy Constraint
$$\sum_{i \in F} a_{ij} = 1 \quad \forall j \in M \quad \text{(if policy == SINGLE)}$$

---

## 6. Exact Objective Reconciliation Under Direct Formulation
Because safety stock costs $\sum_{i,j} \text{IC}_{ij} \cdot a_{ij}$ are directly included in the PuLP MILP objective, the solver objective value matches the independently evaluated total cost ($Z_{\text{MILP}} \equiv Z_{\text{eval}}$):

$$\text{Reconciliation Gap} = |Z_{\text{eval}} - Z_{\text{MILP}}| = 0.00$$
