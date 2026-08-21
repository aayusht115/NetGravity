# NetGravity Orchestrator — Control Plane Architecture

Version 1.0.0 · `netgravity/orchestrator/`

---

## 1. What it is

The control plane between the outside world and NetGravity's deterministic
engines. It interprets requests, decides what needs to run, coordinates agents
and engines, manages execution state and scenario isolation, combines outputs,
applies risk and governance logic, and determines what happens next.

It is **not** the MILP, the REI engine, the RF calculator, a database, or a
chatbot. It contains no optimization mathematics, no cost arithmetic and no risk
formula beyond the deterministic RF combination.

```
LLM              semantic intelligence      (interpretation, explanation)
Orchestrator     coordination + control     (plan, validate, sequence, govern)
MILP             optimization truth         (authoritative)
REI              network exposure truth     (authoritative)
RF               deterministic combination  (pure arithmetic)
Reasoning Agent  synthesis                  (advisory, validated)
Governance       action authorization       (deterministic rules)
Audit            traceability               (decision provenance)
```

---

## 2. Repository assessment (what was found)

| Component | State before this work |
|---|---|
| MILP / scenario engine / REI / KPI / cost reconciliation | Mature, tested, authoritative — **reused unchanged** |
| V1.4 result contracts (`schemas/contracts.py`) | Present — **reused** as the engine boundary |
| LLM integration | **None.** The only "llm" occurrences were comments forbidding it in the math core |
| Forecasting module | **None** in the Python core |
| RF calculation | **None** |
| State store / snapshot registry | **None** |
| HTTP API | Static file server plus `/api/status` only |
| `frontend/js/agent.js` | A UI **simulation** over static `data.js`, not an agent |

So the control plane was greenfield. Nothing deterministic was rewritten.

---

## 3. Module map

```
netgravity/orchestrator/
├── core/
│   ├── orchestrator.py        lifecycle, layer-parallel execution, governance hook
│   ├── execution_context.py   typed per-run state, immutable data references
│   ├── execution_state.py     state machine + legal-transition table
│   └── planner.py             intent → validated DAG (WORKFLOW_TEMPLATES)
├── routing/capability_registry.py   what exists, what it needs, how it may fail
├── tools/base.py              Tool protocol, Capability, timeout/retry adapter
├── engines/
│   ├── deterministic.py       async adapters over MILP / KPI / REI / cost
│   └── scenario_builder.py    materialises hypothetical networks
├── agents/
│   ├── llm_gateway.py         text-gateway client (env credential, degradable)
│   ├── intent_agent.py        rules first, model second
│   ├── reasoning_agent.py     narrative + validation + template fallback
│   └── external_signal_agent.py   evidence extraction with provenance
├── risk/risk_factor.py        RF = P + REI − P·REI (deterministic)
├── governance/action_classifier.py  rules, authorization, approvals
├── state/stores.py            snapshots, scenarios, executions (isolation)
├── validation/validators.py   request / scenario / result / snapshot checks
├── audit/audit_logger.py      execution trace + `explain()`
├── schemas/                   requests, plans, risk, actions
├── registry.py                default capability wiring — the extension seam
├── api.py                     Flask blueprint
└── exceptions.py              failure taxonomy + classification
```

---

## 4. Lifecycle and state machine

```
RECEIVE → UNDERSTAND → CLASSIFY → PLAN → VALIDATE → EXECUTE
        → COLLECT → CALCULATE → REASON → GOVERN → RESPOND → AUDIT
```

States, with legal transitions enforced by `assert_transition`:

```
RECEIVED → UNDERSTANDING → PLANNED → VALIDATING → RUNNING ─┬→ COMPLETED
                                                    │      ├→ INFEASIBLE
                                                    │      ├→ REQUIRES_APPROVAL ─┬→ COMPLETED
                                                    │      │                     └→ CANCELLED
                                                    └→ WAITING                   
any non-terminal ──────────────→ FAILED | CANCELLED | STALE | REQUIRES_HUMAN
```

Terminal states are absorbing, except `REQUIRES_APPROVAL`, which resumes the
**same** execution after a decision — never a new uncontrolled one.

---

## 5. Execution flows

