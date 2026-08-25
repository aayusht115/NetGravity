"""
NetGravity — Field Memory ("the dictionary")
=============================================
Remembers what a column means once a human has confirmed it, so the same
question is never asked twice.

THE SCOPING PROBLEM THIS SOLVES
-------------------------------
The naive version — "confirmed once, trusted everywhere" — is unsafe. Two
spreadsheets can carry identical headers and mean different things: `Qty` on
a despatch register is units shipped; `Qty` on a returns file is units
returned. Trusting the first confirmation everywhere rebuilds exactly the
context-blindness that made the static alias table unreliable.

The opposite extreme — "confirmed per sender, never generalises" — is safe
but useless. Every new vendor re-reviews `Qty` from scratch forever, even
though it has meant the same thing for every sender so far.

So neither scope is hardcoded. This module STORES OBSERVATIONS AND RESOLVES
SCOPE FROM EVIDENCE:

    exact        this sender confirmed this column, in this content type.
                 Highest trust. No review.

    generalised  this sender has never sent it, but >= GENERALISE_AFTER_SOURCES
                 OTHER senders all confirmed the same meaning, in the same
                 content type. The repetition is the evidence. No review.

    suggested    exactly one other sender confirmed it. Plausible, but one
                 data point is not a pattern. Proposed WITH review.

    conflict     other senders confirmed DIFFERENT meanings for this column.
                 This is the ambiguity case, and the most valuable output of
                 the whole module: the disagreement is handed to the review
                 layer, which turns it into a specific question ("this has
                 meant units shipped for vendor_a and units returned for
                 vendor_b — which is it here?") instead of a blank prompt.

    none         never seen. Falls through to AI + dictionary as normal.

CONTENT TYPE IS PART OF THE KEY, ALWAYS
---------------------------------------
Nothing generalises across content types. A confirmation made on a
SHIPMENT_LOG says nothing about the same column on a PRODUCT sheet. That is
the guard that stops the `Qty`-means-two-things trap.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from netgravity.ingestion.field_aliases import normalise_name
from netgravity.ingestion.storage.base import StorageBackend

MEMORY_PREFIX = "field_memory"
MEMORY_ZONE = "standardized"

#: How many DISTINCT other senders must independently agree before a mapping
#: is trusted for a sender who has never sent that column.
#:
#: Two is the smallest number that is a pattern rather than a coincidence.
#: One sender agreeing with itself proves nothing; two unrelated senders
#: using a column the same way is real evidence. Raising this makes the
#: system more cautious and more talkative; lowering it to 1 would mean a
#: single confirmation silently governs every future sender.
GENERALISE_AFTER_SOURCES = 2

SCOPE_EXACT = "exact"
SCOPE_GENERALISED = "generalised"
SCOPE_SUGGESTED = "suggested"
SCOPE_CONFLICT = "conflict"
SCOPE_NONE = "none"


@dataclass
class FieldObservation:
    """One confirmed instance. Facts only — no conclusion drawn here."""

    source_column: str
    target_field: str
    content_type: str
    source_id: str
    confirmed_at: str = ""
    confirmed_by: str = "human"
    note: str = ""
    unit: Optional[str] = None
    period: Optional[str] = None
    definition: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            "source_column": self.source_column,
            "target_field": self.target_field,
            "content_type": self.content_type,
            "source_id": self.source_id,
            "confirmed_at": self.confirmed_at,
            "confirmed_by": self.confirmed_by,
            "note": self.note,
            "unit": self.unit,
            "period": self.period,
            "definition": self.definition,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, str]) -> "FieldObservation":
        return cls(
            source_column=str(raw.get("source_column", "")),
            target_field=str(raw.get("target_field", "")),
            content_type=str(raw.get("content_type", "")),
            source_id=str(raw.get("source_id", "")),
            confirmed_at=str(raw.get("confirmed_at", "")),
            confirmed_by=str(raw.get("confirmed_by", "human")),
            note=str(raw.get("note", "")),
            unit=raw.get("unit") or None,
            period=raw.get("period") or None,
            definition=str(raw.get("definition", "")),
        )


@dataclass
class MemoryAlternative:
    """One candidate meaning, with the evidence behind it."""

    target_field: str
    source_ids: List[str] = field(default_factory=list)

    @property
    def support(self) -> int:
        return len(self.source_ids)


@dataclass
class MemoryResolution:
    """What memory can say about one column, and how strongly."""

    target_field: Optional[str] = None
    scope: str = SCOPE_NONE
    confidence: float = 0.0
    needs_review: bool = True
    rationale: str = ""
    alternatives: List[MemoryAlternative] = field(default_factory=list)
    unit: Optional[str] = None
    period: Optional[str] = None
    definition: str = ""

    @property
    def is_known(self) -> bool:
        return self.scope in {SCOPE_EXACT, SCOPE_GENERALISED}

    @property
    def is_conflict(self) -> bool:
        return self.scope == SCOPE_CONFLICT


def _key(content_type: str, column: str, namespace: str = "default") -> str:
    if not namespace or namespace == "default":
        # Preserve the established on-disk layout for existing installations.
        return f"{MEMORY_PREFIX}/{content_type}/{normalise_name(column)}.json"
    safe = normalise_name(namespace) or "default"
    return (f"{MEMORY_PREFIX}/clients/{safe}/{content_type}/"
            f"{normalise_name(column)}.json")


class FieldMemory:
    """Read/write access to confirmed column meanings."""

    def __init__(self, storage: StorageBackend, namespace: str = "default"):
        self.storage = storage
        self.namespace = namespace or "default"

    # -- persistence ------------------------------------------------------

    def _load(self, content_type: str, column: str) -> List[FieldObservation]:
        try:
            raw = self.storage.get_text(
                MEMORY_ZONE, _key(content_type, column, self.namespace))
        except FileNotFoundError:
            return []
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # A corrupt memory file must degrade to "we know nothing" rather
            # than break ingestion. Losing memory costs a re-review; raising
            # here would stop the run entirely.
            return []
        return [FieldObservation.from_dict(o) for o in payload.get("observations", [])]

    def _save(self, content_type: str, column: str,
              observations: List[FieldObservation]) -> str:
        body = {
            "content_type": content_type,
            "column_key": normalise_name(column),
            "observations": [o.as_dict() for o in observations],
        }
        return self.storage.save_text(
            MEMORY_ZONE, _key(content_type, column, self.namespace),
            json.dumps(body, indent=2, sort_keys=True),
        )

    # -- writing ----------------------------------------------------------

    def record(self, *, source_column: str, target_field: str,
               content_type: str, source_id: str,
               confirmed_by: str = "human", note: str = "",
               unit: Optional[str] = None, period: Optional[str] = None,
               definition: str = "") -> FieldObservation:
        """
        Store one confirmation.

        Re-confirming the same (source, column, content type) REPLACES the
        previous observation rather than appending. Otherwise a sender who
        corrects an earlier mistake would leave both answers on file, and the
        stale one would keep voting in the generalisation count forever.
        """
        observations = self._load(content_type, source_column)
        observations = [
            o for o in observations
            if normalise_name(o.source_id) != normalise_name(source_id)
        ]
        observation = FieldObservation(
            source_column=source_column,
            target_field=target_field,
            content_type=content_type,
            source_id=source_id,
            confirmed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            confirmed_by=confirmed_by,
            note=note,
            unit=unit,
            period=period,
            definition=definition,
        )
        observations.append(observation)
        self._save(content_type, source_column, observations)
        return observation

    # -- reading ----------------------------------------------------------

    def resolve(self, *, source_column: str, content_type: str,
                source_id: str) -> MemoryResolution:
        """Decide how far the evidence lets this column's meaning travel."""
        observations = self._load(content_type, source_column)
        if not observations:
            return MemoryResolution(
                rationale="no prior confirmation for this column in this content type"
            )

        # 1. This exact sender has confirmed it before.
        for obs in observations:
            if normalise_name(obs.source_id) == normalise_name(source_id):
                return MemoryResolution(
                    target_field=obs.target_field,
                    scope=SCOPE_EXACT,
                    confidence=1.0,
                    needs_review=False,
                    rationale=(f"'{source_id}' confirmed this column as "
                               f"'{obs.target_field}' on {obs.confirmed_at or 'a previous run'}"),
                    unit=obs.unit,
                    period=obs.period,
                    definition=obs.definition,
                )

        # 2. Other senders. Group HUMAN confirmations by what they said.
        # Auto-confirmed mappings remain useful for the exact sender, but must
        # never combine into organisation-wide truth without a person.
        by_field: Dict[str, List[str]] = {}
        for obs in observations:
            if normalise_name(obs.confirmed_by) == "auto":
                continue
            by_field.setdefault(obs.target_field, []).append(obs.source_id)
        if not by_field:
            return MemoryResolution(
                rationale=("only machine-confirmed observations exist for this column; "
                           "they do not generalise to another sender")
            )
        alternatives = [
            MemoryAlternative(target_field=f, source_ids=sorted(set(ids)))
            for f, ids in by_field.items()
        ]
        alternatives.sort(key=lambda a: a.support, reverse=True)

        # 3. They disagree — the most useful thing memory can report.
        if len(alternatives) > 1:
            detail = "; ".join(
                f"'{a.target_field}' for {', '.join(a.source_ids)}" for a in alternatives
            )
            return MemoryResolution(
                target_field=alternatives[0].target_field,
                scope=SCOPE_CONFLICT,
                confidence=0.0,
                needs_review=True,
                rationale=(f"this column has meant different things to different "
                           f"senders — {detail}"),
                alternatives=alternatives,
            )

        # 4. Unanimous. Does it have enough independent support to travel?
        only = alternatives[0]
        supporting = [o for o in observations
                      if o.target_field == only.target_field
                      and normalise_name(o.confirmed_by) != "auto"]
        units = {o.unit for o in supporting if o.unit}
        periods = {o.period for o in supporting if o.period}
        definitions = {o.definition for o in supporting if o.definition}
        shared_unit = next(iter(units)) if len(units) == 1 else None
        shared_period = next(iter(periods)) if len(periods) == 1 else None
        shared_definition = next(iter(definitions)) if len(definitions) == 1 else ""
        if only.support >= GENERALISE_AFTER_SOURCES:
            return MemoryResolution(
                target_field=only.target_field,
                scope=SCOPE_GENERALISED,
                confidence=0.95,
                needs_review=False,
                rationale=(f"{only.support} senders ({', '.join(only.source_ids)}) "
                           f"independently confirmed this column as "
                           f"'{only.target_field}' for {content_type} data"),
                alternatives=alternatives,
                unit=shared_unit,
                period=shared_period,
                definition=shared_definition,
            )

        return MemoryResolution(
            target_field=only.target_field,
            scope=SCOPE_SUGGESTED,
            confidence=0.75,
            needs_review=True,
            rationale=(f"one sender ({only.source_ids[0]}) confirmed this column as "
                       f"'{only.target_field}'. One data point is not yet a pattern, "
                       f"so this is proposed rather than applied"),
            alternatives=alternatives,
            unit=shared_unit,
            period=shared_period,
            definition=shared_definition,
        )

    # -- introspection ----------------------------------------------------

    def observations_for(self, content_type: str,
                         source_column: str) -> List[FieldObservation]:
        """Everything on file for one column. Used to build review context."""
        return self._load(content_type, source_column)

    def stats(self) -> Dict[str, int]:
        """Counts for the report: how much has this system actually learned?"""
        try:
            prefix = (MEMORY_PREFIX if self.namespace == "default"
                      else f"{MEMORY_PREFIX}/clients/{normalise_name(self.namespace)}")
            keys = self.storage.list(MEMORY_ZONE, prefix)
        except Exception:
            return {"columns": 0, "observations": 0, "content_types": 0}
        columns = 0
        observations = 0
        types: Counter = Counter()
        for key in keys:
            if self.namespace == "default" and key.startswith(f"{MEMORY_PREFIX}/clients/"):
                continue
            if not key.endswith(".json"):
                continue
            try:
                payload = json.loads(self.storage.get_text(MEMORY_ZONE, key))
            except Exception:
                continue
            columns += 1
            observations += len(payload.get("observations", []))
            types[payload.get("content_type", "")] += 1
        return {
            "columns": columns,
            "observations": observations,
            "content_types": len(types),
        }
