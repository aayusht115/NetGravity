# NetGravity V1.4 — Deterministic Core Hardening

Closure economics · Contractual constraints · Service methodology ·
Optimization modes · Frozen result contracts

---

## 1. Closure economics

### Before

`FacilityRecord.closure_cost` existed in the schema and was validated as
non-negative, but **no code read it**. It appeared in the MILP objective in no
form, in reconciliation in no form, and the `GoNoGoEvidence.closure_cost` /
`has_closure_cost` fields were never populated. Closing a facility was free.

### Now

The objective carries a linear closure term:

```
Σ_{i ∈ closure-eligible}  closure_cost_i · (1 − y_i)
```

Eligibility is decided by `FacilityRecord.closure_cost_applies()`, which charges
the cost **only** when all of these hold:

| Condition | Rationale |
|---|---|
| Facility was `EXISTING` in the **observed baseline** | Only an operating facility can be closed |
| It is closed in this solution (`y_i = 0`) | Open facilities are never charged |
| It is not an involuntary disruption target | An outage is not a voluntary closure |
| `closure_cost > 0` | Nothing to charge otherwise |

And **not** charged for: facilities that stay open, facilities already `CLOSED`
in the baseline, or unselected `CANDIDATE` facilities. Mode policy adds one more
gate — `GREENFIELD_OPTIMIZATION` never charges closure, because a facility being
absent from a greenfield design is not a decision to close it.

### The `baseline_status` field

`ScenarioEngine`'s `CLOSE` action overwrites `status` with `CLOSED`. Keying
closure cost on `status` would therefore have meant it **never fired for the
scenario-driven closures it exists to price**. `FacilityRecord.baseline_status`
preserves the observed status before any override; `effective_baseline_status`
falls back to `status` when no override occurred. This also keeps observed and
hypothetical state cleanly separated (see §5).

### Cost categories stay distinct

Operating (`facility_cost`), opening, closure, CapEx, shortage penalty and
carbon are separate throughout the objective, `objective_components`,
reconciliation, business cost and the result contracts. None is folded into
another.

### Impact on the Case-16 baseline — read this

The Case-16 fixture assigns closure costs to its three existing DCs
(DC_CENTRAL 50,000 / DC_EAST 40,000 / DC_WEST 35,000). Once closing is priced,
the optimum changes:

```
closure-disabled optimum (DC_CENTRAL closed) : 115,638.14
+ one-time closure charge for DC_CENTRAL     :  50,000.00
= cost of closing under V1.4                 : 165,638.14
optimum that keeps DC_CENTRAL open           : 150,627.70  ← now chosen
```

> **KNOWN LIMITATION — period mixing.** A **one-time** closure cost is being
> compared against a **per-period** (here monthly) fixed-cost saving, because
> `cost_period = MONTH`. Over one month a 50,000 charge outweighs a
> 40,000/month saving; over a year it would not. This follows the convention
> already established by `opening_cost`, which is likewise one-time and
> unamortised in a per-period objective.
>
> Mitigations, in order of preference:
> 1. Set `cost_period = CostPeriod.YEAR` for footprint decisions, so one-time
>    and recurring costs are compared over a comparable horizon.
> 2. Amortise `closure_cost` / `opening_cost` externally before supplying them.
> 3. Set `enable_closure_cost = False` to restore pre-V1.4 behaviour exactly.
>
> A proper fix is multi-period NPV, which requires a time-phased MILP and is out
> of scope for V1.

`enable_closure_cost = False` reproduces the original objective
(115,638.1433) exactly — verified by test.

---

## 2. Contractual constraints

### Before

**No contract fields existed anywhere** in the schema. There was nothing to
verify.

### Now

Minimal, deterministic contractual state on `FacilityRecord`:

| Field | Meaning |
|---|---|
| `contract_status` | `NONE` (default) / `ACTIVE` / `EXPIRED` |
| `contract_allows_early_closure` | Default `True`. Only meaningful when `ACTIVE` |

The three required states:

| State | Configuration | Behaviour |
|---|---|---|
| 1. Active, closure prohibited | `ACTIVE` + `allows_early_closure=False` | Constraint **C5c** pins `y_i = 1` — facility must remain open |
| 2. Active, closure allowed with penalty | `ACTIVE` + `allows_early_closure=True` | May close; `closure_cost` charged as the early-termination penalty |
| 3. Expired / none | `EXPIRED` or `NONE` | Normal optimization, no contractual constraint |

**No separate penalty field was added.** The contractual penalty is expressed
through the existing `closure_cost`, so there is one definition of the economic
event "this facility closed early" rather than two competing ones.

### Conflict handling

