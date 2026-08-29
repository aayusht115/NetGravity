# Phase 9.1 — Authoritative KPI & Metric Layer

**Status:** complete. No new agent, no new calculation engine, no LLM in the loop.
**Regression:** see `validation/kpi_authoritative_layer/report.md` §6.

---

## 1. Why this phase started with an audit of the audit

Two pre-existing documents were named as the starting reference:
`docs/kpi_formula_threshold_action_audit.md` and three JSON files under
`validation/kpi_formula_audit/`. Per the phase's own instruction — *"Do not
assume the audit is correct... cross-check every item against source code"* —
every claim in them was verified against the actual, current codebase before
any design decision was made.

**The verification found the audit almost entirely fabricated on specifics.**
Five domain-scoped research passes, each reading the real source files line by
line, found:

- **No Google OR-Tools anywhere.** The solver is `pulp`, default backend
  `HiGHS`. The audit's objective-function pseudocode is OR-Tools API syntax
  that does not exist in this codebase.
- **No CUSUM change-point detector.** The real method is a sup-F /
  Quandt-Andrews test (`SUP_F_THRESHOLD = 8.85`, `SUP_F_STRONG = 12.35`,
  Andrews 1993 critical values) — a different statistical methodology
  entirely, with different constants, a different null model, and different
  materiality gates.
- **No `ETSEngine`, `CrostonEngine`, `QuantileEngine`, `REICalculator`,
  `COGScreener`, `InventoryEngine`, `ServiceCalculator`, or `RegimeClassifier`
  class exists anywhere.** Every one of these class names is fabricated. The
  real classes are `ETSForecaster`, `IntermittentForecaster`,
  `QuantileForecaster` — free functions for REI, CoG and inventory.
- **No VaR (Value at Risk) and no Reorder Point exist in the codebase at
  all** — confirmed by exhaustive, word-bounded grep. Both are pure
  documentation invention, not a mislocated implementation.
- **No governance "Tier 1/2/3" system and no rupee-valued thresholds
  (₹5L/₹25L) exist.** The real classification enum is `AUTO_ACTION` /
  `APPROVAL_REQUIRED` / `HUMAN_ONLY` / `NO_ACTION`, and every governance
  threshold is a **percentage** (`cost_impact_pct`) or a **risk factor**
  (`risk_factor_human = 0.8`, `risk_factor_approval = 0.5`) — not the
  `HIGH_RISK_THRESHOLD=0.70` / `MEDIUM_RISK_THRESHOLD=0.30` the audit
  describes.
- **No deterministic insight-generation engine exists.** The audit's entire
  "Insight Logic" section (capacity-overload text, compound-disruption-risk
  text, cost-optimization text, structural-demand-shift text, each with
  quoted narrative strings) has no corresponding code anywhere in the
  repository.
- **The unmet-demand penalty is ₹1,000,000/unit, not ₹10,000/unit** — 100×
  the audit's claimed figure.
- **Utilization thresholds are 90%/30%, not 85%/95%.**
- **The 15% "material demand surge" constant is real, but lives in
  `orchestrator/schemas/adaptive.py`, not `signal_router.py`, is disabled by
  default, and triggers a conditional plan *replan* — never an "automatic
  re-solve."**

**What the audit got right:** the core formulas that DO exist — RF = P + REI −
P·REI, REI's economic-impact normalization, the carbon formula, the Weiszfeld
center-of-gravity algorithm, Croston's method, the general shape of MASE/MAE
— are real, correctly implemented, and well tested, even where the audit's
prose around them was wrong. The circuit-breaker thresholds (3 failures, 30s
cooldown) are the one section that matched source exactly on first read.

The full, file:line-cited verification is in
`validation/kpi_authoritative_layer/report.md` §Audit and
`validation/kpi_authoritative_layer/formula_validation.json`. This phase's
design was built entirely from that independently-verified source, not from
the pre-existing documents.

---

## 2. What the audit revealed about the existing architecture

The codebase already has a mature, well-typed KPI surface. Nothing needed to
be invented from scratch:

