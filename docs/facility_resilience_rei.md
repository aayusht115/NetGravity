# Facility Resilience Assessment & Risk Exposure Index (REI)

Version 1.0.0 · Module `netgravity/resilience/rei.py`

---

## 1. Definition

The Risk Exposure Index answers one business question:

> If a facility becomes unavailable, how much additional network cost does the
> business incur after the network optimally reconfigures itself?

**REI measures relative economic exposure to facility disruption.** The facility
whose loss costs the business the most scores 1.00; every other facility is
scaled against it.

### What REI does NOT mean

REI must never be presented as any of the following:

- probability of failure
- probability of disruption
- percentage of resilience
- AI confidence
- service-level score
- generic risk score

It is a *relative ranking of economic exposure*, nothing more. Two registries
built under different disruption assumptions are not comparable.

---

## 2. Formula

For facility *i*, under a fixed disruption assumption:

```
Performance Impact    PI_i     = C_business_i − C_business_base        (signed)

Cost Impact           CI_i     = PI_i / C_business_base × 100

Economic Impact       EI_i     = max(0, PI_i)                          (floored)

Risk Exposure Index   REI_i    = EI_i / max_j(EI_j)                    ∈ [0, 1]
```

where *j* ranges over **all facilities evaluated under the same disruption
assumptions**.

### Why REI normalises over `max(0, PI)` and not `PI`

`PI` is signed and retained signed: a disruption that *reduces* optimized cost
is a real result worth investigating, and it stays visible on the row.

But such a facility has **no economic exposure** — losing it does not cost the
business anything. Normalising a negative PI would produce a negative REI, which

