"""
NetGravity — Project Workspace API Blueprint
============================================
Project lifecycle, ownership, and network-snapshot binding.

Phase 10.0 rewrite. The prototype version served a hardcoded five-project list
in which every entry carried the identical `snapshot_id: "snap_case16_synthetic"`,
had no owner field, and was mutated by a module-level list shared across all
users. Projects are now owned, isolated, and bound to a real snapshot only once
data has actually been ingested for them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, g, jsonify, request

from app.backend.services.errors import ApplicationError
from app.backend.services.project_registry import project_registry
from app.backend.services.security import require_auth

logger = logging.getLogger(__name__)

projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")


@projects_bp.route("", methods=["GET"])
@require_auth
def list_projects():
    """
    List the caller's own projects plus shared demo workspaces.

    Another user's projects are never returned, and the filter is applied in
    the registry rather than here so no route can forget it.
    """
    records = project_registry.list_for(g.current_user.user_id)

    query = str(request.args.get("search") or "").lower().strip()
    region_filter = str(request.args.get("region") or "").strip()

    if query:
        records = [
            r for r in records
            if query in r.name.lower()
            or query in r.client.lower()
            or query in r.region.lower()
        ]
    if region_filter and region_filter != "All":
        records = [r for r in records if r.region == region_filter]

    return jsonify({
        "projects": [r.to_dict() for r in records],
        "total": len(records),
    }), 200


@projects_bp.route("/<project_id>", methods=["GET"])
@require_auth
def get_project(project_id: str):
    """Retrieve one project. 403 if it belongs to another user, 404 if absent."""
    record = project_registry.get(project_id, user_id=g.current_user.user_id)
    return jsonify(record.to_dict()), 200


@projects_bp.route("", methods=["POST"])
@require_auth
def create_project():
    """
    Create a workspace owned by the caller.

    The new project starts with `snapshot_id: None` / `has_network: false`. It
    is not silently pointed at the bundled synthetic network, so any analysis
    requested before ingestion returns NO_NETWORK_BOUND rather than figures
    describing a network the user never uploaded.
    """
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    record = project_registry.create(
        name=str(body.get("name") or ""),
        owner_id=g.current_user.user_id,
        region=str(body.get("region") or "India"),
        client=str(body.get("client") or ""),
        description=str(body.get("description") or ""),
    )
    return jsonify(record.to_dict()), 201


@projects_bp.route("/<project_id>", methods=["PUT", "PATCH"])
@require_auth
def update_project(project_id: str):
    """Update mutable project metadata. `snapshot_id` is not client-writable."""
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    record = project_registry.update(
        project_id,
        user_id=g.current_user.user_id,
        name=body.get("name"),
        region=body.get("region"),
        client=body.get("client"),
        description=body.get("description"),
        status=body.get("status"),
    )
    return jsonify(record.to_dict()), 200


@projects_bp.route("/<project_id>/snapshot", methods=["GET"])
@require_auth
def get_project_snapshot(project_id: str):
    """
    The snapshot this project's analysis runs against.

    Returns 409 NO_NETWORK_BOUND when no data has been ingested — the honest
    answer, and the signal the frontend uses to render its empty state.
    """
    snapshot_id = project_registry.snapshot_for(project_id, user_id=g.current_user.user_id)
    return jsonify({"project_id": project_id, "snapshot_id": snapshot_id}), 200


@projects_bp.errorhandler(ApplicationError)
def _project_error(exc: ApplicationError):
    return jsonify(exc.to_payload()), exc.http_status
