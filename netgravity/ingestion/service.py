"""Application service for the UI-facing ingestion clarification workflow."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from netgravity.ingestion import review as review_module
from netgravity.ingestion.ai.clarification import analyse
from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.draft import build_draft
from netgravity.ingestion.memory.field_catalog import FieldCatalog
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.pipeline import run_ingestion
from netgravity.ingestion.session import IngestionSession, IngestionSessionStore
from netgravity.ingestion.storage import get_storage


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class IngestionServiceError(RuntimeError):
    status_code = 400


class SessionNotFound(IngestionServiceError):
    status_code = 404


class RevisionConflict(IngestionServiceError):
    status_code = 409


class ReviewValidationError(IngestionServiceError):
    status_code = 422

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


class IngestionService:
    """Synchronous service today; API shape supports a queued worker later."""

    def __init__(self, config: Optional[IngestionConfig] = None):
        self.config = config or IngestionConfig()
        self.storage = get_storage(self.config)
        self.sessions = IngestionSessionStore(self.storage)

    def start(self, source: Path, *, client_id: str = "default",
              run_id: Optional[str] = None) -> IngestionSession:
        source = Path(source)
        if not source.exists():
            raise IngestionServiceError(f"upload source does not exist: {source}")
        session = IngestionSession(
            run_id=run_id or f"ing_{uuid.uuid4().hex[:12]}",
            source=str(source),
            client_id=client_id or "default",
            status="PROFILING",
        )
        self.sessions.save(session)
        refreshed = self._refresh(session, save=False)
        self._maybe_notify_completeness(refreshed)
        return refreshed

    def get(self, run_id: str) -> IngestionSession:
        try:
            return self.sessions.get(run_id)
        except FileNotFoundError as exc:
            raise SessionNotFound(f"ingestion '{run_id}' was not found") from exc

    def analyse_item(self, run_id: str, item_id: str,
                     user_text: str = "") -> Dict[str, Any]:
        session = self.get(run_id)
        outcome = self._execute(session, save=False)
        request = outcome.review_request
        request.run_id = session.run_id
        item = next((candidate for candidate in request.items
                     if candidate.item_id == item_id), None)
        if item is None:
            raise ReviewValidationError("review item is no longer open")
        suggestion = analyse(get_client(self.config), item, user_text)
        return {
            "run_id": run_id,
            "revision": session.revision,
            "item_id": item_id,
            "suggestion": suggestion.as_dict(),
            "requires_confirmation": True,
        }

    def answer(self, run_id: str, decisions: Sequence[Dict[str, Any]],
               *, expected_revision: Optional[int] = None) -> Dict[str, Any]:
        session = self.get(run_id)
        if expected_revision is not None and expected_revision != session.revision:
            raise RevisionConflict(
                f"run revision is {session.revision}, not {expected_revision}; refresh first")

        outcome = self._execute(session, save=False)
        request = outcome.review_request
        request.run_id = session.run_id
        item_by_id = {item.item_id: item for item in request.items}
        rejected = []
        seen = set()
        for raw in decisions:
            if not isinstance(raw, dict):
                rejected.append({"item_id": "", "reason": "answer must be an object"})
                continue
            item_id = str(raw.get("item_id") or "")
            value = str(raw.get("value") or "")
            answer = review_module.ReviewDecision.from_dict(raw)
            item = item_by_id.get(item_id)
            if item_id in seen:
                rejected.append({"item_id": item_id, "reason": "duplicate answer"})
            elif item is None:
                rejected.append({"item_id": item_id, "reason": "review item is not open"})
            elif not value:
                rejected.append({"item_id": item_id, "reason": "no value supplied"})
            else:
                valid = {option.value for option in item.options}
                if item.kind == review_module.KIND_UNFAMILIAR:
                    valid.update(item.context.get("allowed_canonical_fields") or [])
                if value not in valid:
                    rejected.append({
                        "item_id": item_id,
                        "reason": f"'{value}' is not an allowed answer",
                    })
                else:
                    special = {
                        review_module.KEEP_SUPPLEMENTARY,
                        review_module.KEEP_UNRESOLVED,
                        review_module.PROPOSE_NEW,
                        review_module.NOT_NEEDED,
                    }
                    problem = review_module.metadata_error(
                        answer,
                        canonical=(item.kind != review_module.KIND_CONTENT_TYPE
                                   and value not in special),
                    )
                    if problem:
                        rejected.append({"item_id": item_id, "reason": problem})
            seen.add(item_id)
        if rejected:
            raise ReviewValidationError(
                "one or more review answers were rejected",
                details={"rejected": rejected},
            )
        memory = FieldMemory(self.storage, namespace=session.client_id)
        catalog = FieldCatalog(self.storage, client_id=session.client_id)
        applied = review_module.apply(
            request, decisions, outcome.tabular.mappings, memory, catalog)
        if applied.rejected:
            raise ReviewValidationError(
                "one or more review answers were rejected",
                details=applied.as_dict(),
            )

        answered = {
            str(raw.get("item_id")): str(raw.get("value")) for raw in decisions
        }
        for item_id in applied.applied:
            item = item_by_id.get(item_id)
            if item and item.kind == review_module.KIND_CONTENT_TYPE:
                session.content_type_overrides[item.record_key] = answered[item_id]

        session.revision += 1
        refreshed = self._refresh(session, save=False)
        self._maybe_notify_completeness(refreshed)
        return {
            "outcome": applied.as_dict(),
            "session": refreshed.as_dict(include_draft=False),
            "review": refreshed.review,
        }

    def finalize(self, run_id: str,
                 *, expected_revision: Optional[int] = None) -> IngestionSession:
        session = self.get(run_id)
        if expected_revision is not None and expected_revision != session.revision:
            raise RevisionConflict(
                f"run revision is {session.revision}, not {expected_revision}; refresh first")
        if bool(session.review.get("has_blocking")):
            raise ReviewValidationError(
                "blocking questions must be answered before finalization",
                details={"blocking_count": session.review.get("blocking_count", 0)},
            )
        if not bool(session.report.get("network_assembled")):
            raise ReviewValidationError(
                "a canonical network could not be assembled from this upload",
                details={"error": session.error, "report": session.report},
            )
        session.revision += 1
        refreshed = self._refresh(session, save=True, final=True)
        if refreshed.status != "READY":
            raise ReviewValidationError(
                "ingestion could not be finalized",
                details={"status": refreshed.status, "error": refreshed.error},
            )
        return refreshed

    def resume_with_file(self, run_id: str, filename: str, content: bytes) -> IngestionSession:
        """
        Add a corrected file to an existing session's source directory and
        re-run the pipeline against it — the reply-by-email upload path
        (netgravity/action_agent/inbound_email.py) hands a verified
        attachment here so it goes through the exact same mapping, review
        and completeness logic as any other upload, tagged to the session it
        was a reply to. No second validation path.
        """
        session = self.get(run_id)
        source_dir = Path(session.source)
        source_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "corrected_upload"
        (source_dir / safe_name).write_bytes(content)

        session.revision += 1
        refreshed = self._refresh(session, save=True)
        self._maybe_notify_completeness(refreshed)
        return refreshed

    def _maybe_notify_completeness(self, session: IngestionSession) -> None:
        """
        Fire the Action Agent's missing-data emails at most once per session
        per kind. The dedup flags live on the session itself
        (required_notified_at / optional_notified_at) because that is where
        the fact belongs — this session has been told about, once.

        Imported lazily so `ingestion` never takes a load-time dependency on
        `action_agent`, and wrapped so a notification failure cannot fail an
        ingestion: a missed email is recoverable, a lost upload is not.
        """
        report = session.report or {}
        if not report.get("missing_required") and not report.get("missing_optional"):
            return

        try:
            from netgravity.action_agent import triggers as action_agent_triggers
        except Exception:  # pragma: no cover - package absent
            logger.exception("ingestion.action_agent.import_failed run_id=%s", session.run_id)
            return

        changed = False
        try:
            if report.get("missing_required") and not session.required_notified_at:
                action_agent_triggers.on_completeness_failure(session, kind="required")
                session.required_notified_at = _now_iso()
                changed = True

            if report.get("missing_optional") and not session.optional_notified_at:
                action_agent_triggers.on_completeness_failure(session, kind="optional")
                session.optional_notified_at = _now_iso()
                changed = True
        except Exception:
            logger.exception(
                "ingestion.action_agent.notify_failed run_id=%s", session.run_id)
            return

        if changed:
            self.sessions.save(session)

    def _execute(self, session: IngestionSession, *, save: bool):
        return run_ingestion(
            Path(session.source),
            config=self.config,
            save=save,
            unified=True,
            auto_confirm=False,
            catalog_scope=session.client_id,
            content_type_overrides=session.content_type_overrides,
        )

    def _refresh(self, session: IngestionSession, *, save: bool,
                 final: bool = False) -> IngestionSession:
        try:
            result = self._execute(session, save=save)
            request = result.review_request
            request.run_id = session.run_id
            session.review = request.as_dict()
            session.review["missing_data_items"] = [
                item.as_dict() for item in review_module.build_missing_data_items(
                    result.report.missing_required, result.report.missing_optional)
            ]
            session.draft = build_draft(Path(session.source), result)
            session.report = {
                "rows_read": result.report.total_rows_read,
                "rows_accepted": result.report.total_rows_accepted,
                "network_assembled": result.report.network_assembled,
                "data_version": result.report.data_version,
                "counts": dict(result.report.counts),
                "errors": len(result.report.errors),
                "warnings": len(result.report.warnings),
                "ai_mode": ("stub" if self.config.stub_mode else "live"),
                "ai_failure_count": sum(
                    1 for file_result in result.report.files if file_result.ai_failed),
                "missing_required": list(result.report.missing_required),
                "missing_optional": list(result.report.missing_optional),
            }
            session.snapshot_path = result.report.snapshot_path
            session.error = result.report.extras.get("error")
            live_ai_failed = (
                not self.config.stub_mode
                and bool(session.report["ai_failure_count"])
            )
            if live_ai_failed:
                session.status = "FAILED"
                session.error = "a live AI call failed; refusing fallback data"
            elif request.has_blocking:
                session.status = "AWAITING_REVIEW"
            elif final and result.network is not None and result.report.ok:
                session.status = "READY"
            elif result.network is not None:
                session.status = "PROVISIONAL_READY"
            else:
                session.status = "FAILED"
        except Exception as exc:
            session.status = "FAILED"
            session.error = f"{type(exc).__name__}: {exc}"
        self.sessions.save(session)
        return session
