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
    })


if __name__ == "__main__":
    print("=" * 60)
    print("  NetGravity — AI Decision Intelligence Platform")
    print("  Serving frontend from:", FRONTEND_DIR)
    print("  Open in browser: http://localhost:5050/")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=True)
