"""
NetGravity — Forecasting contracts.

The typed boundary between the Orchestrator and the Forecasting Agent.

    ForecastRequest  →  ForecastingService  →  ForecastResult

Four rules shape every model here, and each exists because of something the
source implementation got wrong.

**1. A forecast is an estimate, never a decision.**
`ForecastResult` has no field able to carry an REI, an RF, a governance verdict
or an optimisation objective — the same structural device `ExtractionResult` and
`DigitalTwinState` use, for the same reason. A test asserts the absence.

**2. Absence is never zero.**
A series with too little history gets `status=INSUFFICIENT_HISTORY` and
`points=[]`, not a forecast of 0. Zero demand is a specific, actionable claim;
"we could not forecast this" is a different fact, and the MILP must be able to
tell them apart. The source bridge silently *dropped* unforecastable markets
from the network entirely, which is worse than either.

**3. Quality is measured or absent.**
The source hardcoded `confidence_score=0.92` on one engine and `0.85` on
another. Those numbers were never measured — they read as accuracy and were
literals. Here `accuracy` is `None` unless a backtest actually ran, and the
`AccuracyMetrics` that fills it names the number of folds behind it.

**4. Signal effects are visible, not baked in.**
`baseline_mean` survives alongside `mean` whenever a signal moved a forecast, so
"what did the model say" and "what did the signal do to it" are always separable.
The source multiplied the two together and kept only the product.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ForecastTarget(str, Enum):
    """
    What can be forecast.

    Exactly one member, deliberately. The integrated engines forecast demand
    quantity per market-product and nothing else — lead time, capacity and
    utilisation have no implementation, so they are not listed. Adding a name
    here without an engine behind it would be the "schema mentions a field,
    therefore it is supported" mistake.
    """
    DEMAND = "DEMAND"


class Frequency(str, Enum):
    """
    Observation frequency of a series.

    Carried for provenance and horizon interpretation. The engines are
    frequency-agnostic — they operate on an ordered sequence of periods — so
    this records what a period MEANS rather than changing the arithmetic. The
    one place it bites is seasonality, which the quantile engine assumes has a
    12-period cycle; see `SeriesForecast.warnings`.
    """
    DAY     = "DAY"
    WEEK    = "WEEK"
    MONTH   = "MONTH"
    QUARTER = "QUARTER"


class DemandPattern(str, Enum):
    """
    Syntetos-Boylan demand classification, plus a cold-start case.

    The classification decides which engine runs, so it is part of the result's
    provenance rather than an internal detail.
    """
    SMOOTH       = "SMOOTH"        # low ADI, low CV²  — regular, predictable
    INTERMITTENT = "INTERMITTENT"  # high ADI, low CV² — sporadic, steady size
    ERRATIC      = "ERRATIC"       # low ADI, high CV² — regular, volatile size
    LUMPY        = "LUMPY"         # high ADI, high CV² — sporadic and volatile
    COLD_START   = "COLD_START"    # too short to classify


class ForecastStatus(str, Enum):
    """
    Outcome for one series, or for a whole request.

    Every non-OK member means `points` is empty. There is no member that means
    "we produced something, but be careful with it" — a forecast the system
    cannot stand behind is not returned as a number with a caveat, because the
    caveat does not survive being read into a MILP.
    """
    OK                    = "OK"
    INSUFFICIENT_HISTORY  = "INSUFFICIENT_HISTORY"   # fewer observations than the floor
    UNSUPPORTED_TARGET    = "UNSUPPORTED_TARGET"     # no engine forecasts this quantity
    UNSUPPORTED_HORIZON   = "UNSUPPORTED_HORIZON"    # horizon outside the supported range
    INVALID_INPUT         = "INVALID_INPUT"          # malformed history
    MODEL_FAILURE         = "MODEL_FAILURE"          # the engine raised
    TIMEOUT               = "TIMEOUT"                # exceeded the time budget
    STALE_DATA            = "STALE_DATA"             # history predates the snapshot
    NOT_COMPUTABLE        = "NOT_COMPUTABLE"         # none of the above, and no forecast


#: Statuses that mean a usable forecast exists.
_SUCCESSFUL = {ForecastStatus.OK}


class SelectionMode(str, Enum):
    """
    How the engine for a series is chosen.

    `PATTERN` is the rule inherited from the source repository: classify the
    demand, route on the class. Fast, and a reasonable prior about which model
    *suits* a shape.

    `BACKTEST` measures every eligible engine on the series' own history and
    takes the lowest MASE. It costs one refit per engine per fold, and on the
    evidence it is usually right where the prior is not — see
    `EngineSelector.select_by_backtest` for the measurements.

    PATTERN remains the default so integrating this package changes no existing
    behaviour by surprise. Callers with enough history and a reason to care
    should ask for BACKTEST.
    """
    PATTERN  = "PATTERN"
    BACKTEST = "BACKTEST"


class SignalEffect(str, Enum):
    """Which way a signal moved a forecast."""
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"
    #: Uncertainty widened, central estimate untouched. The most defensible
    #: response to most signals — see `signals/enrichment.py`.
    WIDEN    = "WIDEN"
    NONE     = "NONE"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

class DemandPoint(BaseModel):
    """One historical observation."""
    period: int = Field(..., description="Sequential period index; ordering is what matters")
    quantity: float = Field(..., ge=0.0)
    timestamp: Optional[str] = Field(None, description="ISO date, when the source carried one")

    model_config = ConfigDict(extra="forbid", frozen=True)


class DemandTimeSeries(BaseModel):
    """
    Observed history for one market-product pair.

    OBSERVED data, always. Nothing in this model can hold a forecast, which is
    what keeps the observed/forecast separation structural rather than a
    convention people remember.
    """
    market_id: str
    product_id: str
    history: List[DemandPoint] = Field(default_factory=list)
    frequency: Frequency = Frequency.MONTH
    #: Carried through onto the forecast's `DemandRecord` so service terms are
    #: not lost when observed demand is replaced.
    sla_days: Optional[float] = None
    service_level: float = Field(0.95, ge=0.0, le=1.0)

    model_config = ConfigDict(extra="forbid")

    @property
    def key(self) -> str:
        return f"{self.market_id}/{self.product_id}"

    @property
    def quantities(self) -> List[float]:
        """Quantities in period order."""
        return [p.quantity for p in sorted(self.history, key=lambda x: x.period)]

    @property
    def total_periods(self) -> int:
        return len(self.history)


class CharacterizationMetrics(BaseModel):
    """
    The ADI / CV² measurements behind a pattern classification.

    Retained on the result so the engine choice is auditable: a reader can see
    that ADI 3.1 and CV² 0.8 is why Croston ran rather than a trend model.
    """
    adi: float = Field(..., description="Average Demand Interval — periods per non-zero period")
    cv2: float = Field(..., description="Squared coefficient of variation of non-zero demand")
    non_zero_periods: int
    total_periods: int
    zero_ratio: float
    pattern: DemandPattern

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class SignalAdjustment(BaseModel):
    """
    A recorded adjustment made to a forecast because of an external signal.

    **This model has no probability field, and must never gain one.** It
    describes an assumption applied to an estimate. The likelihood of a discrete
    hazard is `ExternalSignal.event_probability`, lives in the orchestrator, and
    feeds `RF = P + REI − P·REI`. Nothing converts between them.

    The coefficients recorded here are **manually declared assumptions, not
    learned effects** — see `signals/enrichment.py`. `basis` says which rule
    fired so the assumption can be argued with rather than merely observed.
    """
    signal_id: str
    bucket: str
    direction: str
    effect: SignalEffect
    #: Multiplier applied to the central estimate. 1.0 means untouched.
    mean_multiplier: float = Field(1.0, gt=0.0)
    #: Multiplier applied to the standard deviation. 1.0 means untouched.
    std_multiplier: float = Field(1.0, gt=0.0)
    #: The rule that produced those numbers, named so it is contestable.
    rule_id: str = ""
    basis: str = ""
    #: Qualitative, straight from the signal. Used as a GATE, never as a number.
    signal_confidence: str = ""
    #: Always true for anything this class can express. Present so a future
    #: estimated effect is distinguishable from a declared one.
    is_assumption: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

class ForecastPoint(BaseModel):
    """
    One forecast period.

    Carries both the central estimate and the quantile band the engine
    produced. `baseline_mean` is populated only when a signal moved `mean`, so
    the model's own answer stays recoverable.
    """
    period: int = Field(..., description="1-based offset from the start of the horizon")
    mean: float = Field(..., ge=0.0)
    std_dev: float = Field(0.0, ge=0.0)
    p10: float = Field(..., ge=0.0)
    p50: float = Field(..., ge=0.0)
    p90: float = Field(..., ge=0.0)

    #: The engine's central estimate before signal adjustment. None when no
    #: signal was applied, so `mean` is the model's own number.
    baseline_mean: Optional[float] = Field(None, ge=0.0)
    baseline_std_dev: Optional[float] = Field(None, ge=0.0)

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def _quantiles_are_ordered(self) -> "ForecastPoint":
        """
        p10 ≤ p50 ≤ p90.

        Enforced because a crossed band is not a wide forecast, it is a broken
        one — and downstream a p90 below a p10 would silently invert a
        conservative scenario into an aggressive one.
        """
        if not (self.p10 <= self.p50 <= self.p90):
            raise ValueError(
                f"Forecast quantiles must be ordered p10 ≤ p50 ≤ p90, got "
                f"{self.p10} / {self.p50} / {self.p90}."
            )
        return self

    @property
    def was_signal_adjusted(self) -> bool:
        return self.baseline_mean is not None


class AccuracyMetrics(BaseModel):
    """
    Measured out-of-sample error from a rolling-origin backtest.

    Present only when a backtest actually ran. Every field is an error measure
    — there is deliberately no "score" or "confidence" here, because the source
    implementation's hardcoded `confidence_score=0.92` is exactly the kind of
    unearned quality signal this model exists to replace.
    """
    #: Mean absolute error, in demand units.
    mae: float
    #: Root mean squared error, in demand units.
    rmse: float
    #: Weighted absolute percentage error, as a fraction. Preferred over MAPE
    #: because MAPE is undefined on the zero periods that intermittent demand is
    #: mostly made of.
    wape: Optional[float] = None
    #: Mean absolute scaled error against a naive-1 benchmark. < 1 means the
    #: model beat naive. None when the benchmark itself is degenerate.
    mase: Optional[float] = None
    #: Folds behind the numbers. One fold is an anecdote.
    n_folds: int = 0
    n_observations: int = 0
    #: Backtest configuration, so the measurement is reproducible.
    method: str = "ROLLING_ORIGIN"

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def beat_naive(self) -> Optional[bool]:
        """True when MASE < 1. None when no benchmark was computable."""
        return None if self.mase is None else self.mase < 1.0


class SeriesForecast(BaseModel):
    """
    The forecast for one market-product pair, or the explicit reason there
    isn't one.

    `status` is the field to read first. Anything other than `OK` means `points`
    is empty, and the caller must decide what to do — it must not read a
    quantity out of an empty list and default it.
    """
    market_id: str
    product_id: str
    target: ForecastTarget = ForecastTarget.DEMAND
    status: ForecastStatus

    points: List[ForecastPoint] = Field(default_factory=list)

    #: Engine that produced the forecast. Names the algorithm that actually
    #: ran — never a library that merely happens to be installed.
    engine: str = ""
    engine_version: str = ""
    pattern: Optional[DemandPattern] = None
    metrics: Optional[CharacterizationMetrics] = None

    #: Measured out-of-sample error. None means no backtest ran, which is a
    #: different statement from "the error was small".
    accuracy: Optional[AccuracyMetrics] = None

    #: Signals applied, in order. Empty when the forecast is history-only.
    signal_adjustments: List[SignalAdjustment] = Field(default_factory=list)

    n_history_periods: int = 0
    frequency: Frequency = Frequency.MONTH

    #: Engine name → measured MASE, when selection was by backtest. Retained so
    #: the choice is auditable: a reader can see the engine that ran was the
    #: best of those measured, and by how much.
    selection_scores: Dict[str, Optional[float]] = Field(default_factory=dict)

    #: The structural-break verdict for this series, when detection ran.
    #: Populated whether or not a break was found — "we looked and there was
    #: none" is a result worth recording, and it is what makes the unchanged
    #: forecasts on stable series auditable rather than merely unchanged.
    #: Typed as Any to keep `schemas` free of a dependency on `change_point`,
    #: which imports the characterizer, which imports this module.
    structural_break: Optional[Any] = None

    #: Which slice of history the forecast was actually fitted on, and why.
    #: A `RegimeDecision`. None when detection did not run.
    regime: Optional[Any] = None

    #: Why the status is what it is, when it is not OK.
    reason: str = ""
    warnings: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _failure_carries_no_numbers(self) -> "SeriesForecast":
        """
        A non-OK series must be empty.

        The single most important invariant in this module. Allowing a failed
        forecast to carry points would let "the model crashed" reach the MILP as
        a quantity, which is the failure mode every status enum in this codebase
        exists to prevent.
        """
        if self.status not in _SUCCESSFUL and self.points:
            raise ValueError(
                f"SeriesForecast for {self.market_id}/{self.product_id} has status "
                f"{self.status.value} but carries {len(self.points)} forecast points. "
                f"A forecast that did not succeed must not carry numbers."
            )
        if self.status in _SUCCESSFUL and not self.points:
            raise ValueError(
                f"SeriesForecast for {self.market_id}/{self.product_id} is OK but has "
                f"no points. Success with nothing in it is not a forecast."
            )
        return self

    @property
    def key(self) -> str:
        return f"{self.market_id}/{self.product_id}"

    @property
    def ok(self) -> bool:
        return self.status in _SUCCESSFUL

    @property
    def horizon(self) -> int:
        return len(self.points)

    def point(self, period: int) -> Optional[ForecastPoint]:
        """The forecast for one horizon period, or None."""
        for p in self.points:
            if p.period == period:
                return p
        return None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

class ForecastProvenance(BaseModel):
    """
    Where a forecast came from.

    A forecast consumed by the MILP must be traceable to the data version it was
    built from, the horizon requested, the engines that ran and the signals that
    touched it. Without `snapshot_id` a forecast cannot be checked for staleness
    against the network it will be applied to, so it is required.
    """
    snapshot_id: str
    data_version: Optional[str] = None
    network_id: Optional[str] = None

    target: ForecastTarget = ForecastTarget.DEMAND
    horizon: int = 0
    frequency: Frequency = Frequency.MONTH

    #: Orchestrator execution that requested the forecast.
    execution_id: Optional[str] = None
    request_id: Optional[str] = None
    #: Hypothetical context, when the forecast was produced for a scenario.
    scenario_id: Optional[str] = None

    #: Engines that produced at least one series in this result.
    engines_used: List[str] = Field(default_factory=list)
    selection_mode: SelectionMode = SelectionMode.PATTERN
    model_version: str = ""

    #: Signal ids that influenced at least one series. Empty on a history-only
    #: forecast, which is the default.
    signal_ids: List[str] = Field(default_factory=list)

    #: Series keys forecast from a post-break window instead of full history.
    #: Empty on every run where nothing adapted — so a non-empty list is the
    #: one-line answer to "did a structural break change this forecast?", and
    #: the per-series `structural_break` and `regime` say why.
    adapted_series: List[str] = Field(default_factory=list)

    generated_at: Optional[str] = None
    #: Always the forecasting service. Present so a result whose source is
    #: anything else is visibly wrong.
    source: str = "forecasting_service"

    #: Everything needed to reproduce the run bit-for-bit.
    reproducibility: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Request / Result
# ---------------------------------------------------------------------------

class ForecastRequest(BaseModel):
    """
    What the Orchestrator asks the Forecasting Agent for.

    Deliberately has no field for a network, a config or a solver. The agent
    receives history and signals and returns estimates; it is never handed the
    means to optimise anything, which is what makes "forecasting does not run
    the MILP" structural rather than a matter of discipline.
    """
    #: Observed history, one entry per market-product pair.
    series: List[DemandTimeSeries] = Field(default_factory=list)

    target: ForecastTarget = ForecastTarget.DEMAND
    horizon: int = Field(1, ge=1)
    frequency: Frequency = Frequency.MONTH

    #: The observed snapshot this forecast is for. Required: a forecast with no
    #: snapshot cannot be checked against the network it will be applied to.
    snapshot_id: str
    data_version: Optional[str] = None
    network_id: Optional[str] = None

    execution_id: Optional[str] = None
    request_id: Optional[str] = None
    scenario_id: Optional[str] = None

    #: `MarketIntelligenceSignal` objects the orchestrator judged relevant.
    #: Typed as Any so the forecasting package need not import the ingestion
    #: package; `signals/enrichment.py` validates the shape it needs and
    #: refuses anything carrying a probability.
    signals: List[Any] = Field(default_factory=list)

    #: Off by default. Signal enrichment applies manually-declared
    #: coefficients, so it is opt-in rather than something that happens to a
    #: caller who did not ask for it.
    enable_signal_enrichment: bool = False

    #: Run a rolling-origin backtest and attach measured error. Costs one refit
    #: per fold, so it is opt-in.
    run_backtest: bool = False
    backtest_folds: int = Field(3, ge=1, le=12)

    #: How the engine is chosen. See `SelectionMode`.
    selection_mode: SelectionMode = SelectionMode.PATTERN

    #: Scan each series for a structural break and, when one is found, forecast
    #: from whichever history window measures better.
    #:
    #: On by default. That is a deliberate departure from how signal enrichment
    #: and backtesting are exposed, and the reason is that this is a correctness
    #: guard rather than a feature: without it, a level shift inside the
    #: quantile engine's lag window is read as an explosive autoregressive
    #: coefficient and the recursive forecast diverges — measured at MAE 10,237
    #: against a naive-1 error of 7.8. A safety default that has to be switched
    #: on protects nobody. Detection costs one OLS scan per series, and on a
    #: series with no break the forecast is bit-for-bit what it was before.
    detect_structural_break: bool = True

    #: Pin a specific engine instead of auto-selecting. For reproducing a prior
    #: run, not for routine use — the selector encodes the pattern-to-engine
    #: reasoning. Overrides `selection_mode` when set.
    engine_override: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _no_duplicate_series(self) -> "ForecastRequest":
        """
        One history per market-product.

        Two entries for the same key would make the result ambiguous, and the
        by-key lookups downstream would silently pick whichever came first.
        """
        seen = set()
        for s in self.series:
            if s.key in seen:
                raise ValueError(
                    f"Duplicate history for '{s.key}'. Each market-product pair may "
                    f"appear at most once in a ForecastRequest."
                )
            seen.add(s.key)
        return self


class ForecastResult(BaseModel):
    """
    What the Forecasting Agent returns.

    Contains estimates and the evidence behind them. It has no field capable of
    holding an REI, a risk factor, a governance classification or an
    optimisation objective, and no field capable of holding an event
    probability — the forecasting and RF pathways share no vocabulary, which is
    how they stay separate under maintenance.
    """
    status: ForecastStatus
    provenance: ForecastProvenance

    #: One entry per requested series, ALWAYS. A series that could not be
    #: forecast appears with a non-OK status rather than being omitted, so a
    #: caller iterating the result cannot silently lose an entity.
    series: List[SeriesForecast] = Field(default_factory=list)

    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def ok(self) -> bool:
        """True when at least one series produced a usable forecast."""
        return any(s.ok for s in self.series)

    @property
    def successful(self) -> List[SeriesForecast]:
        return [s for s in self.series if s.ok]

    @property
    def failed(self) -> List[SeriesForecast]:
        return [s for s in self.series if not s.ok]

    def for_key(self, market_id: str, product_id: str) -> Optional[SeriesForecast]:
        for s in self.series:
            if s.market_id == market_id and s.product_id == product_id:
                return s
        return None

    def status_counts(self) -> Dict[str, int]:
        """Series count by status, for logging and quick triage."""
        counts: Dict[str, int] = {}
        for s in self.series:
            counts[s.status.value] = counts.get(s.status.value, 0) + 1
        return counts


__all__ = [
    "ForecastTarget",
    "Frequency",
    "DemandPattern",
    "ForecastStatus",
    "SelectionMode",
    "SignalEffect",
    "DemandPoint",
    "DemandTimeSeries",
    "CharacterizationMetrics",
    "SignalAdjustment",
    "ForecastPoint",
    "AccuracyMetrics",
    "SeriesForecast",
    "ForecastProvenance",
    "ForecastRequest",
    "ForecastResult",
]
