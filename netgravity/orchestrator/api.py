"""
Orchestrator — HTTP service interface.

A Flask blueprint exposing the control plane. API schemas are the Pydantic
models in `orchestrator/schemas/`, kept separate from internal execution
classes, so the wire format can evolve without touching the core.

Endpoints
─────────
    POST /orchestrator/run                  execute a request
    POST /orchestrator/chat                 conversational message
    GET  /orchestrator/chat/<id>/history    conversation turns
    POST /orchestrator/approvals/<id>       approve or reject a pending action
    GET  /orchestrator/executions/<id>      execution status
    GET  /orchestrator/executions/<id>/trace   full audit provenance
    GET  /orchestrator/twin/states          published Digital Twin states
    GET  /orchestrator/twin/states/<id>     one state, flows paginated
    GET  /orchestrator/twin/snapshots/<id>  state for a snapshot/scenario
    GET  /orchestrator/twin/compare         baseline vs scenario
    POST /orchestrator/insights             executive/network/node/lane insight
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

    from netgravity.orchestrator.twin import DEFAULT_FLOW_LIMIT, TwinStateNotFound

    bp = Blueprint("orchestrator", __name__, url_prefix=url_prefix)

    def _error(exc: OrchestratorError, status: int = 400):
        return jsonify({"error": exc.to_dict()}), status

    def _int_arg(name: str, default: int) -> int:
        """A malformed paging argument falls back rather than 500-ing."""
        try:
            return int(request.args[name])
        except (KeyError, TypeError, ValueError):
            return default

    def _bool_arg(name: str, default: bool) -> bool:
        raw = request.args.get(name)
        if raw is None:
            return default
        return raw.strip().lower() not in ("false", "0", "no")

    # One chat service per blueprint, built lazily so conversation history
    # survives across requests without constructing it when chat is unused.
    _chat_holder: Dict[str, Any] = {}

    def _chat_service():
        if "service" not in _chat_holder:
            from netgravity.orchestrator.conversation import ChatService
            _chat_holder["service"] = ChatService(orchestrator)
        return _chat_holder["service"]

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
    # Conversational surface (Phase 3)
    # ------------------------------------------------------------------

    @bp.route("/chat", methods=["POST"])
    def chat():
        """
        Send one conversational message.

        Body::

            {
              "message": "What happens if we reduce Delhi capacity by 2,000?",
              "conversation_id": "conv_...",   // optional; created if absent
              "network_snapshot_id": "snap_...", // optional
              "disable_llm": false               // optional
            }

        Status reflects the OUTCOME KIND, as elsewhere in this API:
        200 answered, 202 awaiting clarification or a human, 500 controlled
        failure. A clarification is a 202 because the exchange is unfinished,
        not because anything went wrong.
        """
        from netgravity.orchestrator.schemas.conversation import ChatRequest

        body: Dict[str, Any] = request.get_json(silent=True) or {}
        try:
            chat_request = ChatRequest(**body)
        except Exception as exc:  # noqa: BLE001 - malformed client input
            return jsonify({"error": {
                "code": "INVALID_REQUEST",
                "message": f"Malformed chat request: {exc}",
            }}), 400

        service = _chat_service()
        response = service.chat(chat_request)

        status_code = 200
        if response.status in ("AWAITING_CLARIFICATION", "REQUIRES_APPROVAL",
                               "REQUIRES_HUMAN"):
            status_code = 202
        elif response.status == "FAILED":
            status_code = 500
        return jsonify(response.model_dump(mode="json")), status_code

    @bp.route("/chat/<conversation_id>/history", methods=["GET"])
    def chat_history(conversation_id: str):
        service = _chat_service()
        turns = service.history(conversation_id)
        if not turns and service.store.get(conversation_id) is None:
            return jsonify({"error": {
                "code": "NOT_FOUND",
                "message": f"Conversation '{conversation_id}' not found.",
            }}), 404
        return jsonify({
            "conversation_id": conversation_id,
            "turns": [t.model_dump(mode="json") for t in turns],
        }), 200

    # ------------------------------------------------------------------
    # Digital Twin (Phase 5)
    # ------------------------------------------------------------------

    @bp.route("/twin/states", methods=["GET"])
    def twin_states():
        """
        List published Digital Twin states.

        Query: ``?snapshot_id=snap_...`` to filter.

        Returns handles, not payloads — a state grows with the network, and a
        listing must not.
        """
        snapshot_id = request.args.get("snapshot_id")
        refs = orchestrator.twin.list_states(snapshot_id)
        return jsonify({
            "snapshot_id": snapshot_id,
            "states": [r.model_dump(mode="json") for r in refs],
        }), 200

    @bp.route("/twin/states/<state_id>", methods=["GET"])
    def twin_state(state_id: str):
        """
        Read one state by id.

        Query:
            ``flow_offset`` / ``flow_limit``  page the lane set
                (``flow_limit=0`` returns every lane)
            ``include_flows=false``           aggregate only — the cheap path
                for a summary view of a large network
        """
        try:
            view = orchestrator.twin.get_by_id(
                state_id,
                flow_offset=_int_arg("flow_offset", 0),
                flow_limit=_int_arg("flow_limit", DEFAULT_FLOW_LIMIT),
                include_flows=_bool_arg("include_flows", True),
            )
        except TwinStateNotFound as exc:
            return jsonify({"error": {"code": "NOT_FOUND", "message": str(exc)}}), 404
        return jsonify(view.model_dump(mode="json")), 200

    @bp.route("/twin/snapshots/<snapshot_id>", methods=["GET"])
    def twin_snapshot_state(snapshot_id: str):
        """
        Read the state for a snapshot, optionally a scenario on it.

        Query: ``?scenario_id=scn_...``, plus the same paging arguments.
        """
        try:
            view = orchestrator.twin.get(
                snapshot_id,
                request.args.get("scenario_id"),
                flow_offset=_int_arg("flow_offset", 0),
                flow_limit=_int_arg("flow_limit", DEFAULT_FLOW_LIMIT),
                include_flows=_bool_arg("include_flows", True),
            )
        except TwinStateNotFound as exc:
            return jsonify({"error": {"code": "NOT_FOUND", "message": str(exc)}}), 404
        return jsonify(view.model_dump(mode="json")), 200

    @bp.route("/twin/compare", methods=["GET"])
    def twin_compare():
        """
        Compare two states.

        Either ``?baseline=<state_id>&comparison=<state_id>``, or the common
        form ``?snapshot_id=...&scenario_id=...`` which compares a scenario
        against its own baseline.
        """
        baseline = request.args.get("baseline")
        comparison = request.args.get("comparison")
        snapshot_id = request.args.get("snapshot_id")
        scenario_id = request.args.get("scenario_id")

        try:
            if baseline and comparison:
                result = orchestrator.twin.compare(baseline, comparison)
            elif snapshot_id and scenario_id:
                result = orchestrator.twin.compare_scenario(snapshot_id, scenario_id)
            else:
                return jsonify({"error": {
                    "code": "INVALID_REQUEST",
                    "message": ("Supply either baseline= and comparison=, or "
                                "snapshot_id= and scenario_id=."),
                }}), 400
        except TwinStateNotFound as exc:
            return jsonify({"error": {"code": "NOT_FOUND", "message": str(exc)}}), 404
        return jsonify(result.model_dump(mode="json")), 200

    # ------------------------------------------------------------------
    # Read-only Reasoning Agent surface
    # ------------------------------------------------------------------

    @bp.route("/insights", methods=["POST"])
    def insights():
        """
        Explain one already-published Digital Twin state.

        Body::

            {
              "state_id": "tws_...",
              "scope": "NETWORK" | "FACILITY" | "LANE" | "COMPARISON",
              "entity_id": "DC_DELHI" | "DC_DELHI->MKT_NORTH", // scoped views
              "comparison_state_id": "tws_...",                 // comparison
              "question": "Why is this route important?",
              "disable_llm": false
            }

        This endpoint is advisory and read-only. It cannot run an optimization,
        mutate the twin, classify an action, or bypass numeric grounding.
        """
        from netgravity.orchestrator.reasoning.evidence import twin_reasoning_payload
        from netgravity.orchestrator.schemas.reasoning import InsightRequest

        body: Dict[str, Any] = request.get_json(silent=True) or {}
        try:
            insight_request = InsightRequest(**body)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": {
                "code": "INVALID_REQUEST",
                "message": f"Malformed insight request: {exc}",
            }}), 400

        try:
            state = orchestrator.twin.materialize(insight_request.state_id)
            comparison = None
            if insight_request.comparison_state_id:
                comparison = orchestrator.twin.compare(
                    insight_request.state_id,
                    insight_request.comparison_state_id,
                )
            payload = twin_reasoning_payload(
                state,
                scope=insight_request.scope,
                entity_id=insight_request.entity_id,
                comparison=comparison,
            )
        except TwinStateNotFound as exc:
            return jsonify({"error": {"code": "NOT_FOUND", "message": str(exc)}}), 404
        except ValueError as exc:
            return jsonify({"error": {
                "code": "INVALID_ENTITY",
                "message": str(exc),
            }}), 400

        unavailable = {
            item.field: {"status": item.status.value, "reason": item.reason}
            for item in state.unavailable
        }
        reasoning_agent = orchestrator.services["reasoning_agent"]
        result = reasoning_agent.reason(
            payload,
            unavailable_evidence=unavailable,
            provenance={
                "state_id": state.state_id,
                "snapshot_id": state.snapshot_id,
                "scenario_id": state.scenario_id or "",
            },
            allow_llm=not insight_request.disable_llm,
            scope=insight_request.scope,
            entity_id=insight_request.entity_id,
            user_question=insight_request.question,
        )
        return jsonify({
            "state_id": state.state_id,
            "snapshot_id": state.snapshot_id,
            "scenario_id": state.scenario_id,
            "scope": insight_request.scope.value,
            "entity_id": insight_request.entity_id,
            "reasoning": result.model_dump(mode="json"),
        }), 200

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
