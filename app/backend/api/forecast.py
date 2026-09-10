"""
NetGravity — Demand Forecasting & Signals API Blueprint
======================================================
Project-scoped demand forecasts produced by the real forecasting engines
(ETS / intermittent / quantile, with sup-F structural-break detection) and
external signals routed through the orchestrator's own signal-routing rules.

Phase 10.0 rewrite. The prototype version of this blueprint:

  * forecast a hardcoded 24-point series (`_NORTH_INDIA_HISTORY`) regardless of
    what the user had ingested;
  * on ANY engine exception did `except Exception: pass` and returned a
    hardcoded P10/P50/P90 cone that was byte-indistinguishable from a real
    quantile forecast — no status field, no log line;
  * hardcoded `growthRate: 14.2`, `breachMonth: "Dec'26"` and
    `breachProjectedUtil: 108` in BOTH the real and the fabricated branch;
  * served three fabricated market-intelligence signals attributed to real
    institutions ("RBI Quarterly Bulletin", "NHAI Press Release").

The engine layer was already honest — `orchestrator/registry.py::forecast_demand`
raises `MissingDataError` when no observed history exists rather than inventing
a series. This blueprint now routes through that capability instead of calling
`ForecastingService` directly, so the planner, plan validator and failure
manager are in the path, and a missing history is reported as such.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, g, jsonify, make_response, request

from app.backend.services.errors import (
    ApplicationError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.ratelimit import rate_limit
from app.backend.services.demand_history_store import (
    demand_history_store,
    uploaded_forecast_store,
    uploaded_signal_store,
)
from app.backend.services.correlation import orchestrator_request_id
from app.backend.services.project_registry import project_registry
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)


def _serialise_series(sf: Any) -> Dict[str, Any]:
    """
    One market-product forecast.

    `status` is emitted first and always; a non-OK series carries an empty
    `points` list, and the client must render the status rather than reading a
    quantity out of it.
    """
    return {
        "market_id": sf.market_id,
        "product_id": sf.product_id,
        "status": sf.status.value if hasattr(sf.status, "value") else str(sf.status),
        "engine": sf.engine,
        "engine_version": getattr(sf, "engine_version", ""),
        "pattern": sf.pattern.value if getattr(sf, "pattern", None) else None,
        "n_history_periods": getattr(sf, "n_history_periods", 0),
        "points": [
            {
                "period": p.period,
                "mean": p.mean,
                "p10": p.p10,
                "p50": p.p50,
                "p90": p.p90,
                "baseline_mean": getattr(p, "baseline_mean", None),
            }
            for p in sf.points
        ],
        "accuracy": (sf.accuracy.model_dump(mode="json")
                     if getattr(sf, "accuracy", None) else None),
        "signal_adjustments": [
            a.model_dump(mode="json") for a in getattr(sf, "signal_adjustments", [])
        ],
    }


def _uploaded_signals_for(orchestrator: Any, snapshot_id: str
                          ) -> Tuple[List[Any], List[str]]:
    """
    The market-intelligence signals stored with this snapshot's network.

    Returns `(signals, notes)`. Rehydrated into
    `MarketIntelligenceSignal` because that is the type the router and the
    enricher read structured fields off; a raw dict would fail every
    `getattr` check and be dropped as inapplicable, which looks exactly like
    "no signal applied" and is not.

    A signal that cannot be rehydrated becomes a NOTE, never an exception and
    never a silent omission: a malformed row in an upload must not stop a
    forecast, and must not disappear either.
    """
    notes: List[str] = []
    try:
        snapshot = orchestrator.snapshots.get(snapshot_id)
        raw = uploaded_signal_store.get(snapshot.network.network_id)
    except Exception as exc:  # noqa: BLE001 — no signals is a normal state
        logger.info("forecast.signals.unavailable snapshot=%s error=%s",
                    snapshot_id, exc)
        return [], []
    if not raw:
        return [], []

    signals: List[Any] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            notes.append(f"uploaded signal {index} is not an object and was skipped")
            continue
        try:
            signals.append(_as_market_intelligence(row))
        except Exception as exc:  # noqa: BLE001
            title = str(row.get("description") or row.get("title")
                        or f"signal {index}")[:60]
            notes.append(
                f"uploaded signal '{title}' could not be read as market "
                f"intelligence and did not reach the forecast: "
                f"{type(exc).__name__}")

    # THE GUARDRAIL, actually run.
    #
    # `route_for_forecast` refuses anything that has not cleared it, and an
    # uploaded row arrives with no verdict at all — so before this, a correctly
    # mapped signal would still have been refused, and asserting a verdict here
    # would be forging the one check that decides whether market intelligence
    # may move a number. This is the same policy, thresholds and classifier the
    # extraction path uses; a signal that does not clear it is refused and the
    # refusal is reported by the router.
    #
    # Scored against every node in the network. A market is a FACILITY with
    # role MARKET, so a signal naming M001 earns the entity-match bonus here
    # exactly as one naming a DC would.
    if signals:
        try:
            from netgravity.ingestion.guardrails import relevance

            network = orchestrator.snapshots.get(snapshot_id).network
            scope = {f.id for f in network.facilities}
            signals = relevance.apply(signals, known_entity_ids=scope)
        except Exception as exc:  # noqa: BLE001 — a forecast without them still stands
            logger.warning("forecast.signals.guardrail_failed: %s", exc)
            notes.append(
                "the relevance guardrail could not be run over the uploaded "
                "signals, so none of them informed this forecast")
            signals = []

    logger.info("forecast.signals.attached snapshot=%s usable=%d unreadable=%d",
                snapshot_id, len(signals), len(notes))
    return signals, notes


def _as_market_intelligence(row: Dict[str, Any]) -> Any:
    """
    One uploaded signal row, as the type the guardrail and router read.

    The ingestion structure's shape shares no field name with
    `MarketIntelligenceSignal`, which is why `MarketIntelligenceSignal(**row)`
    raised on every row of every upload.

    WHAT IS MAPPED AND WHAT IS NOT. `id`, `date`, `description` and `marketId`
    have exact counterparts. `relevance` is HIGH/MEDIUM/LOW on both sides.
    `type` is the upload's own word for what kind of event this is and is left
    for the guardrail's classifier to bucket — it reads the text, and its
    keyword table is the declared policy for that decision.

    DIRECTION is read only where the type states one. A signal typed
    CUSTOMER_EXPANSION or MARKET_GROWTH is unambiguously upward; one typed
    WEATHER_DISRUPTION states no direction about demand, and guessing would
    hand the rules table a mechanism nobody declared. NEUTRAL is the honest
    default, and `_RULES` acts on it only for WEATHER, where the declared
    effect is to widen the band rather than move the estimate.

    `probability` is deliberately dropped rather than carried: a field named
    probability makes this a RISK signal, and `route_for_forecast` refuses
    those outright — event likelihood belongs to the RF pathway, never to a
    forecast. An uploaded row that carries one is market intelligence with a
    number attached in the wrong column, not a hazard.
    """
    from netgravity.ingestion.schemas.signal import (
        MarketIntelligenceSignal,
        SignalConfidence,
        SignalDirection,
    )

    kind = str(row.get("type") or "").upper()
    up = any(w in kind for w in ("EXPANSION", "GROWTH", "INCREASE", "SURGE"))
    down = any(w in kind for w in ("CONTRACTION", "DECLINE", "DECREASE",
                                   "CLOSURE", "SHUTDOWN"))
    direction = (SignalDirection.UP if up
                 else SignalDirection.DOWN if down
                 else SignalDirection.NEUTRAL)

    confidence = str(row.get("relevance") or "").upper()
    market = str(row.get("marketId") or "").strip()
    text = str(row.get("description") or row.get("type") or row.get("id") or "")

    return MarketIntelligenceSignal(
        signal_id=str(row.get("id") or row.get("signal_id") or ""),
        title=text[:200],
        published_date=str(row.get("date") or ""),
        effective_date=str(row.get("date") or "") or None,
        direction=direction,
        # The upload's own type, kept verbatim so the classifier reads the
        # word the client used rather than a paraphrase of it.
        magnitude=str(row.get("type") or ""),
        affected_entities=[market] if market else [],
        geography=market,
        confidence=(SignalConfidence(confidence)
                    if confidence in SignalConfidence.__members__
                    else SignalConfidence.MEDIUM),
        rationale=text,
        structured_by="upload",
    )


def _forecast_explanation(ctx: Any) -> Dict[str, Any]:
    """
    The forecast's grounded briefing, in the shape the card reads.

    Already computed and already grounded — the forecast workflow runs
    `reasoning.synthesise` on every request and `numeric_grounding` has
    re-checked every numeric claim by the time it gets here. Nothing is
    generated and nothing is recomputed; this selects fields off
    `ExecutiveBriefing`, exactly as `_scenario_explanation` does.

    Returns {} when the run produced no briefing, so the screen says it has
    nothing to explain rather than showing the network's briefing in its place
    — which is what it did before, in a second copy of Home's card.
    """
    reasoning = getattr(ctx, "reasoning", None)
    briefing = getattr(reasoning, "briefing", None) if reasoning else None
    if briefing is None:
        return {}

    from netgravity.orchestrator.explanation_service import build_card

    return {
        "card": build_card(reasoning),
        "scope": briefing.scope.value,
        "opening": briefing.opening,
        "insights": [
            {"theme": i.theme, "headline": i.headline,
             "narrative": i.narrative, "severity": i.severity.value}
            for i in briefing.kpi_insights
        ],
        "recommendation": briefing.recommendation,
        "limitation": briefing.limitation,
        "evidence_completeness": briefing.evidence_completeness.value,
        "source": getattr(reasoning, "source", "template"),
        "grounding": {"warnings": list(getattr(reasoning, "validation_warnings", []))},
    }


#: Rows listed in an outlook's "fastest growing" and "shrinking" lists,
#: matching `registry._OUTLOOK_ROWS` so the two paths render the same length.
_OUTLOOK_ROWS = 5


def _uploaded_forecast_card(outlook: Dict[str, Any], n_uncovered: int,
                            banded: int, n_series: int) -> Dict[str, Any]:
    """
    What the SUPPLIED projection says, in the shape the attention card reads.

    Every sentence here is a statement about figures the upload itself states,
    summed and compared — the same arithmetic `outlook` already holds, said in
    words. Nothing is modelled, nothing is inferred about why the demand moves,
    and no language model is involved: a projection nobody in this build
    produced must not acquire an explanation this build made up for it.

    It exists because the screen had the outlook rows and no lead sentence, so
    an uploaded forecast rendered as a table of deltas with nothing saying what
    they amounted to — while the modelled path, beside it, opened with a
    conclusion. The reader has the same question on both screens.
    """
    total = outlook.get("total_forecast_units")
    growth = outlook.get("growth_pct")
    horizon = outlook.get("horizon")
    recent = outlook.get("comparable_recent_units")

    if not isinstance(total, (int, float)):
        return {}

    headline = (f"The supplied forecast projects {total:,.0f} units across "
                f"{n_series} market-product pair(s) over {horizon} period(s).")

    if isinstance(growth, (int, float)) and isinstance(recent, (int, float)):
        direction = "above" if growth >= 0 else "below"
        meaning = (
            f"That is {abs(growth):,.1f}% {direction} the {recent:,.0f} units "
            f"recorded over the same number of periods immediately before it. "
            f"The comparison is arithmetic on two stated quantities: the "
            f"forecast is the supplier's, the recent figure is this network's "
            f"observed history, and nothing was fitted to either.")
    else:
        meaning = (
            "There is no comparable observed window for these pairs, so no "
            "rate of change is stated. The projected total stands on its own.")

    warning = ""
    if n_uncovered:
        warning = (
            f"{n_uncovered} market-product pair(s) in this network are not in "
            f"the upload and have no forecast. They are absent from every "
            f"figure above, and no model was run to fill them in.")
    elif banded == 0:
        warning = ("The upload states no P10-P90 band, so these are point "
                   "estimates with no stated uncertainty around them.")

    next_step = ("Test the network against this demand. The scenario planner "
                 "can apply the projected volumes to the current footprint "
                 "and report where capacity or service would be breached.")

    return {
        "headline": headline,
        "meaning": meaning,
        "warning": warning,
        "next_step": next_step,
        "figures": [],
        "details": [
            "Supplied with the upload. No forecasting engine, quantile model, "
            "structural-break detection or signal enrichment was run against "
            "these figures.",
            "Every quantity above is a sum over the rows the upload states, "
            "or a ratio between two such sums.",
        ],
        # NOT "llm". The card's source field drives what the screen says about
        # who wrote the words, and these were written by this function.
        "source": "template",
        "cached": False,
    }


def _uploaded_forecast_payload(
    project_id: str, snapshot_id: str, network_id: str, horizon: int,
    snapshot: Any,
) -> Dict[str, Any]:
    """
    A forecast that arrived with the upload, in the shape the screen reads.

    Every key `GET /api/forecast` returns is present and means the same thing.
    Four are deliberately different, and each difference is a fact the reader
    needs: `engine` is UPLOADED, `accuracy` is None because no model was
    fitted, `execution_id` is None because no orchestrator run produced this,
    and `provenance.recalculated` is False.

    NOTHING IS COMPUTED FROM A MODEL HERE. `outlook` is summed from the
    upload's own points against the observed history — every figure in it is a
    sum or a ratio over numbers the upload already stated. It is built rather
    than omitted because the attention card renders "no forecast has been
    produced for this network yet" when it has neither a briefing nor an
    outlook, which would contradict the chart beside it.
    """
    series, warnings = uploaded_forecast_store.series(network_id, horizon=horizon)

    observed: List[Any] = []
    if snapshot is not None:
        try:
            observed, _ = demand_history_store.for_snapshot(snapshot)
        except Exception as exc:  # noqa: BLE001 - the forecast still stands
            logger.warning("forecast.uploaded.history_attach_failed: %s", exc)
    by_pair = {(o.market_id, o.product_id): o for o in observed}

    forecast_total = recent_total = 0.0
    rows: List[Dict[str, Any]] = []
    for row in series:
        row.update({
            "status": "OK",
            # Names what produced the numbers, exactly as the engine field does
            # on a modelled forecast. Nothing ran, and this says so.
            "engine": "UPLOADED",
            "engine_version": "",
            "pattern": None,
            # Measured error requires a model and a backtest. There was
            # neither, which is a different statement from "the error was
            # small" and must not be rendered as "not reported".
            "accuracy": None,
            "signal_adjustments": [],
        })
        source = by_pair.get((row["market_id"], row["product_id"]))
        history = (sorted(getattr(source, "history", []) or [],
                          key=lambda x: x.period) if source else [])
        row["n_history_periods"] = len(history)
        row["history"] = [
            {"period": p.period, "timestamp": p.timestamp, "quantity": p.quantity}
            for p in history
        ]

        n_points = len(row["points"])
        future = sum(p["mean"] for p in row["points"] if p["mean"] is not None)
        # The same number of periods, immediately before the forecast starts.
        recent = sum(p.quantity for p in history[-n_points:]) if history else 0.0
        forecast_total += future
        recent_total += recent
        rows.append({
            "market_id": row["market_id"],
            "product_id": row["product_id"],
            "forecast_units": round(future, 2),
            # None, not 0.0: a pair with no comparable observed window has an
            # UNKNOWN growth rate, and 0.0 reads as "demand is flat".
            "recent_units": round(recent, 2) if history else None,
            "growth_pct": (round((future - recent) / recent * 100.0, 2)
                           if recent > 0 and history else None),
            "n_history_periods": len(history),
        })

    # Pairs this network has that the upload's forecast does not cover.
    #
    # Named, never silently absent — and deliberately NOT filled in by running
    # our own model for them, which would put two sources on one screen without
    # saying so. An uploaded forecast is the whole answer or it is not the
    # answer; a chart mixing the two is two different quantities on one axis.
    covered = {(r["market_id"], r["product_id"]) for r in rows}
    uncovered = sorted(
        f"{d.market_id}/{d.product_id}"
        for d in (getattr(getattr(snapshot, "network", None), "demands", []) or [])
        if (d.market_id, d.product_id) not in covered
    )
    if uncovered:
        warnings.append(
            f"The uploaded forecast covers {len(covered)} market-product "
            f"pair(s). {len(uncovered)} pair(s) in this network are not in the "
            f"upload and have no forecast: {', '.join(uncovered[:5])}"
            f"{'…' if len(uncovered) > 5 else ''}. No model was run for them."
        )

    movers = [r for r in rows if r["growth_pct"] is not None]
    movers.sort(key=lambda r: -(r["forecast_units"] - (r["recent_units"] or 0)))

    banded = sum(1 for row in series for p in row["points"]
                 if p.get("p10") is not None and p.get("p90") is not None)

    # THE PERIODS ACTUALLY FORECAST, not the number that was asked for.
    #
    # `horizon` is the request's ceiling. An upload stating three periods
    # against a request for six yields three points — nothing is extrapolated,
    # deliberately — and every sentence built on this reads "over N periods".
    # Reporting the ceiling made the card say "28,400 units over 6 periods"
    # beside a chart showing three, which is the same total spread over twice
    # the time: a materially different claim about the demand.
    periods = max((len(r["points"]) for r in series), default=0)
    outlook = {
        "horizon": periods or horizon,
        "n_series_forecast": len(series),
        "n_series_total": len(series) + len(uncovered),
        "total_forecast_units": round(forecast_total, 2),
        "comparable_recent_units": (round(recent_total, 2)
                                    if recent_total else None),
        "growth_pct": (round((forecast_total - recent_total)
                             / recent_total * 100.0, 2)
                       if recent_total > 0 else None),
        "fastest_growing": movers[:_OUTLOOK_ROWS],
        "shrinking": [r for r in reversed(movers)
                      if r["growth_pct"] < 0][:_OUTLOOK_ROWS],
        # Nothing was adjusted and nothing was detected, because nothing
        # ran. Empty rather than absent, so the shape matches.
        "signal_adjustments": [], "n_signal_adjustments": 0,
        "structural_breaks": [], "n_structural_breaks": 0,
    }

    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        # No orchestrator execution produced this, and saying so is the point.
        "execution_id": None,
        "status": "OK",
        #: "uploaded" or "model". The field every consumer branches on.
        "forecast_source": "uploaded",
        "horizon": max((len(r["points"]) for r in series), default=0),
        "series": series,
        "n_series_uncovered": len(uncovered),
        # A finding about THIS projection — summed from the upload's own rows
        # by `_uploaded_forecast_card`, never borrowed from the network's
        # briefing, which is what the screen used to show here and is a
        # different question answered in the same place.
        "explanation": {
            "card": _uploaded_forecast_card(outlook, len(uncovered), banded,
                                            len(series)),
            "scope": "FORECAST",
            "opening": "",
            "insights": [],
            "recommendation": "",
            "limitation": "",
            "evidence_completeness": "COMPLETE",
            #: Written by this endpoint from the upload's own rows. Not "llm",
            #: and not the reasoning agent's — the screen prints a different
            #: attribution line for each and all three are different claims.
            "source": "uploaded_summary",
            "grounding": {"warnings": []},
        },
        "outlook": outlook,
        "signals": {
            "attached": 0, "series_adjusted": 0,
            "applied_signal_ids": [], "unreadable": [],
            "notice": ("Uploaded signals were not applied. This forecast came "
                       "with the upload and was not recalculated."),
        },
        "warnings": warnings,
        "provenance": {
            "authoritative_source": "upload",
            "routed_through": None,
            "llm_used": False,
            "explanation_source": None,
            #: The one fact the screen must not get wrong.
            "recalculated": False,
            "notice": ("Supplied with the upload and returned unchanged. No "
                       "forecasting engine, quantile model, intermittent-demand "
                       "model, structural-break detection or signal enrichment "
                       "was run against these figures."),
        },
    }


# ---------------------------------------------------------------------------
# "How did it reach that number?" — the forecast, as a document
# ---------------------------------------------------------------------------
#: How each engine works, in one sentence a reader who is not a statistician
#: can act on. Keyed by the `engine` string the forecaster reports.
#:
#: Written out rather than generated, because the description of a method is
#: not a result and must not vary with the run: two documents about the same
#: engine have to say the same thing about it, or a reader comparing them
#: reads a difference into a sentence that was only phrased differently.
_ENGINE_NOTES = {
    "ETS": (
        "Exponential smoothing with a trend and, where the history is long "
        "enough to show one, a seasonal term. Every observed period "
        "contributes, with recent periods weighted more heavily than old "
        "ones; the smoothing weights are fitted to this series' own history "
        "by minimising one-step-ahead error, not set by hand."),
    "CROSTON": (
        "Croston's method for intermittent demand. This series has periods of "
        "genuine zero demand, which an averaging method reads as a low level "
        "rather than as an absence. Croston separates the two questions — how "
        "large an order is when it comes, and how long the gap between orders "
        "is — and forecasts each separately."),
    "QUANTILE": (
        "Empirical quantile regression over the observed history. The band is "
        "read off the distribution of the history's own errors rather than "
        "assumed to be a normal curve around the mean, so a series whose "
        "surprises are one-sided gets a one-sided band."),
    "NAIVE": (
        "A seasonal naive carry-forward: the last comparable period, repeated. "
        "Used when the history is too short to fit anything to, and reported "
        "as such rather than presented as a model."),
    "UPLOADED": (
        "Nothing was fitted. These figures arrived with the upload and are "
        "reproduced exactly as supplied."),
}


def _forecast_engine_note(engine: str) -> str:
    key = str(engine or "").strip().upper()
    for name, note in _ENGINE_NOTES.items():
        if key.startswith(name):
            return note
    return ("The engine that produced this series is reported above. This "
            "build has no written description of it, so none is given rather "
            "than one being inferred from its name.")


def _fmt_units(value: Any) -> str:
    """A quantity, the way the forecast screen prints one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "Not available"
    return f"{value:,.0f} units"


