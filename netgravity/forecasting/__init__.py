"""
NetGravity — Forecasting Agent.

Estimates future demand from observed history, optionally adjusted by external
market-intelligence signals.

    Historical evidence (ingestion staging)
              ↓
          Orchestrator
              ↓
    ForecastRequest → ForecastingService → ForecastResult
              ↓
          Orchestrator          ← validates, then builds the MILP input
              ↓
             MILP

**The agent estimates; it never decides.** This package imports no solver, no
REI engine, no risk calculator and no governance rule — and it never sees a
`CanonicalNetwork`, because `ForecastRequest` has no field for one. Turning a
forecast into a network is `orchestrator/engines/forecast_bridge.py`, on the
other side of the boundary. Tests assert the import graph rather than trusting
this paragraph.

**The two signal pathways stay separate.** External signals may enrich a
forecast, through `signals/enrichment.py`, using the ingestion package's
`MarketIntelligenceSignal` — a type with no probability field by design. Event
likelihood is a different thing entirely: it lives on the orchestrator's
`ExternalSignal.event_probability` and feeds `RF = P + REI − P·REI`. Nothing in
this package can produce, consume or infer a probability.

Usage::

    from netgravity.forecasting import ForecastingService, ForecastRequest

    result = ForecastingService().forecast(ForecastRequest(
        series=[history],
        horizon=3,
        snapshot_id=snapshot.snapshot_id,
        run_backtest=True,
    ))

    for series in result.series:
        if series.ok:
            print(series.engine, [p.p50 for p in series.points])
        else:
            print(series.status.value, series.reason)   # never a number
"""

from netgravity.forecasting.change_point import (
    BreakKind,
    ChangePointDetector,
    ChangePointResult,
    DetectionStatus,
    detect_change_point,
)
from netgravity.forecasting.characterizer import (
    characterize_series,
    compute_demand_metrics,
)
from netgravity.forecasting.regime import (
    RegimeDecision,
    RegimeStrategy,
    StrategyBasis,
    select_regime,
)
from netgravity.forecasting.schemas import (
    AccuracyMetrics,
    CharacterizationMetrics,
    DemandPattern,
    DemandPoint,
    DemandTimeSeries,
    ForecastPoint,
    ForecastProvenance,
    ForecastRequest,
    ForecastResult,
    ForecastStatus,
    ForecastTarget,
    Frequency,
    SelectionMode,
    SeriesForecast,
    SignalAdjustment,
    SignalEffect,
)
from netgravity.forecasting.service import (
    MAX_HORIZON,
    MIN_HISTORY,
    MODEL_VERSION,
    ForecastingService,
)
from netgravity.forecasting.validation import backtest

__all__ = [
    "ForecastingService",
    "ForecastRequest",
    "ForecastResult",
    "ForecastStatus",
    "ForecastTarget",
    "Frequency",
    "SelectionMode",
    "SeriesForecast",
    "ForecastPoint",
    "ForecastProvenance",
    "AccuracyMetrics",
    "SignalAdjustment",
    "SignalEffect",
    "DemandTimeSeries",
    "DemandPoint",
    "DemandPattern",
    "CharacterizationMetrics",
    "characterize_series",
    "compute_demand_metrics",
    "backtest",
    "BreakKind",
    "ChangePointDetector",
    "ChangePointResult",
    "DetectionStatus",
    "detect_change_point",
    "RegimeDecision",
    "RegimeStrategy",
    "StrategyBasis",
    "select_regime",
    "MODEL_VERSION",
    "MAX_HORIZON",
    "MIN_HISTORY",
]