### 5.1 Scenario — "What happens if we close DC_EAST?"

```
load ─┬─→ baseline ────────────────┐
      └─→ create_scenario → validate_scenario → optimize_scenario ─┬→ kpi ─┐
      └─→ rei ──────────────────────────────────────────────────────┴──────┼→ reason → govern
```
Result: `REQUIRES_HUMAN` — closure is structural (rule `R2_STRUCTURAL_ACTION`).

### 5.2 Resilience — "Which facility is most exposed?"

```
load → rei → reason → govern        (no scenario solve; not every engine runs)
```
Result: `COMPLETED`, `AUTO_ACTION` (analysis only).

### 5.3 External event — "Severe flooding expected around DC_EAST"

```
load ─┬→ interpret_signal ─┐
      └→ rei ──────────────┴→ risk (RF) → reason → govern
```
`interpret_signal` and `rei` are independent and run concurrently.

---

## 6. Capability registry

| Capability | Mode | Depends on | Retry |
|---|---|---|---|
| `network.load_snapshot` | deterministic | — | none |
| `scenario.create` | deterministic | load | none |
| `scenario.validate` | deterministic | create | none |
| `optimization.solve` | deterministic | load | 2 attempts |
| `optimization.solve_scenario` | deterministic | validate | 2 attempts |
| `kpi.summarise` | deterministic | optimize | none |
| `resilience.assess` | deterministic | load | none |
| `external.interpret_signal` | probabilistic | load | 3 attempts |
| `risk.compute_rf` | **deterministic** | rei + signal | none |
| `reasoning.synthesise` | probabilistic | — | none (optional) |
| `governance.classify` | deterministic | — | none |

Adding an agent (Carbon, Supplier Risk, Inventory, Transportation, Financial
Impact) is one `registry.register(Capability(...))` call in `registry.py`. The
core, planner and executor do not change — asserted by
`test_new_capability_needs_no_core_change`.

---

## 6b. Dependency semantics (hardening sprint)

Dependency edges carry explicit criticality:

| Type | On upstream failure | Example |
|---|---|---|
| **HARD** | dependent is `BLOCKED` | `scenario.validate → optimization.solve_scenario` — solving an unvalidated scenario yields numbers nobody should trust |
| **SOFT** | dependent **runs**, with explicit missing evidence | `resilience.assess → reasoning` — losing exposure degrades the narrative, it does not invalidate the cost figures |

Declared on `PlanStep`: `depends_on` is topology, `soft_depends_on` overlays
criticality, and **edges out of an `optional` step are SOFT automatically** so
advisory capabilities cannot become mandatory by accident.

`reasoning` and `governance` depend softly on everything, by design. Governance
must always produce a verdict.

### Missing evidence is never zero

A failed step records `UnavailableEvidence{capability, status, reason}` on the
context and is passed into every downstream `ToolRequest`. Status distinguishes
`UNAVAILABLE` / `TIMEOUT` / `INVALID` / `NOT_RUN`. Consumers can therefore tell
"REI is 0" from "REI was never measured" — a distinction the previous version
could not express.

### Terminal-state precedence

A required step failing or being blocked yields `FAILED`, checked **before** the
governance verdict: a governance decision describes an action, and if the
analysis it rests on did not complete, reporting `REQUIRES_HUMAN` would imply a
usable result exists.

---

## 6c. Numeric-claim grounding (hardening sprint)

```
Deterministic Results → Authoritative Facts → Reasoning Agent → Claims
                                                                  ↓
                                                       Numeric Claim Validator
                                                                  ↓
                                                         Validated Response
```

Every number in generated narrative is adjudicated against authoritative values
(`validation/numeric_grounding.py`):

| Verdict | Meaning |
|---|---|
| `GROUNDED` | matches an authoritative value within tolerance |
| `CONTRADICTED` | a value of that kind exists and the claim disagrees |
| `UNSUPPORTED` | no value of that kind exists — the figure was invented |
| `IGNORED` | bare count/ordinal, not a claim about results |

**Tolerance:** relative 0.5% + absolute 0.01, plus rounding to the claim's own
precision. So `14.3` ≡ `14.30` ≡ `14%`, but `15.8%` and `50%` both fail.

