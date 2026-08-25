# Structural-break detection and adaptive forecasting

Phase 6.2. A deterministic change-point layer inside `netgravity/forecasting/`
that notices when a demand series has changed regime and, where the evidence
supports it, forecasts from the new regime instead of the whole history.

| | |
|---|---|
| **Detector** | [`netgravity/forecasting/change_point.py`](../netgravity/forecasting/change_point.py) |
| **Adaptive layer** | [`netgravity/forecasting/regime.py`](../netgravity/forecasting/regime.py) |
| **Tests** | [`netgravity/tests/integration/test_structural_break.py`](../netgravity/tests/integration/test_structural_break.py) — 74 tests |
| **Harness** | [`forecasting_validation/run_structural_break_validation.py`](../forecasting_validation/run_structural_break_validation.py) |
| **Default** | On. `ForecastRequest.detect_structural_break = True` |
| **New dependencies** | None. numpy only. |

---

## 1. The weakness this addresses

The brief described the problem as under-forecasting: history around 100, a
break, then demand around 200, with the forecast anchored too heavily to the old
regime. **That is not the failure this codebase has**, and the difference
matters enough to state plainly before anything else.

Under-forecasting is what a *smoother* does after a break. The engine that
actually runs on a smooth series with enough history is
`QuantileForecaster`, which fits autoregressive lags and then forecasts
recursively — appending its own median back onto the working history at each
step ([`quantile.py:196`](../netgravity/forecasting/engines/quantile.py#L196)).
A level jump inside the lag window is read as an explosive AR coefficient, and
the recursion compounds it.

Measured, 20 seeds per row, forecast horizon 12, against a naive-1 benchmark:

| History | New-regime periods | Pipeline MAE | Naive-1 MAE | Bias |
|---|---|---|---|---|
| 24 | 2 | **10,236.9** | 7.8 | +10,141 |
| 24 | 3 | 540.2 | 7.8 | +445 |
| 36 | 2 | 1,626.1 | 8.3 | +1,530 |
| 60 | 2 | 208.2 | 8.1 | +103 |
| 60 | 4 | 103.2 | 8.1 | +48 |

So the real weakness is not that the forecast lags the new regime. It is that
**a recent level shift makes the forecast diverge**, in the wrong direction,
without bound — an error 1,300× the naive benchmark, on a forecast that feeds
the MILP as demand. Both failures share a root cause (a model fitted across two
regimes describes neither), but only one of them is unbounded, and it is the one
that is here.

---

## 2. Detection methodology

A sup-F (Quandt–Andrews) scan for a level shift, tested against a null that
already contains the things most easily mistaken for one.

```
null          y_t = a + b·t + c·t² + d·sin(2πt/s) + e·cos(2πt/s) + ε_t
alternative   y_t = null + δ·1{t ≥ τ} + ε_t

F(τ)  = (RSS_null − RSS_alt(τ)) / (RSS_alt(τ) / (n − k))
supF  = max over admissible τ
```

Putting smooth trend and seasonality in the **null** is what makes the test
discriminating rather than merely sensitive. A trending series is explained by
`b`, an accelerating one by `c`, a seasonal one by `d, e` — none of them buys an
improvement by adding a step. What does is a discontinuity the smooth terms
cannot absorb, which is the definition being tested.

### Assumptions, stated

- **Additive, roughly constant-variance noise.** A series whose variability
  scales with its level will produce larger residuals in the higher regime; the
  pooled scale used by the gates absorbs some of that, but not all.
- **Constant seasonal amplitude, period 12** — matching `QuantileForecaster`.
  §9 documents what happens when this is violated, because it is violated in
  one of the test scenarios and the detector gets it wrong.
- **At most one break.** The scan returns the strongest single candidate. Two
  breaks in one history will report whichever explains more variance.
- **The threshold is calibrated, not exact.** Scanning τ makes the statistic a
  supremum, so its null distribution is not F(1, n−k). The threshold used is
  Andrews' (1993) asymptotic sup-Wald critical value for one tested parameter
  under 15% trimming (**8.85**), but that value is asymptotic and this
  implementation trims by a minimum segment length rather than a fixed
  fraction. It is therefore used as a calibrated threshold, and the **measured**
  false-positive rate (§6) is reported instead of a nominal size being claimed.

### Gates

Clearing the statistic is necessary, not sufficient. Four further conditions:

| Gate | Value | Question it asks |
|---|---|---|
| `MIN_SEGMENT` | 4 | Is there a *regime* on each side, or just an unusual observation? |
| `MIN_SIGMA_SHIFT` | 3.0 × residual SD | Is the step distinguishable from model noise? |
| `MIN_RELATIVE_SHIFT` | 15% of prior level | Is it material, or merely significant? |
| `MIN_SWING_SIGMA` | 3.5 × within-segment SD | Is it large next to how far this series routinely swings? |

Intermittent and lumpy series are refused outright (`PATTERN_NOT_APPLICABLE`): a
level-shift test on demand that is zero four periods in five measures the
arrival process, and Croston already adapts to the change that matters there.

### Why each gate exists — measured, not assumed

Each was added because something got through without it. False-positive rate on
40 realisations of the compound-growth benchmark pattern:

| Configuration | Compound growth | Linear growth | Noisy | True break |
|---|---|---|---|---|
| Linear null, gates on segment means | **77.5%** | 7.5% | 0% | 100% |
| **+ quadratic** in the null | 17.5% | 12.5% | 0% | 100% |
| **+ gate on δ** rather than segment means | 0% | 0% | 10% | 100% |
| **+ δ/residual-SD ≥ 3.0** (was 2.0) | 0% | 0% | 0% | 100% |
| **+ δ/swing-SD ≥ 3.5** | 0% | 0% | 0% | 100% |

Two of these deserve naming:

**The quadratic term.** A straight line cannot follow a curve, so on compound
growth the residual curvature was absorbed by a step, and demand rising 2.5% a
month was reported as a structural break on 77.5% of realisations.

**Gating on δ, not on segment means.** On a growing series the two segment means
differ mostly *because of the trend*, so a mean-difference gate leaks precisely
where the trend is steepest. δ — the fitted step coefficient — is the
discontinuity with the smooth part already removed. Segment means are still
reported, because `pre_break_level` and `post_break_level` are what a reader
wants to see; they are just not what the decision is made on.

### Determinism and safety

No LLM, no external call, no randomness, no future observation. `detect()` takes
a sequence of quantities and nothing else — there is no parameter through which
a horizon, a held-out set or an external signal could be passed, and a test
asserts the function signature. The verdict carries **no probability field**:
the sup-F null distribution under a scanned break date is non-standard, so a
p-value read off an F table would be wrong, and inventing one is the exact
failure the rest of this package exists to prevent. `sup_f` and `threshold` are
reported instead — both measurable, and a reader can see how far past the line
the evidence went.

---

## 3. Adaptive forecasting methodology

A detected break is **not on its own** a reason to throw history away. Both
candidates are scored and the winner is the one that measured better.

```
                     demand history
                            │
                   change-point detector
                            │
                    ┌───────┴───────┐
                   no              yes
                    │               │
            existing pipeline   compare, on post-break origins only:
            (bit-for-bit           FULL   = y[0 : origin]
             unchanged)            RECENT = y[change_index : origin]
                    │               │
                    └───────┬───────┘
                            │
                        forecast
```

Rolling-origin, paired, with every origin taken from **inside the post-break
regime**. A fold scored on a pre-break period measures how well each candidate
describes a regime the series has already left, and full history wins those by
construction. Only post-break folds measure the thing being decided.

Three design decisions here were wrong on the first attempt and were corrected
against measurement. All three are worth recording, because each looked right.

**1 — Folds are scored over the horizon actually requested, not one step
ahead.** Scoring one step, the recent candidate won by a wide and *correct*
margin on a break with eight new periods (MAE 3.5 vs 13.4) and the resulting
12-step forecast was still worse than the unadapted one on 37% of realisations,
occasionally by 3×. A damped-trend smoother on eight noisy observations reads
the noise as a slope; a slope is almost free at one step and ruinous at twelve.

**2 — Engines are pinned to what will actually ship.** Letting the selector
re-route inside each fold measured a configuration that never deploys: on a
12-period post-break window the folds trained a cold-start prior on three
observations and scored superbly, while deployment routed the same window to the
quantile engine, whose path then ran 167 → 390 against an actual near 200. The
decision was right about the *window* and wrong about what it had measured.

**3 — Engines are ranked, on both windows.** The pattern rule picks Holt's
damped trend for a window of 8–11 observations; on a nearly flat post-break
window it initialises its trend from a single first difference of noise and
extrapolates (208 → 233 against an actual near 200). Scored three steps ahead
against the other candidates, a level-only prior wins that window on 28 of 30
realisations — the measurement already knew, it just was not being asked. Both
windows are ranked the same way: giving the recent candidate best-of-N against
the full candidate's best-of-one would bias the comparison toward adapting,
which is the direction it must not be biased.

### When there is too little new regime to measure

Two conditions send the decision to a rule, labelled `basis=RULE`:

- fewer than 2 usable folds, or
- folds that cannot be scored to at least half the requested horizon
  (`MIN_EVAL_HORIZON_FRACTION`). A 2-step comparison is a poor proxy for a
  12-step forecast — the full-history model's lag-1 feature still tracks the new
  level at two steps and has drifted badly by twelve. Trusting that weak
  comparison beat the unadapted pipeline on 83% of runs against 97% for the
  rule.

The rule takes the recent regime, and the argument for it is asymmetry: using
too little new data costs precision, while keeping full history across a fresh
break costs correctness — mean absolute errors in the thousands, per §1. It is
recorded as a rule and not dressed up as evidence, so a reader can disagree.

The rule applies **only where its own premise holds**. Its justification is that
history describes "a level the series has left", which presumes the new regime
*is* a level. Where the post-break segment is still ramping
(`MAX_POST_BREAK_SLOPE_T`, slope |t| > 2.5) the premise fails and the decision
keeps full history instead — §9 has the measurements.

---

## 4. Before vs after: the specified critical case

Demand ~100 for 48 periods, stepping to ~200, with the future staying in the new
regime. The forecaster sees 60 periods; the last 12 are held out.

Error ratio (adaptive ÷ existing) across 30 seeds, by how many new-regime
periods are visible at forecast time. Below 1.0 means adaptation helped:

| New-regime periods | Median ratio | Mean | 90th pct | Worst | Won |
|---|---|---|---|---|---|
| 3 | **0.127** | 0.163 | 0.356 | 0.800 | 100% |
| 4 | **0.042** | 0.072 | 0.142 | 0.570 | 100% |
| 6 | **0.163** | 0.262 | 0.538 | 1.011 | 97% |
| 8 | **0.248** | 0.429 | 0.968 | 1.158 | 93% |
| 10 | **0.373** | 0.465 | 1.033 | 1.257 | 87% |
| 12 | **0.569** | 0.558 | 0.981 | 1.061 | 90% |
| 16 | **0.472** | 0.530 | 0.890 | 0.962 | 100% |
| 20 | **0.359** | 0.396 | 0.719 | 0.970 | 100% |
| 24 | **0.311** | 0.424 | 0.905 | 1.038 | 97% |

The worst case anywhere in that table is a 1.26× loss. The corresponding figure
before the three corrections in §3 was **46.8×**.

On the harness scenario (`SB_G_Critical_100_to_200`, 30 seeds):

| | MASE | MAE | Bias |
|---|---|---|---|
| Existing | 1.815 | — | over-forecasting |
| **Adaptive** | **0.626** | — | — |

Plot: `forecasting_validation/plots/break_critical_case.png`.

---

## 5. Structural-break accuracy

Six scenarios, 30 seeds each, PATTERN selection mode, 12-period held-out future.

| Scenario | MASE before | MASE after | Δ | Adapted |
|---|---|---|---|---|
| `SB_A_Level_Shift` (100→180) | 1.771 | **0.678** | −1.093 | 96.7% |
| `SB_C_Demand_Surge` (90→260) | 1.670 | **0.667** | −1.002 | 90.0% |
| `SB_D_Demand_Collapse` (220→70) | 1.999 | **0.610** | −1.388 | 100% |
| `SB_G_Critical` (100→200) | 1.815 | **0.626** | −1.189 | 93.3% |
| `I_Structural_Break` (existing) | 1.478 | **0.906** | −0.572 | 93.3% |
| `SB_E_Temporary_Spike` | 0.437 | 0.437 | 0.000 | 0% |
| `SB_F_Seasonal_Regime_Change` | 1.417 | 1.417 | 0.000 | 0% |
| `SB_B_Trend_Shift` | 3.458 | 4.202 → **3.597** | **+0.139** | 16.7% |

Aggregated over all structural-break scenarios:

| Mode | | MASE | MAE | WAPE | Beats naive |
|---|---|---|---|---|---|
| PATTERN | before | 1.795 | 18.63 | 13.3% | 44.8% |
| PATTERN | **after** | **1.148** | **11.87** | **7.8%** | **83.3%** |
| BACKTEST | before | 1.680 | 18.02 | 13.3% | 43.3% |
| BACKTEST | **after** | **1.001** | **10.83** | **7.6%** | **82.4%** |

Every genuine level-shift scenario now beats naive-1 (MASE < 1) where none of
them did before. `SB_B_Trend_Shift` is a residual regression and is treated
honestly in §9 rather than being excluded from the average.

---

## 6. False-positive testing

50 realisations per shape. Every firing on a non-break shape is a false positive
by construction.

| Shape | Fire rate | |
|---|---|---|
| `A_Stable` | **0.0%** | ordinary noise |
| `B_Linear_Growth` | **0.0%** | trend |
| `C_Linear_Decline` | **0.0%** | trend |
| `D_Strong_Growth` | **0.0%** | compound growth |
| `E_Seasonal` | **0.0%** | seasonality |
| `F_Seasonal_Trend` | **0.0%** | seasonality + trend |
| `G_Intermittent` | **0.0%** | refused, `PATTERN_NOT_APPLICABLE` |
| `H_Noisy` | **0.0%** | CV ≈ 0.45 |
| `J_Signal_Affected` | **0.0%** | |
| `SB_E_Temporary_Spike` | **0.0%** | 2-period spike that reverts |

**Zero false positives across all ten non-break shapes.**

Detection on shapes that do break:

| Shape | Detected | Located | True |
|---|---|---|---|
| `SB_A_Level_Shift` | 100% | t=49 | t=49 |
| `SB_C_Demand_Surge` | 100% | t=49 | t=49 |
| `SB_D_Demand_Collapse` | 100% | t=49 | t=49 |
| `SB_G_Critical_100_to_200` | 100% | t=49 | t=49 |
| `I_Structural_Break` | 100% | t=40 | t=40 |
| `SB_B_Trend_Shift` | 48% | t=54 | t=49 |
| `SB_F_Seasonal_Regime_Change` | **0%** | — | t=49 |

The last two are the honest failures, in §9.

### Detection power

Level shift at 36 of 48 periods, noise SD 8, 50 realisations:

| Shift size | 5% | 10% | 15% | 20% | 30% | 50% | 100% |
|---|---|---|---|---|---|---|---|
| Detected | 0% | 4% | 14% | 42% | 88% | **100%** | **100%** |

Located at the true period on every detection. The 15% row sits exactly on
`MIN_RELATIVE_SHIFT`, so ~14% there is the gate working as specified rather than
a power deficit. Shifts below ~20% of the level are deliberately not actioned.

### Discrimination

The four things the detector must tell apart, and does:

| | Result |
|---|---|
| Ordinary noise | not detected, 0/30 |
| Seasonality | not detected, 0/30 |
| Temporary spike (1 and 2 periods, 5 positions) | not detected, 0/15 each |
| Structural break | detected, 30/30, correct period |

---

## 7. Regression results — existing benchmark

The single most important table here. Ten patterns, 30 seeds, both selection
modes, detection off vs on:

| Pattern | PATTERN off → on | BACKTEST off → on | Adapted |
|---|---|---|---|
| `A_Stable` | 0.765 → 0.765 | 0.768 → 0.768 | 0.0% |
| `B_Linear_Growth` | 0.752 → 0.752 | 0.881 → 0.881 | 0.0% |
| `C_Linear_Decline` | 0.749 → 0.749 | 0.875 → 0.875 | 0.0% |
| `D_Strong_Growth` | 1.973 → 1.973 | 1.739 → 1.739 | 0.0% |
| `E_Seasonal` | 0.342 → 0.342 | 0.342 → 0.342 | 0.0% |
| `F_Seasonal_Trend` | 0.442 → 0.442 | 0.442 → 0.442 | 0.0% |
| `G_Intermittent` | 0.929 → 0.929 | 0.832 → 0.832 | 0.0% |
| `H_Noisy` | 0.762 → 0.762 | 0.756 → 0.756 | 0.0% |
| `J_Signal_Affected` | 1.915 → 1.915 | 1.915 → 1.915 | 0.0% |
| `I_Structural_Break` | 1.478 → **0.906** | 1.310 → **0.906** | 93.3% |

**Nine of the ten patterns are identical to three decimal places, with 0%
adaptation.** MAE, WAPE and bias are unchanged on all nine as well. This is not
a small measured difference — it is the same number, because a series with no
detected break takes the `FULL_HISTORY` branch and every line downstream sees
exactly the data it saw before this feature existed. A parametrised test asserts
forecast equality point-by-point across seven shapes and both selection modes.

The tenth pattern is the one that genuinely contains a structural break, and it
improves enough to cross the naive-1 line.

Overall, non-break patterns: MASE 1.011 → 0.954 (PATTERN), 0.986 → 0.946
(BACKTEST) — the whole of that movement is `I_Structural_Break`.

---

## 8. External-signal compatibility

Unchanged, and structurally unable to change.

- **The demand data alone detects the break.** `ChangePointDetector.detect()`
  takes a sequence of quantities. There is no signal parameter, and a test walks
  the AST to assert no function in the module accepts an argument whose name
  contains "signal".
- **No signal can bypass detection or force a value.** Signal enrichment runs
  *after* fitting, applying declared multipliers to the produced points, and
  `baseline_mean` survives alongside `mean` so the two remain separable.
- **`Extraction → Orchestrator → Forecasting` is untouched.**
  `orchestrator/routing/signal_router.py` is unmodified in this phase.
- **`confidence ≠ probability` holds.** `ChangePointResult` and `RegimeDecision`
  both carry no probability, p-value or confidence field, asserted by test. The
  RF pathway (`RF = P + REI − P·REI`) shares no vocabulary with either.

The one interaction worth naming: an external signal indicating that a regime
change *may* occur still flows through the existing enrichment methodology and
adjusts the forecast's central estimate. It cannot make the detector fire, and
the detector cannot consume it.

---

## 9. Known limitations

Stated at full strength, because two of them are failures.

**1 — Trend shifts are detected badly and are the one residual regression.**
The alternative model is a *step*. A series that stops being flat and starts
ramping has no step; the scan approximates the ramp with one placed mid-ramp
(t=54 for a true break at t=49) and fires on 48% of realisations. Adapting to
that mislocated break made things worse — MASE 3.458 → 4.520 initially. The
slope guard on the rule branch cut adaptation from 86.7% to 16.7% and the
regression to **+0.139**, but it has not eliminated it: a ramp buried in enough
noise is not statistically distinguishable from a level on seven observations,
so the guard fires on ramps it can resolve (~74% at noise SD 6, ~36% at SD 8),
not all of them. **A slope-change detector is the correct fix and is not
implemented.** Until it is, trend shifts are a scenario where this layer is
mildly harmful rather than helpful.

**2 — Seasonal amplitude changes are refused, not handled.** The null carries a
constant-amplitude harmonic, so a series whose swings grow from ±20 to ±70 with
its mean untouched leaves a residual that a step absorbs near a seasonal trough.
Unguarded this was the worst regression the feature produced: detected on 100%
of realisations, a flat forecast from five observations of one seasonal phase,
MASE 1.417 → **4.982**. `MIN_SWING_SIGMA` now refuses it (0% detection, MASE
back to 1.417 exactly), which is the right answer for a *level*-shift detector —
but "refused" is not "handled". A variance/amplitude regime change is a real
event this layer does not model.

**3 — The swing gate suppresses genuine level shifts in strongly seasonal
series.** Direct consequence of (2). A series swinging ±40 needs a step of ~140
to clear 3.5 pooled SD. Smaller real shifts in seasonal demand are missed. This
is a deliberate trade: measured across the suite, 3.5 was the smallest threshold
at which every non-break shape reached zero false positives while every genuine
level shift stayed at 100%.

**4 — A break with fewer than 4 new observations cannot be located correctly.**
`MIN_SEGMENT = 4` is what makes a spike distinguishable from a regime. With 2
new observations the detector fires on only 20% of realisations and places the
break too early. This is the correct trade — with 2 points there is no way to
tell a break from a spike — but it means the most recent breaks are the least
reliably located.

**5 — At most one break per series.** The scan returns the strongest single
candidate.

**6 — The threshold is calibrated, not exact.** See §2. The reported
false-positive rate is empirical, on these ten shapes, at these noise levels.

**7 — Intermittent and lumpy demand is not covered at all.** Refused by design.
A regime change in a spare-parts series is real and this layer will not see it.

**8 — The rule branch is a judgement, not evidence.** Where the post-break
window is too short to measure, the decision is made by rule. It is labelled
`basis=RULE`, both error fields stay `None` rather than 0, and the reason string
says "Not measured".

---

## 10. Performance

Detection is one OLS scan per candidate break date. Measured on this machine,
mixed workload (one series in three carrying a structural break), horizon 12:

| Series | Detection alone | Per series | Forecast off | Forecast on | Overhead | Peak memory on |
|---|---|---|---|---|---|---|
| 10 | 0.06 s | 5.6 ms | 0.63 s | 1.64 s | ×2.6 | 0.34 MB |
| 100 | 0.29 s | 2.9 ms | 5.36 s | 12.76 s | ×2.4 | 2.10 MB |
| 500 | 1.05 s | 2.1 ms | 19.3 s | 66.7 s | ×3.5 | 9.78 MB |
| 1,000 | 5.41 s | 5.4 ms | 60.4 s | 128.8 s | ×2.1 | 19.38 MB |

Detection itself is cheap — 2–6 ms per series, ~2% of total runtime, scaling
linearly. **The ×2–3.5 overhead is almost entirely the regime comparison**,
which re-fits several candidate engines at several origins, and which only runs
on series where a break was detected. On a workload with no breaks the overhead
is the detection scan alone.

Memory is flat and small: 19 MB peak at 1,000 series, ~18% above the
detection-off figure.

---

## 11. Recommendation for production evolution

In the order I would do them.

1. **Implement slope-change detection.** The only outstanding regression (§9.1).
   Add a `(t − τ)·1{t ≥ τ}` term for a 2-df joint test, classify the result as
   `LEVEL_SHIFT` or `TREND_SHIFT`, and give the adaptive layer a trend-capable
   response for the latter instead of a flat prior. Guard it carefully — a
   piecewise-linear alternative can fit a smooth curve, which is how the
   compound-growth false positive arose in the first place.

2. **Fix the divergence at source.** The regime layer works around
   `QuantileForecaster`'s explosive recursion rather than fixing it. A
   stationarity check on the fitted AR coefficients — refuse or damp the
   recursive path when the companion matrix's spectral radius is ≥ 1 — would
   make the engine safe on short windows independently of whether a break was
   detected. That is defence in depth for the failure in §1, and it belongs in
   the engine.

3. **Calibrate the threshold by simulation rather than by table.** Bootstrap the
   sup-F null on the actual series length and seasonal structure to replace the
   asymptotic Andrews value. This would let the detector state a genuine
   false-positive rate rather than an empirical one measured on synthetic
   shapes.

4. **Handle variance regime changes as their own event type.** §9.2. Detect them
   explicitly and respond by widening the prediction interval rather than by
   moving the central estimate — the honest response to "this series has become
   less predictable".

5. **Validate against real client history** before trusting any of the measured
   rates. Everything here is synthetic. The DGPs are reasonable and the
   train/test discipline is strict, but the false-positive rate on real demand
   with real outliers, promotions and calendar effects is unknown.

6. **Consider exposing the detector to the Digital Twin as a state annotation.**
   A market whose demand has changed regime is a fact an operator should see
   before they see a forecast built on it. This would go through the Orchestrator
   like everything else.

---

## 12. Deliverables

**Files added**

| File | |
|---|---|
| `netgravity/forecasting/change_point.py` | detector, ~460 lines |
| `netgravity/forecasting/regime.py` | adaptive layer, ~450 lines |
| `netgravity/tests/integration/test_structural_break.py` | 74 tests |
| `forecasting_validation/run_structural_break_validation.py` | harness |
| `docs/forecasting_structural_break.md` | this document |

**Files modified**

| File | Change |
|---|---|
| `netgravity/forecasting/schemas.py` | `detect_structural_break` on the request; `structural_break` and `regime` on `SeriesForecast`; `adapted_series` on provenance |
| `netgravity/forecasting/service.py` | detection before engine selection; deploys the measured engine on the adapted path only |
| `netgravity/forecasting/__init__.py` | exports |
| `netgravity/orchestrator/engines/deterministic.py` | projects break and regime into the control plane |

**Not touched**: MILP, REI, RF, governance, validation, ingestion,
conversation/NLU, Digital Twin, `orchestrator/routing/`, schemas, frontend. No
Git operations. No new dependencies.

**Tests**: **2,021 passed, 3 skipped, 0 failed** (up 74 from 1,947).

**Smoke test**: steps 1–5 pass, including the MILP core (Case-16 $150,627.70).
Step 6 fails on a pre-existing frontend defect — the standalone HTML bundle is
missing its `<script type="module">` tag in the committed `HEAD` as well as the
working tree, from concurrent frontend work. Unrelated to this phase, which
changed Python only.

**Plots** (`forecasting_validation/plots/`): `break_critical_case.png`,
`break_before_after_mase.png`, and one per scenario —
`break_SB_A_Level_Shift.png`, `break_SB_B_Trend_Shift.png`,
`break_SB_C_Demand_Surge.png`, `break_SB_D_Demand_Collapse.png`,
`break_SB_E_Temporary_Spike.png`, `break_SB_F_Seasonal_Regime_Change.png`,
`break_SB_G_Critical_100_to_200.png`.

**Data** (`forecasting_validation/structural_break/`):
`before_after_summary.json`, `false_positive_census.json`; benchmark at
`forecasting_validation/metrics/structural_break_benchmark.json`. The raw
per-seed dump (`before_after_raw.json`, ~47k lines) is deliberately NOT
tracked — it is the working data the summaries were computed from, and
`run_structural_break_validation.py` regenerates it in a few minutes.

---

## 13. Verdict against the acceptance criteria

| | Criterion | |
|---|---|---|
| 1 | Genuine break detected reliably | **Met** — 100% on all four level-shift scenarios, at the correct period |
| 2 | Noise does not trigger | **Met** — 0/50 on stable and noisy |
| 3 | Seasonality does not trigger | **Met** — 0/50 on seasonal and seasonal+trend |
| 4 | Existing path unchanged when no break | **Met** — nine patterns identical to 3 d.p., 0% adaptation |
| 5 | Detected break causes evidence-based adaptation | **Met** — measured where measurable, labelled `RULE` where not |
| 6 | Materially reduces error on break cases | **Met** — MASE 1.795 → 1.148 aggregate; 1.815 → 0.626 on the critical case |
| 7 | No material regression on the benchmark | **Met** for the ten-pattern benchmark; **one exception** — `SB_B_Trend_Shift`, +0.139, §9.1 |
| 8 | No future-data leakage | **Met** — asserted structurally and behaviourally |
| 9 | Signal routing unchanged | **Met** — `routing/` unmodified, asserted by test |
| 10 | Provenance records the adaptation | **Met** — per-series and result-level, projected to the control plane |
| 11 | All existing tests pass | **Met** — 2,021 passed, 0 failed |

**The ultimate test — does detecting the break produce a materially better
forecast of the new regime without damaging normal forecasting behaviour?**

Yes for level shifts, which is what the detector is built for: error on the
specified critical case falls to roughly a third, the worst case anywhere in the
recency sweep is a 1.26× loss against a pre-correction 46.8×, and the existing
benchmark is untouched to three decimal places.

No for trend shifts, where it remains mildly harmful (+0.139 MASE), and not
applicable for seasonal amplitude changes, which it now refuses rather than
mishandles. Both are named in §9 with the cause and the fix.
