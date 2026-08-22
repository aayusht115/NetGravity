# Digital Twin

> **The Orchestrator is the sole upstream integration point.**
> No engine calls the Digital Twin, and the Digital Twin calls no engine.

That sentence is the architecture. Everything below follows from it.

---

## 1. Where it sits

```
User / Workflow
      ↓
  Orchestrator
      ↓
┌─────┼─────┐
↓     ↓     ↓
MILP REI    RF            ← authoritative engines
└─────┼─────┘
      ↓
  Orchestrator            ← composes the authoritative result
      ↓
DigitalTwinState          ← the contract
      ↓
DigitalTwinService        ← store · materialise · compare
      ↓
Visualization / Comparison
```

There is exactly one call site: `Orchestrator._project_twin()`. A test walks
every non-test module and asserts that `twin.update(` appears in one file.

It sits in the `finally` block of `run()` — not on the success path. A stale,
failed or infeasible run is represented too, because publishing nothing leaves
whatever was published last on screen, and a viewer would see a healthy network
with no sign the run behind it collapsed.

## 2. What already existed

The audit found more than expected, and most of the work was reuse:

| Concern | Already existed | Reused as |
|---|---|---|
| Canonical network | `CanonicalNetwork` | referenced by `snapshot_id`, never copied |
| Immutable snapshots | `SnapshotManager` | unchanged — the twin stores no network |
| Scenario isolation | `ScenarioStore` | unchanged |
| Solved state | `NetworkStateResult` | **the twin's input contract** |
| Per-facility decisions | `FacilitySummary` | → `FacilityState` |
| Lane flows | `FlowSummary` | → `FlowState` |
| KPIs | `CostBreakdown`, `DemandSummary` | → `TwinKPIs` |
| Exposure | `FacilityResilienceRegistry` | → `FacilityState.rei` |
| Risk | `RiskAssessment` | → `FacilityState.risk_factor` |
| Missing evidence | `UnavailableEvidence` | → `UnavailableValue` |

No duplicate canonical model, no second snapshot mechanism, no parallel KPI
definition. `ScenarioComparison` in `schemas/results.py` was **not** reused: it
is a MILP-layer schema nothing constructs, and it has no vocabulary for a metric
that could not be compared — which is most of what `compare()` has to say.

### The one real gap

`_flatten_state()` in the engine adapter **discarded** `facilities` and `flows`,
keeping only id lists. Every per-facility utilisation figure and every lane
volume was thrown away before the orchestrator saw it, and nothing downstream
could recover them.

So the adapter now exposes each solve twice — `solve_result()` returning the
typed contract, `solve()` returning the flat dict — following the split
`REIClient.assess_registry()` / `assess()` already used in the same file for the
same reason. The orchestrator keeps the typed contract on
`ExecutionContext.network_states`, exactly as it already keeps `rei_registry`.

## 3. What the twin does not do

Compute. `DigitalTwinState` has no field for a value that would have to be
calculated, and `twin/` imports no engine:

```python
for banned in ("netgravity.optimization", "netgravity.resilience",
               "netgravity.costs", "netgravity.metrics", "orchestrator.risk"):
    assert f"import {banned}" not in code_with_docstrings_stripped
```

Docstrings are stripped before the scan, so a module that *explains* the
boundary is not mistaken for one that crosses it.

The builder accepts only frozen result **contracts** — `NetworkStateResult`,
`FacilityResilienceRegistry`, `RiskAssessment`. It is never handed a
`CanonicalNetwork` or an `OptimizationConfig`, so it could not solve anything
even if it wanted to. That is asserted too.

**Comparison is the one place arithmetic happens**, and it is subtraction
between two authoritative values that already exist. A delta is a statement
*about* two results, not a new result.

## 4. Baseline and scenarios

```
Snapshot (SnapshotManager — one network, immutable)
   │
   ├── BASELINE / OPTIMIZED state          FULL
   ├── Scenario A                          DELTA → baseline
   ├── Scenario B                          DELTA → baseline
   └── Scenario C                          DELTA → baseline
```

Nothing here holds a network. States reference `snapshot_id`; the network stays
single-sourced in `SnapshotManager`.

