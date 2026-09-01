"""
NetGravity — Insights API Blueprint
===================================
Project-scoped insights and recommendations for the network a project is bound
to, produced by the Orchestrator's Reasoning Agent from the solved Digital Twin
state.

Why this endpoint exists
------------------------
The insight machinery was complete and unreachable. `POST
/orchestrator/insights` existed and worked; `reasoning-service.js` existed and
wrapped it; the Reasoning Agent existed and produced grounded briefings. But
nothing on any screen called any of it, and the two structures the dashboard
reads its insight feed from — `HOME_INSIGHTS` and `HOME_ACTION_ITEMS` — were
initialised empty and never written by anything. So every user who uploaded
their own data saw "No insights have been generated for this network yet",
permanently, on a network that had been fully solved.

The orchestrator endpoint could not close that on its own, because it is keyed
by Digital Twin `state_id`. A dashboard holds a `project_id`. Resolving one to
the other means knowing that a project has a snapshot, that a snapshot has a
twin state, and which of several states is the one the KPIs came from — control
plane knowledge that has no business being in a browser.

So this blueprint answers the question the dashboard actually has ("what should
I know about this project's network?") and follows the same shape as
`api/kpis.py`: authenticated, project-scoped, rate-limited, and cached in the
same durable analysis store — a briefing is derived data about one version of
one network, so it is computed once per version rather than per request.

What it does NOT do
-------------------
It does not compute a KPI, run a solve of its own, or invent a figure. Every
number in every insight comes from the Reasoning Agent's evidence pack, which
is built from the twin state, which is built from the MILP result. The numeric
grounding verdict travels with the response, so a consumer can see whether the
narrative was checked against the deterministic facts rather than assuming it.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    ConflictError,
    EngineUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.analysis_store import analysis_service
from app.backend.services.project_registry import project_registry
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import require_auth
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.schemas.reasoning import ReasoningScope
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

logger = logging.getLogger(__name__)

#: Scopes a caller may ask for. `COMPARISON` is deliberately absent: it needs a
#: second state, which is the scenario comparison endpoint's job.
_ALLOWED_SCOPES = {"NETWORK", "FACILITY", "LANE"}

#: Shape of this endpoint's response. Part of the cache key — see the note at
#: the `variant` assignment. Bump on any field added to or removed from the
#: body, including inside `insights[]` and `evidence[]`.
#:
#:   1  the original briefing payload
#:   2  evidence gained `value` and `role`; insights gained `entities`;
#:      the body gained `thresholds` and `series`; ids gained a headline digest
_PAYLOAD_VERSION = 2


#: Theme -> the per-facility field that theme is ABOUT. A chart for a finding
#: about utilisation plots utilisation; one about footprint plots throughput.
#: Absent from this map means the theme is not a per-facility statement, and no
#: entity rows are sent — an empty list is the honest answer, not a fallback to
#: whichever field happens to be present.
_FACILITY_THEME_FIELD = {
    "Capacity": "utilization_pct",
    "Utilisation": "utilization_pct",
    "Footprint": "throughput_units",
    "Resilience": "rei",
}

#: Themes whose subject is a lane rather than a site.
_LANE_THEME_FIELD = {
    "Carbon": "carbon_kg",
}

#: Ceiling on entity rows in one insight. A 400-facility network would
#: otherwise put 400 rows on the wire for a chart that can show perhaps 30.
_MAX_ENTITY_ROWS = 30


def _headline_digest(insight: Any) -> str:
    """
    A short, stable discriminator for two findings sharing a theme.

    Derived from the headline rather than from the position in the list: rank
    shifts when a different insight outranks it, and an id that moves is an id
    a dismissal cannot follow.
    """
    import hashlib

    headline = str(getattr(insight, "headline", "") or "")
    return hashlib.sha1(headline.encode("utf-8")).hexdigest()[:8].upper()


def _finite(value: Any) -> Optional[float]:
    """The value as a plottable float, or None. Bools are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _resolve_entities(insight: Any, pack: Any) -> List[Dict[str, Any]]:
    """
    The facilities or lanes a finding was computed OVER.

    `_utilization_insights` sorts every site by utilisation, names the worst
    three in prose, and discards the list. The sentence "3 sites are above the
    90% threshold" therefore reached the browser citing one scalar, so a screen
    could draw the maximum and nothing else. These are those rows — already
    built, already authoritative, previously thrown away.

    Sorted by the field the theme is about, descending, so a bar chart drawn in
    array order is ranked without the client sorting formatted strings.
    """
    if pack is None:
        return []
    payload = getattr(pack, "payload", {}) or {}
    theme = str(getattr(insight, "theme", "") or "")

    field = _FACILITY_THEME_FIELD.get(theme)
    if field:
        rows = []
        for item in payload.get("facilities", []) or []:
            value = _finite(item.get(field))
            if value is None:
                continue
            rows.append({
                "kind": "FACILITY",
                "entity_id": item.get("facility_id"),
                "label": item.get("facility_name") or item.get("facility_id"),
                "metric": field,
                "value": value,
                # Second-order facts a chart legend or tooltip needs, and which
                # a client must never recompute: a utilisation bar means one
                # thing for an open site and another for a closed one.
                "is_open": bool(item.get("is_open")),
                "role": item.get("role"),
                "capacity_units": _finite(item.get("capacity_units")),
                "throughput_units": _finite(item.get("throughput_units")),
            })
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows[:_MAX_ENTITY_ROWS]

    lane_field = _LANE_THEME_FIELD.get(theme)
    if lane_field:
        rows = []
        for item in payload.get("flows", []) or []:
            value = _finite(item.get(lane_field))
            if value is None:
                continue
            origin, dest = item.get("origin_id"), item.get("destination_id")
            rows.append({
                "kind": "LANE",
                "entity_id": f"{origin}->{dest}",
                "label": f"{origin} → {dest}",
                "metric": lane_field,
                "value": value,
                "flow_units": _finite(item.get("flow_units")),
                "distance_km": _finite(item.get("distance_km")),
            })
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows[:_MAX_ENTITY_ROWS]

    return []


