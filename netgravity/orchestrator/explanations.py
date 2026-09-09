"""
NetGravity — Explanation Store
================================
Saved explanations, each keyed to the exact result it describes.

WHY THE KEY MATTERS MORE THAN THE STORE. "Save the explanation against that
result" only holds if you can tell when the result has moved on. An
explanation keyed to a project would survive a re-solve and quietly describe
a network that no longer exists — the same class of error as showing a
scenario the network's briefing. So every record carries the
`result_fingerprint` it was written about, and a read that does not match
returns nothing rather than something stale.

WHAT THIS BUYS. A model request is spent once, per analysis. Reopening a
pane, switching tabs, reloading the project and viewing the same comparison
again are all views of one analysis and read from here. Running a new
scenario, or comparing a different set, is a new analysis and produces a new
record.

Plain JSON blobs behind `StorageBackend`, the same shape as
`data_requests.py` and the ingestion session store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from netgravity.ingestion.storage.base import StorageBackend

ZONE = "standardized"
PREFIX = "orchestrator/explanations"

#: What an explanation is ABOUT. One value per result-screen flow.
KIND_OPTIMIZED = "optimized"      # the solved network against its reference
KIND_SCENARIO = "scenario"        # one what-if against its own reference
KIND_COMPARISON = "comparison"    # a named set of scenarios, ranked
KIND_FORECAST = "forecast"        # one forecast run

KINDS = (KIND_OPTIMIZED, KIND_SCENARIO, KIND_COMPARISON, KIND_FORECAST)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: The WORDING these explanations are written in. Part of every fingerprint.
#:
#: A saved explanation is keyed by the result it describes, which is right for
#: the figures and says nothing about the sentences. So a correction to
#: materially wrong prose — "a demand fill rate of 1.000", a cost quoted with
#: no currency — changed no fingerprint at all, and every project that had
#: already been explained kept being served the older words for the life of
#: the store. A stale explanation that outlives a deploy is indistinguishable,
#: from the reader's side, from a build that was never fixed.
#:
#: BUMP THIS whenever the narration changes: a template sentence, a prompt, or
#: the formatting of a figure inside one.
PROSE_VERSION = 2


def fingerprint(*parts: Any) -> str:
    """
    A stable id for the result an explanation describes, in the wording of
    this build.

    Built from whatever identifies that result — an execution id, a data
    version, a sorted set of scenario ids — plus `PROSE_VERSION`.
    Order-independent for collections, because comparing A and B is the same
    analysis as comparing B and A and should not spend a second request.
    """
    flat: List[str] = [f"prose:v{PROSE_VERSION}"]
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple, set, frozenset)):
            flat.extend(sorted(str(x) for x in part))
        else:
            flat.append(str(part))
    return hashlib.sha256("|".join(flat).encode("utf-8")).hexdigest()[:16]


@dataclass
class SavedExplanation:
    """One explanation, and the result it was written about."""

    subject_id: str = ""
    kind: str = KIND_OPTIMIZED
    #: Identifies the result. A read whose fingerprint differs is a MISS.
    result_fingerprint: str = ""
    #: The briefing payload, in the shape the panes render.
    content: Dict[str, Any] = field(default_factory=dict)
    #: "template" | "llm" — how the words were produced, so a screen can say.
    source: str = "template"
    #: How many model requests this cost. Asserted in tests; the whole point
    #: of the store is that it is 1 per analysis and 0 per view.
    model_requests: int = 0
    created_at: str = field(default_factory=_now)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "kind": self.kind,
            "result_fingerprint": self.result_fingerprint,
            "content": dict(self.content),
            "source": self.source,
            "model_requests": self.model_requests,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SavedExplanation":
        return cls(
            subject_id=str(raw.get("subject_id") or ""),
            kind=str(raw.get("kind") or KIND_OPTIMIZED),
            result_fingerprint=str(raw.get("result_fingerprint") or ""),
            content=dict(raw.get("content") or {}),
            source=str(raw.get("source") or "template"),
            model_requests=int(raw.get("model_requests") or 0),
            created_at=str(raw.get("created_at") or _now()),
        )


class ExplanationStore:
    """One blob per (subject, kind). The newest explanation wins."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def _key(self, subject_id: str, kind: str) -> str:
        safe = "".join(c for c in subject_id if c.isalnum() or c in "_-")
        if not safe:
            raise ValueError(f"invalid subject id: {subject_id!r}")
        return f"{PREFIX}/{kind}/{safe}.json"

    def get(self, subject_id: str, kind: str,
            result_fingerprint: str) -> Optional[SavedExplanation]:
        """
        The saved explanation for THIS result, or None.

        A record written about a different result is a miss, not a fallback:
        serving it would describe a network that has since been re-solved.
        """
        key = self._key(subject_id, kind)
        if not self.storage.exists(ZONE, key):
            return None
        saved = SavedExplanation.from_dict(
            json.loads(self.storage.get_text(ZONE, key)))
        if saved.result_fingerprint != result_fingerprint:
            return None
        return saved

    def put(self, explanation: SavedExplanation) -> str:
        return self.storage.save_text(
            ZONE, self._key(explanation.subject_id, explanation.kind),
            json.dumps(explanation.as_dict(), indent=2, default=str))

    # No `forget`: `StorageBackend` has no delete, and none is needed. A
    # record is superseded by writing the next one, and a record about a
    # superseded result is already a miss on `result_fingerprint`.
