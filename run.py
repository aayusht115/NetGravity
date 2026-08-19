"""
NetGravity — Master Application Launcher
========================================
Launches the NetGravity web application and decision cockpit.

Usage:
    python run.py

Open in browser:
    http://localhost:5050/
"""

import os
import sys

# Ensure repository root is on Python path
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app.backend.app import app

if __name__ == "__main__":
    print("=" * 65)
    print("  NetGravity -- Supply Chain AI Decision Intelligence")
    print("  Starting local server on http://localhost:5050/")
    print("  Press Ctrl+C to stop.")
    print("=" * 65)
    app.run(host="0.0.0.0", port=5050, debug=True)
