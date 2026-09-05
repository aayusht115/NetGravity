"""
NetGravity — Data Requests
============================
A request for data the analysis is missing, owned by the orchestrator.

WHY THIS EXISTS RATHER THAN A DIRECT SEND
------------------------------------------
"Ask the source for this" is a decision about the analysis, so it belongs
where every other such decision lives. The first version had a Flask route
call `action_agent.triggers` directly, which put a dispatcher in the position
of being the record: if the email failed, nothing remembered that a planner
had asked; if it succeeded, the only trace was in the dispatch log, which is
an audit of what was SENT, not of what was NEEDED.

So the flow matches governance's (orchestrator/core/orchestrator.py
`_govern` → `_notify_action_agent`): the orchestrator records the request,
then tells the Action Agent one exists. The notification is a downstream
effect and may fail without losing the request — a missed email is
recoverable, a forgotten request is not.

The store is a plain JSON blob behind `StorageBackend`, the same shape as
IngestionSessionStore, DispatchLogStore and the recipients stores.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.ingestion.storage.base import StorageBackend

ZONE = "standardized"
PREFIX = "orchestrator/data_requests"

#: A request that has been raised but whose notification has not gone out.
STATUS_OPEN = "OPEN"
#: The Action Agent reported the notification handled (sent, or stubbed).
STATUS_NOTIFIED = "NOTIFIED"
#: Raised, but there is nobody registered to ask. Deliberately a state rather
#: than an error: the request is real and stands until a contact exists.
STATUS_NO_CONTACT = "NO_CONTACT"
#: The data arrived and the gap closed.
STATUS_FULFILLED = "FULFILLED"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DataRequest:
    """One tier of missing data, asked for on one dataset."""

    request_id: str = field(default_factory=lambda: f"dreq_{uuid.uuid4().hex[:12]}")
    #: What the request is about. `project_id` for a project's bound dataset,
    #: `run_id` for an ingestion session.
    subject_id: str = ""
    subject_kind: str = "project"
    #: "required" or "optional" — the completeness tier.
    tier: str = "optional"
    #: The gaps as `CompletenessReport.as_dict()` recorded them. Stored whole
    #: so the request still says what was asked for after the dataset changes.
    fields: List[Dict[str, Any]] = field(default_factory=list)
    #: The execution this was raised from, when it was raised from one.
    execution_id: Optional[str] = None
    requested_by: str = ""
    requested_at: str = field(default_factory=_now)
    status: str = STATUS_OPEN
    notified_at: Optional[str] = None
    recipient: Optional[str] = None
    #: Why the status is what it is, when it is not OPEN or NOTIFIED.
    note: str = ""

    @property
    def field_labels(self) -> List[str]:
        return [str(f.get("display_label") or "") for f in self.fields]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind,
            "tier": self.tier,
            "fields": list(self.fields),
            "field_labels": self.field_labels,
            "execution_id": self.execution_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at,
            "status": self.status,
            "notified_at": self.notified_at,
            "recipient": self.recipient,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DataRequest":
        return cls(
            request_id=str(raw.get("request_id") or f"dreq_{uuid.uuid4().hex[:12]}"),
            subject_id=str(raw.get("subject_id") or ""),
            subject_kind=str(raw.get("subject_kind") or "project"),
            tier=str(raw.get("tier") or "optional"),
            fields=list(raw.get("fields") or []),
            execution_id=raw.get("execution_id"),
            requested_by=str(raw.get("requested_by") or ""),
            requested_at=str(raw.get("requested_at") or _now()),
            status=str(raw.get("status") or STATUS_OPEN),
            notified_at=raw.get("notified_at"),
            recipient=raw.get("recipient"),
            note=str(raw.get("note") or ""),
        )


class DataRequestStore:
    """One JSON blob per request, keyed by its id."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def save(self, request: DataRequest) -> str:
        return self.storage.save_text(
            ZONE, f"{PREFIX}/{request.request_id}.json",
            json.dumps(request.as_dict(), indent=2, default=str))

    def get(self, request_id: str) -> Optional[DataRequest]:
        key = f"{PREFIX}/{request_id}.json"
        if not self.storage.exists(ZONE, key):
            return None
        return DataRequest.from_dict(json.loads(self.storage.get_text(ZONE, key)))

    def list_all(self) -> List[DataRequest]:
        out: List[DataRequest] = []
        for key in self.storage.list(ZONE, prefix=PREFIX):
            if not key.endswith(".json"):
                continue
            out.append(DataRequest.from_dict(json.loads(self.storage.get_text(ZONE, key))))
        return sorted(out, key=lambda r: r.requested_at)

    def for_subject(self, subject_id: str, tier: Optional[str] = None) -> List[DataRequest]:
        return [r for r in self.list_all()
                if r.subject_id == subject_id and (tier is None or r.tier == tier)]

    def open_for(self, subject_id: str, tier: str) -> Optional[DataRequest]:
        """
        The standing request for this subject and tier, if one exists.

        Anything not FULFILLED still stands — including NO_CONTACT, which is a
        request waiting for somebody to ask rather than a request that failed.
        Used for dedup: a planner clicking twice has asked once.
        """
        standing = [r for r in self.for_subject(subject_id, tier)
                    if r.status != STATUS_FULFILLED]
        return standing[-1] if standing else None
