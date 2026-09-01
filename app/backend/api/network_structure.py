"""
NetGravity — Bound Network Structure
====================================
Serves the *structure* of the network bound to a project: its facilities,
markets, products and lanes, exactly as they were registered.

Why this is separate from the KPI endpoints
-------------------------------------------
Structure is INPUT. A facility's id, name, role, coordinates and stated
capacity are things the user uploaded; they are true whether or not a solve
succeeds. Utilisation and throughput are OUTPUT — they only exist once the
MILP has produced flows.

Conflating the two is what made an infeasible network look like an empty one.
`facility_kpis` correctly returns nothing when there is no solved state, and
the Digital Twin read its node list from that endpoint — so a network the
solver could not satisfy rendered as a blank map with no facilities and no
markets, even though the user's eight facilities and seven markets were sitting
in the snapshot the whole time.

This module runs no analysis and computes no KPI. It reads the registered
snapshot and returns it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from flask import Blueprint, g, jsonify, request

from app.backend.services.demand_history_store import (
    capacity_history_store,
    uploaded_signal_store,
)
from app.backend.services.errors import EngineUnavailableError, ValidationError
from app.backend.services.project_registry import project_registry
from app.backend.services.security import require_auth
from netgravity.schemas.network import OptimizationConfig

logger = logging.getLogger(__name__)


def _role_of(facility: Any) -> str:
    role = getattr(facility, "role", None)
    return getattr(role, "value", None) or str(role or "")


def create_network_structure_blueprint(orchestrator: Optional[Any] = None,
                                       url_prefix: str = "/api/network"):
    bp = Blueprint("network_structure", __name__, url_prefix=url_prefix)

    @bp.route("/structure", methods=["GET"])
    @require_auth
    def get_structure():
        """The bound network's nodes and lanes, as registered."""
        if orchestrator is None:
            raise EngineUnavailableError("The analysis engine is not mounted.")

        project_id = str(request.args.get("project_id") or "").strip()
        if not project_id:
            raise ValidationError("A project_id is required.")

        snapshot_id = project_registry.snapshot_for(
            project_id, user_id=g.current_user.user_id
        )
        snapshot = orchestrator.snapshots.get(snapshot_id)
        network = snapshot.network

        # The client's own recorded utilisation, from the uploaded capacity
        # history. Reported beside the stated capacity because it is the same
        # kind of thing — something the client told us — and NOT beside the
        # solver's utilisation, which is a model output for a different period.
        observed = capacity_history_store.latest_utilisation(network.network_id)

        plants, dcs, markets = [], [], []
        for facility in network.facilities:
            role = _role_of(facility)
            node: Dict[str, Any] = {
                "id": facility.id,
                "name": facility.name,
                "role": role,
                "lat": facility.latitude,
                "lng": facility.longitude,
                # Stated capacity, from the upload. Not a measurement of flow.
                "capacity": facility.capacity_units_per_period,
                "handlingCost": facility.handling_cost_per_unit,
                "fixedCostPerYear": facility.fixed_cost_per_year,
                # None when the upload carried no capacity history for this
                # facility — never zero, which would read as "recorded idle".
                "observed": observed.get(facility.id),
            }
            if role == "PLANT":
                node["productionCapacity"] = facility.production_capacity_units_per_period
                plants.append(node)
            elif role == "MARKET":
                markets.append(node)
            else:
                dcs.append(node)

        demand_by_market: Dict[str, float] = {}
        sla_by_market: Dict[str, Any] = {}
        periods: List[Any] = []
        for record in network.demands:
            demand_by_market[record.market_id] = (
                demand_by_market.get(record.market_id, 0.0) + record.quantity
            )
            if record.period is not None and record.period not in periods:
                periods.append(record.period)
            sla = getattr(record, "sla_days", None)
            if sla is not None:
                prior = sla_by_market.get(record.market_id)
                sla_by_market[record.market_id] = sla if prior is None else min(prior, sla)

        for market in markets:
            market["demand"] = demand_by_market.get(market["id"])
            market["slaDays"] = sla_by_market.get(market["id"])

        lanes = [
            {
                "from": lane.origin_id,
                "to": lane.destination_id,
                "ratePerUnit": lane.rate_per_unit,
                "distanceKm": lane.distance_km,
                "leadTimeDays": lane.lead_time_days,
                "capacity": lane.lane_capacity,
                "mode": getattr(lane.mode, "value", str(lane.mode)),
            }
            for lane in network.lanes
        ]

        return jsonify({
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "network_id": network.network_id,
            "data_version": network.data_version,
            # The periods this network's own demand rows are stated for, and
            # the calendar length the optimiser prices a period at.
            #
            # The screens labelled every capacity, throughput and flow figure
            # "units/day" and offered a period selector of four fixed quarters
            # ("Q3 2026" … "Q4 2025"). Neither came from the data: capacity is
            # `capacity_units_per_period`, and the period is whatever the
            # upload said it was. A monthly figure shown as a daily one is a
            # thirty-fold misstatement of the client's own number.
            "periods": sorted(periods, key=lambda p: (isinstance(p, str), p)),
            "costPeriod": getattr(OptimizationConfig().cost_period, "value", "MONTH"),
            # The periods the CLIENT'S OWN RECORDS cover, which is a different
            # and much longer list than the one above.
            #
            # `network.demands` carries the periods the MODEL solves, and the
            # assembler collapses an uploaded history to its latest period — so
            # `periods` above is `[1]` for every real upload, and a selector
            # built on it had exactly one disabled option. The recorded capacity
            # history is per facility per period and untouched by that collapse:
            # 36 months of stated available and used capacity, which is the one
            # genuine time series an uploaded network has.
            #
            # Reported separately, and named for what it is, because a screen
            # must not present a measurement of the past as the plan's own
            # horizon. `observedPeriods` is history; `periods` is the model.
            "observedPeriods": capacity_history_store.periods(network.network_id),
            "observedUtilisation": capacity_history_store.utilisation_series(
                network.network_id),
            "plants": plants,
            "dcs": dcs,
            "markets": markets,
            "lanes": lanes,
            "products": [
                {"id": p.id, "name": p.name} for p in network.products
            ],
            # External signals that arrived with the upload. Context, not
            # structure — the screen used to show the prototype's own signals
            # ("North India GDP Growth Accelerating", RBI Quarterly Bulletin)
            # for every network.
            "signals": uploaded_signal_store.get(network.network_id),
            "notice": (
                "Structure as uploaded. Utilisation and throughput are solver "
                "outputs and are reported by /api/kpis/facilities, which is "
                "empty until a solve succeeds."
            ),
        }), 200

    return bp