A facility whose contract prohibits closure but which is also `is_forced_closed`
produces contradictory constraints and an infeasible model. Validation check
**V-015** names this conflict explicitly, so the diagnostic is readable rather
than a bare `INFEASIBLE`. To close such a facility, the scenario must
**explicitly** relax the contract (`contract_status → EXPIRED`, or
`contract_allows_early_closure → True`). Contracts are never inferred, and no
LLM participates.

### Disruption exemption

`is_disruption_target` (set by the resilience engine, not client input) exempts a
facility from both closure cost and the contractual open-pin. Without this,
every resilience run on a contracted facility would return infeasible for the
wrong reason — a physical outage is not a contractual breach.

### Defaults

`contract_status` defaults to `NONE`, so contractual logic is completely inert
for existing data. `enforce_contracts = False` disables C5c globally.

---

## 3. V1 service methodology

### What is actually implemented

```
PRIMARY SERVICE CONSTRAINT = TRANSIT-TIME SLA FEASIBILITY
```

A lane whose lead time exceeds the destination market's `sla_days` is removed
from the arc set **before** the solve, making it infeasible to use. Two
evaluation modes:

- `LAST_MILE` (default) — outbound lane lead time vs SLA.
- `END_TO_END` — inbound + DC replenishment + outbound lead time vs SLA.

### What is NOT implemented — verified, not assumed

| Declared | Status |
|---|---|
| `ServiceMetric.TRANSIT_TIME` | **Implemented** |
| `ServiceMetric.CSL` | Declared only — no constraint, no objective term |
| `ServiceMetric.FILL_RATE` | Declared only |
| `ServiceMetric.PENALTY` | Declared only |
| `ObjectiveMode.COST_SERVICE` | Declared only — no service-fraction constraint exists |

Verification: `config.service_metric` is read **only** by
`diagnostics/infeasibility.py` and `service/module.py`. The MILP never reads it —
it performs its own inline SLA lane filtering. `ServiceModule` itself has **zero
callers** and is diagnostic-only. `objective_mode` is read by the MILP only for
`WEIGHTED_COST_CARBON`.

Probabilistic service levels and OTIF are not modelled at all.

### How this is surfaced

Nothing was removed — existing fields are preserved for compatibility. Instead
every result now carries a `ServiceReport` stating what was actually enforced:

```python
res.service_report.methodology              # "TRANSIT_TIME_SLA_FEASIBILITY"
res.service_report.sla_enforced             # config.enforce_sla
res.service_report.sla_mode                 # LAST_MILE | END_TO_END
res.service_report.service_metric_supported # False for CSL / FILL_RATE / PENALTY
res.service_report.unsupported_features     # names anything declared-but-inert
res.service_report.total_demand / served_demand / unserved_demand
res.service_report.pct_demand_in_sla
res.service_report.n_lanes_evaluated / n_lanes_sla_excluded
res.service_report.violations               # populated when config.verbose
```

Selecting an unimplemented metric does not silently pretend to work: it is named
in `unsupported_features` **and** appended to `SolverMetadata.warnings`.
`claims_only_supported_capabilities` is a single boolean for callers to assert
on.

---

## 4. Optimization modes

Five modes, **one MILP**. A mode never changes the mathematics — it only fixes
decision variables, selects lane availability, and declares which economic terms
apply. All of that lives in one table (`MODE_POLICIES` in
`optimization/modes.py`); the MILP reads the declarations rather than branching
on the mode.

| Mode | Footprint | Candidates | Lanes | Closure cost | Contracts | Hypothetical |
|---|---|---|---|---|---|---|
| `ACTUAL_AS_IS_EVALUATION` | pinned open | excluded | baseline only | no | yes | **no** |
| `CURRENT_FOOTPRINT_OPTIMIZATION` | pinned open | excluded | all | no | yes | yes |
| `GREENFIELD_OPTIMIZATION` | released | available | all | no | no | yes |
| `BROWNFIELD_SCENARIO_OPTIMIZATION` *(default)* | as supplied | as supplied | all | **yes** | **yes** | yes |
| `DISRUPTION_RESILIENCE_OPTIMIZATION` | as supplied | as supplied | all | **yes**¹ | **yes**¹ | yes |

¹ Disruption targets are exempt from both.

### Mode 1 vs Mode 2

These differ on **lane availability**, using the pre-existing but previously
unused `LaneRecord.is_active_baseline` flag. As-is restricts flow to the
observed lane set; current-footprint frees routing across every lane while
keeping the footprint locked.

> **KNOWN LIMITATION.** NetGravity V1 has no observed-flow input field, so
> `ACTUAL_AS_IS_EVALUATION` evaluates the observed **footprint and lane set**
> with a cost-minimal allocation — it does not replay recorded shipment
> volumes. If observed flows are later added to the schema, this mode is where
> they would be pinned; no other mode changes.

