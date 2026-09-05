"""Persistent metadata for resumable upload → review → finalise workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from netgravity.ingestion.storage.base import StorageBackend

SESSION_ZONE = "standardized"
SESSION_PREFIX = "ingestion_sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class IngestionSession:
    run_id: str
    source: str
    client_id: str = "default"
    status: str = "UPLOADED"
    revision: int = 1
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    content_type_overrides: Dict[str, str] = field(default_factory=dict)
    review: Dict[str, Any] = field(default_factory=dict)
    draft: Dict[str, Any] = field(default_factory=dict)
    report: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    snapshot_path: Optional[str] = None
    # Action Agent dedup: set once the required/optional missing-data email
    # has been sent for this session, so a re-run of the pipeline (e.g. a
    # reviewer answering an unrelated question) never sends it twice.
    required_notified_at: Optional[str] = None
    optional_notified_at: Optional[str] = None

    def as_dict(self, include_draft: bool = True) -> Dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "source": self.source,
            "client_id": self.client_id,
            "status": self.status,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content_type_overrides": dict(self.content_type_overrides),
            "review": dict(self.review),
            "report": dict(self.report),
            "error": self.error,
            "snapshot_path": self.snapshot_path,
            "required_notified_at": self.required_notified_at,
            "optional_notified_at": self.optional_notified_at,
            "next_actions": self.next_actions,
        }
        if include_draft:
            payload["draft"] = dict(self.draft)
        return payload

    @property
    def next_actions(self) -> list[str]:
        if self.status == "AWAITING_REVIEW":
            return ["review_blocking_items", "ask_ai_about_a_field"]
        if self.status == "PROVISIONAL_READY":
            return ["review_unfamiliar_fields", "finalize"]
        if self.status == "READY":
            return ["open_network", "run_analysis"]
        if self.status == "FAILED":
            return ["inspect_data_quality", "upload_corrected_files"]
        return ["refresh_status"]

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "IngestionSession":
        return cls(
            run_id=str(raw["run_id"]),
            source=str(raw["source"]),
            client_id=str(raw.get("client_id") or "default"),
            status=str(raw.get("status") or "UPLOADED"),
            revision=int(raw.get("revision") or 1),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            content_type_overrides=dict(raw.get("content_type_overrides") or {}),
            review=dict(raw.get("review") or {}),
            draft=dict(raw.get("draft") or {}),
            report=dict(raw.get("report") or {}),
            error=raw.get("error") or None,
            snapshot_path=raw.get("snapshot_path") or None,
            required_notified_at=raw.get("required_notified_at") or None,
            optional_notified_at=raw.get("optional_notified_at") or None,
        )


class IngestionSessionStore:
    def __init__(self, storage: StorageBackend):
        self.storage = storage

    @staticmethod
    def _key(run_id: str) -> str:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in "_-")
        if not safe or safe != run_id:
            raise ValueError("invalid ingestion run id")
        return f"{SESSION_PREFIX}/{safe}.json"

    def save(self, session: IngestionSession) -> str:
        session.updated_at = _now()
        return self.storage.save_text(
            SESSION_ZONE, self._key(session.run_id),
            json.dumps(session.as_dict(include_draft=True), indent=2, default=str),
        )

    def get(self, run_id: str) -> IngestionSession:
        raw = self.storage.get_text(SESSION_ZONE, self._key(run_id))
        return IngestionSession.from_dict(json.loads(raw))
