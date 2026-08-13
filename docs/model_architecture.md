# NetGravity — Model Architecture
## Component Interaction & Data Flow

---

## 1. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         DATA INGESTION LAYER                             │
│  schemas/network.py  schemas/scenario.py  schemas/results.py             │
│  (Pydantic typed contracts with validation)                               │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │ validated schemas
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       NETWORK BUILDER                                    │
│  network/builder.py                                                       │
│  - Assembles CanonicalNetwork from Facility, Lane, Product, Demand       │
│  - Resolves feasible arc set A                                           │
│  - Applies lane-eligibility and product-eligibility filters              │
│  - Applies service/SLA lane filters                                      │
└────────────────────────────────┬─────────────────────────────────────────┘
                                 │ CanonicalNetwork
                     ┌───────────┼────────────────────────┐
                     ▼           ▼                        ▼
          ┌──────────────┐ ┌──────────────┐   ┌───────────────────────┐
          │ CoG Screener │ │  Validation  │   │   Baseline Evaluator  │
          │ cog/         │ │  validation/ │   │   optimization/       │
          │ screener.py  │ │  checks.py   │   │   baseline.py         │
          └──────┬───────┘ └──────────────┘   └──────────┬────────────┘
                 │                                        │
          candidate sites                          baseline KPIs
                 │                                        │
                 ▼                                        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                       CORE MILP ENGINE                                   │
│  optimization/milp.py                                                     │
│                                                                           │
│  Inputs:  CanonicalNetwork + OptimizationConfig                          │
│  ┌──────────────────────────────────────────────────────────┐            │
│  │  CostEngine     InventoryModule   CarbonModule           │            │
│  │  costs/engine   inventory/module  carbon/module          │            │
│  └──────────────────────────────────────────────────────────┘            │
│                                                                           │
│  Builds PuLP model:                                                      │
│    y_i ∈ {0,1},  x_{ijvk} ≥ 0                                          │
│    Objective: Mode A/B/C/D                                               │
│    Constraints: C1..C15                                                  │
│                                                                           │
│  SolverInterface (solver.py)                                             │
│    ├── HiGHS (default, via PuLP)                                        │
│    ├── CBC                                                               │
│    └── Gurobi / CPLEX (pluggable)                                       │
│                                                                           │
│  Returns: OptimizationResult                                             │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
                    OptimizationResult
                                 │
                                 ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                         METRICS ENGINE                                   │
│  metrics/kpis.py                                                          │
│  - Derives all KPIs from OptimizationResult + CanonicalNetwork          │
│  - Flow analytics: corridors, hotspots, utilization                     │
│  - Carbon totals                                                         │
│  - Service level measurement                                             │
└──────────────────────────────────────────────────────────────────────────┘
                                 │
                    Structured KPIs + Analytics
                                 │
              ┌──────────────────┼────────────────────┐
              ▼                  ▼                    ▼
   ┌──────────────────┐ ┌─────────────────┐ ┌──────────────────────┐
   │  Scenario Engine │ │ Sensitivity Eng │ │  Resilience Engine   │
   │  scenarios/      │ │ sensitivity/    │ │  resilience/         │
   │  engine.py       │ │ engine.py       │ │  engine.py           │
   └──────────────────┘ └─────────────────┘ └──────────────────────┘
              │                  │                    │
     scenario results    sensitivity table     disruption analysis
              │                  │                    │
              └──────────────────┴────────────────────┘
                                 │
                    Structured outputs for Dashboard / API