### Observed vs optimized can never be conflated

`ACTUAL_AS_IS_EVALUATION` is the only mode with `is_hypothetical = False`. The
flag is stamped on `OptimizationResult` and on the result contracts, and
`ScenarioResult.is_hypothetical` is always `True`.

### Backward compatibility

The default, `BROWNFIELD_SCENARIO_OPTIMIZATION`, is a **strict no-op**:
`prepare_network_for_mode` returns the original network object unchanged. Every
pre-V1.4 configuration behaves exactly as before.

### Resilience override

Resilience runs force `DISRUPTION_RESILIENCE_OPTIMIZATION` for both the baseline
and every disrupted solve. A footprint-locking mode would pin existing
facilities open and make every disruption infeasible for the wrong reason;
forcing the mode symmetrically also preserves the fair-comparison invariant REI
depends on.

---

## 5. Frozen deterministic result contracts

`schemas/contracts.py` defines the stable boundary between the deterministic
core and the Orchestrator / RF layer; `metrics/contracts.py` builds them
(reusing the existing business-cost and reconciliation layers — no second cost
model).

| Contract | Purpose |
|---|---|
| `NetworkStateResult` | One network configuration, observed or optimized |
| `ScenarioResult` | One hypothetical scenario, with baseline identity and deltas |
| `FacilityResilienceResult` / `FacilityResilienceRegistry` | Resilience & REI (existing, extended with snapshot identity) |
| `ForecastResult` | **Not created** — no forecasting module exists, and building one was out of scope |

### Guarantees

1. **Snapshot identity.** Every result carries `network_id` + `data_version`.
2. **Mode and hypothetical status** are always explicit.
3. **Business cost is separate from the solver objective**, with
   `shortage_penalty_cost` broken out and excluded. Consumers never reverse a
   penalty out of an objective.
4. **Scenario results never overwrite observed state** — the baseline is carried
   by identity, not by mutation.
5. Cost categories stay distinct: facility / opening / **closure** / transport /
   handling / inventory / carbon, plus shortage penalty reported separately.

### Deliberately excluded

**Risk Factor, likelihood and probability are not in these contracts.** RF is
computed later inside the Orchestrator Agent. What the contracts provide is the
deterministic evidence RF will need — cost impact, service impact, feasibility,
full cost basis, and reconciliation health. A test asserts these terms never
appear in the contract schema.

---

## 6. REI integrity — unchanged

The REI formulation was **not** modified; no bug was found in it.

```
PI_i  = C_business,i(disrupted) − C_business,baseline
REI_i = PI_i / max_j(PI_j)
```

Preserved and re-tested: business-cost basis (never the raw objective),
infeasible-disruption handling, the shortage-enabled diagnostic solve,
negative-PI diagnostics, determinism, and baseline isolation.

Two additions only: `closure_cost` joined the business-cost basis, and result
rows gained `network_id` / `data_version` for the contract requirement.

---

## 7. What changed in the MILP formulation

Two additions to a formulation that is otherwise untouched:

**Objective** — one new linear term:

```
+ Σ_{i ∈ closure-eligible} closure_cost_i · (1 − y_i)
```

**Constraints** — one new constraint family:

```
(C5c)  y_i = 1    ∀ i : contract_status = ACTIVE
                       ∧ ¬contract_allows_early_closure
                       ∧ ¬is_disruption_target
```

Both are linear; the model remains a MILP. No variables were added, no existing
constraint changed, and the solver is unchanged (PuLP/HiGHS).

---

## 8. Known limitations

1. **One-time vs per-period costs** — closure and opening costs are one-time but
   enter a per-period objective unamortised (§1). Mitigate with
   `cost_period = YEAR` or external amortisation.
2. **No observed flows** — `ACTUAL_AS_IS_EVALUATION` evaluates the observed
   footprint and lane set, not recorded shipment volumes (§4).
3. **Single-period model** — no TTR, no multi-period recovery, no NPV.
4. **Service** — transit-time SLA only. CSL, fill rate, OTIF, probabilistic
   service and service penalties remain unimplemented and are reported as such.
5. **Greenfield is candidate-location optimization** — not arbitrary continuous
   geographic siting.
6. **`ServiceModule` is dead code** — zero callers; the MILP filters SLA inline.
   Retained rather than removed to avoid unnecessary churn.
7. **`FacilityDecision.fixed_cost` is always 0** — pre-existing quirk
   (`milp.py` passes `fixed_cost_period=`, which Pydantic drops). Not fixed
   here; all cost paths recompute from network parameters instead.
8. **`GoNoGoEvidence.closure_cost` / `has_closure_cost` remain unpopulated** —
   pre-existing; the new contracts supersede that path.
