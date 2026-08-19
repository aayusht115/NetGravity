# NetGravity — AI Decision Intelligence for Supply Chain & Logistics Networks

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![MILP Core](https://img.shields.io/badge/Solver-PuLP%20%7C%20HiGHS%20%7C%20CBC-purple.svg)](https://github.com/coin-or/pulp)
[![Tests](https://img.shields.io/badge/Automated%20Tests-348%20Passing-brightgreen.svg)](netgravity/tests/)
[![Ingestion](https://img.shields.io/badge/Ingestion-Structured%20%7C%20Excel%20%7C%20PDF%20%7C%20Signals-teal.svg)](#4-data-ingestion-pipeline-layer-1)
[![Architecture](https://img.shields.io/badge/Architecture-Deterministic%20MILP%20%2B%20AI%20Orchestrator-orange.svg)](#system-architecture)

> **NetGravity** is an enterprise-grade decision-intelligence and network optimization platform designed for modern logistics networks. It bridges the gap between mathematically rigorous Mixed-Integer Linear Programming (MILP) network optimization and intuitive, AI-orchestrated executive decision-making.

---

## 1. Executive Summary & Core Paradigm

Traditional supply chain planning tools force a trade-off between complex mathematical solvers that lack executive interpretability, and AI dashboards that generate unverified hallucinations.

NetGravity solves this with an architectural separation of concerns:
```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                    THE NETGRAVITY PIPELINE                                      │
│                                                                                                 │
│  Data Ingestion ──► Digital Twin ──► Demand Forecast ──► AI Reasoning ──► Scenario Generation   │
│         ▲                 │                │                  │                 │               │
│         │                 ▼                ▼                  ▼                 ▼               │
│  Action ◄── Recommendation ◄── AI Challenger ◄── Scenario Comparison ◄── Exact MILP Solver     │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Mathematical Source of Truth**: The deterministic Mixed-Integer Linear Programming (MILP) solver is the sole authority on optimization, costs, flow assignments, SLA compliance, and capacity constraints. AI **never** calculates or replaces mathematical optimization results.
- **AI Decision Orchestration**: The AI agent acts as a cognitive copilot that monitors telemetry, identifies anomalies, generates candidate scenarios, stress-tests options with a challenger engine, and structures evidence-backed recommendations with complete data provenance.
- **Human-in-the-Loop Governance**: Multi-tier governance ensures high-impact strategic decisions (facility closures, major CapEx investments) always require explicit human sign-off.

---

## 2. Clean Repository Directory Structure

The repository is organized into five focused modules designed for clear client evaluation:

```
NetGravity/
│
├── README.md                          # Master architectural overview & business impact
├── requirements.txt                   # Production dependencies (PuLP, HiGHS, Flask, Pydantic)
├── pyproject.toml                     # Python package metadata & test configurations
├── smoke_test.py                      # 1-command verification suite (< 0.5s execution)
│
├── app/                               # 🌐 Interactive Web Application & Decision Cockpit
│   ├── backend/
│   │   ├── app.py                     # Python Flask API & telemetry endpoints
│   │   └── models/                    # Backend data contracts & schemas
│   ├── frontend/
│   │   ├── index.html                 # Executive Decision Cockpit & tab shells
│   │   ├── css/
│   │   │   └── style.css              # Consulting-grade design system (light theme)
│   │   └── js/
│   │       ├── app.js                 # Central controller, state & view router
│   │       ├── data.js                # Canonical data layer & scenario state
│   │       ├── map.js                 # Interactive Leaflet network digital twin
│   │       ├── charts.js              # Chart.js time-series & multi-scenario radars
│   │       ├── scenarios.js           # Scenario workspace & MILP runner
│   │       └── agent.js               # AI decision agent trace & tool orchestration
│   └── standalone/
│       └── netgravity_standalone.html # Portable zero-dependency single-file HTML build
│
├── docs/                              # 📚 Client & Technical Documentation
│   ├── ingestion_business_rules.md    # Plain-language ingestion rules reference
│   ├── mathematical_model.md          # Full mathematical formulation & notation
│   ├── model_architecture.md          # Echelon architecture & pipeline design
│   ├── model_foundation.md            # Cost functions & inventory theory
│   ├── v1_0_audit.md                  # Verification audit trail & benchmark logs
│   └── *.md                           # Validation reports & Case 16 references
│
├── data/                              # 📥 Data zones (gitignored; .gitkeep only)
│   ├── raw/                           # Immutable source files, never edited
│   ├── standardized/                  # Post-mapping intermediates + AI caches
│   ├── curated/                       # Immutable versioned network snapshots
│   └── mock/india/                    # Sample dataset for local runs & demos
│
├── .env.example                       # Every environment variable, documented
│
├── netgravity/                        # ⚡ Mathematical Optimization Engine (Source of Truth)
│   ├── ingestion/                     # 📥 Layer 1 — Data Ingestion Pipeline
│   │   ├── cli.py                     # `python -m netgravity.ingestion`
│   │   ├── pipeline.py                # Orchestrates one end-to-end run
│   │   ├── builder.py                 # Assembles the CanonicalNetwork
│   │   ├── config.py                  # All env vars & the provider switch
│   │   ├── field_aliases.py           # Client workbook names -> engine names
│   │   ├── snapshot.py                # Content-hashed versioned snapshots
│   │   ├── adapters/                  # structured | distributor | contracts | signals
│   │   ├── ai/                        # LLM client, prompts, stubs, extraction cache
│   │   ├── guardrails/                # External-signal bucket policy (thresholds.yaml)
│   │   ├── storage/                   # Local / Azure Blob abstraction
│   │   ├── validation/                # Row-level checks (R-001..R-020)
│   │   └── tests/                     # Ingestion test suite
│   ├── optimization/                  # Exact MILP formulations (PuLP / HiGHS / CBC)
│   ├── network/                       # Supply chain digital twin (Plants, DCs, Markets, Arcs)
│   ├── costs/                         # Cost accounting, handling rates & dollar reconciliation
│   ├── inventory/                     # Safety stock, cycle stock & inventory holding models
│   ├── service/                       # Lead times, multi-tier SLAs & OTIF calculation
│   ├── carbon/                        # Scope 3 transportation & facility carbon modeling
│   ├── metrics/                       # Performance scorecards & executive analytics
│   ├── scenarios/                     # Interactive scenario generation & parameter sweeps
│   ├── resilience/                    # Disruption testing & AI Challenger stress engine
│   ├── sensitivity/                   # Demand & cost elasticity sensitivity curves
│   ├── diagnostics/                   # Infeasibility diagnosis & bottleneck detection
│   ├── schemas/                       # Pydantic data schemas & validation models
│   ├── assumptions/                   # Explicit policy constants & constraint definitions
│   ├── validation/                    # Data integrity & sanity checks
│   └── tests/                         # Engine test suite
│
└── scripts/                           # 🛠️ Tooling & Build Scripts
    └── build_standalone.py            # Automated single-file HTML compiler
```

---

## 3. Interactive Web Application & Decision Cockpit

The web application (`app/`) delivers a streamlined executive workflow:

### Primary Modules
1. **Home (Decision Cockpit)**:
   - **Executive Control Bar**: Dynamic *View By* (`DC` / `Plant`), *Facility Selector* (`Delhi NCR DC`, `Mumbai DC`, `Bengaluru DC`, etc.), and *Period Selector* (`1 Aug – 31 Aug 2026`, etc.).
   - **4 Primary KPI Cards**: Capacity Utilisation, On-Time Service SLA, Total Cost, and Inventory Days of Supply with period-over-period delta comparisons.
   - **Facility Performance & Analytics Dashboard**: Full-page analytics view accessed via `View more KPIs →` showcasing 12-month historical throughput vs capacity limits, cost breakdowns, corridor flow distributions, and live ERP/WMS/TMS sync telemetry.
   - **Structured Insight Cards**: Actionable anomalies with severity color bars, impact tags, and concise "Why I found this" explanations.
   - **Slide-over Evidence Drawer**: Deep-dive evidence chain with explicit provenance badges (`MODEL FACT`, `FORECAST`, `EXTERNAL SIGNAL`, `AI ASSESSMENT`).

2. **Digital Twin**:
   - Geographic visualization of 4 Manufacturing Plants, 5 Distribution Centres, and 10 Demand Markets across India using Leaflet.
   - Toggle between **Actual**, **Optimised Base**, and **Recommended** network states.
   - Slide-in facility inspection panel detailing capacity, utilisation, handling rates, and connected lane arcs.

3. **Scenario Planning & Comparison**:
   - Interactive scenario workspace comparing baseline vs alternative interventions (e.g., flow rebalancing, facility closure, candidate DC opening, capacity expansion).
   - Multi-metric comparison table, cost component breakdown charts, and 5-axis performance radar.
   - Interactive Scenario Builder triggering solver evaluations.

4. **Demand Forecasting & External Signals**:
   - 24-month historical demand curve with 6-month predictive projection, confidence bands, and facility capacity breach warning threshold.
   - Macroeconomic, regulatory, and weather external signal cards.

5. **AI Recommendation & Governance**:
   - Tiered governance classification (`Tier 1 INFORM`, `Tier 2 PROPOSE`, `Tier 3 HUMAN DECISION`).
   - Impact scorecard (Cost $\downarrow 7.8\%$, SLA $96.7\%$, Carbon $\downarrow 6.2\%$).
   - Rejection rationale for sub-optimal alternatives, risk mitigation checklist, and automated Analyst Briefing Email generator.

---

## 4. Data Ingestion Pipeline (Layer 1)

Turns messy real-world inputs into one validated `CanonicalNetwork` — the single
data contract the MILP engine consumes. Nothing reaches the solver without
passing through here.

### Four Source Paths

| Path | Input | AI? | What it produces |
|---|---|---|---|
| **Structured** | ERP / WMS / TMS exports in a known format | No — pure deterministic parsing | Facility, market, product, demand, lane records |
| **Distributor** | Excel files, a different layout from every distributor | Yes — column mapping | The same records, re-mapped onto canonical fields |
| **Contracts** | Freight contracts & rate-card PDFs | Yes — clause extraction | Base rates + hidden surcharge rules |
| **Signals** | Dated external news / macro / weather records | Guardrail scoring | Bucketed, scored signals for forecast & scenario use |

**Core principle — logic calculates, AI narrates.** The AI proposes mappings and
extracts clauses; every number that reaches the optimizer is computed by
deterministic code. Unit conversions, effective rates and cost adjustments are
arithmetic, never model output.

### Two Validation Layers

Both run; they answer different questions and neither replaces the other.

- **Row-level** (`ingestion/validation/`, codes `R-001`–`R-020`) — *is this row
  even parseable?* Runs before assembly. Unambiguous unit errors are **repaired
  loudly** rather than rejected: dropping a row silently deletes real demand and
  the solver then returns a confident answer to the wrong problem.
- **Network-level** (`netgravity/validation/checks.py`, codes `V-001`–`V-014`) —
  *will this network actually solve?* Runs after assembly.

### Data Conventions

- **Client field names are the contract.** `field_aliases.py` maps the names in
  `NetGravity_Input_Data_Fields.xlsx` (`Facility_ID`, `Daily_Demand_Units`,
  `Unit_Cost`, …) onto internal engine fields. Matching ignores case and
  separators; unrecognised columns are preserved, not dropped. When the workbook
  changes, update that file — not the parsers.
- **Everything is normalised to MONTH.** `OptimizationConfig.cost_period`
  defaults to `MONTH`, so a `Daily_Demand_Units` column is converted on the way
  in (×30), with the conversion recorded as an `R-020` note. Standard deviation
  scales by √30, not 30 — it is a deviation of independent daily draws, not a
  sum. Skipping this understates monthly demand against monthly facility cost by
  ~30× and biases the optimizer toward closing sites that should stay open.

### AI Provider — One Switch

The pipeline runs **without any API key**: the AI client returns canned stub
responses, every stubbed result is labelled as such, and the full test suite
passes offline. Supplying a key is the only change needed to go live.

```bash
NETGRAVITY_USE_CLAUDE=false        # OpenAI / ChatGPT  (default)
NETGRAVITY_USE_CLAUDE=true         # Anthropic Claude

NETGRAVITY_OPENAI_API_KEY=...      # keep both keys set and flipping the
NETGRAVITY_ANTHROPIC_API_KEY=...   # switch is the only edit ever needed
```

The model follows the switch automatically (`gpt-4o-mini` / `claude-sonnet-4-5`);
override with `NETGRAVITY_LLM_MODEL`. A key that doesn't match the selected
provider is detected and warned about *before* any call is made. Every vendor
call is isolated in `ai/client.py` — adding a provider is a single-file change.

**Failures are never silent.** If a live call fails, the run degrades to stub
data but the report shows `[AI: FAILED -> STUB DATA]` and states plainly that
the numbers are not a real extraction. Set `NETGRAVITY_LLM_STRICT=true` — for
any run whose numbers someone might act on — and a failed call fails the run
instead.

### Cost Control

- **Distributor mappings are cached per distributor.** The AI proposes a mapping
  once, a human confirms it via CLI, and every later file from that distributor
  skips the model entirely. Only 5 sample rows are ever sent, so file size does
  not affect cost.
- **Contract extractions are cached by document content.** Keyed on a hash of
  the extracted text, not the filename — so an amended rate card re-extracts
  automatically, while an unchanged one costs nothing on every subsequent run.
  Stub output is never cached, which prevents canned demo data from being served
  after a real key is added.

### Ingestion CLI

```bash
# Ingest the sample dataset and print a validation report
python -m netgravity.ingestion --source data/mock/india

# Ingest, then hand the network to the MILP engine
python -m netgravity.ingestion --source data/mock/india --solve

# Show what the AI proposed and why (mappings, extracted clauses, signal verdicts)
python -m netgravity.ingestion --source data/mock/india --explain

# Parse and validate only — write nothing
python -m netgravity.ingestion --source data/mock/india --dry-run

# Human-in-the-loop mapping confirmation
python -m netgravity.ingestion --list-mappings
python -m netgravity.ingestion --confirm-mapping distributor_north_raw
```

Each successful run writes an immutable, content-hashed snapshot to
`data/curated/`, so any result can be traced back to the exact inputs that
produced it.

### Runs Locally; Built to Scale Later

**Today this runs entirely on a local machine** — local filesystem, local
sample data, no cloud account required. That is the supported setup.

Cloud portability is handled as a *discipline*, not a pending migration: it
costs nothing now and removes rework later. Three habits carry it:

- **No hardcoded paths.** Every read and write goes through the storage
  abstraction in `ingestion/storage/`, which exposes a blob-like
  `zone` + `key` interface. `LocalStorage` writes to disk today; swapping the
  implementation is the entire change.
- **All configuration from environment variables.** Nothing is compiled in —
  credentials, paths, provider choice and model all come from `config.py`,
  documented in `.env.example`.
- **Vendor calls isolated to one function.** `ai/client.py` is the only file
  that imports an SDK.

When the team is ready to deploy, the migration is configuration rather than a
rewrite — `NETGRAVITY_STORAGE_BACKEND=local` becomes `azure_blob`, and the API
key moves from `.env` to a secret store. No ingestion code changes.

> **Not yet built:** the Azure Blob backend is a deployment stub, and Azure
> OpenAI is deliberately rejected rather than silently routed to public OpenAI
> (it needs a distinct client with `azure_endpoint` / `api_version` /
> `azure_deployment`). Both are follow-on work for when deployment actually
> starts — flagged here so nobody mistakes the abstraction for a finished
> integration.

---

## 5. Mathematical Optimization Core (MILP)

The optimization engine is built in Python using **PuLP** with support for **HiGHS** and **CBC** solvers.

### Mathematical Formulation
$$\min \sum_{i \in \mathcal{F}} f_i y_i + \sum_{(i,j) \in \mathcal{A}} \sum_{p \in \mathcal{P}} c_{ijp} x_{ijp} + \sum_{j \in \mathcal{D}} h_j \cdot (\text{Inventory}_j) + \sum_{(i,j) \in \mathcal{A}} e_{ij} x_{ij} \cdot \lambda_{\text{carbon}} + \text{Penalties}$$

Subject to:
1. **Demand Satisfaction**: $\sum_{i} x_{ijp} \ge D_{jp} \quad \forall j \in \mathcal{M}, p \in \mathcal{P}$
2. **Facility Capacity Ceiling**: $\sum_{j} x_{ijp} \le K_i y_i \quad \forall i \in \mathcal{F}$
3. **Echelon Mass Balance**: Inbound Flow = Outbound Flow at each intermediate DC
4. **Service Level & SLA Constraints**: Strict delivery lead-time thresholds by priority tier
5. **Capital Expenditure (CapEx) Budget Limit**: $\sum_{i} \text{CapEx}_i y_i \le \text{Budget}$
6. **Carbon Emission Constraints**: $\sum_{(i,j)} \text{Emissions}_{ij} \le \text{Carbon Cap}$

### Validated Benchmarks
- **Hand-Solvable 2-DC Reference Case**: Evaluates to **`$5,400.00`** (Optimal facility: `DC_T1`).
- **Kearney Case 16 Full Network**: Evaluates to **`$115,638.14/month`** with 100% cost reconciliation.

---

## 6. Quickstart Guide

### Prerequisites
- Python 3.9 or higher
- Modern web browser (Chrome, Edge, Firefox, Safari)

### Installation
```bash
# 1. Clone repository
git clone https://github.com/aayusht115/NetGravity.git
cd NetGravity

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) configure environment — everything works without this
cp .env.example .env
```

With no `.env`, the pipeline runs against local disk in AI stub mode, and the
full test suite passes. That is the intended behaviour on a fresh clone: no
credentials required to evaluate the system.

### Running Tests & Smoke Tests
```bash
# Run 1-command verification (< 0.5 seconds)
python smoke_test.py

# Run complete automated test suite (348 tests: engine + ingestion)
pytest

# Engine only / ingestion only
pytest netgravity/tests
pytest netgravity/ingestion/tests
```

### Running the Ingestion Pipeline
```bash
# Ingest the bundled sample network, validate, and solve it
python -m netgravity.ingestion --source data/mock/india --solve
```
See [§4](#4-data-ingestion-pipeline-layer-1) for the full command set and
configuration.

### Running the Web Application
```bash
# Launch web application from repository root (recommended)
python run.py

# Alternatively, launch directly from backend directory
python app/backend/app.py
```
Open [http://localhost:5050](http://localhost:5050) in your web browser.

### Instant Client Demo (Zero-Dependency Offline HTML)
Open `app/standalone/netgravity_standalone.html` (or `netgravity_standalone.html`) directly in any web browser without needing any Python server or node environment.

---

## 7. AI Agent Roadmap

The platform provides standard tool-call schemas for agentic integration:

```python
def get_network_summary() -> NetworkSummary: ...
def get_bottlenecks() -> List[Bottleneck]: ...
def get_forecast(region: str, horizon_months: int) -> ForecastResult: ...
def run_scenario(scenario_config: ScenarioConfig) -> OptimizationResult: ...
def compare_scenarios(scenario_ids: List[str]) -> ScenarioComparison: ...
def run_resilience_test(scenario_id: str, stress_params: StressParams) -> ResilienceScorecard: ...
def get_data_quality() -> DataQualityReport: ...
```

---

## 8. Attribution
Developed for the **Kearney Case Competition**. Proprietary decision-intelligence and mathematical optimization architecture.
