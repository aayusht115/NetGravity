"""
NetGravity — Scenario Planning & Simulation API Blueprint
=========================================================
Project-scoped what-if scenarios, solved by the real MILP engine through the
orchestrator and reported through the Phase 9.1 authoritative KPI layer.

Phase 10.0 rewrite. The prototype version of this blueprint:

  * shipped two fully hardcoded "canonical" scenarios, complete with fabricated
    cost/SLA/carbon figures, fabricated robustness tests all marked PASS, and a
    fabricated `aiAssessment` narrative;
  * on `/simulate`, ran a REAL orchestrator solve, obtained REAL
    `ScenarioMetricDelta` objects — and then discarded them, returning
    `totalCost: 1205000`, `sla_val = 95.5`, `avgUtil: 68.2`, `carbonKg: 102400`
    as literals, with a fabricated `-6.5` fallback for the one delta it did read;
  * stored every user's scenarios in one process-global list.

Every figure returned by this module now originates in `KPIRegistry` and carries
its `KPIStatus`. Where a value cannot be computed, the status says so and the
value is null — never a plausible substitute (brief §9, §24).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.correlation import orchestrator_request_id
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.metrics.registry import KPIRegistry
from netgravity.orchestrator.schemas.requests import (
    NETWORK_WIDE_ACTIONS,
    Actor,
    ActorRole,
    GreenfieldSiteSpec,
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

logger = logging.getLogger(__name__)

_ACTION_MAP = {
    "CHANGE_CAPACITY": ScenarioActionType.CHANGE_CAPACITY,
    "CHANGE_DEMAND": ScenarioActionType.CHANGE_DEMAND,
    "OPEN_FACILITY": ScenarioActionType.OPEN_FACILITY,
    "CLOSE_FACILITY": ScenarioActionType.CLOSE_FACILITY,
    # A greenfield site, NOT an alias for OPEN_FACILITY.
    #
    # It used to be one, which meant "open a facility" could only ever pin open
    # a site the client already operates — the builder offered a dropdown of
    # their own DCs and plants, and choosing one asked the solver to keep open
    # something it was already free to keep open. "Where should we put a new
    # DC?" was unanswerable through this API.
    "ADD_FACILITY": ScenarioActionType.ADD_FACILITY,
    "NEW_FACILITY": ScenarioActionType.ADD_FACILITY,
    "REMOVE_FACILITY": ScenarioActionType.CLOSE_FACILITY,
    "SHIFT_VOLUME": ScenarioActionType.SHIFT_VOLUME,
    "VOLUME_SHIFT": ScenarioActionType.SHIFT_VOLUME,
    "CHANGE_TRANSPORT_COST": ScenarioActionType.CHANGE_TRANSPORT_COST,
    "CHANGE_SLA": ScenarioActionType.CHANGE_SLA,
}

#: KPIs surfaced on the scenario comparison cards, in display order.
_HEADLINE_METRICS = (
    "business_network_cost",
    "pct_demand_in_sla",
    "demand_fill_rate",
    "avg_utilization_pct",
    "max_utilization_pct",
    "total_carbon_kg",
)


def _scenario_state_key(context: Any) -> Optional[str]:
    """The `scenario:<id>` key this execution wrote, if any."""
    for key in getattr(context, "network_states", {}):
        if key.startswith("scenario:"):
            return key
    return None


def _facility_states(registry: Any, context: Any, key: Optional[str]) -> Dict[str, Any]:
    """
    Per-facility utilisation, throughput and open/closed for one solved state.

    Flattened to plain values because this feeds a map, not an audit trail; the
    full `KPIResult` with its status is available from `/api/kpis/facilities`.
    A metric the solve did not report stays None rather than becoming zero.
    """
    if not key:
        return {}
    out: Dict[str, Any] = {}
    for facility_id, metrics in registry.facility_kpis(context, key=key).items():
        def value(metric_id: str) -> Any:
            result = metrics.get(metric_id)
            return result.value if result and result.status.value == "VALID" else None

        out[facility_id] = {
            "utilPct": value("utilization_pct"),
            "throughput": value("throughput_units"),
            "capacity": value("capacity_units"),
            "isOpen": value("is_open"),
        }
    return out


def _lane_flows(registry: Any, context: Any, key: Optional[str]) -> List[Dict[str, Any]]:
    """Solved volume and cost per lane for one state, keyed origin->destination."""
    if not key:
        return []
    return registry.flow_kpis(context, key=key)


def _new_sites(engine: Any, scenario_key: Optional[str],
               snapshot_id: str) -> List[Dict[str, Any]]:
    """
    Facilities that exist in the scenario network and not in the snapshot.

    A greenfield site is in no uploaded network, so the map has no coordinates
    for it and would draw a scenario that opens a new DC without ever showing
    the DC. `FacilitySummary` — which is what the KPI layer reports per
    facility — carries no latitude or longitude, by design: it is a solver
    outcome, not topology. So the position is read from the materialised
    scenario network the builder actually solved, which is the only place it is
    authoritative.

    Returns [] for every scenario that adds nothing, which is most of them.
    """
    if not scenario_key or not scenario_key.startswith("scenario:"):
        return []
    scenario_id = scenario_key.split(":", 1)[1]
    try:
        record = engine.scenarios.get(scenario_id)
        baseline = engine.snapshots.get(snapshot_id).network
    except Exception:  # noqa: BLE001 — an absent record is simply no new site
        return []

    known = {f.id for f in baseline.facilities}
    out: List[Dict[str, Any]] = []
    for facility in record.network.facilities:
        if facility.id in known:
            continue
        out.append({
            "id": facility.id,
            "name": facility.name,
            "role": getattr(facility.role, "value", str(facility.role)),
            "lat": facility.latitude,
            "lng": facility.longitude,
            "capacity": facility.capacity_units_per_period,
            "handlingCost": facility.handling_cost_per_unit,
            "fixedCostPerYear": facility.fixed_cost_per_year,
        })
    return out


def _overrides_of(engine: Any, scenario_key: Optional[str]) -> List[str]:
    """The builder's own description of what this scenario changed."""
    if not scenario_key or not scenario_key.startswith("scenario:"):
        return []
    try:
        return list(engine.scenarios.get(scenario_key.split(":", 1)[1]).overrides)
    except Exception:  # noqa: BLE001
        return []


