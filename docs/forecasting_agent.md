# Forecasting Agent

> **The agent estimates. It never decides.**
> It is never handed a network, a config or a solver, so it cannot optimise,
> rank or score anything — regardless of intent.

---

## 1. Where it sits

```
Client data / shipment history
          ↓
Extraction / Parsing Agent  →  STAGING zone
          ↓
      Orchestrator
          ↓
ForecastRequest → ForecastingService → ForecastResult
                        │
                        └── change-point detection → regime selection
                            (which slice of history to fit on)
          ↓
      Orchestrator            ← validates, then builds the MILP input
          ↓
   forecast_bridge  →  CanonicalNetwork (as a SCENARIO)
          ↓
         MILP  →  Reasoning → Governance → Digital Twin
```

`ForecastRequest` has no field for a `CanonicalNetwork`, an `OptimizationConfig`
or a solver. Turning an estimate into a network happens in
`orchestrator/engines/forecast_bridge.py`, on the far side of the boundary.
Tests assert the import graph rather than trusting this paragraph.

Before an engine is chosen, each series is scanned for a structural break and,
where the evidence supports it, forecast from the new regime rather than the
whole history. It is on by default because it is a correctness guard rather
than a feature — without it a recent level shift makes the recursive quantile
forecast diverge. Method, measurements and limitations:
[`forecasting_structural_break.md`](forecasting_structural_break.md).

## 2. External signals: who decides what

```
External sources
      ↓
Extraction / Parsing Agent          structures signals · decides nothing
      ↓
structured MarketIntelligenceSignal
      ↓
ORCHESTRATOR                        ← the routing decision, made once
      ├──→ ExternalSignalRouter ──→ ForecastRequest.signals
      │         (may this signal inform a forecast?)
      │                    ↓
      │            Forecasting Agent  (HOW it applies)
      │                    ↓
      │            Forecast → ORCHESTRATOR → MILP
      │
      └──→ RF pathway, only where a genuine event_probability exists
                ExternalSignal.event_probability + REI → RF = P + REI − P·REI
```

**Three layers, three responsibilities:**

| Layer | Decides |
|---|---|
| **Extraction** | *What this signal is* — bucket, direction, entities, guardrail verdict, a policy-derived `scenario_use`. Nothing about forecasting. |
| **Orchestrator** (`routing/signal_router.py`) | *Whether it may inform a forecast* — guardrail passed, use permitted, confidence sufficient, entity inside the pinned network, and not a risk signal. |
| **Forecasting** (`signals/enrichment.py`) | *How it moves the numbers* — bucket mechanism, direction, magnitude, bounds. |

`scenario_use` is read by the orchestrator as **evidence from extraction, not an
instruction**. Extraction saying `FORECAST_ENRICHMENT` means "this is the kind of
signal that can enrich a forecast"; the orchestrator decides whether it does —
and routinely refuses one for reasons extraction could not know, such as the
pinned network not containing the entity.

The Forecasting Agent cannot reach a signal source: `netgravity/forecasting/`
imports no ingestion module, no HTTP client and no model client, and performs no
file I/O outside `history.py`. Whatever the orchestrator does not hand it cannot
influence anything.

### The two pathways share no vocabulary

Phase 4A split the two signal types out of a name collision; this phase consumes
the market-intelligence one and never touches the other. No model in
`netgravity/forecasting/` has a field whose name contains `probab` or
`likelihood`. An `ExternalSignal` offered as a forecasting input is **refused at
the router** (`REFUSED_RISK_SIGNAL`), and refused again by the enricher if a
caller bypasses routing.

**Confidence is a gate, never a number.** It decides *whether* to route, never
by how much. HIGH and MEDIUM produce *exactly the same* multiplier — tested. The
router itself computes nothing: a test asserts its source contains no float
conversion, no numeric literal and no multiplier.

## 3. What was integrated, and what was not

| Component | Decision | Reason |
|---|---|---|
| `characterizer.py` | **REUSE** | Correct Syntetos-Boylan. Published thresholds, correct ADI/CV², `ddof=1`. |
| `engines/intermittent.py` | **REUSE** | Correct Croston/SBA including the (1 − α/2) bias deflation. |
| `engines/ets_smoother.py` | **REUSE** | Holt linear trend, damped, grid-searched α/β. |
| `engines/quantile_regressor.py` | **ADAPT** | Formulation kept. Lag-depth bug fixed; renamed off "LightGBM", which it never imported. |
| `engines/auto_selector.py` | **ADAPT** | Routing kept as the `PATTERN` mode; a measured `BACKTEST` mode added beside it. |
| `engines/foundation_adapter.py` | **REPLACE** | Reported `engine_name="Google_TimesFM_ZeroShot"` whenever `timesfm` was merely *importable*, while always running its own weighted mean. Neither library was ever called. |
| `schemas.py` | **ADAPT** | Time-series models kept. Result rebuilt around status + provenance. |
| `signals/fuser.py` | **REPLACE** | An LLM chose the demand multiplier; the offline path regex-matched raw text. See below. |
| `bridge/milp_bridge.py` | **REPLACE** | Two defects, both reproduced as tests. See §5. |
| `agent.py` | **ADAPT** | Kept as `ForecastingService`; the network-mutating methods removed. |
| pandas dependency | **EXCLUDE** | Used only for a DataFrame convenience path. Not a NetGravity dependency; dropped rather than added. |

