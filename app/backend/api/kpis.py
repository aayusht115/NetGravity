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
from app.backend.services.correlation import orchestrator_request_id
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.metrics.registry import KPIRegistry
from netgravity.orchestrator.metrics.warehouse_deep_dive import (
    DEFAULT_TARGET_UTILISATION_PCT,
    TOP_N,
    GrowthAssumption,
    WarehouseDeepDiveReport,
    compare_corridors,
    compare_facilities,
    size_future_requirements,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

#: WHICH optimisation the "after" column is.
#:
#: `OPTIMIZATION_REQUEST` runs CURRENT_FOOTPRINT_OPTIMIZATION, whose policy
#: pins the existing footprint open and excludes candidates
#: (`netgravity/optimization/modes.py`). It optimises routing, allocation and
#: sourcing; it cannot close a site. On a network already routed well every row
#: comes back unchanged — which is a real finding and reads, unlabelled, as
#: "nothing about this network can be improved".
_COMPARISON_BASIS = (
    "The 'after' plan re-optimises routing, allocation and sourcing with the "
    "footprint held fixed: every existing site stays open and proposed sites "
    "are excluded, so no site's status can change here. Rows that are "
    "unchanged mean this network is already routed as well as it can be under "
    "its current footprint — not that there is nothing to change about the "
    "footprint. Opening or closing a site is a Scenario Planner question, "
    "which solves it with closure economics and contractual constraints "
    "applied."
)

#: What a facility-level saving does and does not include.
#:
#: The same sentence `WarehouseDeepDiveReport.savings_basis` carries. The
#: before-and-after is joined in this module from two separately cached
#: analyses, so the report builder never sees those rows and cannot write the
#: caveat onto them — and a caveat that only appears on one of the two paths
#: is worse than none.
_SAVINGS_BASIS = (
    "A saving here is one site's FACILITY cost in the baseline less its "
    "facility cost in the optimised plan — fixed, opening, closure, handling "
    "and holding. It does not include the transport cost of moving that site's "
    "volume elsewhere, so these figures do not sum to the network saving. The "
    "network figure is stated separately."
)

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

        # Read here, not inside `compute()`: the closure is handed to the
        # analysis cache and there is no promise it runs inside this request.
        request_id = orchestrator_request_id("kpi-baseline")

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
                request_id=request_id,
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
            # The money unit these figures are in. Every cost KPI carries it on
            # its own `unit` too; it is repeated once at the envelope so a
            # screen can format a currency without inspecting a metric.
            "currency": analysis.get("currency"),
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

        request_id = orchestrator_request_id("kpi-resilience")

        def compute() -> Dict[str, Any]:
            req = OrchestratorRequest(
                input="Facility resilience assessment",
                explicit_intent=Intent.RESILIENCE_QUERY,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
                request_id=request_id,
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

    def _optimized_analysis():
        """
        (project_id, snapshot_id, analysis) from an OPTIMISED solve of the same
        network.

        A project's baseline analysis is an ACTUAL_AS_IS_EVALUATION: the
        observed footprint, pinned open, evaluated as it stands. That is the
        right baseline and it is the "before" half of a before-and-after — but
        on its own it cannot be one, because nothing has been optimised to
        compare it against.

        So this runs `OPTIMIZATION_REQUEST` (CURRENT_FOOTPRINT_OPTIMIZATION),
        which lets the model choose which of the existing sites to keep open,
        and caches it as its own variant. Separate and requested rather than
        folded into the baseline for the same reason resilience is: it is a
        second MILP solve of the whole network, and putting it behind every
        dashboard load would double a wait the user already feels.
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
        request_id = orchestrator_request_id("kpi-optimized")

        def compute():
            req = OrchestratorRequest(
                input="Optimise the current footprint",
                explicit_intent=Intent.OPTIMIZATION_REQUEST,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
                request_id=request_id,
            )
            response = orchestrator.run_sync(req)
            ctx = orchestrator.get_execution_state(response.execution_id)
            if ctx is None:
                raise EngineUnavailableError(
                    "The optimisation produced no context, so no before-and-"
                    "after can be reported."
                )
            return serialise_analysis(registry, ctx)

        analysis = analysis_service.get(
            snapshot_id, snapshot.data_version, compute, variant="optimized")
        return project_id, snapshot_id, analysis

    def _growth_from_request():
        """
        The growth assumption this request states, or None.

        Read from the query string and validated here rather than defaulted
        anywhere downstream. `growth_pct` is a network-wide rate;
        `region_growth` refines it per region as `Name:pct,Name:pct`, which is
        how a planner states demand growth — by region, not by warehouse.

        `growth_source` records WHERE the number came from, so a rate lifted
        from the Forecast screen is not shown as though the user had typed it.
        Restricted to the two things it can honestly be.
        """
        args = request.args

        def number(name, low, high):
            raw = (args.get(name) or "").strip()
            if not raw:
                return None
            try:
                value = float(raw)
            except ValueError:
                raise ValidationError(
                    f"'{name}' must be a number, got '{raw}'.", context={"field": name})
            if not (low <= value <= high):
                raise ValidationError(
                    f"'{name}' must be between {low} and {high}, got {value}.",
                    context={"field": name})
            return value

        network_pct = number("growth_pct", -100.0, 1000.0)

        by_region = {}
        for part in (args.get("region_growth") or "").split(","):
            part = part.strip()
            if not part:
                continue
            name, _, raw = part.rpartition(":")
            if not name.strip():
                raise ValidationError(
                    f"'region_growth' entries look like 'Region:12.5'; got '{part}'.",
                    context={"field": "region_growth"})
            try:
                by_region[name.strip()] = float(raw)
            except ValueError:
                raise ValidationError(
                    f"'region_growth' entries look like 'Region:12.5'; got '{part}'.",
                    context={"field": "region_growth"})

        if network_pct is None and not by_region:
            return None

        target = number("target_utilization_pct", 1.0, 100.0)
        source = (args.get("growth_source") or "USER_STATED").strip().upper()
        if source not in {"USER_STATED", "FORECAST_ENGINE"}:
            raise ValidationError(
                "'growth_source' must be USER_STATED or FORECAST_ENGINE.",
                context={"field": "growth_source"})

        described = ", ".join(f"{k} +{v:g}%" for k, v in by_region.items())
        return GrowthAssumption(
            network_pct=network_pct,
            by_region=by_region,
            target_utilization_pct=(target if target is not None
                                    else DEFAULT_TARGET_UTILISATION_PCT),
            source=source,
            description=(
                ("Measured by the forecasting engine and applied here"
                 if source == "FORECAST_ENGINE"
                 else "Stated by the planner")
                + (f" at +{network_pct:g}% network-wide" if network_pct is not None else "")
                + (f" ({described})" if described else "")
                + "."
            ),
        )

    @bp.route("/warehouse", methods=["GET"])
    @require_auth
    @rate_limit("kpi.read", limit=240, window_seconds=60)
    def get_warehouse_deep_dive():
        """
        Warehouse health, the rankings, and — on request — sizing and a
        before-and-after.

        Query:
            ``growth_pct=12.5``       size the footprint against +12.5% demand.
            ``region_growth=North:18,South:4``   per-region rates, applied to
                the sites that actually serve those markets.
            ``target_utilization_pct=85``  the utilisation new capacity is
                sized to. Stated, not assumed by NetGravity.
            ``growth_source=FORECAST_ENGINE``  the rate came from the measured
                forecast rather than from the planner.
            ``include=optimized``     also solve the current footprint and
                return the per-site before-and-after and the savings ranking.
                Costs one MILP solve of the whole network, cached per version.

        Without a growth rate the sizing section reports
        INSUFFICIENT_EVIDENCE and says what it needs. It is never defaulted:
        a capacity gap in units resting on an invented rate is
        indistinguishable, on screen, from a measured one.
        """
        project_id, snapshot_id, analysis = _scoped_analysis()
        stored = analysis.get("warehouse") or {}
        report = WarehouseDeepDiveReport(**stored) if stored else WarehouseDeepDiveReport()

        payload = _envelope(project_id, snapshot_id, analysis)

        # ---- sizing ---------------------------------------------------
        growth = _growth_from_request()
        if growth is not None:
            rows, status = size_future_requirements(
                report.health_kpis, growth,
                market_regions=report.market_regions,
                flows=analysis.get("flows") or [],
            )
            report.future_requirements = rows
            report.future_status = status
            report.growth_assumption = growth
            report.total_capacity_gap_units = round(
                sum(r.capacity_gap_units for r in rows), 2)
            report.n_sites_needing_expansion = sum(1 for r in rows if r.expansion_needed)

        # ---- before and after -----------------------------------------
        wanted = {p.strip().lower() for p in
                  (request.args.get("include") or "").split(",") if p.strip()}
        if "optimized" in wanted:
            _, _, optimized = _optimized_analysis()
            after = WarehouseDeepDiveReport(**(optimized.get("warehouse") or {}))
            # The AS-IS analysis is the "before"; the optimisation is the
            # "after". Read that way round and no other: reversing it would
            # report every saving as a cost increase.
            rows, status = compare_facilities(report.health_kpis, after.health_kpis)
            after.facility_before_after = rows
            after.comparison_status = status
            after.top_savings_opportunities = sorted(
                (r for r in rows if r.facility_cost_savings > 0),
                key=lambda r: -r.facility_cost_savings)[:TOP_N]
            # The report builder writes these when IT holds the rows; the rows
            # here were joined in this module, so it never saw them.
            after.savings_basis = _SAVINGS_BASIS
            after.comparison_basis = _COMPARISON_BASIS
            # The corridor half. On a footprint-fixed optimisation this is the
            # only half that can differ, so leaving it out would report
            # "nothing changed" about a solve whose whole job was routing.
            corridors, _ = compare_corridors(
                analysis.get("flows") or [], optimized.get("flows") or [])
            after.corridor_before_after = corridors
            after.top_corridor_savings = sorted(
                (r for r in corridors if r.transport_cost_savings > 0),
                key=lambda r: -r.transport_cost_savings)[:TOP_N]
            after.baseline_business_cost = report.business_cost
            if (after.baseline_business_cost is not None
                    and after.business_cost is not None):
                after.business_cost_delta = round(
                    after.baseline_business_cost - after.business_cost, 2)
            payload["optimized"] = after.model_dump(mode="json")
            payload["optimized_execution_id"] = optimized.get("execution_id", "")
        else:
            # Absence with a reason, on the same principle as the resilience
            # block below: an empty before-and-after must not read as "nothing
            # changed when we optimised".
            payload["optimized"] = None
            payload["optimized_status"] = {
                "status": "NOT_REQUESTED",
                "reason": (
                    "A before-and-after needs a second solve of the whole "
                    "network, so it is not run with the baseline. Request it "
                    "with ?include=optimized. It re-optimises routing, "
                    "allocation and sourcing with the footprint held fixed. "
                    "This is not a statement that optimising would change "
                    "nothing."
                ),
            }

        payload["warehouse"] = report.model_dump(mode="json")
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
