# NetGravity — AI Decision Intelligence for Supply Chain & Logistics Networks

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![MILP Core](https://img.shields.io/badge/Solver-PuLP%20%7C%20HiGHS%20%7C%20CBC-purple.svg)](https://github.com/coin-or/pulp)
[![Tests](https://img.shields.io/badge/Automated%20Tests-962%20Passing-brightgreen.svg)](netgravity/tests/)
[![Architecture](https://img.shields.io/badge/Architecture-Deterministic%20MILP%20%2B%20Governed%20Orchestrator-orange.svg)](#3-system-architecture)

> **NetGravity** is a decision-intelligence and network optimization platform for logistics networks. It joins mathematically rigorous Mixed-Integer Linear Programming with a governed AI control plane — and keeps a hard line between the two.

---

## 1. The Core Paradigm

Supply chain tools usually force a choice: solvers that are rigorous but opaque, or AI dashboards that are fluent but unverifiable. NetGravity refuses the trade by making the boundary explicit and enforcing it in code.

**One rule governs the whole system:**

> The MILP, the REI engine and the RF calculator are the only sources of numeric truth. A language model may interpret a request and explain a result. It may never produce, adjust or replace a number.

Three mechanisms enforce that rule rather than merely asserting it:

| Mechanism | What it prevents |
|---|---|
| **Read-only evidence** | The reasoning agent receives already-computed results and has no write path back. |
| **Numeric-claim grounding** | Every figure in generated narrative is adjudicated against authoritative values. Contradicted and unsupported numbers are **removed from the text**, not merely flagged. |
| **Proposal validation** | Model-suggested facilities and scenarios are checked against the real network; a hallucinated site fails validation long before the solver runs. |

A fourth principle runs through everything: **missing is not zero.** When exposure, probability or risk cannot be computed, the system reports *why* it could not rather than substituting a value, and missing evidence withholds automation — it can never grant it. Formally: if an action requires evidence `E` to justify running unattended and `E` is `UNAVAILABLE`, `FAILED`, `STALE`, `NOT_COMPUTABLE` or `GROUNDING_FAILED`, `AUTO_ACTION` is prohibited. That says *autonomy cannot be justified*, not *risk is high* — and the two are recorded distinctly. See [`docs/r7_governance_precedence.md`](docs/r7_governance_precedence.md).

---

## 2. What Is Built

| Capability | Status |
|---|---|
| Deterministic MILP core (multi-echelon, capacitated) | Mature — 100% cost reconciliation, benchmark-anchored |
| Facility Resilience Assessment + Risk Exposure Index (REI) | Complete, cached and persisted |
| Risk Factor (RF) calculation from external event probability | Complete, with explicit refusal semantics |
| Orchestrator control plane (planning, dependencies, governance, audit) | Complete |
| Orchestrator ↔ deterministic core integration | Complete — see [§6](#6-integration-phases) |
| Interactive web cockpit | Demonstration build on a synthetic Case-16 fixture |
| Authentication / multi-process persistence | **Not built** — see [§9](#9-known-limitations) |

The mathematics is mature and evidenced. The **system** is not yet deployable — [§9](#9-known-limitations) states exactly what is missing and why passing tests does not settle the question.

---

## 3. System Architecture

```
                         ORCHESTRATOR  (coordinates; computes nothing)
                              │
             ┌────────────────┼──────────────────┐
             ▼                ▼                  ▼
        Intent Agent    Scenario Planner   External Signal
       (proposal only)  (override only)    (evidence only → P)
             │                │                  │
             └────────┬───────┘                  │
                      ▼                          │
              MILP  (authoritative)              │
                      │                          │
              ┌───────┴────────┐                 │
              ▼                ▼                 │
          Baseline           REI ────────────────┘
              │                │
              ▼                ▼
        Network State    REI Registry  (cached on material fingerprint)
                               │
                               ▼
                        RF Assessment       RF = P + REI − P·REI
                               │
                               ▼
                       Reasoning Agent      explains; never computes
                               │
                               ▼
                      Numeric Grounding     strips ungrounded figures
                               │
                               ▼
                      Action Governance
                        /      |      \
                     AUTO  APPROVAL  HUMAN_ONLY
```

### Responsibility boundaries

| Layer | Owns | Never does |
|---|---|---|
| **Orchestrator** | Workflow, dependencies, state, evidence availability, provenance | Any arithmetic |
| **MILP** | Optimization, feasibility, constraints, cost | — |
| **REI** | Facility disruption exposure: PI, EI, REI | Optimize independently — it *wraps* the MILP |
| **RF** | `RF = P + REI − P·REI` | Infer a probability |
| **Reasoning** | Interpretation, explanation, recommendation drafting | Produce a number |
| **Grounding** | Adjudicating claims against authoritative values | — |
| **Governance** | `AUTO` / `APPROVAL_REQUIRED` / `HUMAN_ONLY` | Let risk scores alone decide autonomy |

---

## 4. The Deterministic Chain

```
Network Snapshot → Baseline MILP → REI Batch → REI Registry
                                                    ↓
                      External Event Probability P  ↓
                                               ↘    ↓
                                              RF Calculator
                                                    ↓
                                             Risk Assessment
```

### MILP objective

$$\min \sum_{i \in \mathcal{F}} f_i y_i + \sum_{(i,j) \in \mathcal{A}} \sum_{p \in \mathcal{P}} c_{ijp} x_{ijp} + \sum_{j \in \mathcal{D}} h_j \cdot \text{Inv}_j + \sum_{i} \text{closure}_i (1 - y_i) + \lambda_{\text{carbon}} \sum_{(i,j)} e_{ij} x_{ij} + \text{Penalties}$$

Subject to demand satisfaction, facility capacity ceilings, echelon mass balance, SLA thresholds, CapEx budget, contractual closure terms and carbon caps.

> **Solver objective ≠ business cost.** The shortage penalty (default `1e6`/unit) is a mathematical device that forces demand coverage, not a financial cost. Business Network Cost excludes it. Every REI figure is computed on reconciled *business* cost — using the solver objective would let a penalty five orders of magnitude larger than real cost dominate the ranking.

### Validated benchmarks

| Case | Optimum | Notes |
|---|---|---|
| Hand-solvable 2-DC reference | **$5,400.00** | Optimal facility `DC_T1`; derived by hand in the fixture docstring |
| Kearney Case 16 synthetic network | **$150,627.70 / month** | 100% cost reconciliation |
| Case 16, closure cost disabled | **$115,638.14 / month** | The pre-V1.4 optimum, retained as a regression anchor |

The $34,989.56 difference is the V1.4 closure-cost term `Σ closure_cost_i · (1 − y_i)`: shutting an EXISTING facility now carries its one-time transition cost, so the optimizer no longer treats closure as free. Both figures are asserted by `smoke_test.py`.

### REI — Risk Exposure Index

$$PI_i = C_i - C_0 \qquad EI_i = \max(0,\ PI_i) \qquad REI_i = \frac{EI_i}{\max_j EI_j} \in [0, 1]$$

Each facility is disrupted in turn and the network re-optimized. `PI` is signed and retained (a negative value flags a facility whose loss *reduces* cost — worth investigating). `EI` floors at zero so REI stays in the domain RF requires. Cost is always the reconciled business cost. **REI is a relative ranking metric**; no absolute severity bands are applied, because none has a documented business basis.

### RF — Risk Factor

$$RF = P + REI - (P \times REI)$$

The probabilistic OR of two independent contributors, with the product term removing the double count. Worked example, asserted by test:

```
P = 0.70   REI = 0.80   ⇒   RF = 0.7 + 0.8 − 0.56 = 0.94
```

**Severity is not probability.** An event can be catastrophic and unlikely, or trivial and near-certain. `severity`, `confidence` and `event_probability` are three independent fields, and only the last may feed RF. When no defensible probability exists, RF reports `NOT_COMPUTABLE` with a reason:

| Reason | Meaning |
|---|---|
| `NO_EVENT_PROBABILITY` | No stated probability. Severity was **not** substituted. |
| `NO_REI` | Exposure unavailable or not in the registry. |
| `STALE_REI` | The REI was computed against a different network snapshot. |
| `NODE_MAPPING_UNAVAILABLE` | The event maps to no assessed node. No arbitrary node is chosen. |
| `NO_INPUTS` / `INVALID_INPUT` | Neither available / present but out of range. |

`P = 0` is a **measurement** and computes normally (`RF = REI`). A missing `P` is an **absence** and refuses. The system keeps those different.

---

## 5. Orchestrator Control Plane

```
RECEIVE → UNDERSTAND → CLASSIFY → PLAN → VALIDATE → EXECUTE
        → COLLECT → CALCULATE → REASON → GOVERN → RESPOND → AUDIT
```

**Workflows are data, not branching code.** Adding one means adding an entry to `WORKFLOW_TEMPLATES`; the orchestrator core is untouched.

| Workflow | Graph |
|---|---|
| `wf_network_state` | load → optimize → kpi → reason → govern |
| `wf_scenario_analysis` | load → baseline ‖ create → validate → solve_scenario → kpi ‖ rei → reason → govern |
| `wf_scenario_comparison` | one isolated create/validate/solve chain per scenario |
| `wf_resilience_query` | load → rei → reason → govern |
| `wf_external_event` | load → interpret_signal ‖ rei → risk → reason → govern |
| `wf_explanation` | load → rei → risk → reason → govern — **no optimization step by construction** |

### Hard vs soft dependencies

A dependency failure does one of two things, and the distinction is declared per edge:

- **HARD** unmet → dependent is `BLOCKED`. It cannot run safely.
- **SOFT** unmet → dependent **runs**, handed explicit `UnavailableEvidence` naming what is missing.

Losing REI must not cost the reasoning and governance that the surviving MILP results still support. A required-step failure outranks any governance verdict at settle time — a verdict about an analysis that did not complete would imply a usable result exists.

### Governance

Rules are evaluated in strict precedence order, most restrictive first, and every rule that fires is recorded.

- **Structural actions are `HUMAN_ONLY` regardless of REI, RF or cost.** This rule is evaluated *before* any threshold, so a low-exposure, low-cost facility closure can never be automated. Irreversibility governs, not exposure.
- Infeasible results authorise nothing.
- **Missing critical evidence withholds automation.** Absent risk information is never read as absence of risk — and never as a reason to act unattended.
- A failed grounding check withholds automation: an explanation that cannot be trusted cannot justify an action.

The evidence rule is **action-aware**, not a blanket override. It asks whether the action would have leaned on risk evidence to justify running unattended; a hypothetical scenario is exempt, because no measurement is load-bearing for something that cannot touch observed state. The rule constrains **autonomy**, never **information delivery**: a report whose exposure analysis failed is still produced and still returned, it simply no longer clears itself for unattended action.

```
R7   analytical output                 → AUTO candidate  ─┐
R7B  required risk evidence unresolved → APPROVAL         │ evidence constraints
R7C  numeric grounding failed          → APPROVAL         │ speak first
R7   settlement of the candidate       → AUTO_ACTION    ◄─┘
```

---

## 6. Integration Phases

| Phase | Scope | Outcome |
|---|---|---|
| **V1.4 hardening** | Closure economics, contractual constraints, V1 service methodology, optimization modes, frozen result contracts | Complete |
| **REI V1** | Persistence, caching, invalidation, batch status, parallelism, benchmarks | Complete |
| **Phase 1** | Integrate and validate the deterministic risk core (MILP → REI → P → RF) | Complete — [`docs/facility_resilience_rei.md`](docs/facility_resilience_rei.md) |
| **Phase 2** | Integrate the orchestrator with the real MILP, REI, RF, reasoning, grounding and governance services | Complete — [`docs/phase2_integration.md`](docs/phase2_integration.md) |

Phase 2 added **no** new algorithms, agents, risk scores or optimization objectives. It connected what existed and proved the connections hold. The pre-implementation audit is preserved in [`docs/phase2_integration_gap_report.md`](docs/phase2_integration_gap_report.md).

### Defects found and fixed during integration

Recorded because they are the substance of the work, not incidental to it.

1. **Fabricated node status.** The flattened REI projection omitted `calculation_status`, and the RF layer rebuilt a typed registry defaulting every node to `OK` — so an INFEASIBLE node was recorded as healthy in the audit trail. The rebuild was **deleted** rather than patched; the typed registry is now passed through directly.
2. **A facility rename permanently broke RF.** Snapshot ids hash descriptive fields; the REI cache keys on the material fingerprint, which does not. A rename minted a new snapshot id, hit the cache, and returned a batch stamped with the old id — correctly refused as `STALE_REI`, forever. Fixed by re-stamping a cache-served batch at the point where the fingerprint *proves* equivalence, retaining the original in `computed_for_snapshot_id`. **The staleness check was not relaxed**; a material change still misses the cache and recomputes.
3. **Cached batches over-reported solve cost**, contradicting the field's own documented contract. A cache hit now reports `n_milp_solves = 0`.
4. **Shortage priority-multiplier mismatch** (Phase 1): the MILP objective applied a per-demand priority multiplier that extraction and reconciliation did not, opening a 5.36% reconciliation gap. Fixed at both sites; gap now 0.00.

---

## 7. Repository Structure

```
NetGravity/
├── README.md
├── requirements.txt
├── pyproject.toml
├── smoke_test.py                       # 7-check verification (~2s)
├── run.py
│
├── app/                                # Web cockpit (demonstration build)
│   ├── backend/app.py                  # Flask API & telemetry endpoints
│   ├── frontend/                       # Decision cockpit, digital twin, scenarios
│   └── standalone/                     # Portable single-file HTML build
│
├── docs/
│   ├── mathematical_model.md           # Full formulation & notation
│   ├── model_architecture.md           # Echelon architecture & pipeline
│   ├── facility_resilience_rei.md      # REI methodology & Phase 1 chain
│   ├── orchestrator_architecture.md    # Control-plane design
│   ├── phase2_integration.md           # Integrated architecture & traces
│   ├── phase2_integration_gap_report.md# Pre-implementation audit
│   └── v1_*.md                         # Validation reports & audit trails
│
├── netgravity/                         # Engine + control plane
│   ├── optimization/                   # Exact MILP (PuLP / HiGHS / CBC), modes
│   ├── costs/                          # Cost accounting, business cost, reconciliation
│   ├── resilience/                     # REI engine, service, cache, fingerprint, persistence
│   ├── orchestrator/                   # ── Control plane ──
│   │   ├── core/                       #    orchestrator, planner, execution context & state
│   │   ├── engines/                    #    deterministic adapters, scenario builder
│   │   ├── agents/                     #    intent, reasoning, external signal, LLM gateway
│   │   ├── risk/                       #    RF calculator, event risk assessment
│   │   ├── validation/                 #    numeric grounding, validators
│   │   ├── governance/                 #    action classifier, approvals, authorization
│   │   ├── state/                      #    snapshot / scenario / execution stores
│   │   ├── audit/                      #    execution traces, canonical events
│   │   └── routing/, tools/, schemas/
│   ├── network/  inventory/  service/  carbon/  metrics/
│   ├── scenarios/  sensitivity/  diagnostics/  cog/
│   ├── schemas/  assumptions/  validation/  config/
│   └── tests/
│       ├── test_*.py                   # Unit & subsystem suites
│       └── integration/                # Phase 2 end-to-end integration suite
│
└── scripts/build_standalone.py
```

---

## 8. Testing

```bash
python smoke_test.py                    # 7-check verification (~2s)
pytest -m "not slow"                    # 962 tests, ~32s
pytest                                  # includes large-scale benchmarks
pytest netgravity/tests/integration/    # Phase 2 integration suite only
```

**962 passing, 0 failing** (2 slow benchmarks deselected by default).

| Suite | Tests |
|---|---|
| `integration/` — Phase 2 end-to-end + R7 governance | **243** |
| `test_phase1_risk_chain.py` | 84 |
| `test_orchestrator.py` | 114 |
| `test_orchestrator_hardening.py` | 84 |
| `test_hardening_v14.py` | 69 |
| `test_rei_v1.py` | 65 |
| `test_resilience_rei.py` | 53 |
| MILP core, costs, inventory, service, carbon, scenarios, stress, validation | remainder |

The integration suite exercises the **real** MILP, REI and RF services end to end. Only the LLM gateway and the external signal source are injected doubles — both are boundaries of the system, not parts of it. A test that passed against a faked solver would prove nothing about integration.

### Verified invariants

- The `PHASE2_DELHI` fixture is hand-calculable: `C0 = 1,200`, `PI(Delhi) = 400`, `max EI = 500` ⇒ `REI = 0.80`, and with `P = 0.70`, `RF = 0.94`.
- An 18-case failure matrix: for every component failure, the correct step status, dependency behaviour and evidence state — with **no fabricated zero, no fabricated value, no silent failure and no baseline corruption**.
- Scenario execution never mutates the observed snapshot (asserted byte-for-byte). `ScenarioStore` has no promotion path, and the absence is asserted.
- An explanation query with valid cached evidence performs **0 MILP solves**, measured at the solver rather than self-reported by the cache.
- Concurrent workflows under both `asyncio` and OS threads: distinct execution ids, no evidence crossover, no override leakage, registry consistent.
- A deliberate hallucination (model claims 50% against an authoritative 16.67%) is caught, stripped from the narrative, confidence downgraded, automation withheld.

---

## 9. Known Limitations

Stated plainly. **NetGravity is not production-ready**, and passing tests is not the same as being deployable.

**Architectural**
- `REPORT` is a single action tier covering both a risk assessment and a routine status summary, so both are treated as evidence-dependent. Separating them needs a new action type. The `actions_requiring_risk_evidence` policy override is the intended escape hatch.
- `CHANGE_CAPACITY` is classified neither structural nor operational, so it falls to the conservative default (R12). Worth an explicit policy decision.
- Location → node mapping is string matching. Adequate when identifiers encode location; a real deployment needs a geographic mapping table.

**Engineering / deployment**
- **No authentication.** Capability-level authorization exists and works, but the actor is caller-asserted.
- Persistence is single-process JSON files. Atomic writes make it crash-safe, not concurrent-writer-safe.
- No in-flight cache deduplication: simultaneous cold REI requests each compute a batch. Redundant work, not divergence.
- Request idempotency returns a point-in-time view to a duplicate racing the original. Sequential retry is fully correct.
- The web cockpit runs on a synthetic Case-16 fixture, not live data.

**Performance**
- Parallel speed-up decays with size (1.98× at 7 facilities → 1.14× at 50). Beyond ~50 facilities a process pool or distributed workers is needed; the `max_workers` / `solve_fn` seams accept either.
- The 100-DC batch figure is a **projection** from three measured single solves, labelled as such in the benchmark rather than reported as a measurement.

**Scope deliberately excluded**
- Lane, supplier and demand-surge REI (REI requires one uniform disruption assumption across compared entities).
- Automatic mitigation and scenario generation.
- Time-to-Recovery modelling — the MILP is single-period, and a multi-period TTR cannot be produced without fabricating a temporal calculation. `time_to_recovery_days` rejects any value rather than pretending.

---

## 10. Quickstart

```bash
git clone https://github.com/aayusht115/NetGravity.git
cd NetGravity
pip install -r requirements.txt

python smoke_test.py                    # verify
python run.py                           # http://localhost:5050
```

**Zero-dependency demo:** open `app/standalone/netgravity_standalone.html` directly in a browser.

### Optional: enabling the LLM

The system runs fully offline by default. Without a token the orchestrator uses rule-based intent parsing and template reasoning; **deterministic results are identical either way.**

```bash
export TEXT_API_TOKEN="..."             # server-side secret manager in production
export TEXT_API_URL="..."
export NETGRAVITY_DISABLE_LLM=1         # force offline
```

Credentials are read from the environment only. They are never placed in prompts, URLs, source or logs, and never recorded in the audit trail. The gateway is called from backend code only.

---

## 11. Attribution

Developed for the **Kearney Case Competition** (Case 16 — Interactive Logistics Network Optimisation Agent). Proprietary decision-intelligence and mathematical optimization architecture.