| Concern | Already exists | Where |
|---|---|---|
| Network-level KPIs | `NetworkKPIs` — 26 fields, fully computed | `netgravity/metrics/kpis.py::compute_kpis` |
| Orchestrator-facing network state | `NetworkStateResult` | `netgravity/schemas/contracts.py` |
| Facility resilience | `FacilityResilienceRegistry` / `FacilityResilienceResult` | `netgravity/schemas/results.py`, computed by `netgravity/resilience/rei.py` |
| Combined risk | `RiskAssessment` / `RiskFactorResult`, with `NOT_COMPUTABLE` semantics already built in | `orchestrator/schemas/risk.py`, computed by `orchestrator/risk/risk_factor.py` |
| Forecast accuracy | `ForecastResult` / `SeriesForecast` / `AccuracyMetrics` | `netgravity/forecasting/schemas.py` |
| Scenario cost deltas | `ScenarioResult.business_cost_delta(_pct)` | `netgravity/schemas/contracts.py`, built by `build_scenario_result` |
| Scenario KPI deltas (network-level) | `TwinComparison` / `MetricDelta`, with `NOT_COMPARABLE` semantics | `orchestrator/twin/service.py::_kpi_deltas` |
| Numeric grounding for narrative | `_FACT_SPEC` whitelist, `GroundingReport` | `orchestrator/validation/numeric_grounding.py` |
| Reasoning evidence indexing | `build_evidence_pack` | `orchestrator/reasoning/evidence.py` |

**So this phase is consolidation, not construction.** The gap was never "no
KPIs exist" — it was that five different typed models, each internally
excellent, had five different conventions for "what does an absent or
untrustworthy value look like," and nothing exposed them through one
consistent, traceable envelope.

---

## 3. `KPIResult` — the contract

`netgravity/orchestrator/schemas/kpi.py`

```python
class KPIStatus(str, Enum):
    VALID
    INSUFFICIENT_EVIDENCE   # nothing was attempted — no capability ran
    NOT_COMPUTABLE          # the capability ran and explicitly refused
    INFEASIBLE              # the underlying solve was proven infeasible
    INVALID_INPUT           # an input was out of its declared domain

class KPIResult(BaseModel, Generic[T]):
    metric_id: str
    value: Optional[T]
    unit: str
    scope: MetricScope                    # NETWORK | FACILITY | LANE | MARKET | ...
    entity_id: Optional[str]
    formula_id: str
    source_capability: str                 # e.g. "resilience.assess"
    authoritative_owner: str                # e.g. "netgravity.resilience.rei"
    status: KPIStatus
    threshold: Optional[ThresholdSpec]
    input_evidence: Dict[str, Any]
    snapshot_id / scenario_id / execution_id / calculated_at
```

### Why not reuse `AgentResult`

`AgentResult` (Phase 8.1) already answers a status question — but a different
one. `AgentResult.status` asks *"did the capability execute?"*
`KPIResult.status` asks *"does this number exist, and can it be trusted?"*
These genuinely disagree in both directions: `resilience.assess` can **succeed**
as a capability while one facility's REI is individually `NOT_COMPUTED`
(infeasible disruption); `risk.compute_rf` can correctly report
`NOT_COMPUTABLE` for a facility while the capability itself ran perfectly.
Collapsing the two vocabularies would force a caller to guess which question
was being answered.

### The one invariant that matters

```python
@model_validator(mode="after")
def value_matches_status(self):
    if status not in {VALID} and value is not None: raise ValueError(...)
    if status == VALID and value is None: raise ValueError(...)
```

A status that means "this cannot be trusted" **cannot hold a number** — so
`result.value or 0` has nothing to find. Enforced at construction, not by
convention, and proven with 6 direct tests (`test_a_failing_status_may_not_carry_a_value`
et al.).

---

## 4. `KPIRegistry` — the access layer

`netgravity/orchestrator/metrics/registry.py`

```
KPIRegistry
    ├── network_kpis(ctx)              -> Dict[str, KPIResult]
    ├── facility_kpis(ctx)              -> Dict[facility_id, Dict[str, KPIResult]]
    ├── resilience_kpis(ctx)            -> Dict[str, KPIResult]         (network-level REI)
    ├── facility_resilience_kpis(ctx)   -> Dict[facility_id, Dict[str, KPIResult]]
    ├── risk_kpis(ctx)                  -> Dict[str, KPIResult]         (network-level RF)
    ├── facility_risk_kpis(ctx)         -> Dict[facility_id, Dict[str, KPIResult]]
    ├── forecast_metrics(ctx)           -> Dict[str, KPIResult]         (per market:product)
    ├── sustainability_kpis(ctx)        -> Dict[str, KPIResult]         (carbon subset)
    ├── scenario_comparison(ctx)        -> List[ScenarioMetricDelta]
    ├── thresholds() / evaluate_thresholds(results) -> List[TriggeredThreshold]
    └── evidence_package(ctx)           -> AuthoritativeEvidencePackage
```

**One class, not one class per KPI** — per the phase brief's explicit
guidance, and because nothing in the existing architecture (which organises by
*domain package*: `resilience/`, `orchestrator/risk/`, `orchestrator/twin/`)
suggested otherwise.

