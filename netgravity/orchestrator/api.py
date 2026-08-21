"""
Orchestrator — HTTP service interface.

A Flask blueprint exposing the control plane. API schemas are the Pydantic
models in `orchestrator/schemas/`, kept separate from internal execution
classes, so the wire format can evolve without touching the core.

Endpoints
─────────
    POST /orchestrator/run                  execute a request
    POST /orchestrator/approvals/<id>       approve or reject a pending action
    GET  /orchestrator/executions/<id>      execution status
    GET  /orchestrator/executions/<id>/trace   full audit provenance
    GET  /orchestrator/capabilities         registered capability catalogue
    GET  /orchestrator/workflows            available workflow templates
    GET  /orchestrator/health               control-plane health

Mount with::

    from netgravity.orchestrator.api import create_orchestrator_blueprint
    app.register_blueprint(create_orchestrator_blueprint(orchestrator))
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.exceptions import OrchestratorError
from netgravity.orchestrator.schemas.requests import Actor, ActorRole, OrchestratorRequest

logger = logging.getLogger(__name__)


def create_orchestrator_blueprint(orchestrator: Orchestrator, url_prefix: str = "/orchestrator"):
    """
    Build the Flask blueprint.

    Imported lazily so the orchestrator package stays usable without Flask.
    """
    from flask import Blueprint, jsonify, request

    bp = Blueprint("orchestrator", __name__, url_prefix=url_prefix)

    def _error(exc: OrchestratorError, status: int = 400):
        return jsonify({"error": exc.to_dict()}), status

    # ------------------------------------------------------------------
    @bp.route("/run", methods=["POST"])
    def run():
        """
        Execute an orchestrator request.

        Body::

            {
              "input": "What happens if we close Delhi DC?",
              "actor": {"actor_id": "u1", "role": "PLANNER"},
              "network_snapshot_id": "snap_...",     // optional
              "disable_llm": false                    // optional
            }

        The HTTP status reflects the OUTCOME KIND, not merely success:
        200 completed, 202 awaiting a human, 409 infeasible or stale, 500 failed.
        """
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        try:
            payload = dict(body)
            actor_raw = payload.pop("actor", None)
            if isinstance(actor_raw, dict):
                payload["actor"] = Actor(**actor_raw)
            elif isinstance(actor_raw, str):
                payload["actor"] = Actor(actor_id=actor_raw, role=ActorRole.VIEWER)
            req = OrchestratorRequest(**payload)
        except Exception as exc:  # noqa: BLE001 - malformed client input
            return jsonify({"error": {
                "code": "INVALID_REQUEST",
                "message": f"Malformed request body: {exc}",
            }}), 400

        response = orchestrator.run_sync(req)

        status_map = {
            "COMPLETED": 200,
            "REQUIRES_APPROVAL": 202,
            "REQUIRES_HUMAN": 202,
            "INFEASIBLE": 409,
            "STALE": 409,
            "CANCELLED": 200,
            "FAILED": 500,
        }
        return jsonify(response.model_dump(mode="json")), status_map.get(response.status, 200)

    # ------------------------------------------------------------------
    @bp.route("/approvals/<approval_id>", methods=["POST"])
    def decide_approval(approval_id: str):
        """
        Record a human approval decision and resume the original execution.

        Body: ``{"approved": true, "actor": {...}, "note": "..."}``
        """
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        actor_raw = body.get("actor") or {}
        try:
            actor = Actor(**actor_raw) if isinstance(actor_raw, dict) else Actor()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": {"code": "INVALID_REQUEST",
                                      "message": f"Malformed actor: {exc}"}}), 400

        try:
            response = orchestrator.resolve_approval(
                approval_id,
                actor=actor,
                approved=bool(body.get("approved", False)),
                note=str(body.get("note", "")),
            )
        except OrchestratorError as exc:
            return _error(exc, 403 if exc.code.value == "AUTHORIZATION_FAILURE" else 404)

        return jsonify(response.model_dump(mode="json")), 200

    # ------------------------------------------------------------------
    @bp.route("/executions/<execution_id>", methods=["GET"])
    def get_execution(execution_id: str):
        context = orchestrator.state_store.get(execution_id)
        if context is None:
            return jsonify({"error": {"code": "NOT_FOUND",
                                      "message": f"Execution '{execution_id}' not found."}}), 404
        return jsonify({
            "execution_id": context.execution_id,
            "request_id": context.request_id,
            "status": context.current_state.value,
            "intent": context.intent.value,
            "workflow_id": context.workflow_id,
            "snapshot_id": context.baseline_snapshot_id,
            "scenario_ids": context.scenario_ids,
            "is_hypothetical": context.is_hypothetical,
            "completed_steps": context.completed_steps,
            "failed_steps": context.failed_steps,
            "errors": context.errors,
        }), 200

    # ------------------------------------------------------------------
    @bp.route("/executions/<execution_id>/trace", methods=["GET"])
    def get_trace(execution_id: str):
        """Full decision provenance: why this recommendation, from which data."""
        trace = orchestrator.get_trace(execution_id)
        if trace is None:
            return jsonify({"error": {"code": "NOT_FOUND",
                                      "message": f"No trace for '{execution_id}'."}}), 404
        if request.args.get("format") == "text":
            return trace.explain(), 200, {"Content-Type": "text/plain; charset=utf-8"}
        return jsonify(trace.to_dict()), 200

    # ------------------------------------------------------------------
    @bp.route("/capabilities", methods=["GET"])
    def capabilities():
        return jsonify({"capabilities": orchestrator.capabilities()}), 200

    @bp.route("/workflows", methods=["GET"])
    def workflows():
        return jsonify({"workflows": orchestrator.workflows()}), 200

    @bp.route("/health", methods=["GET"])
    def health():
        return jsonify(orchestrator.health()), 200

    return bp