A scenario stores only the facilities and lanes that **differ**, plus
`removed_lane_keys` — because "this lane no longer carries flow" is invisible in
a changed-entries list, where it looks identical to "this lane was not
mentioned". KPIs, risk and provenance are *not* compressed: they are small,
fixed-size, and the first thing a viewer reads.

Measured on a 100-facility network, a single closure:

| | Full | Delta | Ratio |
|---|---|---|---|
| One scenario | 47.0 KB | 3.1 KB | **6.5 %** |
| Ten scenarios | 479.9 KB if copied | 27.4 KB | **5.7 %** |

### Republishing a base

State ids are deterministic, so a later run legitimately republishes the same
baseline — a workflow that also ran REI attaches exposure the first one had no
way to know. A delta stored against the old content would then materialise
against the new content and describe **a network that never existed**.

`DigitalTwinStore.put()` therefore expands any dependent delta to FULL against
the base it was actually built from, before replacing it. Only the facility and
flow sets are compared: a republished baseline differing solely in its timestamp
changes nothing a dependent reads, and expanding on that would discard the
compression for no reason.

This was found by a test, not by inspection.

## 5. Absence is a value

An unavailable KPI is `None` with an `UnavailableValue` naming why — never
`0.0`. Zero means "measured, and it was zero", which for a cost or a fill rate
is a different and dangerous claim.

| Situation | What the twin shows |
|---|---|
| Facility not assessed for exposure | `rei=None`, `NOT_COMPUTED` — **never 0.0**, which on a [0,1] relative scale is the value of the *least exposed node in the network* |
| Disruption leaves the network infeasible | `rei=None`, `NOT_COMPUTABLE` |
| No event probability stated | `risk_factor=None`, `NOT_COMPUTABLE`, reasons listed |
| Solver proved infeasible | empty state, `calculation_status=INFEASIBLE`, `kpis=None` |
| A metric missing on one side of a comparison | `NOT_COMPARABLE`, naming the absent side |
| Baseline is zero | `abs_delta` reported, `pct_delta=None` — a percentage against zero is undefined, not infinite |

A workflow that runs REI but no solve — `EXTERNAL_EVENT`, `RESILIENCE_QUERY` —
publishes the risk context with **no facilities**, because there are no
decisions to represent. Fabricating an `is_open` for every node so the picture
looked complete would be the wrong trade.

## 6. Scale

Benchmarked, not extrapolated: `netgravity/tests/test_twin_scale_benchmark.py`.

```
[twin-scale] case16-small     facilities=  7  build=  0.77ms  payload=  5.1KB
[twin-scale] 25-facilities    facilities= 25  build=  0.83ms  payload= 13.1KB
[twin-scale] 50-facilities    facilities= 50  build=  1.50ms  payload= 24.7KB
[twin-scale] 100-facilities   facilities=100  build=  3.68ms  payload= 48.0KB
```

A cost-minimising optimum is **sparse** — 100 facilities produced only 61 lanes,
because using a lane costs money. That is a real property of solved networks,
and it means those runs never stress the flow path. Multi-period, multi-product
or multi-modal networks produce far denser sets, so the flow benchmarks build
the lane set directly and are labelled synthetic:

```
[twin-dense]   2000 lanes  build= 12.4ms  publish=0.10ms  page=0.06ms  summary=0.07ms
[twin-dense]  20000 lanes  build=117.1ms  publish=0.32ms  page=0.08ms  summary=0.03ms
[twin-dense]  50000 lanes  build=394.1ms  publish=0.51ms  page=0.07ms  summary=0.02ms
```

Retrieval is flat: **0.07 ms at 2,000 lanes and at 50,000**.

That flatness was not free. The first measurement showed the summary path
costing exactly as much as a full read, because `store.get()` deep-copied all
50,000 lanes before the view ever sliced them — publish 375 ms, read 398 ms. The
fix was to make immutability go all the way down: every model in the contract is
`frozen=True`, so no element can be edited in place, and the only remaining risk
is a caller mutating a list container it still holds. Replacing the containers
closes that, at a pointer copy per element instead of reconstructing every one.
Publish fell to 0.5 ms and reads to 0.07 ms.

Design points that carry the scale:

- **Stable ids.** `tws_{snapshot}_{type}` / `tws_{snapshot}_{scenario}`, so a
  client can construct the id it wants without a lookup, and a re-run overwrites
  its own state rather than accumulating near-duplicates.
- **Pagination.** `flow_limit` defaults to 500; `0` returns everything.
- **Aggregation.** `FlowAggregate` carries totals and per-origin/per-destination
  rollups, so a client can render structure without paging through every lane.
- **Lazy detail.** `include_flows=False` returns the aggregate alone.
- **Deltas.** Above.

## 7. Concurrency

- States are frozen and containers are replaced on ingest and egress, so a
  caller cannot reach into published state.
- Store writes are `RLock`-guarded; a 40-way concurrent write lands 40 states.
- Three closures solved simultaneously each report their own hand-calculable
  cost — 1,600 / 1,700 / 1,400 on the Delhi fixture. A crossover would show up
  as the wrong figure on a scenario, which is what makes the assertion worth
  making.
- Publishing scenarios concurrently leaves the baseline byte-identical.

## 8. Provenance

Every state answers "which network version and which solver run produced what I
am looking at?" without consulting the audit log:

```
snapshot_id · data_version · network_id
scenario_id · scenario_version · parent_snapshot_id · scenario_overrides
run_id · solver_status · optimality_label · execution_id
model_version · optimization_mode · is_hypothetical
generated_at · source
```

`source` is always `"orchestrator"`, present so a state whose source is anything
else is visibly wrong rather than quietly accepted.

Two validators keep the observed/hypothetical distinction honest, because it is
the one a viewer must never get wrong:

- `state_type` and `scenario_id` must agree — a scenario cannot hide under
  `OPTIMIZED`;
- only `BASELINE` may set `is_hypothetical=False`. An optimum is a proposal, not
  reality.

Publication emits `twin_state_published` per state, so a comparison run
producing three scenarios emits three.

## 9. Using it

```python
from netgravity.orchestrator import build_orchestrator

orch = build_orchestrator(network=network)
response = orch.run_sync(request)

# The response carries handles, not payloads — a state grows with the
# network, and a workflow response should not.
for ref in response.twin_states:
    print(ref["state_id"], ref["state_type"])

view = orch.twin.get(snapshot_id, scenario_id)      # materialised, paginated
view = orch.twin.get(snapshot_id, include_flows=False)   # summary only
full = orch.twin.materialize(state_id)              # whole state, one object
diff = orch.twin.compare_scenario(snapshot_id, scenario_id)
```

HTTP, on the existing blueprint:

```
GET /orchestrator/twin/states?snapshot_id=…
GET /orchestrator/twin/states/<state_id>?flow_offset=0&flow_limit=500
GET /orchestrator/twin/states/<state_id>?include_flows=false
GET /orchestrator/twin/snapshots/<snapshot_id>?scenario_id=…
GET /orchestrator/twin/compare?snapshot_id=…&scenario_id=…
GET /orchestrator/twin/compare?baseline=<id>&comparison=<id>
```

The service itself is framework-free and returns Pydantic models, so a
visualisation frontend attaches without the core moving.

## 10. Known limitations

1. **The frontend is not connected.** `app/frontend/js/twin3d.js` and the
   standalone HTML still render from their own data. Wiring them to these
   endpoints is the obvious next step and was outside this phase.
2. **Storage is in-memory.** Same as `SnapshotManager` and `ScenarioStore`,
   behind the same kind of narrow interface, so a database can replace the
   internals without a caller noticing. Nothing survives a restart.
3. **No cross-snapshot attribution.** Comparing states from different snapshots
   is permitted and flagged, but the twin cannot separate the network change
   from the decision change. Doing so needs a snapshot diff, which does not
   exist.
4. **Dense flow benchmarks are synthetic.** Real solved networks in this
   codebase are sparse; the 20k/50k lane figures measure the twin's flow
   handling and make no claim about what a particular solve produces.
5. **Delta compression is single-level.** A scenario compresses against a
   baseline, not against another scenario. Chained deltas would save more on
   families of near-identical scenarios and would cost a dependency graph to
   maintain; not worth it until such families exist.