**Stateless.** `KPIRegistry()` holds only the threshold catalogue (loaded
once, read-only). Every method reads what `ExecutionContext` already holds and
returns a fresh result; nothing is cached, and calling a method twice on an
unchanged context returns an equal answer (`test_the_package_is_a_view_and_calling_it_twice_agrees`).

### What each method actually does — wrap, or a documented derived calculation

| Method | Computes anything new? |
|---|---|
| `network_kpis` | No — reads `NetworkStateResult` fields verbatim |
| `facility_kpis` | No — reads `FacilitySummary` fields verbatim |
| `resilience_kpis` / `facility_resilience_kpis` | No — reads `FacilityResilienceRegistry` rows verbatim |
| `risk_kpis` / `facility_risk_kpis` | No — reads `RiskAssessment` rows verbatim |
| `forecast_metrics` | No — reads `AccuracyMetrics` verbatim |
| `sustainability_kpis` | No — re-exposes `total_carbon_kg` under a different grouping |
| `scenario_comparison` — cost | No — reads the existing `business_cost_delta`/`_pct` from the flattened `flatten_scenario_result` projection the scenario handler already computed |
| `scenario_comparison` — fill rate / utilization / carbon / SLA | **Yes, but not a new formula.** A generic `(right - left)` / `(right-left)/left*100` diff over two already-authoritative `NetworkStateResult`s, with the same `NOT_COMPARABLE`-on-missing-side semantics `orchestrator/twin/service.py::_kpi_deltas` already uses for the Digital Twin. |
| `scenario_comparison` — risk factor | **Genuinely new territory, honestly refused.** The Digital Twin's comparison never diffs RF/REI at all (`_COMPARED_KPIS` only covers `TwinKPIs` fields; RF/REI live in a separate `RiskContext`). This phase does not fabricate a two-sided comparison it cannot support with the data an execution actually holds — a single risk assessment cannot be diffed against itself, so it is reported `NOT_COMPARABLE` with the specific reason, never guessed. |
| `evaluate_thresholds` | No — pure comparison against the read-only catalogue |
| `evidence_package` | No — assembles the above into one object |

---

## 5. Ownership map (Step 4) — verified, not assumed

| Domain | Owner (verified) | This phase's role |
|---|---|---|
| Total network cost, transport, facility, inventory, allocation, flow, feasibility | `netgravity/optimization/milp.py` (PuLP/HiGHS) | Wrap only |
| Fill rate, utilization, weighted distance, SLA %, carbon | `netgravity/metrics/kpis.py::compute_kpis` | Wrap only |
| REI, cost exposure, service loss | `netgravity/resilience/rei.py` (free functions, no class) | Wrap only |
| P, RF | `orchestrator/risk/risk_factor.py::compute_risk_factor` | Wrap only |
| Forecast, P10/P50/P90, MAE, MASE, WAPE, structural break, regime | `netgravity/forecasting/` (`ETSForecaster`, `IntermittentForecaster`, `QuantileForecaster`, `ColdStartForecaster`, `change_point.py`, `regime.py`) | Wrap only |
| Savings / cost delta | `netgravity/schemas/contracts.py::build_scenario_result` (the canonical, most-used of three implementations found — see §7) | Wrap only |
| Scenario KPI deltas (network-level) | `orchestrator/twin/service.py::_kpi_deltas` | Reused pattern for a genuinely new set of metrics (RF/REI) it does not cover |
| Governance thresholds | `orchestrator/governance/action_classifier.py::GovernancePolicy` | Exposed via `ThresholdSpec`, read live from the real object — never copied as a literal that could drift |

---

## 6. Threshold integration (Step 7)

`netgravity/orchestrator/metrics/thresholds.py` builds the catalogue by
**importing the real constant** from its owning module at call time —
`GovernancePolicy().risk_factor_human`, not a hardcoded `0.8` — so if the real
value ever changes, this catalogue changes with it rather than silently
drifting. 17 thresholds, across four bases:

| Basis | Count | Example |
|---|---|---|
| `BUSINESS_POLICY` | 7 | `GOV_RISK_FACTOR_HUMAN = 0.8` |
| `ENGINEERING` | 4 | `UTILIZATION_OVER = 90%`, circuit breaker 3/30s |
| `STATISTICAL` | 2 | sup-F 8.85 / 12.35 (Andrews 1993) |
| `UNCONFIGURED` | 4 | 3 REI absolute bands (disabled by design) + adaptive materiality surge (disabled by default) |