def _forecast_derivation(payload: Dict[str, Any], series: Dict[str, Any],
                         project_name: str) -> Any:
    """
    One forecast series, as a `DerivationReport`.

    The counterpart of `insights._derivation_for`, and deliberately the same
    shape: the reader asking "how did it reach that number?" is asking one
    question, and two documents answering it differently would make the
    answer look like a property of which screen it was pressed from.

    NOTHING IS FORECAST HERE. Every figure is one the forecasting engine
    already produced and this endpoint already returned; the sums are over
    those points, and each is labelled as a sum so no reader takes it for a
    separately modelled quantity.
    """
    from datetime import datetime, timezone

    from netgravity.reporting import DerivationReport, DerivationStep, Figure

    points = list(series.get("points") or [])
    history = list(series.get("history") or [])
    market = str(series.get("market_id") or "")
    product = str(series.get("product_id") or "")
    engine = str(series.get("engine") or "")
    uploaded = payload.get("forecast_source") == "uploaded"

    total = sum(p["mean"] for p in points
                if isinstance(p.get("mean"), (int, float)))
    observed_window = history[-len(points):] if history and points else []
    observed_total = sum(p["quantity"] for p in observed_window
                         if isinstance(p.get("quantity"), (int, float)))

    steps: List[Any] = []

    # ── 1. what it was built from ────────────────────────────────────
    history_figures = [
        Figure("Observed periods available", f"{len(history):,}",
               "Measured", "demand history"),
        Figure("Periods forecast", f"{len(points):,}",
               "Measured", "forecast engine"),
    ]
    if observed_window:
        history_figures.append(Figure(
            f"Observed demand, last {len(observed_window)} period(s)",
            _fmt_units(observed_total), "Compared against", "demand history"))
    steps.append(DerivationStep(
        title="What this was built from",
        detail=(
            "The observed history for this market-product pair, as recorded in "
            "the upload. Nothing else enters the calculation: a forecast for "
            "one pair is fitted to that pair's own history, not to the "
            "network total."
            if not uploaded else
            "The forecast arrived with the upload. The observed history is "
            "shown for comparison only — nothing was fitted to it, and the "
            "figures below were not derived from it."),
        figures=tuple(history_figures)))

    # ── 2. the method ────────────────────────────────────────────────
    if not uploaded:
        method_figures = [
            Figure("Engine selected", engine or "Not reported",
                   "Method", "forecast engine"),
        ]
        if series.get("pattern"):
            method_figures.append(Figure(
                "Demand pattern detected", str(series["pattern"]),
                "Method", "forecast engine"))
        accuracy = series.get("accuracy") or {}
        if isinstance(accuracy, dict) and accuracy.get("mase") is not None:
            mase = accuracy["mase"]
            method_figures.append(Figure(
                "Backtest error (MASE)", f"{mase:.3f}",
                "Measured", "rolling-origin backtest"))
            method_figures.append(Figure(
                "Read as",
                ("better than a naive seasonal forecast" if mase < 1
                 else "no better than a naive seasonal forecast"),
                "Compared against", "rolling-origin backtest"))
        steps.append(DerivationStep(
            title="How the number was produced",
            detail=_forecast_engine_note(engine),
            figures=tuple(method_figures)))

    # ── 3. the points ────────────────────────────────────────────────
    #
    # The whole horizon, period by period. This is the part a screen can only
    # draw and a reader is most often asked for: which period, what figure,
    # and how wide the band around it was.
    point_figures = []
    for point in points:
        label = str(point.get("timestamp") or f"Period +{point.get('period')}")
        band = ""
        if (isinstance(point.get("p10"), (int, float))
                and isinstance(point.get("p90"), (int, float))):
            band = f"  (p10 {point['p10']:,.0f} – p90 {point['p90']:,.0f})"
        point_figures.append(Figure(
            label, _fmt_units(point.get("mean")) + band,
            "Forecast", engine or "forecast engine"))
    if point_figures:
        steps.append(DerivationStep(
            title="The forecast, period by period",
            detail=(
                "Each period's central estimate, with the p10-p90 band where "
                "the engine produced one. A band is absent, never assumed: a "
                "forecast with no stated bounds is a point estimate and is "
                "shown as one."),
            figures=tuple(point_figures)))

    # ── 4. what the total is, and what it is not ─────────────────────
    total_figures = [
        Figure(f"Forecast demand, {len(points)} period(s)", _fmt_units(total),
               "Sum of the periods above", "sum"),
    ]
    if observed_window and observed_total > 0:
        change = (total - observed_total) / observed_total * 100.0
        total_figures.append(Figure(
            "Against the same number of observed periods",
            f"{change:+,.1f}%", "Compared against", "sum"))
    steps.append(DerivationStep(
        title="What it adds up to",
        detail=("A sum over the periods listed above and nothing more. The "
                "comparison is against the same number of immediately "
                "preceding observed periods, so the two cover equal spans."),
        figures=tuple(total_figures)))

    warnings = [str(w) for w in (payload.get("warnings") or [])]
    limitations = list(warnings)
    if not uploaded and not (series.get("accuracy") or {}):
        limitations.append(
            "No backtest error is reported for this series, so the forecast's "
            "accuracy on this history has not been measured.")
    if uploaded:
        limitations.append(
            "These figures were supplied, not produced here. This document "
            "records what was received and how it is being used; it cannot "
            "account for how the supplier arrived at it.")

    provenance_block = payload.get("provenance") or {}
    provenance = (
        f"Source: {provenance_block.get('authoritative_source') or 'unknown'}. "
        f"Snapshot {payload.get('snapshot_id') or 'unknown'}."
    )
    if payload.get("execution_id"):
        provenance += f" Orchestrator execution {payload['execution_id']}."
    if provenance_block.get("notice"):
        provenance += f" {provenance_block['notice']}"

    subject = f"{market} · {product}" if product else market
    if uploaded:
        conclusion = (f"{_fmt_units(total)} forecast for {subject} over "
                      f"{len(points)} period(s), supplied with the upload")
    else:
        conclusion = (f"{_fmt_units(total)} forecast for {subject} over "
                      f"{len(points)} period(s)")

    return DerivationReport(
        kind="Demand forecast",
        subject=f"{subject}{f' — {project_name}' if project_name else ''}",
        conclusion=conclusion,
        summary=(
            "This document states how the demand forecast for this "
            "market-product pair was produced, the history it was produced "
            "from, and what it does and does not establish."),
        method=(
            "The forecast is produced in one deterministic stage. The "
            "forecasting engine reads this pair's observed history, selects a "
            "method by the demand pattern it finds, fits that method to the "
            "history, and emits a central estimate per future period with a "
            "band where the method supports one. No language model takes part "
            "in producing any figure in this document, and nothing here is "
            "re-derived: every number is the one the engine reported."
            if not uploaded else
            "No forecast was produced. The figures below arrived with the "
            "upload and are reproduced exactly as supplied. No forecasting "
            "engine, quantile model, structural-break detection or signal "
            "enrichment was run against them."),
        steps=steps,
        limitations=limitations,
        provenance=provenance,
        generated_at="Generated "
                     + datetime.now(timezone.utc).strftime("%d %B %Y, %H:%M UTC"),
    )


