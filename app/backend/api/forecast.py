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

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    EngineUnavailableError,
    ValidationError,
)
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
        # No reasoning step ran, so there is no briefing. Empty, not borrowed
        # from the network's — which is what the screen used to do.
        "explanation": {},
        "outlook": {
            "horizon": horizon,
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
        },
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


def create_forecast_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/forecast"):
    bp = Blueprint("forecast", __name__, url_prefix=url_prefix)

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