### Why the signal fuser was replaced

It did two things:

1. **It asked an LLM for the numbers.** The prompt requested a
   `demand_multiplier` between 0.5 and 2.0, and every forecast point was then
   multiplied by whatever came back. A model answering "1.7" made demand 70%
   higher, with no history, no fitting and no validation behind it.
2. **Its offline fallback regex-matched prose** — `(diwali|festival)` → ×1.25 —
   and compounded matches multiplicatively.

The *shape* of the idea survives: a bounded multiplicative adjustment, attributed
to a named rule. What changed is the input (a structured, guardrailed,
entity-scoped signal rather than free text), the source of the numbers (a
declared table rather than a model), and the honesty of the label — every
adjustment carries `is_assumption=True`.

**The coefficients are declared assumptions, not estimated effects.** No history
with labelled events was available to fit them against. That is a real gap, not
a rounding of one.

Only demand-side buckets may move the central estimate:

| Bucket | Effect on demand |
|---|---|
| `CUSTOMER` UP / DOWN | ×1.10 / ×0.90, σ ×1.15 |
| `MACRO` UP / DOWN | ×1.05 / ×0.95, σ ×1.20 |
| `WEATHER` | **mean untouched**, σ ×1.30 — widen, don't shift |
| `CARRIER`, `SUPPLIER` | **no rule** — they act on lead time and rate, not on how much a market buys |
| `COMPETITOR`, `UNKNOWN` | no rule |

A cyclone warning justifies "the next few periods are less predictable" far more
readily than "demand will be 15% lower". Widening is what actually reaches the
MILP anyway, through `DemandRecord.std_dev` and safety stock.

## 4. Defects found by measurement

### The quantile engine was never fitting

`min(max_lags, n // 3)` is not the binding constraint on lag depth. A feature row
is `lags + 5` wide and lagging costs `lags` rows, so the design matrix is only
determined when `n ≥ 2·lags + 5`. At twelve periods with four lags: **eight rows
against nine features**. The LP fell into its intercept-only fallback, all three
quantiles collapsed to the same constant, and the engine returned the series mean
with a **zero-width prediction interval** and a hardcoded `confidence_score=0.92`.

The selector routes every SMOOTH series with ≥12 observations to this engine — so
twelve periods, the exact threshold, hit the degenerate path by default.

Fixing the lag depth to the largest determined value:

| series | MASE before | MASE after |
|---|---|---|
| linear trend | 2.89 | **0.86** |
| strong growth | 1.89 | **0.05** |
| noisy | — (band width 0) | band width 47.3 |

A collapsed fit is now reported through `diagnostics["degenerate_fit"]` and
surfaced as a warning, so a zero-width interval never again reads as certainty.

### The routing rule is a prior, and often wrong

Backtested across five shapes (MASE; below 1 beats naive-1):

| series | Quantile | Holt-ETS | ColdStart | `PATTERN` picks | `BACKTEST` picks |
|---|---|---|---|---|---|
| linear trend | 0.86 | **0.64** | 4.26 | 0.86 | Holt **0.64** |
| flat + noise | 4.03 | 1.35 | **0.60** | 4.03 | ColdStart **0.60** |
| seasonal | **0.92** | 1.11 | 1.06 | 0.92 | Quantile 0.92 |
| strong growth | **0.05** | 0.47 | 3.03 | 0.05 | Quantile 0.05 |
| noisy | 2.79 | 0.86 | **0.52** | 2.79 | ColdStart **0.52** |

`SelectionMode.BACKTEST` measures every eligible engine on the series' own
history and takes the lowest MASE. It is **never worse than `PATTERN`** across
these shapes and up to 6.7× better. `PATTERN` remains the default so integration
changes no behaviour by surprise; `BACKTEST` costs one refit per engine per fold.

### `confidence_score` was fabricated

The source hardcoded `0.92` on the quantile engine, `0.85` on Croston, `0.75` on
the cold-start prior. Those numbers were never measured — and the signal fuser
multiplied them together (`confidence_score * modifier.confidence`), compounding
two invented quantities into a third.

They are gone. `SeriesForecast.accuracy` is `None` unless a rolling-origin
backtest actually ran, and `AccuracyMetrics` names the fold count. WAPE is
preferred over MAPE throughout because MAPE divides by the actual, and
intermittent demand is mostly zeros.