`UNCONFIGURED` thresholds carry `value=None` and `ThresholdSpec.evaluate()`
refuses to fire on `None` regardless of the value being tested — verified by
`test_disabled_thresholds_do_not_fire`, which checks an extreme input (999.0)
still does not trigger.

---

## 7. Scenario comparison (Step 8) and the three "savings" implementations

The audit — and independent verification — found **three separate cost-delta
implementations**:

1. `metrics/kpis.py::_build_go_no_go` — `annual_savings`, 4 cost components, no percentage
2. `schemas/contracts.py::build_scenario_result` — `business_cost_delta`/`_pct`, the one actually consumed by tests and the orchestrator
3. `orchestrator/twin/service.py::compare()` — generic `MetricDelta` over `business_network_cost`

**This phase adopts #2 as canonical** and wraps it, rather than building a
fourth. It is the one already flowing through the live scenario workflow, the
one with the most real test coverage, and the one already reachable from
`ExecutionContext` without a network/config object the orchestrator does not
retain.

`KPIRegistry.scenario_comparison` reads it from
`context.output_of("optimization.solve_scenario")` — the **flattened**
transport projection — because `ctx.network_states["scenario:<id>"]` holds
only `ScenarioResult.state` (the plain `NetworkStateResult`); the delta fields
live exclusively on the wrapping `ScenarioResult`, which is not what gets
stored in `ExecutionContext.network_states`. Verified directly against a real
scenario run (`test_cost_delta_matches_the_existing_scenario_result_exactly`).

---

## 8. `AuthoritativeEvidencePackage` (Step 9)

```
AuthoritativeEvidencePackage
    ├── network_kpis: Dict[str, KPIResult]
    ├── facility_kpis: Dict[facility_id, Dict[str, KPIResult]]
    ├── lane_kpis: Dict[str, Dict[str, KPIResult]]        (empty — see §9 limitations)
    ├── forecast_metrics: Dict[str, KPIResult]
    ├── resilience_metrics: Dict[str, KPIResult]
    ├── risk_metrics: Dict[str, KPIResult]
    ├── sustainability_metrics: Dict[str, KPIResult]
    ├── scenario_comparison: List[ScenarioMetricDelta]
    ├── triggered_thresholds: List[TriggeredThreshold]
    ├── unavailable_evidence: List[UnavailableMetric]
    └── provenance: EvidenceProvenance
```

A VIEW, exactly like `ExecutionContext.agent_result()` — built on demand,
nothing stored between calls.

**Forward-compatible with the existing reasoning pipeline, without touching
it.** `AuthoritativeEvidencePackage.to_evidence_payload()` flattens the
package into the same plain-dict shape
`orchestrator/reasoning/evidence.py::build_evidence_pack` already consumes.
`test_the_package_flattens_to_a_payload_build_evidence_pack_accepts` proves
the round trip works — **without either module importing the other's types,
and without this phase changing what the live reasoning path actually
does.**

---

## 9. Reasoning Agent boundary (Step 10)

```
Specialist Engines
       ↓
Authoritative KPI Layer      (this phase)
       ↓
Authoritative Evidence Package
       ↓
Reasoning Agent              (unchanged — reads `to_evidence_payload()` output
                               through the SAME `build_evidence_pack` it always used,
                               when a future phase wires the call site)
```

**Nothing in this phase modifies `reasoning_agent.py`, `evidence.py`'s runtime
behaviour, or the `synthesise` capability handler.** The interface is
established; the wiring is deliberately deferred, per the phase's own
instruction not to "implement the reasoning changes... unless absolutely
required to establish the interface."

Verified structurally, not by convention:

- `orchestrator/agents/reasoning_agent.py` imports neither
  `orchestrator.metrics.registry` nor `orchestrator.schemas.kpi`
  (`test_reasoning_output_cannot_overwrite_a_kpiresult`).
- `orchestrator/metrics/registry.py` imports no `ReasoningResult`, no LLM
  gateway, no `reasoning_agent` module at all
  (`test_llm_output_cannot_construct_a_valid_kpiresult_without_a_value`).
- Neither `KPIResult` nor `AuthoritativeEvidencePackage` exposes a setter,
  `override`, `from_narrative`, or `merge` method
  (`test_the_evidence_package_has_no_write_path_from_outside`).
- Nothing in the KPI layer imports a frontend module
  (`test_frontend_cannot_become_kpi_authority`).

---

## 10. Data gaps found — documented, not fabricated

