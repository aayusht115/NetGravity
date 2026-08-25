"""Flask API for upload, provisional draft, clarification, and finalization."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService, IngestionServiceError

ALLOWED_UPLOAD_SUFFIXES = {
    ".csv", ".tsv", ".xlsx", ".xlsm", ".pdf", ".txt", ".md",
}
MAX_UPLOAD_FILES = 50


def create_ingestion_blueprint(config: Optional[IngestionConfig] = None,
                               url_prefix: str = "/api/ingestions"):
    from flask import Blueprint, jsonify, request
    from werkzeug.utils import secure_filename

    service = IngestionService(config)
    bp = Blueprint("ingestions", __name__, url_prefix=url_prefix)

    def error(exc: Exception, status: Optional[int] = None):
        details = getattr(exc, "details", {})
        code = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).upper()
        return jsonify({
            "error": {
                "code": code,
                "message": str(exc),
                "details": details,
            }
        }), status or getattr(exc, "status_code", 400)

    def session_payload(session, include_draft: bool = False) -> Dict[str, Any]:
        payload = session.as_dict(include_draft=include_draft)
        payload.pop("source", None)  # never expose a server filesystem path to the UI
        payload["links"] = {
            "self": f"{url_prefix}/{session.run_id}",
            "draft": f"{url_prefix}/{session.run_id}/draft",
            "reviews": f"{url_prefix}/{session.run_id}/reviews",
            "finalize": f"{url_prefix}/{session.run_id}/finalize",
        }
        return payload

    @bp.route("", methods=["POST"])
    def upload():
        """Accept multipart files and immediately return a provisional run."""
        files = request.files.getlist("files") or request.files.getlist("file")
        if not files:
            return error(ValueError("at least one file is required"), 400)
        if len(files) > MAX_UPLOAD_FILES:
            return error(ValueError(f"at most {MAX_UPLOAD_FILES} files are allowed"), 413)

        run_id = f"ing_{uuid.uuid4().hex[:12]}"
        upload_root = service.config.raw_path / "ingestion_uploads" / run_id
        categories: Dict[str, str] = {}
        try:
            categories = json.loads(request.form.get("categories") or "{}")
            if not isinstance(categories, dict):
                raise ValueError("categories must be a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            return error(ValueError(f"invalid categories: {exc}"), 400)

        saved = []
        for uploaded in files:
            original = uploaded.filename or ""
            filename = secure_filename(original)
            suffix = Path(filename).suffix.lower()
            if not filename or suffix not in ALLOWED_UPLOAD_SUFFIXES:
                return error(ValueError(f"unsupported upload '{original}'"), 415)

            category = str(categories.get(original) or categories.get(filename) or "").lower()
            if category not in {"", "tabular", "contract", "signal"}:
                return error(ValueError(
                    f"category for '{original}' must be tabular, contract, or signal"), 400)
            if not category:
                category = "contract" if suffix in {".pdf", ".txt", ".md"} else "tabular"
            folder = {"contract": "contracts", "signal": "signals"}.get(category, "")
            target_dir = upload_root / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            counter = 2
            while target.exists():
                target = target_dir / f"{Path(filename).stem}_{counter}{suffix}"
                counter += 1
            uploaded.save(target)
            saved.append({"filename": target.name, "category": category})

        try:
            session = service.start(
                upload_root,
                client_id=str(request.form.get("client_id") or "default"),
                run_id=run_id,
            )
        except IngestionServiceError as exc:
            return error(exc)
        payload = session_payload(session)
        payload["uploaded_files"] = saved
        status = {
            "AWAITING_REVIEW": 202,
            "FAILED": 422,
        }.get(session.status, 201)
        return jsonify(payload), status

    @bp.route("/<run_id>", methods=["GET"])
    def get_run(run_id: str):
        try:
            return jsonify(session_payload(service.get(run_id))), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/draft", methods=["GET"])
    def get_draft(run_id: str):
        try:
            session = service.get(run_id)
            return jsonify({
                "run_id": run_id,
                "revision": session.revision,
                "status": session.status,
                "draft": session.draft,
            }), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/reviews", methods=["GET"])
    def get_reviews(run_id: str):
        try:
            session = service.get(run_id)
            return jsonify({
                "run_id": run_id,
                "revision": session.revision,
                "status": session.status,
                "review": session.review,
            }), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/reviews/<path:item_id>/analyse", methods=["POST"])
    def analyse_item(run_id: str, item_id: str):
        body = request.get_json(silent=True) or {}
        try:
            payload = service.analyse_item(
                run_id, item_id, str(body.get("user_text") or body.get("question") or ""))
            return jsonify(payload), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/reviews/analyse", methods=["POST"])
    def analyse_item_from_body(run_id: str):
        """Browser-friendly variant: item ids may contain '#' and stay in JSON."""
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("item_id") or "")
        if not item_id:
            return error(ValueError("item_id is required"), 400)
        try:
            payload = service.analyse_item(
                run_id, item_id,
                str(body.get("user_text") or body.get("question") or ""),
            )
            return jsonify(payload), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/reviews", methods=["POST"])
    def answer_reviews(run_id: str):
        body = request.get_json(silent=True) or {}
        decisions = body.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            return error(ValueError("decisions must be a non-empty list"), 400)
        try:
            payload = service.answer(
                run_id,
                decisions,
                expected_revision=body.get("revision"),
            )
            return jsonify(payload), 200
        except IngestionServiceError as exc:
            return error(exc)

    @bp.route("/<run_id>/finalize", methods=["POST"])
    def finalize(run_id: str):
        body = request.get_json(silent=True) or {}
        try:
            session = service.finalize(run_id, expected_revision=body.get("revision"))
            return jsonify(session_payload(session)), 200
        except IngestionServiceError as exc:
            return error(exc)

    # Exposed for service-level tests without relying on module globals.
    bp.ingestion_service = service  # type: ignore[attr-defined]
    return bp
