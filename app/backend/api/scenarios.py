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

from netgravity.orchestrator.explanation_llm import (  # noqa: E402
    explanation_reasoning_agent,
    explanations_llm_enabled,
)

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


def _scenario_explanation(ctx: Any) -> Dict[str, Any]:
    """
    The scenario's grounded briefing, in the shape the explanation pane reads.

    Already computed and already grounded — `numeric_grounding` has re-checked
    every numeric claim by the time it gets here. Nothing is generated or
    recomputed; this selects fields off `ExecutiveBriefing`.

    Returns {} when the run produced no briefing, so the pane says it has
    nothing to explain rather than showing the network's briefing in its place.
    """
    reasoning = getattr(ctx, "reasoning", None)
    briefing = getattr(reasoning, "briefing", None) if reasoning else None
    if briefing is None:
        return {}

    from netgravity.orchestrator.explanation_service import build_card

    return {
        # The ONE card the screen renders. Everything below it is the fuller
        # record, kept for the audit view rather than for display.
        "card": build_card(reasoning, figures=_scenario_figures(ctx)),
        "scope": briefing.scope.value,
        "entity_id": briefing.entity_id,
        "opening": briefing.opening,
        "context": briefing.context,
        "insights": [
            {
                "theme": item.theme,
                "headline": item.headline,
                "narrative": item.narrative,
                "severity": item.severity.value,
            }
            for item in briefing.kpi_insights
        ],
        "key_drivers": list(briefing.key_drivers),
        "recommendation": briefing.recommendation,
        "limitation": briefing.limitation,
        "evidence_completeness": briefing.evidence_completeness.value,
        "suggested_questions": list(briefing.suggested_questions),
        "missing_information": [m.model_dump(mode="json")
                                for m in briefing.missing_information],
        "grounding": {"warnings": list(getattr(reasoning, "validation_warnings", []))},
    }


def _scenario_figures(ctx: Any) -> List[Any]:
    """
    The three numbers for one scenario, supplied by code.

    Cost, demand served and capacity risk — the same three the comparison
    shows, so a reader moving between them is reading the same quantities.
    Money travels as an amount; the screen applies the project's currency.
    """
    from netgravity.orchestrator.reasoning.card import Figure

    state = {}
    try:
        result = (ctx.network_states or {}).get("optimization.solve_scenario")
        state = result.kpis.model_dump(mode="json") if result and result.kpis else {}
    except Exception:  # noqa: BLE001 — figures are advisory
        state = {}

    fill = state.get("demand_fill_rate")
    return [
        Figure.money("Cost", state.get("business_network_cost")),
        Figure(label="Demand served",
               value=(f"{fill * 100:,.1f}%" if isinstance(fill, (int, float))
                      else "Not available")),
        Figure(label="Sites open",
               value=(f"{state['n_facilities_open']:,.0f}"
                      if isinstance(state.get("n_facilities_open"), (int, float))
                      else "Not available")),
    ]


def _serialise_kpis(results: Dict[str, Any]) -> Dict[str, Any]:
    """`KPIResult` -> JSON, status and provenance preserved verbatim."""
    return {k: v.model_dump(mode="json") for k, v in results.items()}


def optimised_reference_for(snapshot_id: str, user_id: str) -> Dict[str, Any]:
    """
    The network re-solved with the freedom a scenario has, for THIS snapshot.

    Replaced with the real, cached implementation when the scenario blueprint
    is mounted. Until then it answers "no reference", which is the honest
    answer for a process that cannot produce one.
    """
    return {}


# ---------------------------------------------------------------------------
# Comparing scenarios
#
# The ranking and the recommendation are made HERE, from the authoritative
# KPI values, not in the browser. A screen that ranks its own rows decides
# what to recommend in JavaScript, where the decision is invisible to the
# audit trail, untestable from the backend suite, and free to disagree with
# whatever the same numbers say elsewhere.
# ---------------------------------------------------------------------------

#: A metric is only usable when its own status says so. A None value with a
#: non-VALID status is a refusal, and must never be read as a number.
def _valid(kpis: Dict[str, Any], metric_id: str) -> Optional[float]:
    result = (kpis or {}).get(metric_id) or {}
    if result.get("status") != "VALID":
        return None
    value = result.get("value")
    return float(value) if isinstance(value, (int, float)) else None


