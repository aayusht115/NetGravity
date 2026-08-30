"""
NetGravity — Action Agent Dispatch Log
=========================================
Records every notification the Action Agent sends: what, to whom,
referencing which session/card, and when. Two purposes:

  1. Audit trail — "what did we send, and why".
  2. Dedup for triggers 3/4 (recommendation / investigate emails). Rather
     than bolting a `notified_at` field onto the orchestrator's own
     `ApprovalRequest` (which would mean this package reaching into and
     mutating orchestrator state), the Action Agent tracks its own
     "have I already sent this" bookkeeping — it owns the answer to that
     question, the orchestrator does not need to know it was asked.

One JSON file per dispatch, following the same StorageBackend-JSON-blob
pattern IngestionSessionStore already uses.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List

from netgravity.ingestion.storage.base import StorageBackend

ZONE = "standardized"
PREFIX = "action_agent/dispatch_log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DispatchRecord:
    dispatch_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: "required_data" | "optional_data" | "recommendation" | "investigate"
    #: | "inbound_hold"
    trigger_type: str = ""
    #: session run_id, approval_id, or execution_id — whatever this
    #: trigger_type keys its dedup on.
    reference_id: str = ""
    recipients: List[str] = field(default_factory=list)
    subject: str = ""
    sent_at: str = field(default_factory=_now)
    #: "sent" | "stubbed" | "failed"
    result: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "trigger_type": self.trigger_type,
            "reference_id": self.reference_id,
            "recipients": list(self.recipients),
            "subject": self.subject,
            "sent_at": self.sent_at,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DispatchRecord":
        return cls(
            dispatch_id=str(raw.get("dispatch_id") or uuid.uuid4().hex[:12]),
            trigger_type=str(raw.get("trigger_type") or ""),
            reference_id=str(raw.get("reference_id") or ""),
            recipients=list(raw.get("recipients") or []),
            subject=str(raw.get("subject") or ""),
            sent_at=str(raw.get("sent_at") or _now()),
            result=str(raw.get("result") or ""),
        )


class DispatchLogStore:
    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def record(self, entry: DispatchRecord) -> str:
        key = f"{PREFIX}/{entry.dispatch_id}.json"
        return self.storage.save_text(
            ZONE, key, json.dumps(entry.as_dict(), indent=2, default=str))

    def list_all(self) -> List[DispatchRecord]:
        out: List[DispatchRecord] = []
        for key in self.storage.list(ZONE, prefix=PREFIX):
            if not key.endswith(".json"):
                continue
            out.append(DispatchRecord.from_dict(json.loads(self.storage.get_text(ZONE, key))))
        return out

    def already_dispatched(self, trigger_type: str, reference_id: str) -> bool:
        return any(
            r.trigger_type == trigger_type and r.reference_id == reference_id
            for r in self.list_all()
        )