**On failure** the offending figure is *replaced in the text*
(`[UNGROUNDED CLAIM REMOVED — authoritative … = 14.3]`), confidence is forced to
`LOW`, `grounding_status = GROUNDING_FAILED`, and governance withholds
automation (rule `R7C`). Replacement rather than a warning, because a reader of
the summary would never see the warning.

Authoritative sources are declared per field: MILP → cost/flow/feasibility, KPI
engine → SLA/utilisation/demand, REI engine → REI, risk engine → P and RF.
Accepted claims carry `{execution_id, snapshot_id, scenario_id, source, fact}`.

The agent is asked for **structured claims** first; free-text extraction is the
fallback, because a prompt-only gateway cannot guarantee structure.

---

## 6d. External likelihood semantics (hardening sprint)

Three variables, previously conflated, now strictly separate:

| Field | Meaning | Feeds RF? |
|---|---|---|
| `event_probability` | how LIKELY the event is | **yes, as P** |
| `severity` | how BAD it would be (categorical) | **never** |
| `confidence` | trust in THIS assessment | **never** |

`probability = 0.4` with `confidence = 0.95` is coherent: high confidence in a
low chance.

The previous `_SEVERITY_PRIOR` table (`severe ⇒ P = 0.7`) is **deleted**, and a
test guards against its reintroduction. Probability is populated only from an
explicitly stated figure; the model path additionally requires a supporting
quote, and drops the value if none is given.

`ExternalSignal.likelihood` was **renamed** to `event_probability`
(`schema_version: 2`). Passing the old field raises with a migration message
rather than being silently reinterpreted — silent mapping would have preserved
the exact bug the rename fixes.

### RF computability

RF is produced only when **both** P and REI are available and valid. Otherwise:

```
status                = NOT_COMPUTABLE
not_computable_reason = NO_EVENT_PROBABILITY | NO_REI | NO_INPUTS | INVALID_INPUT
risk_factor           = None
```

Present-but-invalid inputs still raise (a defect); absent inputs report
`NOT_COMPUTABLE` (a fact about the evidence). The assessment carries a
`not_computable` list so "not assessed" is visible rather than silently absent.

---

## 7. Deterministic risk

```
RF = P + REI − (P × REI)
```

Probabilistic OR of an external likelihood and NetGravity's own exposure. Never
produced by a model; `risk_factor.py` contains no gateway reference (asserted by
test). Properties verified: identities, symmetry, monotonicity, bounds,
reproducibility, provenance.

**REI flooring.** REI is normalised as `PI_i / max_j(PI_j)`, so it is bounded
above by 1.0 but **not below** — a facility whose loss reduces cost has negative
PI and therefore negative REI (V1.4 deliberately retains this rather than
clamping). RF is only defined on [0,1], so a negative REI is read as *no
economic exposure* and floors to 0, making `RF = P`. The raw value is recorded
in `notes`, never hidden. A REI **above** 1.0 is rejected as a defect.

Missing inputs are **skipped with a warning**, never defaulted to 0 — a
fabricated zero reads as "no risk", which is a different claim from "not
assessed".

---

## 8. Governance matrix

Evaluated in strict precedence order; the first match wins.

| # | Condition | Classification |
|---|---|---|
| R0 | No action proposed | `NO_ACTION` |
| R1 | Result infeasible | `HUMAN_ONLY` |
| **R2** | **Action is structural (CLOSE/OPEN facility)** | **`HUMAN_ONLY`** |
| R3 | Data quality degraded | `HUMAN_ONLY` |
| R4 | Unserved demand > 2% | `HUMAN_ONLY` |
| R5 | Cost impact ≥ 20% | `HUMAN_ONLY` |
| R6 | RF ≥ 0.8 | `HUMAN_ONLY` |
| R7 | Analytical only (REPORT / CREATE_SCENARIO) | `AUTO_ACTION` |
| R8 | RF ≥ 0.5 | `APPROVAL_REQUIRED` |
| R9 | Cost impact ≥ 5% | `APPROVAL_REQUIRED` |
| R10 | Confidence < HIGH | `APPROVAL_REQUIRED` |
| R11 | Reversible, low impact, high confidence | `AUTO_ACTION` |
| R12 | Default | `APPROVAL_REQUIRED` (conservative) |