def _serialise_kpis(results: Dict[str, Any]) -> Dict[str, Any]:
    """`KPIResult` -> JSON, status and provenance preserved verbatim."""
    return {k: v.model_dump(mode="json") for k, v in results.items()}


def create_scenario_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/scenarios"):
    bp = Blueprint("scenarios", __name__, url_prefix=url_prefix)
    registry = KPIRegistry()

    # Scenarios are stored per project, never in one shared list, and written
    # through to the database so a restart does not discard an afternoon's
    # work. The dictionary stays the read path; the database is what it is
    # rebuilt from.
    _store: Dict[str, List[Dict[str, Any]]] = {}
    _lock = threading.RLock()
    _restored = {"done": False}

    def _load_scenarios() -> None:
        """Rebuild `_store` from the database, once, on first use."""
        with _lock:
            if _restored["done"]:
                return
            _restored["done"] = True
            from app.backend.services import persistence
            count = 0
            for project_id, record in persistence.load_scenarios():
                if not record.get("id"):
                    continue
                _store.setdefault(project_id, []).append(record)
                count += 1
            if count:
                logger.info("scenario.store.restored scenarios=%d", count)

    # One re-optimised reference per snapshot; see `_optimised_reference`.
    _reference: Dict[str, Dict[str, Any]] = {}
    _reference_lock = threading.RLock()

    def _optimised_reference(snapshot_id: str, user_id: str) -> Dict[str, Any]:
        """
        The same network, unchanged, solved the way every SCENARIO is solved.

        Without this, every scenario appears to save about the same 47%.

        The project baseline is deliberately an `ACTUAL_AS_IS_EVALUATION`: the
        client's footprint pinned open, because that is the network they
        actually run and the figure they recognise. A scenario is solved as a
        `BROWNFIELD_SCENARIO_OPTIMIZATION`, which is free to close sites. So the
        difference between the two columns is the change PLUS the whole value of
        redesigning the footprint — and on this network the redesign dominates.
        Three unrelated scenarios came back at −47.1%, −46.8% and −47.1%, which
        reads exactly as a screen showing the same number whatever you ask it.

        The reference is a no-change scenario: a capacity delta of zero, run
        through the identical code path, so what it isolates is guaranteed to be
        comparable rather than approximately so. Cached per snapshot because it
        does not depend on the scenario.
        """
        with _reference_lock:
            cached = _reference.get(snapshot_id)
        if cached is not None:
            return cached

        engine = _require_engine()
        snapshot = engine.snapshots.get(snapshot_id)
        anchor = next((f.id for f in snapshot.network.facilities
                       if getattr(f.role, "value", str(f.role)) not in
                       ("MARKET", "CUSTOMER")), None)
        if anchor is None:
            return {}

        req = OrchestratorRequest(
            input="Re-optimised reference: the network unchanged",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=[anchor],
                capacity_delta_units=0.0,
                label="Re-optimised, no change",
            )],
            actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            disable_llm=True,
            request_id=orchestrator_request_id("scenario-reference"),
        )
        try:
            response = engine.run_sync(req)
            ctx = engine.get_execution_state(response.execution_id)
            key = _scenario_state_key(ctx) if ctx else None
            result = _serialise_kpis(registry.network_kpis(ctx, key=key)) if key else {}
        except Exception:  # noqa: BLE001 — a missing reference is not fatal
            logger.warning("scenario.reference.failed snapshot_id=%s", snapshot_id)
            result = {}

        with _reference_lock:
            _reference[snapshot_id] = result
        return result

    def _require_engine() -> Orchestrator:
        if orchestrator is None:
            raise EngineUnavailableError(
                "The analysis engine is not mounted, so scenarios cannot be solved."
            )
        return orchestrator

    def _project_scope() -> tuple[str, str]:
        """(project_id, snapshot_id) for this request, access-checked."""
        project_id = str(request.args.get("project_id")
                         or (request.get_json(silent=True) or {}).get("project_id")
                         or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")
        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id
        )
        return project_id, snapshot_id

    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    @require_auth
    def list_scenarios():
        """Scenarios previously solved for this project."""
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            records = list(_store.get(project_id, []))
        return jsonify({
            "project_id": project_id,
            "scenarios": records,
            "total": len(records),
        }), 200

    @bp.route("/baseline", methods=["GET"])
    @require_auth
    @rate_limit("scenario.baseline", limit=60, window_seconds=300)
    def get_baseline():
        """
        The project's immutable baseline: a solve of the bound snapshot with no
        scenario applied. Recomputed on demand from the snapshot, so no scenario
        run can ever mutate it (brief §13).
        """
        engine = _require_engine()
        project_id, snapshot_id = _project_scope()

        req = OrchestratorRequest(
            input="Baseline network solve",
            explicit_intent=Intent.NETWORK_STATE_QUERY,
            actor=Actor(actor_id=g.current_user.user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            disable_llm=True,
            request_id=orchestrator_request_id("scenario-baseline"),
        )
        response = engine.run_sync(req)
        ctx = engine.get_execution_state(response.execution_id)
        if ctx is None:
            raise EngineUnavailableError("Baseline execution produced no context.")

        kpis = registry.network_kpis(ctx)
        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "execution_id": response.execution_id,
            "type": "BASELINE",
            "kpis": _serialise_kpis(kpis),
            "triggered_thresholds": [
                t.model_dump(mode="json")
                for t in registry.evaluate_thresholds(list(kpis.values()))
            ],
        }), 200

    @bp.route("/simulate", methods=["POST"])
    @require_auth
    # Two MILP solves per call — the scenario and the re-optimised reference.
    # One caller can otherwise occupy every worker and the platform stops
    # answering for everyone else, with no malice required.
    @rate_limit("scenario.simulate", limit=30, window_seconds=300)
    def simulate_scenario():
        """
        Solve a what-if scenario against the project's bound snapshot.

        Returns authoritative baseline KPIs, scenario KPIs and deterministic
        deltas. An infeasible scenario is reported as infeasible; it is not
        rendered as a cheaper network.
        """
        engine = _require_engine()
        project_id, snapshot_id = _project_scope()
        _load_scenarios()
        body: Dict[str, Any] = request.get_json(silent=True) or {}

        name = str(body.get("name") or "").strip() or "Custom what-if scenario"
        action_str = str(body.get("action") or "CHANGE_CAPACITY").upper()
        if action_str not in _ACTION_MAP:
            raise ValidationError(
                f"Unsupported scenario action '{action_str}'.",
                context={"supported": sorted(_ACTION_MAP)},
            )
        action = _ACTION_MAP[action_str]

        facility_ids = body.get("facility_ids") or []
        if not isinstance(facility_ids, list):
            raise ValidationError("facility_ids must be a list.")
        # Demand, freight rates and the delivery promise are properties of the
        # whole network; a greenfield site names no existing facility because it
        # is not one yet. Requiring a facility for all four made three of the
        # six scenario types in the builder impossible to run.
        needs_facility = (action not in NETWORK_WIDE_ACTIONS
                          and action != ScenarioActionType.ADD_FACILITY)
        if needs_facility and not facility_ids:
            raise ValidationError(
                f"At least one facility_id is required for {action_str}.")

        def number(key: str, *aliases: str) -> Optional[float]:
            raw = body.get(key)
            for alias in aliases:
                if raw is None:
                    raw = body.get(alias)
            if raw is None:
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValidationError(f"{key} must be numeric, got {raw!r}.")

        cap_delta = number("capacity_delta_units")
        demand_scale = number("demand_multiplier", "demand_scale")
        transport_mult = number("transport_cost_multiplier")
        sla_delta = number("sla_days_delta")

        required = {
            ScenarioActionType.CHANGE_CAPACITY: (
                cap_delta, "capacity_delta_units"),
            ScenarioActionType.CHANGE_DEMAND: (
                demand_scale, "demand_multiplier"),
            ScenarioActionType.CHANGE_TRANSPORT_COST: (
                transport_mult, "transport_cost_multiplier"),
            ScenarioActionType.CHANGE_SLA: (sla_delta, "sla_days_delta"),
        }
        if action in required and required[action][0] is None:
            raise ValidationError(
                f"{required[action][1]} is required for {action.value}.")

        site: Optional[GreenfieldSiteSpec] = None
        if action == ScenarioActionType.ADD_FACILITY:
            raw_site = body.get("new_facility")
            if not isinstance(raw_site, dict):
                raise ValidationError(
                    "ADD_FACILITY requires a new_facility object with a name, "
                    "latitude, longitude and capacity_units_per_period.")
            try:
                site = GreenfieldSiteSpec(**raw_site)
            except Exception as exc:  # noqa: BLE001 — pydantic message is the useful part
                raise ValidationError(f"new_facility is not usable: {exc}")

        spec = ScenarioIntentSpec(
            action=action,
            facility_ids=list(facility_ids),
            capacity_delta_units=cap_delta if action == ScenarioActionType.CHANGE_CAPACITY else None,
            demand_multiplier=demand_scale if action == ScenarioActionType.CHANGE_DEMAND else None,
            transport_cost_multiplier=(
                transport_mult if action == ScenarioActionType.CHANGE_TRANSPORT_COST else None),
            sla_days_delta=sla_delta if action == ScenarioActionType.CHANGE_SLA else None,
            new_facility=site,
            label=name,
        )

        req = OrchestratorRequest(
            input=f"Simulate scenario: {name}",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[spec],
            actor=Actor(actor_id=g.current_user.user_id, role=ActorRole.PLANNER),
            network_snapshot_id=snapshot_id,
            disable_llm=True,
            request_id=orchestrator_request_id("scenario-simulate"),
        )

        try:
            response = engine.run_sync(req)
        except Exception as exc:  # noqa: BLE001 — surfaced, never substituted
            logger.exception("scenario.simulate.failed project_id=%s", project_id)
            return jsonify({
                "error": {
                    "code": "CAPABILITY_FAILURE",
                    "message": f"The scenario could not be solved: {exc}",
                }
            }), 502

        ctx = engine.get_execution_state(response.execution_id)
        if ctx is None:
            raise EngineUnavailableError("Scenario execution produced no context.")

        scenario_key = _scenario_state_key(ctx)

        # A scenario that never materialised is not a scenario.
        #
        # `run_sync` never raises — it captures every failure and returns it on
        # the response, which is right for a control plane and wrong to treat as
        # success here. A refused build (a site with no capacity, an SLA change
        # on a network that states none) came back 201 with a stored record
        # whose every figure was null, and the comparison table rendered it as a
        # scenario with no results rather than saying the run was rejected.
        if scenario_key is None:
            reasons = [str(e.get("message") or e.get("error") or e)
                       for e in (response.errors or [])]
            detail = reasons[0] if reasons else (
                response.summary or "the scenario engine produced no scenario state")
            logger.info(
                "scenario.simulate.rejected project_id=%s action=%s reason=%s",
                project_id, action_str, detail,
            )
            return jsonify({
                "error": {
                    "code": "SCENARIO_NOT_BUILT",
                    "message": f"This scenario could not be run: {detail}",
                    "context": {"action": action_str,
                                "execution_id": response.execution_id,
                                "orchestrator_status": str(response.status)},
                }
            }), 422

        baseline_kpis = registry.network_kpis(ctx, key="optimization.solve")
        scenario_kpis = (registry.network_kpis(ctx, key=scenario_key)
                         if scenario_key else {})
        deltas = registry.scenario_comparison(ctx)

        # Headline projection for the comparison cards. Values appear ONLY when
        # the authoritative result is VALID; otherwise the status travels to the
        # client and the card renders an explicit unavailable state.
        headline: Dict[str, Any] = {}
        for metric_id in _HEADLINE_METRICS:
            result = scenario_kpis.get(metric_id)
            if result is None:
                headline[metric_id] = {"value": None, "status": "NOT_COMPUTABLE", "unit": ""}
            else:
                headline[metric_id] = {
                    "value": result.value if result.status.value == "VALID" else None,
                    "status": result.status.value,
                    "unit": result.unit,
                }

        record = {
            "id": f"SCN_{uuid.uuid4().hex[:8]}",
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "name": name,
            "type": "USER_CREATED",
            "source": "user",
            "created_at": time.time(),
            "execution_id": response.execution_id,
            "orchestrator_status": str(getattr(response, "status", "")),
            "feasible": scenario_key is not None and bool(scenario_kpis),
            "request": {
                "action": action_str,
                "facility_ids": list(facility_ids),
                "capacity_delta_units": cap_delta,
                "demand_multiplier": demand_scale,
                "transport_cost_multiplier": transport_mult,
                "sla_days_delta": sla_delta,
                "new_facility": site.model_dump(mode="json") if site else None,
            },
            # What the builder actually did to the network, in its own words.
            # The drawer used to describe changes from a hand-written list that
            # no builder produced.
            "overrides": _overrides_of(engine, scenario_key),
            # Sites this scenario introduces. Empty for every scenario that
            # only rearranges the existing footprint.
            "new_sites": _new_sites(engine, scenario_key, snapshot_id),
            "baseline_kpis": _serialise_kpis(baseline_kpis),
            "scenario_kpis": _serialise_kpis(scenario_kpis),
            # The network unchanged but solved the way scenarios are, so a
            # scenario's own effect can be separated from the value of
            # re-optimising the footprint. See `_optimised_reference`.
            "reference_kpis": _optimised_reference(
                snapshot_id, g.current_user.user_id),
            "reference_note": (
                "The network as uploaded, re-solved with the same freedom a "
                "scenario has to open and close sites. The difference between "
                "the baseline and this reference is the value of re-optimising "
                "your existing footprint; the difference between this reference "
                "and the scenario is what the change itself does."
            ),
            # The topology BOTH states produced. Without these the Digital Twin
            # cannot show what a scenario changed: it had only network totals,
            # so its map fell back to a hardcoded table of prototype facilities
            # and rendered the baseline for every scenario ever created.
            "baseline_facilities": _facility_states(
                registry, ctx, "optimization.solve"),
            "scenario_facilities": _facility_states(registry, ctx, scenario_key),
            "baseline_flows": _lane_flows(registry, ctx, "optimization.solve"),
            "scenario_flows": _lane_flows(registry, ctx, scenario_key),
            "headline": headline,
            "deltas": {d.metric_id: d.model_dump(mode="json") for d in deltas},
            "triggered_thresholds": [
                t.model_dump(mode="json")
                for t in registry.evaluate_thresholds(list(scenario_kpis.values()))
            ],
            "provenance": {
                "engine": "netgravity MILP (PuLP/HiGHS)",
                "authoritative_source": "KPIRegistry (Phase 9.1)",
                "llm_used": False,
                "computed_by": "orchestrator.run_sync",
            },
        }

        with _lock:
            _store.setdefault(project_id, []).append(record)

        from app.backend.services import persistence
        persistence.guarded(persistence.save_scenario)(
            record["id"], project_id, record, record["created_at"],
        )

        logger.info(
            "scenario.simulated project_id=%s scenario_id=%s execution_id=%s feasible=%s",
            project_id, record["id"], response.execution_id, record["feasible"],
        )
        return jsonify(record), 201

    @bp.route("/<scenario_id>", methods=["GET"])
    @require_auth
    def get_scenario(scenario_id: str):
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            for rec in _store.get(project_id, []):
                if rec["id"] == scenario_id:
                    return jsonify(rec), 200
        raise NotFoundError(f"Scenario '{scenario_id}' not found in this project.")

    @bp.route("/<scenario_id>", methods=["DELETE"])
    @require_auth
    def delete_scenario(scenario_id: str):
        """
        Discard a solved scenario.

        The comparison holds three scenarios at a time, so removing one is part
        of ordinary use. It was a client-side splice only, which meant a
        scenario the user had deleted came back on the next page load.
        """
        project_id, _ = _project_scope()
        _load_scenarios()
        with _lock:
            records = _store.get(project_id, [])
            remaining = [r for r in records if r["id"] != scenario_id]
            if len(remaining) == len(records):
                raise NotFoundError(
                    f"Scenario '{scenario_id}' not found in this project.")
            _store[project_id] = remaining

        from app.backend.services import persistence
        persistence.guarded(persistence.delete_scenario)(scenario_id)
        logger.info("scenario.deleted project_id=%s scenario_id=%s",
                    project_id, scenario_id)
        return jsonify({"deleted": scenario_id, "remaining": len(remaining)}), 200

    @bp.errorhandler(ApplicationError)
    def _scenario_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
