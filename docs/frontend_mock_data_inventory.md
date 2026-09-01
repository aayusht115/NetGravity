# NetGravity — Frontend Mock Data Inventory

**Purpose:** Comprehensive catalog of all mock, static, hardcoded, and sample data currently present in the frontend prototype, classified according to production replacement path.

---

## 1. Classification Taxonomy

- **REAL UI CONSTANT:** Pure client-side static presentation values (e.g. SVG icons, animation constants, coordinate fan radii for de-overlapping icons on Leaflet/WebGL canvas).
- **DISPLAY-ONLY:** Presentational formatting functions or label lookups with no underlying business logic.
- **DEMO PLACEHOLDER:** Mock data kept as an offline/fallback demo asset, strictly isolated from the production API path.
- **BACKEND-DERIVED:** Mock objects that represent server-side domain entities and must be fetched from the backend API.
- **BUSINESS LOGIC:** Calculations, KPI formulas, or metric derivations that must be removed from the frontend and replaced by authoritative backend outputs.
- **UNKNOWN:** Unclassified items requiring architectural alignment.

---

## 2. Mock Data Inventory by Module

### A. `app/frontend/js/data.js`

| Data Object / Variable | Classification | Description & Current Content | Target Backend Source & Endpoint |
|---|---|---|---|
| `PLANTS` | `BACKEND-DERIVED` | 4 Plant nodes (Baddi, Pune, Hyderabad, Kolkata) with lat, lng, capacity, throughput, region, status | `GET /orchestrator/twin/snapshots/<id>` (Facility summaries) |
| `DCS` | `BACKEND-DERIVED` | 5 DC nodes (Delhi NCR, Mumbai, Bengaluru, Kolkata, Guwahati) with capacity, fixedCost, handlingCost, utilPct | `GET /orchestrator/twin/snapshots/<id>` and `GET /api/kpis/facilities/<id>` |
| `MARKETS` | `BACKEND-DERIVED` | 10 Demand markets (Delhi, Mumbai, etc.) with demand quantity, SLA days, priority | `GET /orchestrator/twin/snapshots/<id>` (Market demands) |
| `LANES` | `BACKEND-DERIVED` | 26 Freight corridors (Plant→DC, DC→Market) with cost, distance, lead time, flow units | `GET /orchestrator/twin/snapshots/<id>` or `/orchestrator/twin/states/<id>` |
| `deoverlapNodes()` | `REAL UI CONSTANT` | Cosmetic visual offset logic (0.9 deg fan) to prevent stacked icons on Leaflet/WebGL | Retained in frontend UI map layer for presentation only |
| `DEMAND_HISTORY` | `BACKEND-DERIVED` | 24-month historical monthly demand sequence | `GET /api/forecast` (`ForecastingService`) |
| `FORECAST` | `BACKEND-DERIVED` | 6-month projected demand cone (mean, upper, lower, +14.2% growth, breach callout) | `GET /api/forecast` (`ForecastingService`) |
| `EXTERNAL_SIGNALS` | `BACKEND-DERIVED` | 3 Market intelligence signals (GDP growth, diesel price, Delhi-Jaipur expressway) | `GET /api/signals` (`ExternalSignalRouter`) |
| `DATA_QUALITY` | `BACKEND-DERIVED` | Record stats (4,820 total, 98.4% valid) and 8 data quality issues | `GET /api/ingestions/<id>/reviews` (`IngestionService`) |
| `CONTRACT_DEMO` | `BACKEND-DERIVED` | TransCorp / SpeedFreight extracted contract clauses, surcharges, effective costs | `GET /api/ingestions/<id>/reviews` (`IngestionService` contract pipeline) |
| `SCHEMA_MAPPING` | `BACKEND-DERIVED` | Distributor field mapping confidences (Qty -> Demand_Units) | `GET /api/ingestions/<id>/draft` (`IngestionService` field classifier) |
| `SCENARIOS` | `BACKEND-DERIVED` | 5 pre-computed scenarios (`SCN_ACTUAL`, `SCN_REBALANCE`, `SCN_USER_1`, `SCN_USER_2`, `SCN_AI_REC_4`) with cost, SLA, util %, carbon, trade-offs | `GET /api/scenarios` and `POST /api/scenarios/simulate` (`ScenarioBuilder`, `OptimizationClient`, `KPIRegistry`) |

---

### B. `app/frontend/js/projects.js`

| Data Object / Variable | Classification | Description & Current Content | Target Backend Source & Endpoint |
|---|---|---|---|
| `PROJECTS` | `BACKEND-DERIVED` | 5 Mock workspaces (`India Network 2024`, `North Region Revamp`, etc.) | `GET /api/projects` (`ProjectStateStore`) |
| `REGIONS` | `REAL UI CONSTANT` | Static dropdown list of regions (North India, Pan India, etc.) | Retained in UI form helpers |
| `CLIENTS` | `DEMO PLACEHOLDER` | Static client name autocomplete suggestions | Retained as UI autocomplete helper or fetched via `/api/projects/clients` |

---

### C. `app/frontend/js/ingestion.js`

| Data Object / Variable | Classification | Description & Current Content | Target Backend Source & Endpoint |
|---|---|---|---|
| `SCHEMA_FIELDS` | `REAL UI CONSTANT` | Canonical schema dropdown choices | Sourced from `SCHEMA_FIELDS` / Ingestion canonical dictionary |
| `baseMappingRows()` | `DEMO PLACEHOLDER` | Fallback sample field mappings for Excel ingestion demo | Replaced by `GET /api/ingestions/<id>/draft` |
| `CONTRACT_VENDOR` | `DEMO PLACEHOLDER` | Hardcoded vendor terms for PDF review | Replaced by `GET /api/ingestions/<id>/reviews` |
| `mockRowsAnalyzed()` | `DEMO PLACEHOLDER` | Deterministic hash for demo row counts | Replaced by `IngestionSession.file_summary.total_rows` |

---

### D. `app/frontend/js/chatbot.js`

| Data Object / Variable | Classification | Description & Current Content | Target Backend Source & Endpoint |
|---|---|---|---|
| `FAQ_KNOWLEDGE_BASE` | `DEMO PLACEHOLDER` | 6 pre-authored consulting questions and answers with action links | Isolated as fallback quick prompts; query submission calls `POST /orchestrator/chat` |

---

### E. `app/frontend/js/insight-detail.js`

| Data Object / Variable | Classification | Description & Current Content | Target Backend Source & Endpoint |
|---|---|---|---|
| `emailBody()` | `DISPLAY-ONLY` | Formatter constructing email draft string from insight view model | Retained as presentational template function |
| `modalTableRows()` | `DISPLAY-ONLY` | Formatter mapping before/after impact to table rows | Retained as presentational template function |
| Action item view models | `BACKEND-DERIVED` | Insight cards (Critical Capacity Warning, Cost Reduction, etc.) | `POST /orchestrator/insights` and `GET /orchestrator/executions/<id>/trace` |
