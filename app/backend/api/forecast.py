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
    uploaded_signal_store,
)
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

    from netgravity.ingestion.schemas.signal import MarketIntelligenceSignal

    signals: List[Any] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            notes.append(f"uploaded signal {index} is not an object and was skipped")
            continue
        try:
            signals.append(MarketIntelligenceSignal(**row))
        except Exception as exc:  # noqa: BLE001
            title = str(row.get("title") or f"signal {index}")[:60]
            notes.append(
                f"uploaded signal '{title}' could not be read as market "
                f"intelligence and did not reach the forecast: "
                f"{type(exc).__name__}")
    logger.info("forecast.signals.attached snapshot=%s usable=%d unreadable=%d",
                snapshot_id, len(signals), len(notes))
    return signals, notes


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

        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "execution_id": response.execution_id,
            "status": "OK",
            "horizon": horizon,
            "series": series,
            "signals": {
                "attached": len(signals),
                "series_adjusted": adjusted,
                "unreadable": signal_notes,
            },
            "warnings": list(getattr(ctx, "warnings", []) or []) + signal_notes,
            "provenance": {
                "authoritative_source": "netgravity.forecasting",
                "routed_through": "orchestrator capability 'forecast.demand'",
                "llm_used": False,
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
