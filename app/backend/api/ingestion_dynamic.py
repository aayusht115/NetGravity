"""
NetGravity — Upload Preview & Mapping-Inspection API Blueprint
==============================================================
Accepts Excel/CSV uploads and returns the ACTUAL columns, sample values, and
measured data-quality metrics so a user can review field mapping before
committing a dataset.

Phase 10.0 rewrite. The prototype version of this blueprint:

  * ran a second, independent MILP on every upload (removed — see
    `network_extractor.py`) and returned its output, which hardcoded
    `fillRate: 100.0` and `slaAdherence: 96.5`, as if solved;
  * reported `validPct: 98.0` and `validRows = total * 0.98` as LITERALS,
    regardless of the data — a fabricated quality claim on real customer files;
  * did `total_rows … or 100`, inventing 100 records for an empty upload;
  * performed no file-type, file-size or file-count validation;
  * stored the parsed network in ONE process-global dict shared by every user
    and every project;
  * leaked a loop variable (`file_mappings`) into its response, so the mapping
    was correct only for single-file uploads.

Scope note: this endpoint is a **preview**. It does not produce an analysable
network. Committing a dataset goes through the real 58-file pipeline at
`/api/ingestions` (parse → detect → map → review → confirm → validate →
canonicalize), whose output is a `CanonicalNetwork` bound to the project by
`app.backend.services.project_registry`. Keeping the two separate is deliberate:
the preview is allowed to be fast and lossy; only the real pipeline may produce
something the solver is permitted to see.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List

import pandas as pd
from flask import Blueprint, g, jsonify, request
from werkzeug.utils import secure_filename

from app.backend.services.demand_history_store import (
    capacity_history_store,
    build_series_from_structure,
    demand_history_store,
    uploaded_signal_store,
)
from app.backend.services.errors import ApplicationError, ValidationError
from app.backend.services.network_assembler import assemble_network_from_structure
from app.backend.services.project_registry import project_registry
from app.backend.services.security import require_auth
from app.backend.api.network_extractor import (
    build_network_from_dataframes,
    CANONICAL_FIELDS,
    classify_column_name,
    classify_sheet,
    extract_tables_from_file,
)

logger = logging.getLogger(__name__)

ingestion_dynamic_bp = Blueprint(
    "ingestion_dynamic", __name__, url_prefix="/api/ingestions/preview"
)

#: Upload guardrails (brief §7, §22).
_ALLOWED_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm"}
_MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 MB per file
_MAX_FILES_PER_REQUEST = 10

#: Previews are per-project, never one process-global dict.
_previews: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()


def _validate_upload(file_storage) -> str:
    """Validate one uploaded file, returning its sanitised name."""
    raw_name = file_storage.filename or ""
    name = secure_filename(raw_name)
    if not name:
        raise ValidationError("A file with a valid name is required.")

    ext = name[name.rfind("."):].lower() if "." in name else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValidationError(
            f"Unsupported file type '{ext or name}'.",
            context={"supported": sorted(_ALLOWED_EXTENSIONS)},
        )

    # Size is measured from the stream rather than trusting Content-Length.
    stream = file_storage.stream
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        raise ValidationError(f"'{name}' is empty.")
    if size > _MAX_FILE_BYTES:
        raise ValidationError(
            f"'{name}' is {size / 1_048_576:.1f} MB, above the "
            f"{_MAX_FILE_BYTES // 1_048_576} MB limit.",
        )
    return name


def _measure_quality(tables: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
    """
    Measure real data quality. Nothing here is assumed.

    A row is counted invalid when it is fully empty or duplicates an earlier
    row. Every issue carries the count that produced it, so the figure is
    explainable — the prototype simply asserted 98%.
    """
    total_rows = 0
    empty_rows = 0
    duplicate_rows = 0
    null_cells = 0
    total_cells = 0
    issues: List[Dict[str, Any]] = []

    for label, df in tables.items():
        rows = len(df)
        total_rows += rows
        total_cells += int(df.size)
        null_cells += int(df.isna().sum().sum())

        empties = int(df.isna().all(axis=1).sum())
        empty_rows += empties
        if empties:
            issues.append({
                "type": "Empty rows",
                "severity": "warning",
                "table": label,
                "count": empties,
                "detail": f"{empties} fully empty row(s) in '{label}'.",
            })

        dupes = int(df.duplicated().sum())
        duplicate_rows += dupes
        if dupes:
            issues.append({
                "type": "Duplicate rows",
                "severity": "warning",
                "table": label,
                "count": dupes,
                "detail": f"{dupes} duplicate row(s) in '{label}'.",
            })

        for col in df.columns:
            nulls = int(df[col].isna().sum())
            if rows and nulls / rows > 0.5:
                issues.append({
                    "type": "Sparse column",
                    "severity": "warning",
                    "table": label,
                    "column": str(col),
                    "count": nulls,
                    "detail": f"'{col}' is {nulls / rows:.0%} empty in '{label}'.",
                })

    invalid_rows = empty_rows + duplicate_rows
    valid_rows = max(0, total_rows - invalid_rows)

    return {
        "totalRecords": total_rows,
        "validRecords": valid_rows,
        "invalidRecords": invalid_rows,
        # None, not 100 or 98, when there is nothing to measure.
        "validPct": (round(valid_rows / total_rows * 100.0, 1) if total_rows else None),
        "nullCellPct": (round(null_cells / total_cells * 100.0, 1) if total_cells else None),
        "emptyRows": empty_rows,
        "duplicateRows": duplicate_rows,
        "issues": issues,
    }


@ingestion_dynamic_bp.route("/upload-and-parse", methods=["POST"])
@require_auth
def upload_and_parse():
    """
    Parse uploaded files and return detected columns, samples and measured
    quality for mapping review. Scoped to one project; nothing is solved.
    """
    project_id = str(request.form.get("project_id") or request.args.get("project_id") or "").strip()
    if not project_id:
        raise ValidationError("A project_id is required.")
    # Access check — raises 403/404 before any file is read.
    project_registry.get(project_id, user_id=g.current_user.user_id)

    uploaded = request.files.getlist("files") or request.files.getlist("file")
    if not uploaded:
        raise ValidationError("No files were provided.")
    if len(uploaded) > _MAX_FILES_PER_REQUEST:
        raise ValidationError(
            f"At most {_MAX_FILES_PER_REQUEST} files may be uploaded at once."
        )

    all_tables: Dict[str, pd.DataFrame] = {}
    file_summaries: List[Dict[str, Any]] = []
    mappings_by_file: Dict[str, List[Dict[str, Any]]] = {}
    parse_errors: List[Dict[str, str]] = []
    detected = auto = review = ignored = 0

    for storage in uploaded:
        fname = _validate_upload(storage)
        try:
            tables = extract_tables_from_file(storage)
        except Exception as exc:  # noqa: BLE001 — reported, never silently dropped
            logger.warning("ingestion.preview.parse_failed file=%s err=%s", fname, exc)
            parse_errors.append({"file": fname, "error": str(exc)})
            continue

        if not tables:
            parse_errors.append({
                "file": fname,
                "error": "No table with more than one column was found.",
            })
            continue

        file_mappings: List[Dict[str, Any]] = []
        for sheet_name, df in tables.items():
            all_tables[f"{fname}::{sheet_name}"] = df
            # What this sheet IS decides what its columns mean. Classifying a
            # column with no sheet context made `Capacity_Units` ambiguous
            # between a facility's capacity and a lane's, and resolved it to
            # neither.
            sheet_role = classify_sheet(df)
            for col in df.columns:
                detected += 1
                mapped, status, confidence = classify_column_name(col, sheet_role)
                if status == "auto":
                    auto += 1
                elif status == "review":
                    review += 1
                else:
                    ignored += 1

                samples = [str(x) for x in df[col].dropna().head(3).tolist()]
                file_mappings.append({
                    "source": str(col),
                    "sheet": sheet_name,
                    "sheetRole": sheet_role,
                    "rows": int(len(df)),
                    "sample": ", ".join(samples) if samples else "—",
                    "mapped": mapped,
                    "confidence": ("high" if confidence >= 0.90
                                   else "medium" if confidence >= 0.70 else "low"),
                    "status": status,
                })

        mappings_by_file[fname] = file_mappings
        file_summaries.append({
            "name": fname,
            "sheets": list(tables.keys()),
            "rows": sum(len(df) for df in tables.values()),
            "columnsCount": len(file_mappings),
        })

    if not all_tables:
        return jsonify({
            "error": {
                "code": "INGESTION_ERROR",
                "message": "None of the uploaded files could be parsed.",
                "context": {"parse_errors": parse_errors},
            }
        }), 422

    structure = build_network_from_dataframes(all_tables)
    quality = _measure_quality(all_tables)

    preview = {
        "status": "PREVIEW",
        "project_id": project_id,
        "files": file_summaries,
        "parse_errors": parse_errors,
        # Always the per-file map. The prototype returned a leaked loop
        # variable when exactly one file was uploaded.
        "mapping": mappings_by_file,
        "mapStats": {
            "detected": detected,
            "auto": auto,
            "review": review,
            "ignored": ignored,
        },
        # The dropdown's options, from the same table that produced `mapped`.
        # The screen shipped its own nine-item list, so a mapping the server
        # returned that was not in it rendered as the list's FIRST entry —
        # every row silently displaying "Customer ID".
        "schemaFields": list(CANONICAL_FIELDS),
        "dataQuality": quality,
        "structure": structure,
        "notice": (
            "This is a parsing preview for mapping review. No optimisation has "
            "run and no KPI has been produced. Confirm the dataset through "
            "/api/ingestions to build a canonical network and enable analysis."
        ),
    }

    with _lock:
        _previews[project_id] = preview

    logger.info(
        "ingestion.preview project_id=%s files=%d rows=%d detected=%d errors=%d",
        project_id, len(file_summaries), quality["totalRecords"], detected, len(parse_errors),
    )
    return jsonify(preview), 200


@ingestion_dynamic_bp.route("/commit", methods=["POST"])
@require_auth
def commit_preview():
    """
    Turn the confirmed preview into an analysable network.

    This is the step that makes an upload matter: the parsed structure is
    assembled into a real `CanonicalNetwork`, registered with the orchestrator's
    `SnapshotManager`, and bound to the project — after which every KPI,
    scenario and forecast for that project runs against the user's own data.

    Nothing is optimised here. Assembly and analysis stay separate, so a
    failure to build is never reported as a failure to solve.
    """
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    project_id = str(body.get("project_id") or request.args.get("project_id") or "").strip()
    if not project_id:
        raise ValidationError("A project_id is required.")

    project = project_registry.get(project_id, user_id=g.current_user.user_id)

    with _lock:
        preview = _previews.get(project_id)
    if preview is None:
        raise ValidationError(
            "There is no parsed upload for this project to commit. Upload files first.",
            context={"project_id": project_id},
        )

    structure = preview.get("structure") or {}

    network, assumptions, issues = assemble_network_from_structure(
        structure,
        network_id=f"net_{project_id}",
        description=f"Uploaded network for {project.name}",
    )

    # Observed demand history travels with the network, so the orchestrator's
    # forecast capability can find it. Without this the forecasting engine —
    # which is fully built — has nothing to read for an uploaded network.
    sla_by_market = {
        d.market_id: d.sla_days
        for d in network.demands
        if getattr(d, "sla_days", None) is not None
    }
    series, history_notes = build_series_from_structure(
        structure, sla_by_market=sla_by_market
    )
    if series:
        demand_history_store.put(network.network_id, series)

    uploaded_signals = structure.get("signals") or []
    if uploaded_signals:
        uploaded_signal_store.put(network.network_id, uploaded_signals)

    # The client's own record of how much capacity each site had and used, per
    # period. Parsed since Phase 10.1 and carried no further, so 288 rows of
    # measurement reached nothing. It is measurement, not a solver output, so it
    # is served with the structure and shown as the recorded prior beside the
    # modelled utilisation.
    capacity_rows = structure.get("capacityHistory") or []
    if capacity_rows:
        capacity_history_store.put(network.network_id, capacity_rows)

    snapshot_id = project_registry.bind_network(
        project_id, network,
        user_id=g.current_user.user_id,
        label=f"{project.name} — uploaded",
    )

    logger.info(
        "ingestion.committed project_id=%s snapshot_id=%s facilities=%d history_series=%d",
        project_id, snapshot_id, len(network.facilities), len(series),
    )
    return jsonify({
        "status": "BOUND",
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "network_summary": {
            "facilities": len(network.facilities),
            "demands": len(network.demands),
            "lanes": len(network.lanes),
            "products": len(network.products),
            "demand_history_series": len(series),
            "data_version": network.data_version,
        },
        # Every default this assembly had to apply, in words. Shown, not hidden.
        "assumptions": assumptions + list(structure.get("notes") or []) + history_notes,
        "issues": issues,
        "message": (
            "Your network is bound to this project. All KPIs, scenarios and "
            "forecasts now run against your uploaded data."
        ),
    }), 201


@ingestion_dynamic_bp.route("/active", methods=["GET"])
@require_auth
def get_active_preview():
    """The most recent parsing preview for this project, if any."""
    project_id = str(request.args.get("project_id") or "").strip()
    if not project_id:
        raise ValidationError("A project_id is required.")
    project_registry.get(project_id, user_id=g.current_user.user_id)

    with _lock:
        preview = _previews.get(project_id)

    if preview is None:
        return jsonify({
            "project_id": project_id,
            "status": "NO_PREVIEW",
            "message": "No file has been uploaded for this project yet.",
        }), 200
    return jsonify(preview), 200


@ingestion_dynamic_bp.errorhandler(ApplicationError)
def _ingestion_error(exc: ApplicationError):
    return jsonify(exc.to_payload()), exc.http_status