def _rank_scenarios(baseline: Dict[str, Any],
                    records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Cheapest first, on the solver's own business network cost.

    A scenario whose cost is not VALID is still returned — it was compared,
    and dropping it would silently shorten the comparison — but it ranks last
    and is marked not comparable rather than being given a position it did
    not earn.
    """
    baseline_cost = _valid(baseline, "business_network_cost")
    baseline_fill = _valid(baseline, "demand_fill_rate")

    rows: List[Dict[str, Any]] = []
    for record in records:
        kpis = record.get("scenario_kpis") or {}
        cost = _valid(kpis, "business_network_cost")
        fill = _valid(kpis, "demand_fill_rate")
        rows.append({
            "scenario_id": record.get("id"),
            "name": record.get("name"),
            "cost": cost,
            "cost_delta": (None if cost is None or baseline_cost is None
                           else round(cost - baseline_cost, 4)),
            "fill_rate": fill,
            "fill_delta": (None if fill is None or baseline_fill is None
                           else round((fill - baseline_fill) * 100.0, 4)),
            "comparable": cost is not None and baseline_cost is not None,
        })
    # Deterministic regardless of the order the ids arrived in.
    #
    # Sorting on cost alone left ties — two scenarios with equal cost, or two
    # with no comparable cost at all — resolved by input order. So comparing
    # A and B named a different winner than comparing B and A, which is the
    # same analysis asked twice. The id is the tiebreak: arbitrary, but
    # stable, which is the property that matters.
    rows.sort(key=lambda r: (r["cost_delta"] is None,
                             r["cost_delta"] if r["cost_delta"] is not None else 0.0,
                             str(r["scenario_id"] or "")))
    return rows


#: How much demand a plan may serve below the baseline before the saving is
#: called what it is: a smaller promise, not a cheaper way of keeping the
#: same one. Percentage points.
_MATERIAL_FILL_DROP_PTS = 0.05


#: Below this, demand coverage is a problem in its own right and the cheapest
#: option cannot be presented as simply "the answer". Read from the policy
#: module rather than written here, so the screen and the engine draw the line
#: in the same place.
def _service_floor() -> float:
    try:
        from netgravity.config.defaults import SERVICE_THRESHOLDS

        return float(SERVICE_THRESHOLDS.get("fill_rate_floor", 0.95))
    except Exception:  # noqa: BLE001
        return 0.95


def _service_warning(best: Dict[str, Any], record: Optional[Dict[str, Any]]) -> str:
    """
    The thing a cost ranking must not be allowed to bury.

    "Cheapest" is a fact about cost and nothing else. A plan that costs less
    while stranding a third of demand, or while leaving a site at its limit,
    is cheaper and not therefore better — and a card headed with the cost
    alone invites exactly that reading.

    Returns "" only when there is genuinely nothing to warn about.
    """
    problems: List[str] = []

    fill = best.get("fill_rate")
    if isinstance(fill, (int, float)) and fill < _service_floor():
        problems.append(f"it still serves only {fill * 100:,.1f}% of demand")

    risk = str((record or {}).get("capacity_risk") or "").strip()
    if risk.lower() in ("high", "critical"):
        problems.append(f"capacity risk remains {risk.lower()}")

    if not problems:
        return ""
    joined = problems[0] if len(problems) == 1 else " and ".join(problems)
    return (f"This is the lower-cost option, but {joined}. The cheapest "
            f"scenario is not necessarily an acceptable one.")


def _comparison_verdict(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    What the numbers say, in one sentence, and what it rests on.

    No branch states a figure the KPI layer did not report, and none of them
    recommends acting — this says which scenario the comparison ranks first
    and why, which is a finding, not a decision.
    """
    if not rows:
        return {"recommended_scenario_id": None,
                "verdict": "No scenario was compared.", "caveats": []}

    best = rows[0]
    caveats: List[str] = []
    incomparable = [r for r in rows if not r["comparable"]]
    if incomparable:
        caveats.append(
            f"{len(incomparable)} scenario(s) produced no cost the engine could "
            f"compare, so they are listed but not ranked.")

    if not best["comparable"]:
        return {
            "recommended_scenario_id": None,
            "verdict": ("None of the compared scenarios produced a cost that can "
                        "be measured against the current network."),
            "caveats": caveats,
        }

    delta = best["cost_delta"]
    # Plain business English, and no engine vocabulary. It read "below the
    # current network on solved business network cost", which is a sentence
    # about a solver rather than about a decision.
    if delta < 0:
        verdict = (f"{best['name']} costs less than the network you run today, "
                   f"and less than the {len(rows) - 1} other option(s) compared."
                   if len(rows) > 1 else
                   f"{best['name']} costs less than the network you run today.")
    else:
        verdict = (f"Nothing compared costs less than the network you run "
                   f"today. {best['name']} comes closest.")

    if best["fill_delta"] is not None and best["fill_delta"] < -_MATERIAL_FILL_DROP_PTS:
        caveats.append(
            f"{best['name']} serves less demand than the network does today — "
            f"part of any saving is a smaller promise, not a cheaper way of "
            f"keeping the same one.")

    return {"recommended_scenario_id": best["scenario_id"],
            "verdict": verdict, "caveats": caveats, "best_row": best}


def _comparison_figures(best: Dict[str, Any],
                        record: Optional[Dict[str, Any]]) -> List[Any]:
    """
    Three numbers, and they are the three that decide this: what it costs,
    how much demand it serves, and whether capacity is at risk.

    Cost alone was the whole card, which is how "cheapest" came to read as
    "best" on a plan serving 68.5% of demand.
    """
    from netgravity.orchestrator.reasoning.card import Figure

    fill = best.get("fill_rate")
    risk = str((record or {}).get("capacity_risk") or "").strip()
    return [
        Figure.money("Cost", best.get("cost")),
        Figure(label="Demand served",
               value=(f"{fill * 100:,.1f}%" if isinstance(fill, (int, float))
                      else "Not available")),
        Figure(label="Capacity risk", value=risk or "Not available"),
    ]


def _comparison_explanation(project_id: str, rows: List[Dict[str, Any]],
                            verdict: Dict[str, Any],
                            baseline: Dict[str, Any],
                            figures: Optional[List[Any]] = None,
                            warning: str = "") -> Dict[str, Any]:
    """
    The comparison's own grounded briefing.

    One model request per SET of scenarios, keyed on the set — so comparing
    A and B twice, or reopening the Decision Package, spends nothing. See
    orchestrator/explanation_service.py.

    Never raises: an explanation is advisory, and the ranking beside it is
    perfectly good without one.
    """
    try:
        from netgravity.ingestion.config import IngestionConfig
        from netgravity.ingestion.storage import get_storage
        
        from netgravity.orchestrator.explanation_service import ExplanationService
        from netgravity.orchestrator.explanations import (
            KIND_COMPARISON,
            ExplanationStore,
        )
        from netgravity.orchestrator.reasoning.comparison_evidence import (
            comparison_reasoning_payload,
        )
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        scenario_ids = [r.get("scenario_id") for r in rows]
        service = ExplanationService(
            # The SHARED connection, not a bare agent. A bare
            # `ReasoningAgent()` has no gateway, so it produced
            # templates however the switch was set.
            explanation_reasoning_agent(),
            ExplanationStore(get_storage(IngestionConfig())))
        return service.explain(
            subject_id=project_id,
            kind=KIND_COMPARISON,
            scope=ReasoningScope.COMPARISON,
            # The SET identifies the analysis. Same set, any order, one call.
            result_parts=[scenario_ids, verdict.get("recommended_scenario_id")],
            build_payload=lambda: comparison_reasoning_payload(
                ranked=rows,
                recommended_scenario_id=verdict.get("recommended_scenario_id"),
                verdict=verdict.get("verdict", ""),
                baseline_cost=_valid(baseline, "business_network_cost"),
            ),
            # One switch for every explanation flow. Off by default; see
            # netgravity/orchestrator/explanation_llm.py.
            allow_llm=explanations_llm_enabled(),
            figures=figures,
            details=list(verdict.get("caveats") or []),
        )
    except Exception as exc:  # noqa: BLE001 — the ranking still stands
        logger.warning("scenario.comparison_explanation_failed: %s", exc)
        return {}


#: Solved-topology changes that permanently alter the physical network.
#: Mirrors STRUCTURAL_ACTIONS in orchestrator/governance/action_classifier.py.
_STRUCTURAL_ACTIONS = {"CLOSE_FACILITY", "OPEN_FACILITY", "ADD_FACILITY"}


def _is_structural(record: Dict[str, Any]) -> bool:
    """
    Whether this scenario opens or closes a site.

    Read from the SOLVED topology as well as the request, because a scenario
    that merely offered a site to the solver has not opened one, and a
    capacity change that made a site unviable has closed one.
    """
    if str((record.get("request") or {}).get("action") or "") in _STRUCTURAL_ACTIONS:
        return True
    before = record.get("baseline_facilities") or {}
    after = record.get("scenario_facilities") or {}
    for fid, state in after.items():
        was_open = bool((before.get(fid) or {}).get("isOpen"))
        is_open = bool((state or {}).get("isOpen"))
        if was_open != is_open:
            return True
    return False


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

    # Published so `/api/insights` can pair the network as it RUNS with the
    # same network re-solved freely, using this one cached solve rather than
    # running a second. Assigned at mount, so a process with no scenario
    # blueprint simply has no reference and the briefing omits the comparison.
    global optimised_reference_for
    optimised_reference_for = _optimised_reference

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

    @bp.route("/compare", methods=["POST"])
    @require_auth
    def compare_scenarios():
        """
        Rank a set of solved scenarios and say which one the numbers favour.

        This is the Decision Package's source. The ranking, the verdict and
        the caveats are decided HERE, from the authoritative KPI values and
        their statuses — not in the browser, where the reasoning would be
        invisible to the audit trail and free to disagree with the same
        numbers elsewhere on screen.

        It RANKS. It does not approve: a structural change is flagged as a
        human decision whatever the economics say, matching
        orchestrator/governance/action_classifier.py.
        """
        project_id, _ = _project_scope()
        body = request.get_json(silent=True) or {}
        wanted = [str(x) for x in (body.get("scenario_ids") or [])]

        _load_scenarios()
        with _lock:
            records = list(_store.get(project_id, []))
        by_id = {r.get("id"): r for r in records}
        if wanted:
            # A requested comparison that cannot be resolved is REFUSED, not
            # quietly widened. Falling back to every saved scenario answered a
            # different question than the one asked, under the heading of the
            # one asked — and the user had no way to see the substitution.
            unknown = [i for i in wanted if i not in by_id]
            if unknown:
                raise ValidationError(
                    "Some of the scenarios you asked to compare are not "
                    "available for this project, so the comparison was not "
                    "run.",
                    context={"unknown_scenario_ids": unknown,
                             "requested": wanted})
            selected = [by_id[i] for i in wanted]
        else:
            selected = records
        if not selected:
            raise ValidationError(
                "There is no solved scenario for this project to compare.")

        # One baseline for every row, from a scenario's own baseline_kpis —
        # the same snapshot solve each was measured against.
        baseline = selected[0].get("baseline_kpis") or {}
        rows = _rank_scenarios(baseline, selected)
        verdict = _comparison_verdict(rows)

        recommended = by_id.get(verdict["recommended_scenario_id"])
        caveats = list(verdict["caveats"])
        if recommended and recommended.get("reference_note"):
            caveats.append(recommended["reference_note"])

        # Cost NEXT TO service and risk, never cost alone. Supplied by code,
        # so the model states no figure and cannot state one in the wrong
        # currency. Money travels as an amount; the screen applies the
        # project's own currency to it.
        best_row = verdict.get("best_row") or {}
        warning = _service_warning(best_row, recommended)
        figures = _comparison_figures(best_row, recommended)

        return jsonify({
            "project_id": project_id,
            "baseline_kpis": baseline,
            "ranked": rows,
            "recommended_scenario_id": verdict["recommended_scenario_id"],
            "verdict": verdict["verdict"],
            "caveats": caveats,
            # Why the recommended one is preferable to the others — a
            # COMPARISON-scope briefing about the set, not about the winner
            # alone. Produced once per set of scenarios and saved against it,
            # so re-opening the Decision Package spends nothing.
            "explanation": _comparison_explanation(
                project_id, rows, verdict, baseline,
                figures=figures, warning=warning),
            # The one thing a cost ranking must not bury. Empty when there is
            # genuinely nothing to warn about.
            "warning": warning,
            "structural": bool(recommended and _is_structural(recommended)),
            "governance": {
                "classification": ("HUMAN_ONLY" if recommended
                                   and _is_structural(recommended) else "ANALYSIS"),
                "note": ("Opening or closing a site is a structural change and is "
                         "always a human decision, whatever the economics say."
                         if recommended and _is_structural(recommended)
                         else "This is an analysis of what the solver found."),
                "actioned": False,
            },
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
            # Growth the client states for one region and/or one product
            # category. Empty string and missing are the same thing — no scope,
            # i.e. the whole network, which is what this endpoint did before.
            demand_region=(
                (str(body.get("demand_region") or "").strip() or None)
                if action == ScenarioActionType.CHANGE_DEMAND else None),
            demand_product_category=(
                (str(body.get("demand_product_category") or "").strip() or None)
                if action == ScenarioActionType.CHANGE_DEMAND else None),
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
            # This run produces the scenario's OWN explanation, so it honours
            # the explanation switch. The two solves above do not: the
            # baseline and the re-optimised reference are numbers, and nothing
            # narrates them.
            #
            # The reasoning step inside is `single_request=True`, so a live
            # scenario costs exactly one model request, once, saved against
            # the run.
            disable_llm=not explanations_llm_enabled(),
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
            # THIS scenario's own explanation, from the reasoning step the
            # scenario workflow already runs (`_reason_and_govern`). It was
            # computed on every simulate and returned on none of them, so a
            # screen that wanted to explain a what-if had only the network's
            # general briefing to show — an explanation of something else,
            # next to this scenario's numbers.
            "explanation": _scenario_explanation(ctx),
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