def _thresholds_from(pack: Any) -> Dict[str, Any]:
    """The policy thresholds indexed into the pack, or an empty block."""
    payload = getattr(pack, "payload", {}) or {}
    block = payload.get("thresholds") or {}
    return {k: v for k, v in block.items() if _finite(v) is not None}


def _network_series(pack: Any) -> Dict[str, Any]:
    """
    Whole-network breakdowns a chart can draw, with raw values.

    Only what the solve actually produced. There is deliberately no utilisation
    time series here: see `_period_series`, which returns one when — and only
    when — the network states more than one demand period.
    """
    if pack is None:
        return {}
    payload = getattr(pack, "payload", {}) or {}
    network = payload.get("network_state") or {}
    out: Dict[str, Any] = {}

    components = network.get("cost_components") or {}
    if isinstance(components, dict):
        rows = [{"label": str(k).replace("_", " ").title(), "key": k,
                 "value": _finite(v)}
                for k, v in components.items() if _finite(v) is not None]
        # Ranked, and zero-valued components dropped: a cost breakdown listing
        # six components of which four are 0.00 reads as a solver failure.
        rows = [r for r in rows if r["value"] != 0.0]
        rows.sort(key=lambda r: r["value"], reverse=True)
        if rows:
            out["cost_components"] = rows

    return out


def _resolve_evidence(refs: List[str], pack: Any,
                      role: str = "metric") -> List[Dict[str, Any]]:
    """
    The metrics an insight cites, with their authoritative values.

    A deep-dive screen has to show what a finding is BASED on, and a list of
    opaque refs (`network_state.avg_utilization_pct`) is not that. Resolved here
    from the same evidence pack the narrative was written against, so the figure
    on the screen and the figure in the sentence cannot disagree.

    `value` carries the RAW number beside the formatted `display_value`.
    Without it a chart had two options, both bad: parse `"₹1,234,567.00"` back
    into a float in the browser — locale grouping, a currency glyph, `"12,000
    units"`, `"92.41%"` and the literal `"Not available"` all in the same field
    — or invent its own series. `display_value` stays authoritative for
    anything a user READS, so a figure in prose and the same figure on an axis
    cannot drift apart; `value` exists only to be plotted.

    `role` says WHY the figure is cited — the measurement, the thing it was
    compared against, or the driver behind it. The three ref lists used to be
    concatenated into one flat array, which threw that distinction away and
    left a table unable to label its own rows.
    """
    out: List[Dict[str, Any]] = []
    metrics = getattr(pack, "metrics", {}) or {}
    for ref in refs:
        metric = metrics.get(ref)
        if metric is None:
            continue
        raw = getattr(metric, "value", None)
        out.append({
            "ref": ref,
            "label": metric.label,
            "display_value": metric.display_value,
            # Only real, finite numbers. A bool is an int in Python and would
            # plot as 0/1; None means the engine could not compute it, and a
            # chart must render that as a gap rather than as zero.
            "value": (raw if isinstance(raw, (int, float))
                      and not isinstance(raw, bool)
                      and math.isfinite(raw) else None),
            "unit": metric.unit,
            "source": metric.source,
            "entity_id": metric.entity_id,
            "role": role,
        })
    return out


