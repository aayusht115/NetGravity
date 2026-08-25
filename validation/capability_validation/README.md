# Phase 8.0 — Individual Capability Validation

Drives every major NetGravity capability at its own entry point, against one
controlled synthetic dataset, using the real implementations. **No orchestration
is under test here** — the point is to establish that each capability works on
its own before agents are made to collaborate.

```
python validation/capability_validation/run_validation.py
```

Nothing in this directory modifies an implementation. Where a capability behaves
unexpectedly the harness records what happened and carries on; a validation run
that edits the thing it measures is worthless.

## Layout

| Path | |
|---|---|
| `run_validation.py` | the harness — one function per capability, each returning a verdict |
| `synthetic.py` | the single dataset: 15 facilities, 41 lanes, 2 products, 7 demand histories |
| `budget.py` | hard gate on live model calls (max 20 for the whole run) |
| `report.md` | the findings |
| `metrics/*.json` | per-capability checks and evidence, plus `summary.json` |
| `plots/*.png` | forecast, MILP and signal-enrichment figures |
| `synthetic_data/` | the generated CSVs and `dataset_manifest.json` |
| `traces/` | API call ledger, provenance chain, live-LLM evidence |

## The dataset

Three plants, five DCs, seven markets, two products, 41 lanes. Feasible by
design with ~1.8× DC capacity headroom, because REI is defined against a
feasible baseline and a network that only just solves would make every
disruption infeasible for reasons unrelated to the code.

Every market carries 36 observed monthly periods and **6 held-out periods** that
no forecast ever sees. The split happens once, in `build_demand_history`, so no
section can leak it by accident.

Demand patterns, one per market: stable+seasonal, growth, seasonal,
intermittent, structural break, decline, noisy.

`build_fragile_network()` is the same network with a 2-day premium SLA, which
leaves two markets single-sourced — used to exercise the infeasible REI branch
honestly.

## Live model calls

The gateway (`gpt-5-mini`, prompt-only) has a **shared, cumulative** budget and
a **shared daily request quota**. `budget.LLMBudget` is the only thing allowed
to call it: it refuses past 20, spaces calls to respect the rolling-minute
window, records every attempt including refusals, and never retries a timeout
(the call may have completed, and a retry would spend shared budget twice).

Credentials come from `TEXT_API_TOKEN` / `NETGRAVITY_GATEWAY_TOKEN` in a
gitignored `.env`. They are never logged, never placed in a prompt or URL, and
never written into these artifacts.

Every section except the two live ones runs in **stub mode** (no key passed), so
re-running the deterministic bulk of this harness costs nothing.

## Reading a verdict

`PASS` · `PARTIAL` · `FAIL` · `NOT_TESTED`, derived from the section's checks —
except where a section calls `downgrade()`, which forces a lower verdict and
records why. That exists so a section whose checks all pass but which uncovered
something a reader must not read as clean cannot report itself as clean.

A `NOT_TESTED` verdict on a live section means the call never happened, and the
ledger says why. An external quota refusal is recorded as `EXTERNAL_LIMIT` and
**not charged** against the run's budget — the gateway's own guide says such a
request does not count against its counters either.
