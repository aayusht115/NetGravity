"""
NetGravity — Flask Backend (Prototype)
=======================================
Serves the frontend prototype.
In production, this will wrap the netgravity/ MILP engine with clean API endpoints.

Run: python app.py
Open: http://localhost:5050/
"""

import os
from flask import Flask, send_from_directory, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    return send_from_directory(FRONTEND_DIR, path)


# ---------------------------------------------------------------------------
# Future API endpoints (scaffold for production integration)
# These will wrap the netgravity/ Python modules.
# ---------------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
def api_status():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "version": "2.0.0-prototype",
        "engine": "netgravity MILP (PuLP/HiGHS)",
        "mode": "prototype",
    })


# Future endpoints (not yet connected to MILP engine):
# POST /api/optimize/baseline     → netgravity.optimization.baseline.evaluate_baseline()
# POST /api/optimize/scenario     → netgravity.scenarios.engine.ScenarioEngine().run()
# POST /api/sensitivity           → netgravity.sensitivity.engine.SensitivityEngine().run()
# POST /api/resilience            → netgravity.resilience.engine.ResilienceEngine().facility_failure()
# GET  /api/network               → return canonical network data


if __name__ == "__main__":
    print("=" * 60)
    print("  NetGravity — AI Decision Intelligence Platform")
    print("  Open in browser: http://localhost:5050/")
    print("  Mode: Prototype (frontend-only, mock data)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=True)