def create_forecast_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/forecast"):
    bp = Blueprint("forecast", __name__, url_prefix=url_prefix)

    def _gateway() -> Any:
        """
        The gateway the reasoning agent already holds.

        Not a second `LLMGateway()`. The budget is cumulative and SHARED
        across every holder of the token — 100 requests a day for the whole
        product — so two clients each believing they have the full allowance
        is how a shared limit gets exceeded rather than respected.
        """
        if orchestrator is None:
            return None
        agent = (orchestrator.services or {}).get("reasoning_agent")
        return getattr(agent, "gateway", None)

    @bp.route("", methods=["GET"])
    @require_auth
    def get_forecast():
        """
        Demand forecast for the project's bound network.

        Returns 409 NO_NETWORK_BOUND when the project has no snapshot, and an
        explicit FORECAST_UNAVAILABLE when the snapshot has no observed demand
        history. Neither case is answered with a fabricated cone.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The forecasting engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")
        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id
        )

        try:
            horizon = int(request.args.get("horizon", 6))
        except (TypeError, ValueError):
            raise ValidationError("horizon must be an integer.")
        if not 1 <= horizon <= 24:
            raise ValidationError("horizon must be between 1 and 24 periods.")

        # A forecast that ARRIVED WITH THE UPLOAD is the forecast.
        #
        # Returned before the signals are routed and before the orchestrator
        # request is built, so nothing below this point runs: no ETS, Croston
        # or quantile engine, no sup-F structural-break detection, no signal
        # enrichment, no rolling-origin backtest, no reasoning step. A forecast
        # this build produced beside one the upload supplied would be a second
        # answer to a question the upload has already answered, and "their
        # forecast, adjusted by our model" is a number nobody supplied and
        # nobody could audit.
        #
        # A project whose upload carried no forecast never enters this branch
        # and takes the engine path below exactly as before.
        try:
            snapshot = orchestrator.snapshots.get(snapshot_id)
            network_id = snapshot.network.network_id
        except Exception as exc:  # noqa: BLE001 - the engine path reports it
            logger.info("forecast.snapshot_unreadable snapshot=%s error=%s",
                        snapshot_id, exc)
            snapshot, network_id = None, ""

        if network_id and uploaded_forecast_store.has(network_id):
            # The upload's own horizon, not this endpoint's default. Truncating
            # a twelve-period forecast to six would discard half of the file
            # the user is being shown their own numbers from. An explicit
            # ?horizon= still wins.
            if request.args.get("horizon") is None:
                stated = len(uploaded_forecast_store.periods(network_id))
                horizon = max(1, min(stated, 24)) or horizon
            logger.info(
                "forecast.uploaded project_id=%s network_id=%s horizon=%d",
                project_id, network_id, horizon)
            return jsonify(_uploaded_forecast_payload(
                project_id, snapshot_id, network_id, horizon, snapshot)), 200

        # Signals the client uploaded WITH this network, handed to the
        # forecaster through the orchestrator's own routing.
        #
        # They were parsed, stored and displayed, and that is all: the router
        # (`routing/signal_router.py`) and the enricher
        # (`forecasting/signals/enrichment.py`) were both complete, both tested,
        # and reachable only by a caller that constructed a request by hand.
        # Every screen therefore showed a forecast that had never seen the
        # market intelligence sitting in the same upload — and the signals card
        # said so, which was honest but was not the fix.
        #
        # Nothing is bypassed by attaching them here: the router still decides
        # what may inform a forecast, on confidence, guardrail verdict and
        # whether a signal names an entity this network contains. A refused
        # signal comes back as a warning, so one that arrived and did nothing is
        # visible rather than silent.
        signals, signal_notes = _uploaded_signals_for(orchestrator, snapshot_id)

        req = OrchestratorRequest(
            input=f"Forecast demand for the next {horizon} periods",
            explicit_intent=Intent.FORECAST,
            actor=Actor(actor_id=g.current_user.user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            market_signals=signals,
            disable_llm=True,
            request_id=orchestrator_request_id("forecast"),
        )

        try:
            response = orchestrator.run_sync(req)
        except Exception as exc:  # noqa: BLE001
            # Logged and surfaced. The prior implementation swallowed this and
            # returned a plausible forecast instead.
            logger.exception("forecast.failed project_id=%s", project_id)
            return jsonify({
                "error": {
                    "code": "FORECAST_FAILURE",
                    "message": f"No forecast could be produced: {exc}",
                    "context": {"project_id": project_id, "snapshot_id": snapshot_id},
                }
            }), 502

        ctx = orchestrator.get_execution_state(response.execution_id)
        result = getattr(ctx, "forecast_result", None) if ctx else None

        if result is None or not getattr(result, "series", None):
            warnings = list(getattr(ctx, "warnings", []) or []) if ctx else []
            logger.info("forecast.unavailable project_id=%s", project_id)
            return jsonify({
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "execution_id": response.execution_id,
                "status": "FORECAST_UNAVAILABLE",
                "message": (
                    "No observed demand history is available for this network, "
                    "so no forecast can be produced. History reaches the "
                    "forecaster through the ingestion staging zone."
                ),
                "warnings": warnings,
                "series": [],
            }), 200

        series = [_serialise_series(sf) for sf in result.series]

        # Attach the OBSERVED history each series was built from. A forecast is
        # only interpretable beside the history it continues, and the client
        # had no way to obtain it — so the forecast screen kept drawing the
        # prototype's own 24-month demo series instead of the user's.
        # This is the same observed data the forecaster was given; nothing is
        # recomputed here.
        try:
            snapshot = orchestrator.snapshots.get(snapshot_id)
            observed, _ = demand_history_store.for_snapshot(snapshot)
            by_pair = {(o.market_id, o.product_id): o for o in observed}
            for row in series:
                source = by_pair.get((row["market_id"], row["product_id"]))
                if source is None:
                    row["history"] = []
                    continue
                row["history"] = [
                    {"period": p.period, "timestamp": p.timestamp, "quantity": p.quantity}
                    for p in sorted(source.history, key=lambda x: x.period)
                ]
        except Exception as exc:  # noqa: BLE001 — the forecast still stands
            logger.warning("forecast.history_attach_failed: %s", exc)
            for row in series:
                row.setdefault("history", [])

        # How many signals were supplied, and how many actually moved a
        # forecast. Both numbers, because "3 signals attached" and "0
        # adjustments applied" is a state a reader has to be able to see: the
        # router refuses a signal that names no entity in this network, and a
        # screen that showed only the attachment count would imply an influence
        # that was refused.
        adjusted = sum(1 for row in series if row.get("signal_adjustments"))

        # What the forecast IMPLIES — how much demand against how much was
        # observed, where it is growing, what the signals moved. Computed by
        # the forecasting capability itself (`_forecast_outlook`), so the
        # briefing above and this block cannot disagree about a number.
        outlook = (ctx.output_of("forecast.demand") or {}).get("outlook") or {}
        # Built once. Called twice it could, in principle, answer differently,
        # and the two answers would sit in the same response.
        explanation = _forecast_explanation(ctx)

        # WHICH uploaded signal actually moved this forecast, by id.
        #
        # The screen showed "Not yet applied" on every signal, from a hardcoded
        # string written when nothing routed them. They are routed; the chip
        # simply had no way to know. This is the routing's own answer.
        applied_signal_ids = sorted({
            a["signal_id"] for row in series
            for a in (row.get("signal_adjustments") or [])
            if a.get("signal_id")
        })

        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "execution_id": response.execution_id,
            "status": "OK",
            #: "uploaded" or "model". Stated on both paths so a consumer never
            #: has to infer which one answered from the absence of a field.
            "forecast_source": "model",
            "horizon": horizon,
            "series": series,
            # The forecast's own grounded briefing, FORECAST-scoped, from the
            # reasoning step this workflow already runs. It was computed on
            # every request and returned on none of them, so the screen had
            # only the network's general briefing to show beside a projection.
            "explanation": explanation,
            "outlook": outlook,
            "signals": {
                "attached": len(signals),
                "series_adjusted": adjusted,
                "applied_signal_ids": applied_signal_ids,
                "unreadable": signal_notes,
            },
            "warnings": list(getattr(ctx, "warnings", []) or []) + signal_notes,
            "provenance": {
                "authoritative_source": "netgravity.forecasting",
                "routed_through": "orchestrator capability 'forecast.demand'",
                # Every FIGURE is still the forecaster's, whichever voice the
                # briefing is in. `disable_llm=True` on this request means the
                # briefing is the deterministic template's — stated here rather
                # than as a bare False, which said nothing about the words.
                "llm_used": False,
                "explanation_source": explanation.get("source") or "template",
                #: This forecast was computed by the engines named above.
                "recalculated": True,
            },
        }), 200

    @bp.route("/document", methods=["GET"])
    @require_auth
    @rate_limit("forecast.document", limit=30, window_seconds=60)
    def forecast_document():
        """
        One forecast series, as a document somebody can take into a meeting.

        WHY THIS EXISTS. "Where did that number come from?" is the first
        question a demand plan is asked, and a chart cannot answer it: it shows
        the answer, not the route to it. The screen has the shape of the
        series; this has the history it was fitted to, the method that was
        chosen and why, the figure for every period with its band, and what the
        forecast does not establish.

        Query:
            ``project_id``  required
            ``market_id``   which series; the first returned when absent
            ``product_id``  narrows `market_id` when a market has several
            ``horizon``     as on `GET /api/forecast`

        NOTHING IS FORECAST HERE. The payload is the one `GET /api/forecast`
        returns for this project — the identical call, so the document and the
        chart cannot disagree about a figure — and this route selects a series
        from it and writes it out.
        """
        from netgravity.reporting import build_derivation_docx, narrate

        # The same call the screen makes, through the same view.
        #
        # Deliberately not a second assembly of the payload: the forecast body
        # is built along several paths (uploaded, modelled, and two failure
        # shapes), and a second builder here would be a second answer to
        # "what is this project's forecast?" that drifts from the first the
        # next time either changes.
        result = get_forecast()
        response = result[0] if isinstance(result, tuple) else result
        status = result[1] if isinstance(result, tuple) else 200
        payload = response.get_json() if hasattr(response, "get_json") else {}
        if status != 200 or not isinstance(payload, dict):
            # The forecast itself could not be produced. Say that, with the
            # engine's own reason, rather than handing back an empty document
            # that looks like an answer.
            reason = ""
            if isinstance(payload, dict):
                reason = str(payload.get("message")
                             or payload.get("error") or "").strip()
            raise NotFoundError(
                "There is no forecast for this project to document."
                + (f" {reason}" if reason else ""))

        series_list = list(payload.get("series") or [])
        if not series_list:
            raise NotFoundError(
                "This project's forecast contains no series, so there is "
                "nothing to document.")

        market_id = str(request.args.get("market_id") or "").strip()
        product_id = str(request.args.get("product_id") or "").strip()
        if market_id:
            matches = [x for x in series_list
                       if str(x.get("market_id")) == market_id
                       and (not product_id
                            or str(x.get("product_id")) == product_id)]
            if not matches:
                raise NotFoundError(
                    f"'{market_id}"
                    + (f"/{product_id}" if product_id else "")
                    + "' is not a series in this project's forecast.")
            series = matches[0]
        else:
            # The first series is the one the chart opens on, so a download
            # with no series named documents what the reader is looking at.
            series = series_list[0]

        project_name = ""
        try:
            project = project_registry.get(
                str(payload.get("project_id") or ""),
                user_id=g.current_user.user_id)
            project_name = str(getattr(project, "name", "") or "")
        except Exception:  # noqa: BLE001 — the title reads fine without it
            project_name = ""

        report = _forecast_derivation(payload, series, project_name)

        # The model writes the joining-up, and only that. Every figure it
        # quotes is checked against the figures already in the report and any
        # sentence quoting one that is not there is dropped, so a forecast
        # document cannot acquire a number the forecaster did not produce.
        narration = narrate(report, _gateway(), purpose="forecast_document")
        report.narrative = list(narration.paragraphs)
        # The note is printed only when there is something for it to explain:
        # a passage that was written, or one that was written and withheld. An
        # unconfigured gateway is not a fact about this analysis, and a line
        # about a missing service in a document about a network reads as a
        # caveat on the network.
        report.narrative_note = (
            narration.note
            if (narration.paragraphs or narration.source == "rejected") else "")

        document = build_derivation_docx(report)
        out = make_response(document)
        out.headers["Content-Type"] = (
            "application/vnd.openxmlformats-officedocument"
            ".wordprocessingml.document")
        out.headers["Content-Disposition"] = (
            f'attachment; filename="{report.filename()}"')
        out.headers["Cache-Control"] = "no-store"
        return out

    @bp.errorhandler(ApplicationError)
    def _forecast_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp


def create_signals_blueprint(orchestrator: Optional[Orchestrator] = None,
                             url_prefix: str = "/api/signals"):
    bp = Blueprint("signals", __name__, url_prefix=url_prefix)

    @bp.route("", methods=["GET"])
    @require_auth
    def get_signals():
        """
        External market-intelligence signals available to this deployment.

        Signals reach the platform through the Extraction Agent
        (`extraction.parse` -> `market.score_signal`) and are supplied to the
        orchestrator by a configured `signal_provider`. With no provider
        configured this returns an empty list and says so; it does not serve
        fabricated bulletins attributed to real institutions, as the prototype
        did.
        """
        provider = None
        if orchestrator is not None:
            provider = getattr(orchestrator, "services", {}).get("signal_provider")

        if provider is None:
            return jsonify({
                "signals": [],
                "total": 0,
                "status": "NO_SIGNAL_SOURCE_CONFIGURED",
                "message": (
                    "No external signal source is configured for this "
                    "deployment. Signals appear here once an extraction source "
                    "is connected."
                ),
            }), 200

        try:
            snapshot = orchestrator.snapshots.current()
            signals, warnings = provider(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.exception("signals.provider.failed")
            return jsonify({
                "error": {"code": "SIGNAL_SOURCE_FAILURE", "message": str(exc)}
            }), 502

        return jsonify({
            "signals": [
                s.model_dump(mode="json") if hasattr(s, "model_dump") else dict(s)
                for s in signals
            ],
            "total": len(signals),
            "status": "OK",
            "warnings": list(warnings or []),
        }), 200

    @bp.errorhandler(ApplicationError)
    def _signal_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