1. inverts the ranking (a cost-*reducing* facility would sort as "most negative
   exposure"), and
2. breaks the `[0, 1]` bound that `RF = P + REI − P·REI` requires.

So the floor applies **only** to the quantity REI normalises over. Both values
are reported: `performance_impact` (signed, raw) and `economic_impact`
(floored). Nothing is hidden.

Guarantees, all test-asserted:

```
0 ≤ REI ≤ 1                    for every assessed facility
REI = 1                        for the largest positive exposure
REI = 0                        where PI ≤ 0
REI = None                     where the facility could not be assessed
no division by zero            when every impact is zero
```

Worked example:

| Facility | Incremental cost | REI  |
|----------|-----------------:|-----:|
| A        | ₹18L             | 1.00 |
| B        | ₹12L             | 0.67 |
| C        | ₹6L              | 0.33 |
| D        | ₹2L              | 0.11 |

---

## 3. Architecture — the engine sits around the MILP

There is exactly **one** optimisation model in NetGravity:
`netgravity.optimization.milp.solve`. The REI engine calls it; it never
reformulates, approximates or duplicates it.

```
                     Existing Network
                           │
                     Existing MILP                 (solved ONCE)
                           │
                     Baseline Solution
                           │
                     Business Cost  C_base
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
         Facility i                Facility j
         unavailable               unavailable
              │                         │
         Existing MILP             Existing MILP   (re-optimisation)
              │                         │
         Business Cost C_i         Business Cost C_j
              └────────────┬────────────┘
                           ▼
                  PI_i = C_i − C_base
                           ▼
                 REI_i = PI_i / max_j(PI_j)
                           ▼
              Facility Resilience Registry
                           ▼
              (future) Resilience Agent
```

A network-level assessment costs **1 + N + D** MILP solves: the baseline once
(reused by every facility), one re-optimisation per facility, and one optional
service-diagnostic re-solve per *infeasible* facility (`D ≤ number of infeasible
facilities`, see §5). `registry.total_milp_solves` reports the exact count.

---

## 4. Cost basis

### Three concepts kept explicitly separate

| Concept | What it is | Used for REI? |
|---|---|---|
| **Solver Objective** | What the MILP mathematically minimises. Contains artificial devices. | No |
| **Business Network Cost** | The economic cost the business actually incurs. | **Yes** |
| **Shortage Penalty** | A penalty on unmet demand that forces coverage. | No |

### Business Network Cost

```
C_business = C_facility + C_opening + C_transport + C_handling + C_inventory + C_carbon*
```

Components come from `costs.reconciliation.reconcile_costs`, which independently
evaluates each one from the raw decision vectors (`y_i`, `x_ijvk`, `a_ij`) and
canonical network parameters. `costs/business_cost.py` only *selects* which of
those components count as business cost — there is no second cost model.

\* **Carbon** is included only when carbon is genuinely part of the configured
business objective, i.e. `OptimizationConfig.enable_carbon_cost = True` and a
real `carbon_price` applies. Under `ObjectiveMode.WEIGHTED_COST_CARBON` the
`carbon_weight` term is a modelling *preference weight*, not money, and is
excluded on the same grounds as the shortage penalty.

Configure the basis via `ResilienceCostBasis`.

---

## 5. Shortage treatment

**The shortage penalty is excluded from business network cost.**

The default penalty is 1e6 per unmet unit. It exists to force the optimiser to
cover demand; it is not a measured financial loss. Including it would invent a
monetary value for lost demand, which the model has no basis to assert.

The magnitude of the problem is not theoretical. Disrupting a plant in the
Case-16 fixture strands 1,300 units:

```
solver objective        1,300,106,134.72     ← dominated by the penalty
business network cost         106,134.72     ← the real economic quantity
excluded penalty        1,300,000,000.00
```

Differencing raw solver objectives would measure the penalty, not the business
impact. So this is explicitly **not** how PI is computed:

```python
# WRONG — contaminated by an artificial penalty
performance_impact = disrupted_solver_objective - baseline_solver_objective
```

### Shortage is still retained

Unmet demand remains a first-class resilience diagnostic. Every result carries
`unserved_demand`, `unserved_demand_rate`, `service_loss` and
`excluded_shortage_penalty`, so the system can explain *why* a facility is
exposed without contaminating the cost-based REI.

To treat the penalty as real cost — only with a documented business basis — set
`ResilienceCostBasis(include_shortage_penalty=True)`.

### Why the default basis disables shortage

`DisruptionConfig.allow_shortage` defaults to **False**, and this is a
scalability decision rather than a stylistic one.

With shortage *enabled*, a disruption the network cannot absorb still solves —
but the disrupted network then serves **less volume**, so its business cost
falls and the facility scores a **negative** Performance Impact. The most
operationally exposed facility ranks *last*. On the Case-16 fixture,
`PLANT_NORTH` stranded 17.8% of demand and scored PI = −9,503 — so its economic
impact floors to 0 and it receives **REI = 0**, ranking joint-last despite being
the most operationally exposed node in the network.

(Before REI V1 the same case produced REI = −1.571. Flooring fixed the out-of-
range value but not the underlying ranking problem: a cost-reducing disruption
still cannot be ranked by cost, because there is no cost increase to rank.)

That failure is silent, and it **worsens as the network grows**: more markets
means more single-source coverage means more contaminated rows. A silent failure
mode that degrades with N does not scale.

With shortage *disabled*, every compared solution serves 100% of demand, so the
cost comparison is like-for-like by construction, and an unabsorbable disruption
reports `INFEASIBLE → CRITICAL` — loud, and correct at any network size. The
same `PLANT_NORTH` now surfaces at the top of the risk view instead of the
bottom of the cost ranking.

### Service diagnostic for infeasible disruptions

Disabling shortage alone would discard the unmet-demand figures that §5 requires
alongside REI. So when a disruption is INFEASIBLE under the primary basis, the
engine re-solves **that facility only**, once, with shortage enabled
(`service_diagnostic_on_infeasible`, default True), purely to quantify the
damage:

```
DC_HUB
  performance_impact   None          ← cost undefined, never estimated
  cost_impact_pct      None
  rei                  None
  risk_classification  CRITICAL
  unserved_demand      1,000 units   ← quantified by the diagnostic pass
  unserved_demand_rate 90.9%
  service_loss         0.909
  solver_status        INFEASIBLE
  service_diagnostic_applied  True
```

The pass returns **only** service and carbon fields. No cost, PI, CI or REI is
derived from it, so the artificial penalty can never reach the cost-based
ranking. Cost is left undefined; severity is still measurable, which means two
CRITICAL facilities can be compared operationally even though neither has a PI.

Work stays bounded: at most one extra solve per infeasible facility. Set
`service_diagnostic_on_infeasible=False` to skip it entirely.

---

## 6. Comparison requirement

REI is meaningful **only** when every facility is assessed under identical
assumptions: same disruption type, same disruption period, same demand, same
cost parameters, same service constraints, same capacity assumptions, same model
configuration.

This is enforced structurally. `assess_network_resilience` takes **one**
`DisruptionConfig` and derives a single effective `OptimizationConfig` used for
the baseline *and* every disrupted solve. Note that `allow_shortage` is applied
symmetrically — the pre-existing `ResilienceEngine.facility_failure` solves the
baseline with the caller's config but the disrupted network with shortage
enabled, which compares two different models. REI requires one.

**Never mix registries produced under different disruption assumptions into a
single ranking.**

---

## 6b. Batching, caching and invalidation (V1)

### Solve count

A batch costs **1 baseline + N disruptions** (plus one optional service
diagnostic per infeasible node). The baseline is solved once and reused; it is
never re-solved inside the loop. Measured on Case-16: 7 nodes → 9 solves.

### The REI service

```
Orchestrator → REIService → assess_network_resilience → MILP → REIRegistryStore
```

`REIService.get_or_compute()` returns a stored batch when one is still valid,
and computes only when it is not. A cache hit executes **zero** MILP solves.
Layering is strict: `rei.py` knows the mathematics and nothing about caching;
`registry_store.py` knows storage and nothing about the mathematics.

### Cache identity

A batch is reusable only for an exact match on:

```
(material_fingerprint, model_version, disruption_type, disruption_signature)
```

`disruption_signature` covers the shortage policy, cost basis, eligibility
filters and risk rules — REI is only comparable within one set of assumptions,
so a change to any of them is a different calculation, not a cache hit.

### Material fingerprint

`resilience/fingerprint.py` hashes **only inputs that can change the optimum**:

| Material (invalidates) | Not material (does not) |
|---|---|
| demand, σ, SLA, priority | facility name |
| capacity, status, open/close flags | region, country, tags |
| fixed / handling / opening / closure cost, capex | network description |
| lane existence, rate, distance, lead time, capacity | solver name, time limit, threads, verbosity |
| contractual closure terms | parameter provenance/confidence |
| product weight / value / holding rate | |
| material config switches | |

This is deliberately narrower than `compute_data_version()`, which hashes every
field including labels — renaming a warehouse under that hash would force 1 + N
solves for a cosmetic edit. Solver *tuning* is excluded on the same principle:
raising a time limit does not change the optimum.

### Invalidation

A material change alters the fingerprint, so the old entry simply stops
matching — no explicit sweep is needed. `REIService.invalidate_for()` exists for
deliberate operator action ("recompute regardless").

---

## 6c. Phase 1 — the deterministic risk chain (MILP → REI → P → RF)

```
Network Snapshot -> Baseline MILP -> REI Batch -> REI Registry
                                                       |
                            External Event Probability |
                                                    \  v
                                                  RF Calculator
                                                       |
                                                 Risk Assessment
```

Responsibilities stay strictly separate. `risk/risk_assessment.py` is the
integration layer: it maps an event to a node, looks the node's REI up, checks
the snapshot, and asks the RF calculator. It contains no optimization, no REI
arithmetic and no RF arithmetic of its own.

### Node mapping is explicit

RF is never computed against an arbitrary node. Resolution prefers explicit
`affected_entity_ids`; failing that, an exact-or-token match on `location`. An
event that resolves to nothing yields `NODE_MAPPING_UNAVAILABLE` — it never
broadcasts across the network, because a flood in Delhi says nothing about
Mumbai's probability.

### RF not-computable reasons

| Reason | Meaning |
|---|---|
| `NO_EVENT_PROBABILITY` | no defensible P; severity/confidence are never substituted |
| `NO_REI` | node absent from the registry, or its REI is null (infeasible / time-limited / errored) |
| `STALE_REI` | the registry was computed against a different network snapshot |
| `NODE_MAPPING_UNAVAILABLE` | the event could not be tied to an eligible node |
| `NO_INPUTS` | neither P nor REI available |
| `INVALID_INPUT` | present but out of range |

`P = 0` is an explicit value and **computes** (RF = REI). Missing P is not zero.

### Snapshot consistency

The REI batch records the snapshot id it ran against, and RF refuses to combine
it with a different one. This required threading the orchestrator's snapshot id
into the REI service — without it the batch defaulted to `data_version` and
every RF was wrongly flagged stale.

### Persistence

`resilience/persistence.py` separates the durable source of truth from the
in-memory performance cache:

| Backend | Durable | Use |
|---|---|---|
| `NullPersistenceBackend` | no | default; preserves prior behaviour |
| `JsonFilePersistenceBackend` | yes | one atomic JSON document per batch |

Entries are written through on `put` and read back on a cache miss, so a
restarted process does not re-run 1 + N solves. Invalidation deletes the durable
record too, so a restart cannot resurrect a stale batch. A record with a
mismatched format version or cache key is ignored rather than trusted.

### Scale benchmark (measured)

Synthetic 2-plant / N-DC / (N/2)-market networks, every DC reaching every market:

| DCs | facilities | lanes | nodes | solves | baseline | node mean | sequential | parallel(4) | peak mem |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 7 | 13 | 42 | 9 | 10 | 0.11 s | 0.06 s | 0.67 s | 0.34 s (1.98x) | 0.8 MB |
| 25 | 39 | 350 | 27 | 28 | 0.20 s | 0.23 s | 6.5 s | 5.2 s (1.26x) | 8.9 MB |
| 50 | 77 | 1,350 | 52 | 53 | 1.05 s | 1.00 s | 53.2 s | 46.7 s (1.14x) | 58.7 MB |
| 100 | 152 | 5,200 | ~101 | ~102 | 6.0 s | — | **~10-11 min (projected)** | — | — |

The 100-DC row is a PROJECTION from three measured single solves
(5.92 / 5.94 / 6.27 s, identical objective), not an end-to-end batch run.

Cache effectiveness: 7 DCs 0.31 s -> 1.5 ms (209x); 25 DCs 2.86 s -> 3.1 ms (911x).

**Honest reading:** cost grows super-linearly with network density, and thread
parallelism decays as problems grow (1.98x at 7 DCs, 1.14x at 50) because the
solve holds more of its time in GIL-bound Python. Beyond ~50 facilities a
process pool or distributed workers would be needed; the `max_workers` /
`solve_fn` seams accept either without touching REI domain logic.

---

## 7. Edge cases

### Solver status handling

Each node records a `calculation_status` alongside the raw `solver_status`:

| Solver outcome | `calculation_status` | REI |
|---|---|---|
| `OPTIMAL` | `OK` | computed |
| `INFEASIBLE` | `INFEASIBLE` | `None` |
| `TIME_LIMIT` | `TIME_LIMIT` | `None` |
| engine exception | `ERROR` | `None` |

**`TIME_LIMIT` yields no REI.** A time-limited incumbent is not proven optimal,
so differencing it against a proven-optimal baseline would measure solver effort
rather than exposure. The incumbent objective, runtime and gap are all retained
for inspection; only the REI is withheld.

### Batch status

One failing node never destroys a batch:

| Condition | `batch_status` |
|---|---|
| every node computed | `COMPLETED` |
| some infeasible/errored, at least one usable | `COMPLETED_WITH_ERRORS` |
| no node produced a usable result | `FAILED` |

A `FAILED` batch is **not cached** — caching it would serve the failure back
instead of retrying.

### Infeasible disruption

If disrupting a facility makes the network infeasible, no cost is fabricated:

```
solver_status       = INFEASIBLE
is_feasible         = False
performance_impact  = None
cost_impact_pct     = None
rei                 = None
rank                = None
risk_classification = CRITICAL
```

Business interpretation: *the network cannot absorb this disruption under the
current constraints.*

Under the default basis these rows still carry quantified service damage from
the diagnostic pass (§5), so a CRITICAL facility is never just a bare
"infeasible" with no measure of severity.

### Zero PI

If `max_j(PI_j) == 0`, every REI is 0 and the registry reports
`rei_status = NO_RELATIVE_COST_EXPOSURE`. No division is performed.

### Negative PI

A negative PI is **retained, never clamped**. It generates a logged warning and
a diagnostic on the result. Two distinct causes are distinguished:

1. **Negative PI with unserved demand** — only reachable when the default basis
   has been overridden with `allow_shortage=True`. The disrupted network serves
   *less volume*, so part of the apparent saving is cost avoided by not serving
   demand. The comparison is not like-for-like; read `unserved_demand_rate` and
   `service_loss` as the exposure signal instead. Restoring the default
   (`allow_shortage=False`) removes the condition entirely — the facility
   reports INFEASIBLE → CRITICAL with its service damage quantified.
2. **Negative PI with full service** — genuinely anomalous. It may indicate an
   open facility whose fixed cost exceeded its routing benefit (meaning the
   baseline itself is suboptimal), a cost configuration issue, or an invalid
   comparison. Investigate before using the result.

---

## 8. Risk classification

REI is a relative ranking metric. **No REI band is applied**, because there is no
documented business basis for thresholds such as "REI > 0.8 = Critical".
Inventing them would manufacture a risk score.

Only deterministic rules classify risk:

| Rule | Classification |
|---|---|
| Disruption infeasible | `CRITICAL` |
| Solver error / no solution | `UNKNOWN` |
| `unserved_demand_rate ≥ unserved_demand_rate_critical` (if configured) | `CRITICAL` |
| `unserved_demand_rate ≥ unserved_demand_rate_high` (if configured) | `HIGH` |
| `cost_impact_pct ≥ cost_impact_pct_high` (if configured) | `HIGH` |
| Otherwise | `NOT_CLASSIFIED` |

All thresholds default to `None` (disabled) in `RiskClassificationRules`. Out of
the box, only the unambiguous infeasibility rule fires and REI + rank carry the
signal.

---

## 9. Future TTR support

The HBR resilience framework uses **Time to Recovery** (TTR = 1 / 2 / 4 / 8
weeks). The NetGravity MILP is a **single-period** model: demand, capacities and
lane limits are all per planning period, and no decision variable is indexed by
time. A multi-period recovery therefore cannot be modelled today.

Rather than fabricate a temporal calculation, V1 defines the experiment
explicitly as:

> **Facility unavailable for the modelled planning period.**
> (`DisruptionPeriodBasis.MODELLED_PLANNING_PERIOD`)

`DisruptionConfig.time_to_recovery_days` exists as a marked extension point and
**raises** if given a value. Genuine TTR support requires a time-phased
formulation; when that lands, `DisruptionPeriodBasis` gains the TTR variants and
the rest of this architecture is unchanged.

---

## 10. Public interface

```python
from netgravity.resilience import (
    assess_facility_resilience,   # one facility
    assess_network_resilience,    # full registry
    compute_baseline,             # reusable baseline
    discover_eligible_facilities, # dynamic discovery
    normalize_rei,                # pure REI arithmetic
)
from netgravity.schemas.resilience import DisruptionConfig, ResilienceCostBasis
```

```python
registry = assess_network_resilience(network, config, DisruptionConfig())

for row in registry.results:
    print(row.rank, row.facility_id, row.performance_impact, row.rei)
```

Facilities are **discovered from `network.facilities`** — no identity is ever
hard-coded. Markets, CLOSED and force-closed facilities are excluded; filters
(`eligible_roles`, `exclude_facility_ids`, `only_baseline_open_facilities`)
refine the set.

### Future execution strategies

Both entry points accept a `solve_fn` hook. Caching, parallel facility
evaluation, async execution and remote (Azure) execution can all be introduced
by supplying an alternative executor, with **no change to the public
interface**. Parallelism is deliberately not introduced prematurely; the registry
records `baseline_solve_seconds`, `total_assessment_seconds`, `n_diagnostic_solves`,
`total_milp_solves` and per-facility `solve_seconds` so the decision can be made
on measurements.

Measured on the Case-16 synthetic fixture (7 facilities, 8 markets, 1 product):

```
facilities assessed (N)   4
infeasible facilities     1
diagnostic solves         1
MILP solves               6  (1 + N + diagnostics)
baseline solve            0.018 s
per-disruption mean       0.024 s
total assessment          0.115 s
```

---

## 11. Determinism and the agent boundary

The REI engine is **fully deterministic**. Every number derives from the MILP and
from arithmetic on MILP outputs. **No LLM participates in the calculation of
PI or REI.**

The future resilience agent *consumes* the registry:

```
RESILIENCE ENGINE → REI REGISTRY → RESILIENCE AGENT
                                        ↓
                            Identify material exposure
                                        ↓
                              Generate scenarios
                                        ↓
                                      MILP
                                        ↓
                                 Stress testing
                                        ↓
                                Recommendation
```

The agent may: identify high-exposure facilities, explain why they are exposed,
identify disruption drivers, recommend further analysis, generate mitigation
scenarios, invoke the MILP, compare alternatives, stress-test recommendations,
explain cost/service trade-offs, and recommend an action.

The agent must **not**: calculate REI itself, invent costs, override MILP
results, invent disruption probabilities, or modify mathematical constraints
without explicit orchestration logic.

---

## 12. Related fix: shortage reconciliation parity

While building this capability, a genuine defect was found and fixed in the
existing cost audit.

The MILP objective penalises shortage with a demand-priority multiplier
([`milp.py`](../netgravity/optimization/milp.py) — `shortage_cost_term`):

```
shortage_penalty × (1 + (priority − 1) × 0.5) × u
```

but both the post-solve extraction and `reconcile_costs` applied a **flat**
`shortage_penalty × u`. Whenever a demand record with `priority > 1` went short,
reconciliation reported a spurious gap. On the Case-16 fixture:

```
before:  solver 5,600,060,998.59 vs independent 5,300,060,998.59
         gap 300,000,000.00 (5.36%)   is_reconciled = False
after:   gap 0.00                     is_reconciled = True
```

Both sites now mirror the objective term exactly. All existing shortage
assertions use `priority = 1` (multiplier 1.0) and were unaffected. The fix is
covered by `TestShortageReconciliationParity`.

This does **not** change the business-cost position: the shortage penalty
reconciles the *solver objective*, and remains excluded from business network
cost regardless of its multiplier.

---

## 13. Test coverage

`netgravity/tests/test_rei_v1.py` — 65 tests covering the frozen V1 scope
(baseline, single/multiple disruption, zero impact, negative incremental cost,
infeasibility, failure isolation, baseline immutability, snapshot consistency,
idempotency, material-change invalidation, normalisation bounds, store
behaviour, parallel equivalence, orchestrator integration).

`netgravity/tests/test_rei_performance.py` — 4 benchmark tests. Measured on
Case-16 (15 facilities, 8 demands, 41 lanes; 7 nodes assessed, 9 solves):

```
baseline solve            0.022 s
per node   min/mean/max   0.005 / 0.011 / 0.015 s
batch total  sequential   0.099 s
batch total  4 workers    0.054 s   (1.85x)
cached batch              0.0005 s  (0 solves, 9 avoided)
```

Threads do help: `highspy` releases the GIL inside the solve, so independent
scenarios genuinely overlap. `max_workers` defaults to 1 so behaviour is
predictable unless a caller opts in; the same seam accepts a process pool or a
remote worker without touching REI domain logic.

`netgravity/tests/test_resilience_rei.py` — 53 tests:

| Area | Coverage |
|---|---|
| Business cost | Hand-verified components; handling/opening; carbon gating; unsolved result raises |
| Shortage exclusion | Penalty inflates objective but not business cost; PI uses business cost (mandatory); explicit opt-in |
| Reconciliation parity | Priority multiplier reconciles to 0.00 gap |
| Failure with alternative | Reroute, zero shortage, hand-verified PI |
| High / low exposure | REI = 1.00 and rank 1; redundant facility low REI |
| REI normalisation | 100/50/25 → 1.00/0.50/0.25; None handling; empty input |
| Zero PI | REI 0, no divide-by-zero, end-to-end registry |
| Infeasibility | None PI/CI/REI, CRITICAL, registry survives; default basis is like-for-like; service diagnostic quantifies damage and can be disabled |
| Negative PI | Retained, flagged, not clamped |
| Fair comparison | Ranking by PI alone; shared assumptions; TTR rejected |
| Discovery | Reads from network; filters; no hard-coded identities |
| Error handling | Unknown/empty/market ID; infeasible baseline; invalid rules |
| Risk classification | No default bands; configured thresholds apply |
| Integration | Full Case-16 pipeline, MILP not mocked |
| Determinism | Identical repeat runs; input network not mutated |
| Performance | Exactly 1 + N solves when all feasible; diagnostics bounded by infeasible count; `solve_fn` injection; telemetry |
