"""
NetGravity — Authoritative KPI API Blueprint
============================================
Network and facility KPIs derived exclusively from the Phase 9.1 `KPIRegistry`
and `AuthoritativeEvidencePackage`.

Phase 10.0 changes. The authority chain in this blueprint was already correct —
it is the one application endpoint the forensic audit found sound — so its
computation path is untouched. What changed:

  * every route is authenticated and project-scoped, so a caller cannot read
    KPIs for a project they do not own;
  * the execution context is keyed by snapshot and given a TTL. It was
    previously cached forever under a `"default"` key and never invalidated,
    so KPIs silently went stale after any state change (brief §20);
  * the `AuthoritativeEvidencePackage` built in Phase 9.1 finally gets an HTTP
    surface, closing gap P2-2.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.analysis_store import analysis_service, serialise_analysis
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.metrics.registry import KPIRegistry
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

def create_kpi_blueprint(orchestrator: Optional[Orchestrator] = None,
                         url_prefix: str = "/api/kpis"):
    bp = Blueprint("kpis", __name__, url_prefix=url_prefix)
    registry = KPIRegistry()

    def _scoped_analysis():
        """
        (project_id, snapshot_id, analysis) for this request.

        The analysis is computed once per network version and kept — see
        `app.backend.services.analysis_store`. This used to cache an
        `ExecutionContext` for 120 seconds, written after the solve returned,
        so the five KPI endpoints a single page opens each started their own
        MILP solve of the same network, and did it again two minutes later.

        Raises NO_NETWORK_BOUND (409) when the project has no ingested network —
        the honest answer, rather than falling back to the bundled synthetic
        network as the prototype did.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The analysis engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")

        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id
        )
        snapshot = orchestrator.snapshots.get(snapshot_id)
        user_id = g.current_user.user_id

        def compute() -> Dict[str, Any]:
            # The intent is stated explicitly rather than left to free-text
            # classification. Passing prose here made the deterministic NLU
            # return REQUIRES_HUMAN with zero capabilities executed, so every
            # KPI came back unavailable — honest, but useless.
            # NETWORK_STATE_QUERY is the intent whose workflow genuinely runs a
            # solve, which is what a KPI baseline is.
            req = OrchestratorRequest(
                input="Authoritative network KPI baseline execution",
                explicit_intent=Intent.NETWORK_STATE_QUERY,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
            )
            response = orchestrator.run_sync(req)
            ctx = orchestrator.get_execution_state(response.execution_id)
            if ctx is None:
                raise EngineUnavailableError(
                    "The baseline execution produced no context, so no KPI can "
                    "be reported."
                )
            return serialise_analysis(registry, ctx)

        analysis = analysis_service.get(snapshot_id, snapshot.data_version, compute)
        return project_id, snapshot_id, analysis

    def _envelope(project_id: str, snapshot_id: str,
                  analysis: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "execution_id": analysis.get("execution_id", ""),
            # When the figures on screen were actually produced, not when they
            # were fetched. A page that stamps "computed just now" on an answer
            # from an hour ago is claiming freshness it does not have.
            "computed_at": analysis.get("computed_at", time.time()),
            # How long the solve that produced these figures took. Recorded when
            # it ran, so it is still the truth on a later request served from
            # the store — which is the only way a caller can tell a cached
            # answer's cost from the cost of fetching it.
            "compute_seconds": analysis.get("compute_seconds"),
            "data_version": analysis.get("data_version", ""),
            # What span of time the figures cover. On every KPI response rather
            # than one of them, because every one of them reports totals over
            # this horizon and a caller reading /flows must not have to fetch
            # /network to learn what period its numbers are on.
            "horizon": analysis.get("horizon", {}),
        }

    # ------------------------------------------------------------------
    @bp.route("/network", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_network_kpis():
        """Authoritative network-wide KPIs, each carrying its own KPIStatus."""
        project_id, snapshot_id, analysis = _scoped_analysis()
        payload = _envelope(project_id, snapshot_id, analysis)
        payload["kpis"] = analysis["kpis"]
        payload["triggered_thresholds"] = analysis["triggered_thresholds"]
        return jsonify(payload), 200

    @bp.route("/facilities", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_facility_kpis():
        project_id, snapshot_id, analysis = _scoped_analysis()
        payload = _envelope(project_id, snapshot_id, analysis)
        payload["facilities"] = analysis["facilities"]
        return jsonify(payload), 200

    @bp.route("/flows", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_flow_kpis():
        """
        Solved volume and cost per lane.

        Separate from `/facilities` because a flow belongs to a lane, not to
        either end of it. Empty when no solve produced flows — the corridors
        then render with no volume rather than an assumed one.
        """
        project_id, snapshot_id, analysis = _scoped_analysis()
        payload = _envelope(project_id, snapshot_id, analysis)
        payload["flows"] = analysis["flows"]
        return jsonify(payload), 200

    def _resilience_analysis():
        """
        (project_id, snapshot_id, analysis) from a RESILIENCE assessment.

        A separate, separately-cached execution from the baseline one, and
        deliberately so. `NETWORK_STATE_QUERY` — the workflow the KPI endpoints
        run — does not assess resilience, so `facility_resilience` and
        `facility_risk` were EMPTY on every response from this blueprint and the
        per-facility endpoint silently omitted both blocks. Its own docstring
        claimed to return them.

        The reason it was not simply added to the baseline workflow is cost:
        REI re-solves the network once per facility, so a network with eight
        sites is nine MILP solves. Putting that behind every dashboard load
        would multiply the wait by the size of the footprint. It is requested
        (`?include=resilience`) and then cached per network version, so a
        project pays for it once.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The analysis engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")
        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id)
        snapshot = orchestrator.snapshots.get(snapshot_id)
        user_id = g.current_user.user_id

        def compute() -> Dict[str, Any]:
            req = OrchestratorRequest(
                input="Facility resilience assessment",
                explicit_intent=Intent.RESILIENCE_QUERY,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
            )
            response = orchestrator.run_sync(req)
            ctx = orchestrator.get_execution_state(response.execution_id)
            if ctx is None:
                raise EngineUnavailableError(
                    "The resilience execution produced no context, so no "
                    "exposure figure can be reported."
                )
            return serialise_analysis(registry, ctx)

        analysis = analysis_service.get(
            snapshot_id, snapshot.data_version, compute, variant="resilience")
        return project_id, snapshot_id, analysis

    @bp.route("/facilities/<facility_id>", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_single_facility_kpis(facility_id: str):
        """
        Utilisation and throughput for one facility, and — on request — its
        resilience and risk.

        Query:
            ``include=resilience``  also run and return the REI assessment.
                Costs one MILP solve per facility in the network, cached per
                network version. Without it the response says the blocks were
                not requested rather than omitting them without explanation.
        """
        project_id, snapshot_id, analysis = _scoped_analysis()

        metrics = analysis["facilities"].get(facility_id)
        if metrics is None:
            raise NotFoundError(
                f"Facility '{facility_id}' is not present in this project's network.",
                context={"project_id": project_id, "snapshot_id": snapshot_id},
            )

        payload = _envelope(project_id, snapshot_id, analysis)
        payload["facility_id"] = facility_id
        payload["metrics"] = metrics

        wanted = {p.strip().lower() for p in
                  (request.args.get("include") or "").split(",") if p.strip()}
        if "resilience" in wanted:
            _, _, rei_analysis = _resilience_analysis()
            payload["resilience"] = rei_analysis.get(
                "facility_resilience", {}).get(facility_id, {})
            payload["risk"] = rei_analysis.get(
                "facility_risk", {}).get(facility_id, {})
            payload["resilience_execution_id"] = rei_analysis.get("execution_id", "")
        else:
            # Absence with a reason. These blocks used to be dropped silently
            # whenever the baseline workflow had not produced them, which reads
            # as "this facility carries no exposure" — the one conclusion the
            # absence of an assessment cannot support.
            payload["resilience"] = {}
            payload["risk"] = {}
            payload["resilience_status"] = {
                "status": "NOT_REQUESTED",
                "reason": (
                    "Relative Economic Impact is not computed by the baseline "
                    "solve: it re-solves the network once per facility. Request "
                    "it with ?include=resilience. This is not a statement that "
                    "the facility has no exposure."
                ),
            }
        return jsonify(payload), 200

    @bp.route("/evidence", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_evidence_package():
        """
        The complete `AuthoritativeEvidencePackage` for this project.

        This is the payload the Reasoning Agent consumes. Exposing it lets a
        caller answer "where did this number come from?" without inspecting any
        LLM response — the question Phase 9.1 built the package to answer, which
        until now had no HTTP surface.
        """
        project_id, snapshot_id, analysis = _scoped_analysis()
        payload = _envelope(project_id, snapshot_id, analysis)
        payload["evidence"] = analysis["evidence"]
        return jsonify(payload), 200

    @bp.route("/readiness", methods=["GET"])
    @require_auth
    def get_readiness():
        """
        Is this project's analysis ready, without starting one?

        The loading screen needs to know whether the numbers exist before it
        hands the user a dashboard. Asking any of the endpoints above would
        answer the question by doing the work — this reports the state and
        returns immediately, so a client can hold the loading screen and poll.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The analysis engine is not mounted.")
        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")
        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id)
        snapshot = orchestrator.snapshots.get(snapshot_id)
        analysis = analysis_service.peek(snapshot_id, snapshot.data_version)
        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "data_version": snapshot.data_version,
            "ready": analysis is not None,
            "computed_at": analysis.get("computed_at") if analysis else None,
            "metrics": len(analysis.get("kpis", {})) if analysis else 0,
        }), 200

    @bp.route("/thresholds", methods=["GET"])
    @require_auth
    def get_thresholds():
        """
        The authoritative threshold catalogue.

        Not project-scoped: thresholds are a property of the platform's policy
        configuration, identical for every project, and carry no customer data.
        """
        return jsonify({
            "thresholds": [t.model_dump(mode="json") for t in registry.thresholds()],
        }), 200

    @bp.errorhandler(ApplicationError)
    def _kpi_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
