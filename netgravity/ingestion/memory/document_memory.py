"""
NetGravity — Document Pattern Memory
=====================================
Recognises a document we have effectively seen before, even when its content
has changed.

WHERE THIS SITS BETWEEN THE OTHER TWO
-------------------------------------
    ai/cache.py         EXACT match. Same bytes of text -> replay the stored
                        extraction, zero model calls. Perfect when a document
                        is genuinely unchanged; useless the moment one rate
                        is updated.

    THIS MODULE         SHAPE match. A renewed contract has the same clause
                        structure and wording but different numbers. The exact
                        cache misses it completely, so a renewal is treated as
                        a total stranger and re-extracted with no prior
                        context at all.

    field_memory.py     Column-level meaning for TABULAR data. Does not apply
                        here: a contract has no columns, and there is no short
                        repeating token like "Qty" to key on — one vendor
                        writes "a fuel surcharge of Rs. 2.00 per kg", another
                        phrases the identical concept completely differently.

WHY THE SIGNATURE DROPS NUMBERS
-------------------------------
The signature is the set of normalised ALPHABETIC words in the document.
Digits are excluded deliberately, because the numbers are precisely what a
renewal changes. Keeping them would make every renewal look like a different
document — the exact failure this module exists to fix.

SCOPE IS NOT ASSUMED TO BE "VENDOR"
-----------------------------------
An earlier design keyed this on vendor identity. That bakes in an assumption
before seeing the evidence: several vendors may share a broker's template,
and one vendor may use different templates for different service lines. So
nothing here is keyed on vendor. Documents are matched on SHAPE, and whatever
label the extraction discovered (vendor name, document type) is carried along
as an observed hint for the next similar document — never as the key.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from netgravity.ingestion.storage.base import StorageBackend

MEMORY_PREFIX = "document_patterns"
MEMORY_ZONE = "standardized"

#: Jaccard overlap of word sets above which two documents are treated as the
#: same template.
#:
#: Calibration: two renewals of one contract share nearly all wording and
#: differ only in figures, scoring well above 0.8. Two unrelated freight
#: contracts still share generic legal boilerplate ("shall", "agreement",
#: "carrier", "consignment") and typically land in the 0.4-0.6 band. 0.75
#: sits clearly above that overlap band without demanding near-identity.
SIMILARITY_THRESHOLD = 0.75

#: Documents shorter than this contribute too few distinct words for a
#: Jaccard score to mean anything, so they are never matched by shape.
MIN_SIGNATURE_TOKENS = 40

_WORD_RE = re.compile(r"[A-Za-z]{2,}")


def signature(text: str) -> List[str]:
    """
    Structural fingerprint: the sorted set of distinct alphabetic words.

    Case-folded and de-duplicated, so ordering and repetition do not affect
    the match — a clause moved from section 3 to section 4 is still the same
    template.
    """
    return sorted({w.lower() for w in _WORD_RE.findall(text or "")})


def similarity(left: List[str], right: List[str]) -> float:
    """Jaccard overlap of two signatures. 0.0 when either is empty."""
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class DocumentPattern:
    """One remembered document shape."""

    pattern_id: str
    signature: List[str] = field(default_factory=list)
    #: Labels the EXTRACTION discovered, not ones we imposed. Free-form on
    #: purpose — whatever the model found useful to call this document.
    observed_labels: Dict[str, str] = field(default_factory=dict)
    seen_documents: List[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""

    @property
    def times_seen(self) -> int:
        return len(self.seen_documents)

    def as_dict(self) -> Dict[str, object]:
        return {
            "pattern_id": self.pattern_id,
            "signature": self.signature,
            "observed_labels": self.observed_labels,
            "seen_documents": self.seen_documents,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, object]) -> "DocumentPattern":
        return cls(
            pattern_id=str(raw.get("pattern_id", "")),
            signature=[str(s) for s in (raw.get("signature") or [])],
            observed_labels={str(k): str(v)
                             for k, v in (raw.get("observed_labels") or {}).items()},
            seen_documents=[str(s) for s in (raw.get("seen_documents") or [])],
            first_seen=str(raw.get("first_seen", "")),
            last_seen=str(raw.get("last_seen", "")),
        )


@dataclass
class PatternMatch:
    """Result of looking a document up by shape."""

    pattern: Optional[DocumentPattern] = None
    score: float = 0.0

    @property
    def matched(self) -> bool:
        return self.pattern is not None

    @property
    def rationale(self) -> str:
        if not self.pattern:
            return "no previously seen document has a similar shape"
        labels = ", ".join(f"{k}={v}" for k, v in self.pattern.observed_labels.items())
        return (f"{self.score:.0%} wording overlap with a document seen "
                f"{self.pattern.times_seen} time(s) before"
                + (f" ({labels})" if labels else ""))


class DocumentMemory:
    """Find and record document shapes."""

    def __init__(self, storage: StorageBackend):
        self.storage = storage

    def _key(self, pattern_id: str) -> str:
        return f"{MEMORY_PREFIX}/{pattern_id}.json"

    def _all(self) -> List[DocumentPattern]:
        try:
            keys = self.storage.list(MEMORY_ZONE, MEMORY_PREFIX)
        except Exception:
            return []
        patterns: List[DocumentPattern] = []
        for key in keys:
            if not key.endswith(".json"):
                continue
            try:
                patterns.append(
                    DocumentPattern.from_dict(
                        json.loads(self.storage.get_text(MEMORY_ZONE, key))))
            except Exception:
                continue          # a corrupt entry loses memory, never the run
        return patterns

    def find(self, text: str) -> PatternMatch:
        """Best shape match for this document, if any clears the threshold."""
        sig = signature(text)
        if len(sig) < MIN_SIGNATURE_TOKENS:
            return PatternMatch()

        best: Optional[DocumentPattern] = None
        best_score = 0.0
        for pattern in self._all():
            score = similarity(sig, pattern.signature)
            if score > best_score:
                best, best_score = pattern, score

        if best is not None and best_score >= SIMILARITY_THRESHOLD:
            return PatternMatch(pattern=best, score=best_score)
        return PatternMatch(score=best_score)

    def record(self, text: str, *, document_name: str,
               labels: Optional[Dict[str, str]] = None) -> Optional[DocumentPattern]:
        """
        Remember this document's shape.

        If it matches an existing pattern it EXTENDS that pattern rather than
        creating a near-duplicate, so a template seen five times stays one
        entry with five sightings instead of five entries that each look novel.
        """
        sig = signature(text)
        if len(sig) < MIN_SIGNATURE_TOKENS:
            return None

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        match = self.find(text)

        if match.matched and match.pattern is not None:
            pattern = match.pattern
            if document_name not in pattern.seen_documents:
                pattern.seen_documents.append(document_name)
            pattern.observed_labels.update(labels or {})
            pattern.last_seen = now
            # Union the signatures: a renewal that adds a clause should widen
            # the remembered shape, not be forced to match the older, narrower
            # one forever.
            pattern.signature = sorted(set(pattern.signature) | set(sig))
        else:
            pattern = DocumentPattern(
                pattern_id=f"pattern_{abs(hash(tuple(sig[:60]))):016x}",
                signature=sig,
                observed_labels=dict(labels or {}),
                seen_documents=[document_name],
                first_seen=now,
                last_seen=now,
            )

        self.storage.save_text(
            MEMORY_ZONE, self._key(pattern.pattern_id),
            json.dumps(pattern.as_dict(), indent=2, sort_keys=True),
        )
        return pattern