**R2 sits above every REI, RF and cost rule deliberately.** A facility closure
with REI 0.01, cost impact 0.1% and HIGH confidence is still `HUMAN_ONLY`:
irreversibility, not exposure, governs there. REI is never the sole determinant.

Authorization is separate: **no role — not even ADMIN — may initiate a
structural action directly.** All thresholds are configurable via
`GovernancePolicy`.

---

## 9. Scenario isolation

```
observed snapshot ──(deep copy)──> scenario network ──> scenario store
        ▲                                                    │
        └────────────── no write path back ──────────────────┘
```

* Snapshots are content-addressed on `compute_data_version()` and deep-copied on
  ingest — mutating the caller's network cannot alter stored observed state.
* Scenarios carry `is_hypothetical=True`, `source`, `parent_snapshot_id`,
  version and overrides.
* `ScenarioStore` has **no** `promote_to_observed()` — asserted by test.
* An explicitly pinned `network_snapshot_id` always wins over the current
  snapshot, so pinning cannot be silently overridden.
* Stale snapshots are detected and the run ends `STALE`; an approval raised
  against a snapshot that has since moved also lands `STALE`.

---

## 10. Error handling

| Failure | Class | Behaviour |
|---|---|---|
| `INVALID_REQUEST` / `INVALID_SCENARIO` / `VALIDATION_FAILURE` | NON_RETRYABLE | fail fast |
| `SOLVER_INFEASIBLE` | **NON_RETRYABLE** | reported as an outcome with diagnostics |
| `ENGINE_TIMEOUT` / `ENGINE_FAILURE` | RETRYABLE | bounded exponential backoff |
| `LLM_FAILURE` | RETRYABLE | degrades to rule/template path |
| `LLM` auth / budget / 413 / client timeout | NON_RETRYABLE | no retry |
| `MISSING_DATA` / `STALE_SNAPSHOT` | REQUIRES_HUMAN | escalate |
| `AUTHORIZATION_FAILURE` | NON_RETRYABLE | refuse |

Infeasibility being non-retryable is load-bearing: it is a property of the
model, so re-solving cannot change it.

---

## 11. LLM boundary

**The model may:** interpret intent, interpret external evidence, and write the
narrative.

**The model may never be the source of truth for:** MILP results, cost, capacity,
SLA, REI, RF, feasibility, authorization, state transitions, audit records,
observed data, or scenario versioning.

Enforcement:
* Proposed facility ids are filtered against the real network in the agent, then
  re-validated by `ScenarioValidator` before any engine runs — a hallucinated
  facility fails validation and no scenario is created.
* Reasoning output is validated; claiming feasibility against an `INFEASIBLE`
  solver is flagged and confidence downgraded.
* Governance and authorization are pure rules.
* `risk_factor.py` contains no gateway reference.

### Gateway integration

`POST /v1/generate`, Bearer auth, body `{"prompt": ...}` — **one field only**, so
all instruction is inlined into the prompt and every response is parsed
defensively.

```bash
export TEXT_API_URL="https://<gateway-host>"
export TEXT_API_TOKEN="<token>"     # server-side secret manager; never in source
```

The token is read from the environment only. It is never hardcoded, logged, or
included in prompts or error messages — asserted by tests.

Limits are **shared** across all consumers ($10 cumulative, 100 requests/day,
20/minute, 100k prompt chars, 60s processing). The client therefore:
* refuses oversized/empty prompts locally rather than spending a request;
* caps calls per gateway instance so one run cannot exhaust daily capacity;
* retries only 429/500/502 with jittered exponential backoff;
* **never** retries a client-side timeout (no idempotency key ⇒ duplicate risk).

**Degradation is a first-class path.** With no token, the orchestrator uses
rule-based intent parsing and template reasoning. Deterministic results are
byte-identical — asserted by
`test_llm_absence_does_not_change_deterministic_numbers`.

---

## 12. API

