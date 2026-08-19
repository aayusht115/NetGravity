# NetGravity — AI Decision Intelligence for Supply Chain & Logistics Networks

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![MILP Core](https://img.shields.io/badge/Solver-PuLP%20%7C%20HiGHS%20%7C%20CBC-purple.svg)](https://github.com/coin-or/pulp)
[![Tests](https://img.shields.io/badge/Automated%20Tests-215%20Passing-brightgreen.svg)](netgravity/tests/)
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
│   ├── mathematical_model.md          # Full mathematical formulation & notation
│   ├── model_architecture.md          # Echelon architecture & pipeline design
│   ├── model_foundation.md            # Cost functions & inventory theory
│   ├── v1_0_audit.md                  # Verification audit trail & benchmark logs
│   └── *.md                           # Validation reports & Case 16 references
│
├── netgravity/                        # ⚡ Mathematical Optimization Engine (Source of Truth)
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
│   └── tests/                         # Automated test suite (215 passing tests)
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

## 4. Mathematical Optimization Core (MILP)

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

## 5. Quickstart Guide

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
```

### Running Tests & Smoke Tests
```bash
# Run 1-command verification (< 0.5 seconds)
python smoke_test.py

# Run complete automated test suite (215 tests)
pytest
```

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

## 6. AI Agent Roadmap

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

## 7. Attribution
Developed for the **Kearney Case Competition**. Proprietary decision-intelligence and mathematical optimization architecture.
