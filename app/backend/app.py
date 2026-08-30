"""
NetGravity — Flask Backend Server
==================================
Serves the Decision Intelligence Platform frontend and API endpoints.

Run from repository root:
    python run.py
Or from backend directory:
    python app.py

Open in browser: http://localhost:5050/
"""

import os
import mimetypes
from flask import Flask, send_from_directory, jsonify
from flask_cors import CORS

# Explicitly ensure correct MIME types across Windows / Linux environments
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

app = Flask(__name__)
CORS(app)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html", mimetype="text/html")


@app.route("/<path:path>")
def serve_static(path):
    # Resolve MIME type explicitly to prevent Windows registry text/plain issues on ES modules
    mimetype, _ = mimetypes.guess_type(path)
    if path.endswith(".js"):
        mimetype = "application/javascript"
    elif path.endswith(".css"):
        mimetype = "text/css"
    return send_from_directory(FRONTEND_DIR, path, mimetype=mimetype)


# ---------------------------------------------------------------------------
# API Endpoints & Health Check
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "version": "2.0.0",
        "engine": "netgravity MILP (PuLP/HiGHS)",
        "mode": "interactive",
        "orchestrator": _ORCHESTRATOR_STATUS,
        "ingestion": _INGESTION_STATUS,
        "action_agent": _ACTION_AGENT_STATUS,
    })


# ---------------------------------------------------------------------------
# Orchestrator control plane (optional mount)
# ---------------------------------------------------------------------------
# Mounted best-effort so the existing static/API behaviour is unchanged if the
# orchestrator cannot start. Endpoints live under /orchestrator/*.
#
# The LLM gateway reads TEXT_API_URL / TEXT_API_TOKEN from the environment.
# With no token configured the control plane still runs, using rule-based
# intent parsing and template reasoning; deterministic results are identical.

_ORCHESTRATOR_STATUS = {"mounted": False, "reason": "not initialised"}

try:
    from netgravity.orchestrator import build_orchestrator
    from netgravity.orchestrator.api import create_orchestrator_blueprint
    from netgravity.tests.fixtures.case16_synthetic import build_case16_network

    # NOTE: the Case-16 synthetic fixture is FABRICATED demonstration data.
    # Replace this with the real observed network before any production use.
    _orchestrator = build_orchestrator(network=build_case16_network())
    app.register_blueprint(create_orchestrator_blueprint(_orchestrator))
    _ORCHESTRATOR_STATUS = {
        "mounted": True,
        "url_prefix": "/orchestrator",
        "capabilities": len(_orchestrator.capabilities()),
        "llm_available": _orchestrator.health()["llm"].get("available", False),
        "network_source": "case16_synthetic (FABRICATED demo data)",
    }
except Exception as exc:  # noqa: BLE001 - never block the existing app
    _ORCHESTRATOR_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Data-ingestion console API
# ---------------------------------------------------------------------------
# Separate from the orchestrator control plane: uploads and unresolved client
# fields have their own lifecycle before a trusted snapshot exists.

_INGESTION_STATUS = {"mounted": False, "reason": "not initialised"}
try:
    from netgravity.ingestion.api import create_ingestion_blueprint

    app.register_blueprint(create_ingestion_blueprint())
    _INGESTION_STATUS = {
        "mounted": True,
        "url_prefix": "/api/ingestions",
    }
except Exception as exc:  # noqa: BLE001 - static demo must remain available
    _INGESTION_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Action Agent API (inbound-email webhook)
# ---------------------------------------------------------------------------
# A dispatcher, not a decision-maker — see netgravity/action_agent/. Runs in
# stub mode with no outbound email credential configured; the inbound
# webhook route exists regardless, since it only ever receives, never sends
# on its own initiative.

_ACTION_AGENT_STATUS = {"mounted": False, "reason": "not initialised"}
try:
    from netgravity.action_agent.api import create_action_agent_blueprint

    app.register_blueprint(create_action_agent_blueprint())
    _ACTION_AGENT_STATUS = {
        "mounted": True,
        "url_prefix": "/api",
    }
except Exception as exc:  # noqa: BLE001 - static demo must remain available
    _ACTION_AGENT_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Deep-link placeholder pages (PATCH POINT — see the module docstring)
# ---------------------------------------------------------------------------
# Gives Action Agent emails' links somewhere real to land before the
# frontend owns /ingestion/<id>/review and /insights/<id>. Remove this block
# (or point NETGRAVITY_APP_BASE_URL at the frontend instead) once it does.

try:
    from netgravity.action_agent.deep_link_placeholder import (
        create_deep_link_placeholder_blueprint,
    )

    app.register_blueprint(create_deep_link_placeholder_blueprint())
except Exception as exc:  # noqa: BLE001 - static demo must remain available
    print(f"deep-link placeholder not mounted: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    print("=" * 60)
    print("  NetGravity — AI Decision Intelligence Platform")
    print("  Serving frontend from:", FRONTEND_DIR)
    print("  Open in browser: http://localhost:5050/")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=True)