```

---

## 2. Module Responsibilities

### schemas/network.py
**Purpose**: Typed data contracts for all network entities.
**Key Classes**:
- `NodeRole` — SUPPLIER / PLANT / WAREHOUSE / DC / MARKET / DARKSTORE
- `TransportMode` — ROAD / RAIL / AIR / SEA / INTERMODAL
- `FacilityRecord` — complete facility specification
- `ProductRecord` — product / SKU specification
- `DemandRecord` — demand at market for product in period
- `LaneRecord` — arc with cost/distance/lead-time/capacity
- `CanonicalNetwork` — assembled, validated network object
- `OptimizationConfig` — solver + model configuration

**Invariants enforced**:
- Lane endpoints must reference known facility IDs
- Product eligibility matrix consistent with facility list
- Demand node IDs must be markets

---

### network/builder.py
**Purpose**: Assembles `CanonicalNetwork` from raw schema objects.
**Key Function**: `build_network(facilities, products, demands, lanes, config)`
**Outputs**:
- Resolved feasible arc set A
- Aggregated demand per market/product
- Indexed facility and lane maps for MILP builder

---

### costs/engine.py
**Purpose**: Abstract transport cost computation.
**Interface**: `CostEngine.compute(lane, flow, product) → float`
**V1**: `rate_per_unit × flow`
**Future V2**: Fixed trip cost + variable cost + fuel surcharge + toll

---

### inventory/module.py
**Purpose**: Compute inventory cost for open facilities.
**Interface**: `InventoryModule.compute_cost(facility, assigned_demands, config) → float`
**V1**: Safety stock formula SS = z × σ × √LT
**Assumption**: Normal demand distribution (documented, overridable)
**Future**: Replace with stochastic model without changing MILP interface

---

### carbon/module.py
**Purpose**: Compute CO₂ emissions from flow decisions.
**Interface**: `CarbonModule.compute(flow, lane, product) → float`
**Formula**: dist_km × weight_kg × emission_factor_kg_co2_per_tonne_km / 1000
**Scope**: Transport-only by default; warehouse emissions extensible

---

### service/module.py
**Purpose**: Translate service requirements into MILP constraints or lane filters.
**Interface**: `ServiceModule.get_eligible_lanes(market, all_lanes) → list[LaneRecord]`
**Modes**:
- TRANSIT_TIME_FILTER: removes ineligible lanes from A
- PENALTY: adds penalty cost for late delivery (requires shortage variable)
- CSL_CONSTRAINT: constrains fraction of demand served on time

---

### cog/screener.py
**Purpose**: Weiszfeld center-of-gravity as a GEOGRAPHIC SCREENING TOOL only.
**Outputs**: Suggested geographic coordinates for candidate DC placement.
**NOT used as**: Final optimization decision.
**Documentation**: Clearly labels output as "Screening Output — Not Optimal"

---

### validation/checks.py
**Purpose**: Pre-solve validation of network consistency.
**Checks**:
- Total supply ≥ total demand
- All demand markets have at least one eligible inbound arc
- No negative costs or demands
- All referenced IDs exist
- Capacity not negative
- Lane endpoints valid

---

### optimization/milp.py
**Purpose**: Core MILP builder. Single entry point for solving.
**Interface**: `solve(network: CanonicalNetwork, config: OptimizationConfig) → OptimizationResult`
**Internals**:
1. Pre-processes network (index maps, arc set A)
2. Instantiates PuLP model
3. Creates y_i binary variables
4. Creates x_{ijvk} continuous flow variables
5. Builds cost engine, inventory module, carbon module
6. Adds all active constraints (C1..C15)
7. Calls SolverInterface
8. Extracts and returns OptimizationResult

---

### optimization/solver.py
**Purpose**: Solver abstraction layer.
**Interface**: `SolverInterface.solve(prob: LpProblem, config) → SolverMetadata`
**Implementations**: HiGHS (default), CBC, Gurobi (conditional import), CPLEX (conditional)
**Returns**: status, objective, MIP gap, runtime, solver version

---

### optimization/baseline.py
**Purpose**: Evaluate KPIs for the current-state network WITHOUT optimization.
**Use case**: "What does the network cost today?" before any optimization.
**Method**: Solves the LP relaxation with all y_i fixed to their current state.

---

### metrics/kpis.py
**Purpose**: Derive all dashboard KPIs from OptimizationResult.
**Outputs**:
- NetworkKPIs (cost breakdown, utilization, service, carbon, distance)
- FlowAnalytics (top corridors, hotspots, underutilized/overutilized nodes)
- GoNoGoEvidence (structured data for decision support)

---

### scenarios/engine.py
**Purpose**: Apply scenario overrides to base network and re-solve.
**Interface**: `ScenarioEngine.run(network, scenario, config) → ScenarioResult`
**Scenario types**: CLOSE_FACILITY, ADD_FACILITY, CAPACITY_CHANGE, DEMAND_CHANGE,
COST_CHANGE, LANE_DISRUPTION, FACILITY_DISRUPTION, CARBON_FACTOR_CHANGE

---

### sensitivity/engine.py
**Purpose**: Systematic parameter variation to understand model sensitivity.
**One-way sweep**: Vary one parameter over a range; record objective and KPIs.
**Two-way sweep**: Vary two parameters over a grid.
**Tornado output**: Sensitivity rank ordered by impact.

---

### resilience/engine.py
**Purpose**: Evaluate network behavior under disruption scenarios.
**Disruptions**: Facility failure, lane failure, capacity loss, demand surge.
**Metrics**: cost_delta, service_delta, unmet_demand, rerouted_flow.
**NOT**: An arbitrary resilience score.

---

## 3. Data Flow for a Typical Optimization Run

```
User provides:
  facilities.json   (list of FacilityRecord)
  products.json     (list of ProductRecord)
  demands.json      (list of DemandRecord)
  lanes.json        (list of LaneRecord)
  config.json       (OptimizationConfig)
        │
        ▼
  validation/checks.py  →  ValidationReport (fail-fast)
        │
        ▼
  network/builder.py    →  CanonicalNetwork
        │
        ├──► cog/screener.py   →  CandidateLocationReport (optional)
        │
        ├──► optimization/baseline.py  →  BaselineResult
        │
        └──► optimization/milp.py     →  OptimizedResult
                    │
                    └──► metrics/kpis.py   →  KPIReport
                                │
                                ├──► scenarios/engine.py  →  [ScenarioResult, ...]
                                │
                                └──► sensitivity/engine.py →  SensitivityReport
```

---

## 4. Extension Points

| Future Capability | Extension Point | Notes |
|---|---|---|
| Multi-period (T > 1) | Add T dimension to x vars and constraints | Schema T field already present |
| Multi-echelon (S→P→W→M) | Add S, P layers to CanonicalNetwork | Node roles already enumerated |
| Manufacturing decisions | Add production variable `p_{ikt}` | New constraint set |
| Supplier selection | Add supplier binary `y_s ∈ {0,1}` | Mirror facility pattern |
| Stochastic inventory | Replace `SafetyStockModule` | Same interface, different impl |
| Nonlinear transport cost | Replace `CostEngine` | Same interface, PWL in MILP |
| Dark stores / last-mile | Add DARKSTORE node role | Schema ready |
| Retail / POS | Add RETAIL node role | Schema ready |
| Benders decomposition | Decompose `y` and `x` subproblems | For large-scale problems |
| Gurobi / CPLEX | New `SolverImplementation` subclass | SolverInterface ready |
| Real-world distance | Replace Euclidean with OSRM/HERE/Google | LaneRecord.distance_km param |
| Carbon marketplace | CarbonModule with offset pricing | Carbon price in OptimizationConfig |
