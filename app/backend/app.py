"""
NetGravity — Flask Application
==============================
Serves the frontend and the production API surface.

Run from repository root:
    python run.py
Open in browser: http://localhost:5050/

Phase 10.0 changes
------------------
The prototype entrypoint built ONE process-global orchestrator at import time
from `build_case16_network()` — a fixture annotated in this very file as
"FABRICATED demonstration data" — and every project in the application pointed
at it. Ingested customer networks, though fully assembled by
`netgravity/ingestion/`, were never registered with the engine.

The synthetic network is still loaded, but now as an explicitly-labelled *demo
project* rather than the implicit default that real projects inherit. Real
projects bind their own snapshot after ingestion, via
`app.backend.services.project_registry`, using the orchestrator's existing
`SnapshotManager` — which was always multi-snapshot capable and always honoured
a per-request `network_snapshot_id`.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# Explicit MIME types: Windows' registry maps .js to text/plain, which breaks
# ES module loading in the browser.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)

# ---------------------------------------------------------------------------
# Environment configuration (brief §26)
# ---------------------------------------------------------------------------
ENV = os.environ.get("NETGRAVITY_ENV", "development").strip().lower()
IS_PRODUCTION = ENV == "production"

# Comma-separated allowlist. Wide-open CORS is a development-only convenience;
# the prototype applied `CORS(app)` unconditionally.
_CORS_ORIGINS = [
    o.strip() for o in os.environ.get("NETGRAVITY_CORS_ORIGINS", "").split(",") if o.strip()
]

app = Flask(__name__)

if IS_PRODUCTION:
    if not _CORS_ORIGINS:
        # Same-origin only. The frontend is served by this app, so this is the
        # correct default rather than a limitation.
        logger.info("CORS: same-origin only (no NETGRAVITY_CORS_ORIGINS set)")
    else:
        CORS(app, origins=_CORS_ORIGINS, supports_credentials=True)
else:
    CORS(app, origins=_CORS_ORIGINS or "*")

# Cap request bodies before Flask buffers them.
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("NETGRAVITY_MAX_UPLOAD_BYTES", 64 * 1024 * 1024)
)

_ORCHESTRATOR_STATUS = {"mounted": False, "reason": "not initialised"}
_INGESTION_STATUS = {"mounted": False, "reason": "not initialised"}
_APP_API_STATUS = {"mounted": False, "reason": "not initialised"}
_DURABILITY_STATUS = {"enabled": False, "reason": "not initialised"}
_orchestrator = None


# ---------------------------------------------------------------------------
# Storage, checked before anything else
# ---------------------------------------------------------------------------
# A configured-but-unreachable database is refused rather than fallen back on:
# a process that runs, looks healthy, and writes a user's work to an unbacked
# local file is worse than one that will not start. Checked HERE so the reason
# is reported once, at the top, instead of surfacing as "the application API
# failed to mount" thirty seconds later.
try:
    from app.backend.services.persistence import database as _database

    _STORAGE = {
        "engine": _database.kind,
        "target": _database.path,
        "schema_version": _database.schema_version,
        "migrations_applied_now": _database.migrations_applied,
    }
    logger.info("storage engine=%s target=%s schema=v%s",
                _database.kind, _database.path, _database.schema_version)

    # Rate-limit counters move to the shared store only once the database has
    # answered and its schema is known to carry the table. Switching at import
    # would make the limiter raise on a fresh database and take down the very
    # endpoints it protects.
    try:
        from app.backend.services.ratelimit import limiter as _limiter
        _limiter.use_shared_store()
        _STORAGE["rate_limit_counters"] = "shared"
    except Exception as exc:  # noqa: BLE001
        _STORAGE["rate_limit_counters"] = f"per-process ({type(exc).__name__})"
        logger.warning("rate limit counters remain per-process: %s", exc)

    if _database.kind == "sqlite":
        logger.warning(
            "Running on SQLite (%s). It has one writer: fine for a single "
            "process, wrong for two. Set NETGRAVITY_DATABASE_URL for PostgreSQL.",
            _database.path,
        )
except Exception as exc:  # noqa: BLE001
    _STORAGE = {"engine": None, "error": f"{type(exc).__name__}: {exc}"}
    logger.error("STORAGE UNAVAILABLE — %s", exc)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
@app.route("/")
def serve_index():
    return send_from_directory(FRONTEND_DIR, "index.html", mimetype="text/html")


@app.route("/<path:path>")
def serve_static(path):
    mimetype, _ = mimetypes.guess_type(path)
    if path.endswith(".js"):
        mimetype = "application/javascript"
    elif path.endswith(".css"):
        mimetype = "text/css"
    # send_from_directory rejects traversal outside FRONTEND_DIR.
    return send_from_directory(FRONTEND_DIR, path, mimetype=mimetype)


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------
# The application shipped with none. That mattered most for the Content
# Security Policy: the session token was in `localStorage` precisely because
# nothing stopped injected script from running, and moving it to an httpOnly
# cookie removes the prize without removing the injection. A CSP is what
# addresses the injection itself.
#
# The policy is tight but honest about what this page actually does:
#   * `script-src 'self'` — every module is served from this origin, and there
#     is NO 'unsafe-inline'. That required real work: four libraries were
#     loaded from three third-party CDNs and are now vendored under
#     `frontend/vendor/`, one inline block became `js/landing-bootstrap.js`,
#     and thirty-five inline `onclick` attributes became `data-action`
#     attributes dispatched through an allowlist in `js/actions.js`. Inline
#     handlers cannot be nonced, so none of this was optional.
#   * `'unsafe-eval'` is NOT granted. Nothing here compiles strings.
#   * `img-src` allows `data:` because the India basemap is embedded as a data
#     URI, which is the change that removed the third-party tile CDN.
#   * `connect-src 'self'` — the browser may not send data anywhere else, which
#     is the control that turns an exfiltration into a blocked request.
#   * `frame-ancestors 'none'` — clickjacking, and it supersedes X-Frame-Options
#     in every browser that implements CSP.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


@app.after_request
def _security_headers(response):
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # No camera, microphone, geolocation or payment surface exists here; saying
    # so removes them from anything embedded too.
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if IS_PRODUCTION:
        # Only in production: sent over plain HTTP in development it would pin
        # a developer's browser to HTTPS on localhost for a year.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains")
    # An API response must never be cached by an intermediary: it is
    # per-account data behind a cookie.
    if request.path.startswith("/api/") or request.path.startswith("/orchestrator/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.route("/api/status", methods=["GET"])
def api_status():
    """Health check. Public by design — carries no customer data."""
    return jsonify({
        "status": "ok",
        "version": "2.0.0",
        "environment": ENV,
        "engine": "netgravity MILP (PuLP/HiGHS)",
        "orchestrator": _ORCHESTRATOR_STATUS,
        "ingestion": _INGESTION_STATUS,
        "application_api": _APP_API_STATUS,
        # Stated explicitly, because "does this survive a restart" is the
        # single question that decides whether this is a demo or a system.
        "durability": _DURABILITY_STATUS,
        # Which store, resolved before anything mounts. Present even when the
        # rest of the application failed to start because of it.
        "storage": _STORAGE,
        # A deployment is misconfigured if resets cannot actually be delivered,
        # and that is invisible until someone needs one — so it is stated here
        # rather than discovered on the day it matters.
        "reset_delivery": _reset_delivery_status(),
    })


def _reset_delivery_status():
    try:
        from app.backend.services.notifications import password_reset_delivery
        return password_reset_delivery.describe()
    except Exception as exc:  # noqa: BLE001
        return {"channel": None, "configured": False, "reason": str(exc)}


# ---------------------------------------------------------------------------
# Orchestrator control plane
# ---------------------------------------------------------------------------
try:
    from app.backend.services.demand_history_store import demand_history_store
    from netgravity.forecasting.history import load_staging_history
    from netgravity.ingestion.config import IngestionConfig
    from netgravity.orchestrator import build_orchestrator
    from netgravity.orchestrator.api import create_orchestrator_blueprint

    def _history_provider(snapshot):
        """
        Observed demand history for a snapshot.

        Two sources, in order:

        1. History that arrived with this network's own upload, held by
           `demand_history_store`. This is the path a user's workbook takes —
           it was the missing link, so an uploaded network with 36 months of
           history still reported "no observed demand history" and the
           forecast screen stayed empty.
        2. The ingestion staging zone on disk, for networks ingested through
           the batch pipeline.

        `load_staging_history` treats a missing directory as "nothing ingested
        yet" and returns a warning, not an exception.
        """
        series, warnings = demand_history_store.for_snapshot(snapshot)
        if series:
            return series, []
        try:
            staging = IngestionConfig().standardized_path
            staged, staged_warnings = load_staging_history(Path(staging))
            return staged, list(warnings) + list(staged_warnings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("history_provider failed: %s", exc)
            return [], list(warnings) + [f"history unavailable: {exc}"]

    _orchestrator = build_orchestrator(history_provider=_history_provider)

    def _orchestrator_actor():
        """
        The signed-in user, as the control plane's actor.

        `/orchestrator/*` was mounted with no authentication of any kind. Every
        endpoint under it — run a solve, read any Digital Twin state, read any
        execution's full decision trace, approve a governed action — was open
        to anyone who could reach the process. This binds it to the same
        session the rest of the API uses, and derives the actor's ROLE from the
        stored account rather than from the request body.
        """
        from app.backend.api.auth import bearer_actor
        return bearer_actor()

    app.register_blueprint(create_orchestrator_blueprint(
        _orchestrator, authenticator=_orchestrator_actor))

    _ORCHESTRATOR_STATUS = {
        "mounted": True,
        "url_prefix": "/orchestrator",
        "capabilities": len(_orchestrator.capabilities()),
        "llm_available": _orchestrator.health()["llm"].get("available", False),
    }
except Exception as exc:  # noqa: BLE001
    logger.exception("orchestrator mount failed")
    _ORCHESTRATOR_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Canonical ingestion pipeline (the real one)
# ---------------------------------------------------------------------------
try:
    from netgravity.ingestion.api import create_ingestion_blueprint

    app.register_blueprint(create_ingestion_blueprint())
    _INGESTION_STATUS = {"mounted": True, "url_prefix": "/api/ingestions"}
except Exception as exc:  # noqa: BLE001
    logger.exception("ingestion mount failed")
    _INGESTION_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Application API
# ---------------------------------------------------------------------------
try:
    from app.backend.api.auth import auth_bp
    from app.backend.api.forecast import create_forecast_blueprint, create_signals_blueprint
    from app.backend.api.insights import create_insights_blueprint
    from app.backend.api.oidc import oidc_bp
    from app.backend.api.ingestion_dynamic import ingestion_dynamic_bp
    from app.backend.api.kpis import create_kpi_blueprint
    from app.backend.api.network_structure import create_network_structure_blueprint
    from app.backend.api.projects import projects_bp
    from app.backend.api.scenarios import create_scenario_blueprint
    from app.backend.services.project_registry import project_registry
    from app.backend.services.security import register_error_handler

    project_registry._orchestrator = _orchestrator  # bind engine to registry

    app.register_blueprint(auth_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(ingestion_dynamic_bp)
    app.register_blueprint(create_kpi_blueprint(_orchestrator))
    app.register_blueprint(create_scenario_blueprint(_orchestrator))
    app.register_blueprint(create_forecast_blueprint(_orchestrator))
    app.register_blueprint(create_insights_blueprint(_orchestrator))
    app.register_blueprint(oidc_bp)
    app.register_blueprint(create_signals_blueprint(_orchestrator))
    app.register_blueprint(create_network_structure_blueprint(_orchestrator))

    register_error_handler(app)

    # Connect every store to durable storage and reload what is already there.
    #
    # Before the first request, and before the demo workspace is seeded, so a
    # restored project that happens to share the demo's snapshot is not
    # overwritten by the re-seed.
    #
    # Until this existed, the whole application layer was process-local:
    # restarting the server discarded every account, session, project, uploaded
    # network and solved scenario. A user came back to a sign-up form.
    from app.backend.services import durability

    try:
        _DURABILITY_STATUS = durability.install(_orchestrator)
    except Exception as exc:  # noqa: BLE001
        # The application still runs, in memory, and says so — rather than
        # starting up while quietly appearing to be durable.
        logger.exception("durable storage unavailable")
        _DURABILITY_STATUS = {
            "enabled": False,
            "reason": f"{type(exc).__name__}: {exc}",
            "warning": "Running WITHOUT durable storage: a restart will lose "
                       "accounts, projects, uploads and scenarios.",
        }
    else:
        _DURABILITY_STATUS["enabled"] = True

    # Seed the bundled synthetic network as an explicitly-labelled demo
    # workspace. It is the only network available offline, so it stays — but it
    # is now named as synthetic in the project record itself, flagged
    # `is_demo`, and is no longer what a freshly-created project silently
    # inherits.
    demo_seeded = False
    if _orchestrator is not None and os.environ.get("NETGRAVITY_SEED_DEMO", "1") == "1":
        try:
            from netgravity.tests.fixtures.case16_synthetic import build_case16_network

            record = project_registry.seed_demo_project(build_case16_network())
            demo_seeded = record is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("demo project seeding skipped: %s", exc)

    _APP_API_STATUS = {
        "mounted": True,
        # Read off the app, not written by hand.
        #
        # This was a hardcoded literal, and it was already wrong: it named
        # "ingestion_preview", which is not a blueprint on this app, and it
        # omitted every blueprint added after it was written — so a health
        # endpoint whose job is to report what is mounted was reporting what
        # someone remembered mounting. A new blueprint answering 404 looked, on
        # this endpoint, exactly like one that was mounted and working.
        "blueprints": sorted(app.blueprints),
        "demo_project_seeded": demo_seeded,
    }
except Exception as exc:  # noqa: BLE001
    logger.exception("application API mount failed")
    _APP_API_STATUS = {"mounted": False, "reason": f"{type(exc).__name__}: {exc}"}


def create_app():
    """Application factory, for tests and WSGI servers."""
    return app


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Debug is opt-in and never on in production. The prototype hardcoded
    # `debug=True, host="0.0.0.0"`, which exposes the Werkzeug console.
    debug = (not IS_PRODUCTION) and os.environ.get("NETGRAVITY_DEBUG", "0") == "1"
    host = os.environ.get("NETGRAVITY_HOST", "127.0.0.1")
    port = int(os.environ.get("NETGRAVITY_PORT", "5050"))

    print("=" * 62)
    print("  NetGravity — AI Decision Intelligence Platform")
    print(f"  Environment : {ENV}")
    print(f"  Frontend    : {FRONTEND_DIR}")
    print(f"  Listening   : http://{host}:{port}/")
    print("=" * 62)
    app.run(host=host, port=port, debug=debug)