def _serialise_insight(insight: Any, index: int, *, scope: str,
                       entity_id: Optional[str],
                       pack: Any = None) -> Dict[str, Any]:
    """
    One KPI insight, in the shape a feed can render.

    `id` is derived from the scope, the entity and the theme rather than being
    random, so the same finding keeps the same identity across refreshes — a
    feed that lets a user dismiss an item needs an id that survives a re-fetch,
    and a UUID per request would resurrect everything they had dismissed.
    """
    theme = str(getattr(insight, "theme", "") or "GENERAL")
    slug = theme.upper().replace(" ", "_")
    entity = (entity_id or "NETWORK").replace(" ", "_")
    severity = getattr(insight, "severity", None)
    metric_refs = list(getattr(insight, "metric_refs", []) or [])
    comparison_refs = list(getattr(insight, "comparison_refs", []) or [])
    driver_refs = list(getattr(insight, "driver_refs", []) or [])
    return {
        # The theme alone is not unique within a scope: `_service_insights` can
        # emit two `theme="Service"` findings (unserved demand, and SLA), and
        # both used to serialise to INS_NETWORK_NETWORK_SERVICE. The deep dive
        # looks a record up BY id, so the second insight's card opened the
        # first insight's page. The headline discriminates them, and a short
        # digest of it keeps the id stable across refreshes — which is what a
        # dismissable feed needs, and what a UUID per request would destroy.
        "id": f"INS_{scope}_{entity}_{slug}_{_headline_digest(insight)}",
        "theme": theme,
        "headline": getattr(insight, "headline", ""),
        "narrative": getattr(insight, "narrative", ""),
        # Stated by the engine, not inferred from the wording by the client.
        # The Home feed used to decide a card's colour, icon and priority by
        # searching its prose for "high impact" / "opportunity" / "positive",
        # so an insight phrased differently was rendered neutral whatever it
        # had found.
        "severity": (severity.value if hasattr(severity, "value")
                     else str(severity or "INFORMATION")),
        "metric_refs": metric_refs,
        "comparison_refs": comparison_refs,
        "driver_refs": driver_refs,
        # The figures this finding rests on, with their authoritative values.
        # A deep dive needs to show its basis, and the alternative — a screen
        # inventing plausible before/after numbers to fill the space — is what
        # this replaces.
        #
        # Still one flat list, because that is what the table renders, but each
        # row now says which role it played.
        "evidence": (
            _resolve_evidence(metric_refs, pack, role="metric")
            + _resolve_evidence(comparison_refs, pack, role="comparison")
            + _resolve_evidence(driver_refs, pack, role="driver")
        ) if pack is not None else [],
        # The facilities or lanes this finding was computed OVER, not merely the
        # one scalar it cites. "3 sites are above the threshold" cited only
        # `max_utilization_pct`, so a screen could name the worst site and
        # nothing else — the three rows behind the sentence were built, used to
        # write the prose, and dropped. A chart needs the rows.
        "entities": _resolve_entities(insight, pack),
        "rank": index + 1,
    }