| Gap | Evidence | Disposition |
|---|---|---|
| `weighted_avg_distance_km`, `inbound_avg_distance_km`, `outbound_avg_distance_km`, `carbon_per_unit`, `min_utilization_pct` | Computed by `NetworkKPIs` (`metrics/kpis.py:110-170`) but not copied across the `OptimizationResult → NetworkStateResult` bridge (`metrics/contracts.py:194-215`) — `ExecutionContext` never receives them, typed or flattened. | Reported `INSUFFICIENT_EVIDENCE` with the exact reason, never fabricated as zero. A low-risk, additive fix (new Optional fields on `NetworkStateResult`, populated at the existing construction site) is recommended but **not applied** in this phase — see `data_gap_inventory.json` for the exact change and its risk assessment. |
| `AssignmentDecision.safety_stock_units` | Field exists (`schemas/results.py:728`) but `optimization/milp.py` never populates it — only `inventory_cost` is set. The true per-pair value exists in `inv_coeffs` at that point and is simply not copied across. | Documented as a latent, currently-harmless bug (nothing reads the field today). Not fixed — outside this phase's KPI-exposure scope, and touches MILP output construction. |
| `FacilityDecision.fixed_cost`, `.status`, `.latitude`, `.longitude` | `milp.py:680-694` passes these kwargs, but `FacilityDecision` (`schemas/results.py:117-140`) has no such fields; Pydantic v2's default `extra="ignore"` silently drops them. `fixed_cost` is therefore always `0.0`. | Documented. Not fixed — no code currently reads `fd.fixed_cost` (network cost is summed independently from `objective_components`), so this is latent, not presently corrupting any KPI. |
| Center of Gravity (`weiszfeld_cog`/`multi_cog`) | Real, tested, correct algorithm — but returns plain `@dataclass` objects never wired into any Pydantic schema or the orchestrator. Explicitly documented in its own module as "a geographic screening tool, not an optimization decision." | Not exposed as a `KPIResult` in this phase — doing so would require either inventing a schema/capability integration point that does not exist today (architectural expansion, explicitly out of scope) or wiring ad hoc dataclasses through `ExecutionContext`. Documented as available-on-demand, not orchestrator-integrated. |
| Safety Stock formula completeness | Real formula is `SS = z·σ_d·√(LT/days_per_period)` — deterministic lead time, no `σ_LT` term. The audit's claimed formula includes a lead-time-variance term that does not exist in code. | Wrapping the EXISTING formula (not the audit's invented one) was considered; deferred for the same reason as CoG — no orchestrator capability or `ExecutionContext` field currently carries it. |
| Reorder Point | Confirmed absent from the codebase entirely — no formula, no field, no function, anywhere. | **MISSING.** Not implemented: doing so would require inventing a formula, which the phase explicitly forbids. |
| Value at Risk | Confirmed absent from the codebase entirely. | **NOT_APPLICABLE.** No `ThroughputValue` field exists on any facility/network schema either — the audit's own formula (`RF × ThroughputValue`) has no computable input. |

---

## 11. What this phase deliberately did not build

- **No new agent** — `KPIRegistry` is a plain class, not a capability, not
  registered in `CapabilityRegistry`, has no `AgentResult`-shaped return.
- **No LLM call anywhere** in `orchestrator/metrics/` — verified by AST
  import-graph check.
- **No new calculation engine** — every formula-bearing value is read from an
  existing typed result; the only new arithmetic is the generic scenario-delta
  diff, itself modelled on an existing, tested pattern.
- **No change to any existing formula, threshold value, or test.** Zero files
  outside `orchestrator/schemas/kpi.py`, `orchestrator/metrics/`, and the new
  test file were modified.
- **No reasoning-agent wiring** — the interface exists; the connection is
  Phase 9.2's work, per the brief.

---

## 12. Architecture diagram

```
User Request
     ↓
Orchestrator
     ↓
Specialist Engines
  MILP · REI · RF · Forecasting · Twin
     ↓                                    (each produces its OWN typed,
     │                                     already-tested result — unchanged)
     ↓
ExecutionContext
  network_states · rei_registry · risk_results · forecast_result
     ↓
KPIRegistry                                (Phase 9.1 — this layer)
  wraps verbatim, or performs a documented derived calculation
     ↓
KPIResult[T]           — status, unit, scope, formula_id, provenance
     ↓
AuthoritativeEvidencePackage
  network_kpis · facility_kpis · resilience_metrics · risk_metrics
  · forecast_metrics · scenario_comparison · triggered_thresholds
  · unavailable_evidence · provenance
     ↓
to_evidence_payload()  ← forward-compatible; not wired to the live path yet
     ↓
(Phase 9.2+) Reasoning Agent — reads, never writes, never computes a ₹ figure,
             an REI, an RF, a VaR, a forecast, a utilization, a fill rate, or
             a carbon figure of its own.
```