```http
POST /orchestrator/run
{"input": "What happens if we close DC_EAST?", "actor": {"actor_id":"u1","role":"PLANNER"}}
```
```json
{
  "execution_id": "1dbf4488-133",
  "status": "REQUIRES_HUMAN",
  "intent": "SCENARIO_ANALYSIS",
  "scenario_id": "scn_2a809692c5",
  "is_hypothetical": true,
  "network_snapshot_id": "snap_762b4d277ffb",
  "summary": "Business network cost is 162,665.58 per period. The scenario increases business cost by 12,037.88 (+7.99%).",
  "results": {"network": {"business_network_cost": 162665.58, "unserved_demand": 0.0}},
  "governance": {
    "classification": "HUMAN_ONLY",
    "reason": "'CLOSE_FACILITY' is a structurally significant, effectively irreversible network change...",
    "triggered_rules": ["R2_STRUCTURAL_ACTION"]
  }
}
```

HTTP status reflects outcome kind: 200 completed · 202 awaiting human ·
409 infeasible/stale · 500 failed.

Other endpoints: `POST /orchestrator/approvals/<id>`,
`GET /orchestrator/executions/<id>[/trace]`, `/capabilities`, `/workflows`,
`/health`.

---

## 13. Auditability

Every run produces a trace recording input, interpreted intent **and its
source**, workflow, snapshot id + data version, scenario ids and overrides,
every tool invocation with timing/attempts/errors, engine results, the RF
calculation, the governance verdict **and the rules that fired**, model outputs,
state history and outcome. `trace.explain()` renders it as text.

Prompts are not stored (business data, low forensic value); model **outputs**
are, truncated. No credential material is ever recorded.

---

## 14. Test results

| Suite | Result |
|---|---|
| `test_orchestrator.py` | **114 passed** |
| `test_orchestrator_llm_gateway.py` | **25 passed** |
| `test_orchestrator_hardening.py` | **84 passed** |
| Pre-existing NetGravity suites | **339 passed** |
| **Full suite** | **562 passed, 0 failed** |

Gateway tests mock the HTTP transport — the shared budget is never spent. Live
reachability was confirmed via the unauthenticated `/health` probe.

---

## 15. Known limitations

**Implemented and exercised end-to-end:** lifecycle and state machine, capability
registry, DAG planner with layer-parallel execution, scenario isolation and
versioning, snapshot staleness detection, deterministic RF, governance and
authorization, approval workflow with resumption, retry classification,
idempotency, audit trail, HTTP API, gateway client with degradation.

**Implemented but not exercised against a live model:** intent interpretation and
reasoning via the gateway. The code paths are unit-tested with a mocked
transport and the endpoint is confirmed reachable, but no end-to-end run against
the real model is included in the suite, deliberately, because capacity is
shared.

**Deliberately not built:** Risk Factor consumption logic beyond the formula,
likelihood engine, news interpretation, autonomous action execution, TTR/TTS,
multi-period MILP, forecasting (no module exists — `ForecastResult` was
correctly omitted rather than invented).

**Known gaps:**
1. **State is in-memory.** Snapshots, scenarios, executions and audit traces do
   not survive a restart. Interfaces are narrow so a database can replace the
   internals; the audit ring buffer is bounded at 500 traces.
2. **No authentication.** `Actor` is taken from the request body. Authorization
   logic is real and enforced, but identity must be established by a real
   authentication layer before any deployment.
3. **Parallelism is in-process.** `asyncio` plus a thread pool. Genuine for
   independent branches, but bounded by one machine.
4. **`SHIFT_VOLUME` is modelled as source closure + target pinned open**, letting
   the MILP reallocate. Defensible, but it does not force a specific
   source→target flow.
5. **The Flask mount uses the Case-16 synthetic fixture**, which is fabricated
   demonstration data. It is labelled as such in `/api/status` and must be
   replaced with the real observed network.

---

## 16. Recommended next steps

1. Replace the demo network mount with real observed-network ingest, and add
   authentication in front of `/orchestrator/*`.
2. Persist snapshots, scenarios, executions and audit traces.
3. Run a small, budgeted live-model evaluation of intent and reasoning quality
   against a fixed prompt set, recording accuracy before relying on either.
4. Build the RF consumption layer (likelihood sourcing, event feeds) on top of
   the deterministic RF already provided.
5. Add metrics export (latencies, failure/retry rates, approval and auto-action
   rates) — the data is already recorded on traces.
