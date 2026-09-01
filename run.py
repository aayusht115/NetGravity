"""
NetGravity — Master Application Launcher
========================================
Launches the NetGravity web application and decision cockpit.

Usage:
    python run.py

Open in browser:
    http://127.0.0.1:5050/production.html   production console
    http://127.0.0.1:5050/                  approved prototype

Configuration (all optional):
    NETGRAVITY_ENV    development | production   (default: development)
    NETGRAVITY_HOST   bind address               (default: 127.0.0.1)
    NETGRAVITY_PORT   port                       (default: 5050)
    NETGRAVITY_DEBUG  1 to enable the reloader   (default: 0, ignored in production)
"""

import os
import sys

# Ensure repository root is on Python path
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.backend.app import ENV, IS_PRODUCTION, app

if __name__ == "__main__":
    # This launcher previously hardcoded `host="0.0.0.0", debug=True`, which
    # overrode the safe defaults in app/backend/app.py and exposed the Werkzeug
    # debug console on every network interface. It now defers to the same
    # environment configuration the application module uses.
    host = os.environ.get("NETGRAVITY_HOST", "127.0.0.1")
    port = int(os.environ.get("NETGRAVITY_PORT", "5050"))
    debug = (not IS_PRODUCTION) and os.environ.get("NETGRAVITY_DEBUG", "0") == "1"

    print("=" * 65)
    print("  NetGravity — Supply Chain AI Decision Intelligence")
    print(f"  Environment : {ENV}")
    print(f"  Production console : http://{host}:{port}/production.html")
    print(f"  Prototype          : http://{host}:{port}/")
    print("  Press Ctrl+C to stop.")
    print("=" * 65)
    app.run(host=host, port=port, debug=debug)