def create_insights_blueprint(orchestrator: Optional[Orchestrator] = None,
                              url_prefix: str = "/api/insights"):
    bp = Blueprint("insights", __name__, url_prefix=url_prefix)

    def _resolve_state(project_id: str, user_id: str) -> Any:
        """
        The Digital Twin state the project's current figures came from, built if
        this process does not have one.

        Twin states live in a process-local store while the KPI analysis is
        durable, so the two do not survive a restart together. That asymmetry
        made this endpoint return 409 "not solved yet" on a network that had
        been solved, whose KPIs were on screen, and whose analysis had just been
        restored from the database — the report was about the process, not about
        the network.

        So when no state is present, the baseline execution is run to publish
        one. It costs one solve, once per process per snapshot, and the briefing
        it produces is then cached in the durable analysis store (see
        `_briefing_analysis`) so the next process pays nothing.

        Prefers an OPTIMIZED state: that is what the KPI endpoints report, and
        an insight describing a different state from the numbers beside it is
        worse than no insight.
        """
        snapshot_id = project_registry.snapshot_for(project_id, user_id=user_id)
        refs = orchestrator.twin.list_states(snapshot_id)
        if not refs:
            logger.info("insights.publishing_twin_state snapshot=%s", snapshot_id)
            orchestrator.run_sync(OrchestratorRequest(
                input="Baseline network state for insight generation",
                explicit_intent=Intent.NETWORK_STATE_QUERY,
                actor=Actor(actor_id=user_id, role=ActorRole.PLANNER),
                network_snapshot_id=snapshot_id,
                disable_llm=True,
            ))
            refs = orchestrator.twin.list_states(snapshot_id)
        if not refs:
            raise ConflictError(
                "This network could not be solved, so there is nothing to "
                "explain. The KPI endpoints report why.",
                context={"project_id": project_id, "snapshot_id": snapshot_id},
            )
        optimized = [r for r in refs
                     if str(getattr(r, "state_type", "")).upper().endswith("OPTIMIZED")]
        chosen = (optimized or refs)[-1]
        return snapshot_id, orchestrator.twin.materialize(chosen.state_id)

    def _briefing_for(state: Any, scope: ReasoningScope,
                      entity_id: Optional[str], question: str,
                      allow_llm: bool) -> Any:
        """Returns `(ReasoningResult, ReasoningEvidencePack)`."""
        from netgravity.orchestrator.reasoning.evidence import (
            build_evidence_pack, twin_reasoning_payload, with_policy_thresholds,
        )

        # Wrapped, not called bare. `with_policy_thresholds` exists precisely so
        # a narrative may cite "the 90% threshold" without the numeric validator
        # adjudicating 90 against whatever unrelated percentage it finds nearest
        # — and this endpoint was calling the payload builder directly, so the
        # thresholds reached neither the pack nor the response. A chart drawing
        # a threshold line would otherwise have to hardcode 90/40, i.e. restate
        # a policy constant it does not own.
        payload = with_policy_thresholds(twin_reasoning_payload(
            state, scope=scope, entity_id=entity_id, comparison=None))
        unavailable = {
            item.field: {"status": item.status.value, "reason": item.reason}
            for item in state.unavailable
        }
        # Built here as well as inside the agent. It is a pure, cheap indexing
        # function over the same payload, so this is reuse rather than a second
        # implementation — and it is what lets the response carry the value
        # behind every ref the narrative cites.
        pack = build_evidence_pack(
            payload, scope=scope, entity_id=entity_id, user_question=question,
            unavailable=unavailable, provenance={"state_id": state.state_id},
        )
        agent = orchestrator.services["reasoning_agent"]
        result = agent.reason(
            payload,
            unavailable_evidence=unavailable,
            provenance={
                "state_id": state.state_id,
                "snapshot_id": state.snapshot_id,
                "scenario_id": state.scenario_id or "",
            },
            allow_llm=allow_llm,
            scope=scope,
            entity_id=entity_id,
            user_question=question,
        )
        return result, pack

    # ------------------------------------------------------------------
    @bp.route("", methods=["GET"])
    @require_auth
    @rate_limit("insights.read", limit=120, window_seconds=60)
    def get_insights():
        """
        Insights and a recommendation for a project's network.

        Query:
            ``project_id``  required
            ``scope``       NETWORK (default) | FACILITY | LANE
            ``entity_id``   required for FACILITY and LANE
            ``question``    an optional question to answer alongside
            ``use_llm``     ``1`` to allow a model to phrase the briefing.
                            Off by default: the deterministic template is
                            grounded by construction and costs nothing, and a
                            dashboard load should not spend a model call per
                            facility.
        """
        if orchestrator is None:
            raise EngineUnavailableError("The reasoning engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")

        scope_arg = str(request.args.get("scope") or "NETWORK").strip().upper()
        if scope_arg not in _ALLOWED_SCOPES:
            raise ValidationError(
                f"scope must be one of {', '.join(sorted(_ALLOWED_SCOPES))}.")
        entity_id = str(request.args.get("entity_id") or "").strip() or None
        if scope_arg in {"FACILITY", "LANE"} and not entity_id:
            raise ValidationError(f"scope={scope_arg} requires an entity_id.")

        question = str(request.args.get("question") or "")[:1000]
        allow_llm = request.args.get("use_llm") == "1"
        scope = ReasoningScope(scope_arg)

        payload = _briefing_analysis(project_id, scope_arg, scope, entity_id,
                                     question, allow_llm)
        return jsonify(payload), 200

    def _briefing_analysis(project_id: str, scope_arg: str, scope: ReasoningScope,
                           entity_id: Optional[str], question: str,
                           allow_llm: bool) -> Dict[str, Any]:
        """
        The briefing for one scope, cached per network version.

        Cached in the same durable store the KPI analysis uses, under its own
        variant. A briefing is derived data about one version of one network,
        exactly as the KPIs are: it does not change until the network does, so
        recomputing it per request would pay for a reasoning pass — and, on a
        fresh process, a solve — on every dashboard load and every facility
        click.
        """
        user_id = g.current_user.user_id
        snapshot_id = project_registry.snapshot_for(project_id, user_id=user_id)
        snapshot = orchestrator.snapshots.get(snapshot_id)

        # A question or a model call makes the answer specific to this request,
        # so neither is cached: an ad-hoc question must not be served to the next
        # caller who asks a different one.
        cacheable = not question and not allow_llm
        # The payload SHAPE is part of the cache key, not just the network.
        #
        # A cached briefing is invalidated when the network changes
        # (`data_version`), which is right for the figures but says nothing
        # about the fields. Adding `value`, `role`, `entities`, `thresholds`
        # and `series` to this response changed the shape while leaving every
        # `data_version` untouched, so every project that had ever loaded its
        # insights kept being served the older, thinner payload — the browser
        # showed `thresholds: {}` and `value: null` while the endpoint itself
        # demonstrably returned both. A stale cache that outlives a deploy is
        # indistinguishable, from the client's side, from a broken serialiser.
        #
        # Bump this whenever a field is added to or removed from the response.
        variant = f"insights:v{_PAYLOAD_VERSION}:{scope_arg}:{entity_id or ''}"

        def compute() -> Dict[str, Any]:
            _, state = _resolve_state(project_id, user_id)
            try:
                result, pack = _briefing_for(state, scope, entity_id, question,
                                            allow_llm)
            except ValueError as exc:
                # `twin_reasoning_payload` raises this when the entity is not in
                # the state — a client asking about a facility this network does
                # not have, which is a bad request rather than a server fault.
                raise NotFoundError(str(exc)) from exc

            briefing = result.briefing
            return {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "state_id": state.state_id,
                "scenario_id": state.scenario_id,
                "scope": scope_arg,
                "entity_id": entity_id,
                "insights": [
                    _serialise_insight(item, i, scope=scope_arg,
                                       entity_id=entity_id, pack=pack)
                    for i, item in enumerate(briefing.kpi_insights)
                ],
                # The policy constants a threshold line may be drawn at, so the
                # chart and the sentence quote the same number and neither
                # hardcodes it. Sourced from `UTILIZATION_THRESHOLDS`.
                "thresholds": _thresholds_from(pack),
                # Whole-network series a chart can plot without inventing one.
                # `cost_components` was reaching the browser as a single ref —
                # the largest component only — so a breakdown chart had one
                # slice and no total.
                "series": _network_series(pack),
                # The recommendation is ONE string chosen by the evidence, not a
                # list of options. A list would imply the engine had ranked
                # alternatives it has not evaluated.
                "recommendation": briefing.recommendation,
                "opening": briefing.opening,
                "context": briefing.context,
                "key_drivers": list(briefing.key_drivers),
                "limitation": briefing.limitation,
                "suggested_questions": list(briefing.suggested_questions),
                "missing_information": [m.model_dump(mode="json")
                                        for m in briefing.missing_information],
                "evidence_completeness": briefing.evidence_completeness.value,
                # Whether the narrative's numbers were checked against the
                # deterministic results, and what failed if any did. A consumer
                # that renders prose must be able to see this.
                "grounding": {
                    "status": result.grounding_status,
                    "warnings": list(result.validation_warnings),
                    "source": result.source,
                },
                "provenance": {
                    "authoritative_source":
                        "netgravity.orchestrator.agents.reasoning_agent",
                    "evidence_from": "digital_twin_state",
                    "llm_used": result.source != "template",
                },
            }

        if not cacheable:
            body = compute()
            body["computed_at"] = time.time()
            return body
        return analysis_service.get(
            snapshot_id, snapshot.data_version, compute, variant=variant)

    @bp.errorhandler(ApplicationError)
    def _insight_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status

    return bp