## 5. Two defects the bridge exists to prevent

Both were live in `bridge/milp_bridge.py`, both reproduced against this codebase.

**Unforecast demand vanished.** `update_network_with_forecast` rebuilt the demand
list from the forecasts alone, so any market-product the forecaster had not
covered was *deleted from the network* — not zeroed, removed. Measured on a
two-market network, forecasting one market dropped the other's 250 units
entirely. The MILP then optimised a smaller problem and reported a lower cost,
with nothing indicating demand had gone missing.

**The forecast network impersonated the observed one.**
`network.model_copy(update={"demands": ...})` leaves `data_version` untouched.
Since `SnapshotManager` keys on `snap_` + `data_version[:12]` and returns the
existing record on a hit:

```
observed snapshot: snap_72a48e1a0664
forecast snapshot: snap_72a48e1a0664      ← same id
stored demands:    [100.0, 100.0, 100.0]  ← observed, not the forecast
```

The forecast was silently discarded and the store returned observed state.

Every network the bridge builds recomputes its data version, and every demand
record survives with an explicit `DemandProvenance` of `OBSERVED` or `FORECAST`.
`UnforecastPolicy.REJECT` is the default: partial coverage refuses to build a
network at all, because silently mixing forecast and observed demand produces an
optimum nobody can attribute to either. `KEEP_OBSERVED` permits the mix and names
every substituted record.

## 6. Forecast-driven optimisation is a scenario

`Orchestrator.build_forecast_scenario()` registers the forecast network in
`ScenarioStore` rather than as a snapshot. A forecast is hypothetical by
definition — it describes a period that has not happened — and `ScenarioStore`
already guarantees exactly what such a network needs: tagged `is_hypothetical`,
parented to its snapshot, isolated from siblings, with no API that writes it back
over observed state.

Reusing it means forecast-driven optimisation inherits scenario isolation,
Digital Twin representation and governance treatment without any of them being
taught what a forecast is.

## 7. Failure semantics

Nothing becomes a number.

| Situation | Result |
|---|---|
| Fewer than 2 observations | `INSUFFICIENT_HISTORY`, `points=[]` |
| Engine raised | `MODEL_FAILURE`, `points=[]` |
| Horizon > 12 | `UNSUPPORTED_HORIZON`, whole request refused |
| Unknown engine pin | `INVALID_INPUT` — a pin that did not take is reported |
| Backtest impossible | `accuracy=None` — unmeasured, not good |
| Forecast from another snapshot | bridge refuses; no scenario is created |
| No history at all | capability reports it; the run degrades, it does not fabricate |

A `SeriesForecast` validator makes this structural: a non-OK status carrying
points raises at construction. Every requested series appears in the result
exactly once, so a caller iterating the output cannot silently lose an entity.

An all-zero series forecasting zero is a *different* thing — that is a measured
statement about a series that has been zero throughout, and it is correct.

## 8. Known limitations

1. **Only demand.** `ForecastTarget` has one member. Lead time, capacity and
   utilisation have no engine, and are absent rather than declared.
2. **Signal coefficients are unvalidated assumptions.** They should be replaced
   by estimated effects once history with labelled events exists. Until then
   `is_assumption=True` on every adjustment is the honest label.
3. **Signals must name their entities.** An unscoped signal is refused by the
   router (`OUT_OF_SCOPE`) rather than applied network-wide. Defensible, but it
   makes the feature unusable for signals that arrive without entity
   resolution. `ExternalSignalRouter(require_entity_scope=False)` relaxes it
   deliberately; it is not the default.
4. **Multi-step forecasts are recursive**, feeding the median back as the next
   lag, so intervals widen more slowly than true uncertainty. Warned beyond 6
   periods, refused beyond 12.
5. **No calendar handling.** `period` is a sequential index. Staging rows whose
   period is a date string are skipped and counted, because mapping dates onto
   planning periods needs a calendar this layer does not own.
6. **No model persistence or lifecycle.** Every forecast refits from scratch.
   Fine at the scale measured (~2 ms/series); no model registry and no
   retraining schedule. Structural-break detection covers *abrupt* level
   changes only — gradual drift is not detected, and trend shifts are detected
   badly (see `forecasting_structural_break.md` §9).
7. **The chat surface still refuses forecasts.** `chat_service._forecast_response`
   says "NetGravity has no forecasting capability registered", which is now
   false. Left untouched because this phase was instructed not to modify the
   chatbot; it is a one-message fix and should be made.
8. **Backtesting is one-step-ahead.** `validation.backtest` and
   `select_by_backtest` score period-1 forecasts only, so the MASE they report
   describes one-step accuracy. The regime comparison in `regime.py` scores
   multi-step, having measured that a one-step ranking is a poor proxy for a
   twelve-step forecast; the engine selector has not been changed to match, and
   probably should be.
