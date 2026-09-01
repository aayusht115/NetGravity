# NetGravity — Frontend ↔ Backend Integration Specification
## Production Application Assembly & Wiring Architecture

**Status:** Completed / Active Integration  
**Test Suite Status:** 2,460 passed, 4 skipped, 0 failed (129.98s)  
**UI Source of Truth:** `app/frontend/index.html` (Finalized Prototype, 0 visual changes)  
**Authoritative Business Logic:** `netgravity` Python backend & Orchestrator Control Plane  
**Authoritative KPI Layer:** Phase 9.1 `KPIRegistry` (`netgravity/orchestrator/metrics/registry.py`)

---

## 1. System Architecture & Request Lifecycle

```
┌────────────────────────────────────────────────────────────────────────┐
│               FINALIZED FRONTEND PROTOTYPE (HTML5 / ES6)               │
│   Landing · Onboarding · Projects · Ingestion · Home Cockpit · Twin   │
│       Facility KPIs · Forecast · Scenarios · Deep-Dive · Assistant     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   FRONTEND INTEGRATION & CLIENT LAYER                   │
│   • Central API Client (base URL, auth token, req-id, error handling)  │
│   • Typed Service Layer (projects, ingestion, twin, kpi, scenarios)    │
│   • Contract Adapters & Normalizers (Domain DTO -> UI Presentation)    │
│   • State Management (Server Cache vs Transient UI Selection State)    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP REST / JSON
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                      BACKEND API & ADAPTER LAYER                       │
│   • Flask Application Gateway (/api/status, /api/auth, /api/projects)  │
│   • Ingestion Blueprint (/api/ingestions/*)                            │
│   • Orchestrator Control Plane Blueprint (/orchestrator/*)             │
│   • Authoritative KPI & Scenario Adapters (/api/kpis, /api/scenarios)   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                   ORCHESTRATOR & DETERMINISTIC ENGINES                 │
│   • Controlled Orchestrator (Intent -> Plan -> Validator -> Executor)  │
│   • MILP Optimization Engine (PuLP / HiGHS / CBC)                      │
│   • Authoritative KPIRegistry & Evidence Package (Phase 9.1)           │
│   • Digital Twin State Materializer & Diff Engine                      │
│   • Scenario Builder & Stress-Testing Engine                           │
│   • Ingestion Parser & AI Reconciliation Pipeline                       │
│   • LLM Reasoning Runtime & Governance Action Classifier                │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Implemented Screen → API → Backend Capability Matrix

| Screen ID | Prototype Screen | Frontend Module | Operation | Method | Endpoint | Backend Capability | Status |
|---|---|---|---|---|---|---|---|
| **O1** | Landing & Sign In | `landing.js`, `auth.js` | User login | `POST` | `/api/auth/login` | Session token handler | Active |
| **O2** | Create Account | `landing.js`, `auth.js` | User sign up | `POST` | `/api/auth/signup` | User workspace registration | Active |
| **O3** | Create Project | `projects.js` | Create workspace | `POST` | `/api/projects` | Project State Store | Active |
| **O4** | Select Project | `projects.js` | List & load projects | `GET` | `/api/projects`, `/api/projects/<id>` | Project & Snapshot context | Active |
| **O5/O6**| Data Ingestion | `ingestion.js` | Upload, mapping review & finalize | `POST` | `/api/ingestions`, `/api/ingestions/<id>/reviews`, `/api/ingestions/<id>/finalize` | `IngestionService` (`CAP_EXTRACT`) | Active |
| **S1** | Home Overview | `app.js` | Network KPIs & Attention Feed | `GET` / `POST` | `/api/kpis/network`, `/orchestrator/insights`, `/orchestrator/twin/snapshots/<id>` | `KPIRegistry`, `ReasoningAgent`, `TwinService` | Active |
| **S2** | Digital Twin | `twin3d.js`, `map.js` | 3D WebGL & 2D Leaflet topology | `GET` | `/orchestrator/twin/snapshots/<id>`, `/orchestrator/twin/states/<id>` | `TwinService` (`CAP_TWIN_PUBLISH`) | Active |
| **S3** | Facility KPIs | `app.js`, `charts.js` | Node telemetry & cost split | `GET` | `/api/kpis/facilities/<id>` | `KPIRegistry.facility_kpis` | Active |
| **S4** | Demand Forecast | `app.js`, `charts.js` | 24m history + 6m forecast, signals | `GET` | `/api/forecast`, `/api/signals` | `ForecastingService`, `ExternalSignalRouter` | Active |
| **S5–S8**| Scenario Planning | `scenarios.js`, `map.js` | Comparison table, What-If simulation | `GET` / `POST` | `/api/scenarios`, `/api/scenarios/simulate`, `/orchestrator/twin/compare` | `ScenarioBuilder`, `OptimizationClient`, `KPIRegistry` | Active |
| **S9** | Explainability | `insight-detail.js` | Before/after metrics, AI reasoning, review modal | `POST` | `/orchestrator/insights`, `/orchestrator/approvals/<id>`, `/orchestrator/executions/<id>/trace` | `ReasoningAgent`, `GovernancePolicy` | Active |
| **S10** | Ask Netgravity | `chatbot.js` | Conversational query with deep links | `POST` | `/orchestrator/chat`, `/orchestrator/chat/<id>/history` | `ChatService` | Active |
| **S11/12**| Guardrails & Logs | `agent.js`, `agent-reasoning.js` | Execution traces & threshold alerts | `GET` | `/orchestrator/executions/<id>/trace`, `/api/kpis/thresholds` | `AuditLogger`, `KPIRegistry.evaluate_thresholds` | Active |

---

## 3. Authoritative KPI Mapping Matrix

| KPI Label | UI Screen | Backend Source | API Field / Method | Unit | Status Handling |
|---|---|---|---|---|---|
| **Total Network Cost** | S1 Top Card, S5 Scenarios | `KPIRegistry.network_kpis()` | `data.business_network_cost` | ₹ Lakhs / mo | If infeasible: `INFEASIBLE`. Never default 0. |
| **On-Time SLA** | S1 Top Card, S5 Scenarios | `KPIRegistry.network_kpis()` | `data.pct_demand_in_sla` | % (0–100) | If unserved demand > 0: `INSUFFICIENT_EVIDENCE`. |
| **Peak DC Utilisation** | S1 Top Card, S3 Facility | `KPIRegistry.network_kpis()` | `data.max_utilization_pct` | % (0–100) | Sourced directly from solver `FacilityDecision`. |
| **Scope 3 Carbon** | S1 Top Card, S5 Scenarios | `KPIRegistry.network_kpis()` | `data.total_carbon_kg` | kg CO₂ / mo | Sum of `FlowDecision.carbon_kg`. |
| **Average Utilisation** | S1 Cockpit, S5 Scenarios | `KPIRegistry.network_kpis()` | `data.avg_utilization_pct` | % (0–100) | Mean across all active open distribution centers. |
| **Demand Fill Rate** | S1 Overview, S3 Market | `KPIRegistry.network_kpis()` | `data.demand_fill_rate` | Ratio (0–1) | `total_served / total_demand`. |
| **Transport Cost** | S3 Cost Breakdown, S5 Scenarios | `CostBreakdown.transport_cost` | `data.transport_cost` | ₹ Lakhs / mo | Solver objective transport component. |
| **Facility Fixed Cost**| S3 Cost Breakdown, S5 Scenarios | `CostBreakdown.facility_cost` | `data.facility_cost` | ₹ Lakhs / mo | Sum of active facility fixed charges. |
| **Handling Cost** | S3 Cost Breakdown, S5 Scenarios | `CostBreakdown.handling_cost` | `data.handling_cost` | ₹ Lakhs / mo | Throughput × unit handling rate. |
| **Facility Resilience (REI)**| S3 Telemetry, S11 Logs | `KPIRegistry.facility_resilience_kpis()` | `data.rei` | Resilience metric | `REIClient` computed disruption impact. |
| **Risk Factor (RF)** | S3 Telemetry, S11 Logs | `KPIRegistry.facility_risk_kpis()` | `data.risk_score` | Score (0–1) | `assess_network_risk()` formula. |
| **Scenario Cost Delta** | S5 Cards, S9 Deep-Dive | `KPIRegistry.scenario_comparison()` | `data.cost_delta_value` | ₹ Lakhs / mo | Computed strictly by `KPIRegistry.scenario_comparison()`. |

---

## 4. Frontend Integration Layer Layout

```
app/frontend/js/integration/
├── config.js               # API URL & runtime environment configuration
├── errors.js               # Normalized error model & hierarchy
├── api-client.js           # Centralized fetch client (Bearer auth, timeouts, correlation IDs)
├── services/
│   ├── auth-service.js     # /api/auth
│   ├── project-service.js  # /api/projects
│   ├── ingestion-service.js# /api/ingestions
│   ├── twin-service.js     # /orchestrator/twin
│   ├── kpi-service.js      # /api/kpis
│   ├── scenario-service.js # /api/scenarios
│   ├── forecast-service.js # /api/forecast & /api/signals
│   ├── reasoning-service.js# /orchestrator/insights & /orchestrator/approvals
│   └── chat-service.js     # /orchestrator/chat
└── mappers/
    ├── project-mapper.js   # Project workspace DTO normalizer
    ├── twin-mapper.js      # NetworkStateView to Leaflet/Three.js normalizer
    ├── kpi-mapper.js       # KPIResult and AuthoritativeEvidencePackage normalizer
    ├── scenario-mapper.js  # ScenarioComparison and What-If DTO normalizer
    └── insight-mapper.js   # ReasoningAgent output normalizer
```
