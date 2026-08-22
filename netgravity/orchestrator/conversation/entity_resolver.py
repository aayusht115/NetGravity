"""
Orchestrator — Entity resolution against authoritative master data.

A user says "the Delhi warehouse". This module decides which network node — if
any — that means, using only the live snapshot.

Two rules, both structural rather than advisory:

1. **Nothing is invented.** Every id returned came out of
   `CanonicalNetwork.facilities`. There is no code path that produces an
   identifier from user text, which is why a hallucinated site surfaces as an
   unresolved mention instead of reaching the MILP.

2. **Two matches is not one match.** A phrase matching several facilities
   returns all of them and is reported AMBIGUOUS. Picking the first would be
   the same class of error as substituting a default for a missing value: a
   confident answer to a question nobody could actually answer.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Sequence

from netgravity.orchestrator.schemas.conversation import EntityKind, EntityMention
from netgravity.schemas.network import CanonicalNetwork, NodeRole

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}

#: Words that describe a KIND of node rather than identifying one. Matching on
#: these alone would make "the warehouse" resolve to every DC in the network.
_TYPE_WORDS = frozenset({
    "dc", "dcs", "warehouse", "warehouses", "facility", "facilities",
    "centre", "center", "distribution", "hub", "site", "node", "plant",
    "plants", "factory", "market", "markets", "depot", "the", "our", "a", "an",
    "new", "old", "main", "in", "at", "of", "and", "or",
})

#: Ordinary English words that are capitalised for grammatical reasons rather
#: than because they name anything. Never proper nouns.
_QUESTION_WORDS = frozenset({
    "how", "what", "why", "when", "where", "which", "who", "whose", "can",
    "could", "should", "would", "will", "does", "did", "are", "was", "were",
    "show", "tell", "give", "list", "explain", "compare", "close", "open",
    "reduce", "increase", "assess", "there", "this", "that", "these", "those",
    "please", "and", "but", "for", "the", "our", "we", "you", "it", "is",
    "if", "run", "make", "put", "add", "remove", "simulate", "forecast",
    "ignore", "pretend", "return", "calculate", "instead", "also",
})

#: Words that mark the thing beside them as a network node. Used to spot a
#: node reference regardless of capitalisation.
_ROLE_NOUNS = (
    "dc", "dcs", "warehouse", "warehouses", "facility", "facilities", "plant",
    "plants", "factory", "depot", "hub", "site", "node", "centre", "center",
    "distribution centre", "distribution center", "distribution center",
)

#: Determiners, quantifiers and connectives that can sit immediately before a
#: role noun without naming anything. Without these, "how many warehouses" reads
#: "many" as a missing facility and "FROM facilities" reads "FROM" as one.
_NON_NAME_WORDS = frozenset({
    "many", "much", "few", "several", "all", "some", "any", "each", "every",
    "both", "other", "another", "more", "most", "less", "fewer", "no", "none",
    "from", "with", "into", "onto", "than", "then", "there", "here", "they",
    "existing", "current", "currently", "total", "overall", "per", "via",
    "one", "two", "three", "four", "five", "six", "ten", "first", "last",
    "next", "previous", "same", "such", "only", "just", "still", "also",
    "big", "biggest", "large", "largest", "small", "smallest", "key",
    # Adjectives that routinely qualify a role noun without naming one:
    # "the most exposed facility", "our riskiest facility".
    "exposed", "critical", "riskiest", "vulnerable", "important", "main",
    "primary", "secondary", "best", "worst", "cheapest", "closest", "nearest",
    "busiest", "open", "closed", "active", "inactive", "remaining", "affected",
    "available", "whole", "entire", "single", "backup", "regional", "central",
})

#: "<name> <role>"  or  "<role> <name>" — "bangalore dc", "the Pune facility",
#: "warehouse in Chennai". Case-insensitive by construction.
_TYPED_REFERENCE_RE = re.compile(
    r"\b(?P<name>[A-Za-z][A-Za-z\-]{2,})\s+(?:" + "|".join(_ROLE_NOUNS) + r")\b"
    r"|\b(?:" + "|".join(_ROLE_NOUNS) + r")\s+(?:at|in|for|called|named)\s+"
    r"(?P<name2>[A-Za-z][A-Za-z\-]{2,})\b",
    re.IGNORECASE,
)

#: Role words the user may use to narrow a match ("the Delhi *plant*").
_ROLE_HINTS = {
    "plant": NodeRole.PLANT, "factory": NodeRole.PLANT,
    "dc": NodeRole.DC, "warehouse": NodeRole.DC, "depot": NodeRole.DC,
    "distribution": NodeRole.DC,
    "market": NodeRole.MARKET, "customer": NodeRole.CUSTOMER,
}


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _distinctive(text: str) -> List[str]:
    """Tokens that could actually identify a node, type-words removed."""
    return [t for t in _tokens(text) if len(t) > 2 and t not in _TYPE_WORDS]


class EntityResolver:
    """
    Resolves natural-language references to real network node ids.

    Built per snapshot. Cheap to construct — it indexes the facility list once.
    """

    def __init__(self, network: CanonicalNetwork) -> None:
        self.network = network
        self._by_id = {f.id: f for f in network.facilities}
        self._by_lower_id = {f.id.lower(): f.id for f in network.facilities}
        self._by_lower_name = {
            (f.name or "").strip().lower(): f.id
            for f in network.facilities if (f.name or "").strip()
        }
        #: Identifier prefixes this network actually uses ("dc", "plant",
        #: "mkt"). Read from master data so identifier-shaped detection is
        #: specific to the network in front of us.
        self._id_prefixes = {
            f.id.split("_", 1)[0].lower()
            for f in network.facilities if "_" in f.id
        }

    # ------------------------------------------------------------------
    # Catalogue helpers — used for status answers and clarification options
    # ------------------------------------------------------------------

    def facilities_of_role(self, role: NodeRole) -> List[str]:
        return sorted(f.id for f in self.network.facilities if f.role == role)

    def describe(self, facility_id: str) -> Dict[str, str]:
        """Label for a clarification option. Never invents fields."""
        fac = self._by_id.get(facility_id)
        if fac is None:
            return {"id": facility_id, "label": facility_id}
        return {
            "id": fac.id,
            "label": f"{fac.name or fac.id} ({fac.role.value})",
            "role": fac.role.value,
        }

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_phrase(self, phrase: str) -> EntityMention:
        """
        Resolve one phrase to zero, one or several real node ids.

        Match order, most specific first:
          1. exact id            "DC_DELHI"
          2. exact display name  "Delhi NCR DC"
          3. distinctive tokens  "Delhi" → every node whose id/name contains it

        A role hint in the phrase ("the Delhi *plant*") narrows a multi-match.
        It never widens one: a hint cannot add a node the tokens did not find.
        """
        raw = (phrase or "").strip()
        if not raw:
            return EntityMention(phrase=phrase or "", method="none")

        lowered = raw.lower()

        exact_id = self._by_lower_id.get(lowered)
        if exact_id:
            return self._mention(raw, [exact_id], "exact_id")

        exact_name = self._by_lower_name.get(lowered)
        if exact_name:
            return self._mention(raw, [exact_name], "name")

        distinctive = _distinctive(raw)
        if not distinctive:
            return EntityMention(phrase=raw, method="none")

        matches: List[str] = []
        for fac in self.network.facilities:
            haystack = f"{fac.id} {fac.name or ''}".lower()
            haystack_tokens = set(_tokens(haystack))
            if any(token in haystack_tokens for token in distinctive):
                matches.append(fac.id)

        if not matches:
            return EntityMention(phrase=raw, method="none")

        # Narrow by role hint only when it does not eliminate everything.
        hinted = {role for word, role in _ROLE_HINTS.items() if word in _tokens(raw)}
        if hinted and len(matches) > 1:
            narrowed = [
                fid for fid in matches
                if self._by_id[fid].role in hinted
            ]
            if narrowed:
                matches = narrowed

        return self._mention(raw, sorted(set(matches)), "token")

    def _mention(self, phrase: str, ids: List[str], method: str) -> EntityMention:
        kind = EntityKind.UNKNOWN
        if ids:
            roles = {self._by_id[i].role for i in ids if i in self._by_id}
            if roles <= MARKET_ROLES:
                kind = EntityKind.MARKET
            elif roles:
                kind = EntityKind.FACILITY
        return EntityMention(phrase=phrase, kind=kind, resolved_ids=ids, method=method)

    def resolve_all(self, phrases: Sequence[str]) -> List[EntityMention]:
        """Resolve several phrases, dropping exact duplicates."""
        seen: set = set()
        out: List[EntityMention] = []
        for phrase in phrases:
            key = (phrase or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(self.resolve_phrase(phrase))
        return out

    # ------------------------------------------------------------------
    # Candidate extraction from free text
    # ------------------------------------------------------------------

    def extract_mentions(self, text: str) -> List[EntityMention]:
        """
        Find the network entities a sentence refers to.

        Works by testing what the network actually contains against the text,
        rather than by trying to spot proper nouns — which is why it cannot
        produce an entity the network does not have. The cost is that it only
        finds things it knows about; an unknown site is detected separately, by
        `find_unknown_candidates`.
        """
        lowered = f" {text.lower()} "
        hits: Dict[str, EntityMention] = {}

        for fac in self.network.facilities:
            if fac.id.lower() in lowered:
                hits.setdefault(fac.id, self._mention(fac.id, [fac.id], "exact_id"))
                continue
            name = (fac.name or "").strip().lower()
            if name and name in lowered:
                hits.setdefault(fac.id, self._mention(fac.name, [fac.id], "name"))

        if hits:
            return self._group_by_phrase(list(hits.values()))

        # Nothing matched wholesale — try distinctive tokens from the ids/names.
        text_tokens = set(_tokens(text))
        token_hits: Dict[str, List[str]] = {}
        for fac in self.network.facilities:
            for token in _distinctive(f"{fac.id} {fac.name or ''}"):
                if token in text_tokens:
                    token_hits.setdefault(token, []).append(fac.id)

        return [
            self._mention(token, sorted(set(ids)), "token")
            for token, ids in sorted(token_hits.items())
        ]

    @staticmethod
    def _group_by_phrase(mentions: List[EntityMention]) -> List[EntityMention]:
        return sorted(mentions, key=lambda m: (m.phrase.lower(), m.resolved_ids))

    def unknown_node_references(self, text: str) -> List[str]:
        """
        Unresolved references that are UNMISTAKABLY about a network node.

        Narrower than `find_unknown_candidates`, and the distinction earns its
        keep. A typed reference ("the Bangalore DC") or an identifier in this
        network's own shape ("DC_SHADOW") names a node and nothing else, so an
        unresolved one is a missing facility even when the sentence also names a
        real facility. A bare capitalised word is weaker evidence: in "Cyclone
        Amphan may hit Kolkata", "Amphan" is a storm, and refusing the request
        because the network contains no facility called Amphan would be absurd.

        Strong references therefore block on their own; weak ones only when the
        sentence resolved nothing at all.
        """
        strong: List[str] = []
        known_ids = {f.id.lower() for f in self.network.facilities}
        known_tokens: set = set()
        for fac in self.network.facilities:
            known_tokens.update(_tokens(f"{fac.id} {fac.name or ''}"))

        raw = text or ""
        for match in re.finditer(r"\b([A-Za-z]{1,8}_[A-Za-z0-9_]{1,})\b", raw):
            word = match.group(1)
            if word.lower() in known_ids:
                continue
            if word.split("_", 1)[0].lower() in self._id_prefixes \
                    and word not in strong:
                strong.append(word)

        mixed_case = any(ch.isupper() for ch in raw)
        for match in _TYPED_REFERENCE_RE.finditer(raw):
            word = match.group("name") or match.group("name2")
            if not word:
                continue
            token = word.lower()
            if token in known_tokens or token in _TYPE_WORDS \
                    or token in _QUESTION_WORDS or token in _ROLE_NOUNS \
                    or token in _NON_NAME_WORDS:
                continue
            if mixed_case and not word[0].isupper():
                continue
            if word not in strong:
                strong.append(word)
        return strong

    def find_unknown_candidates(self, text: str) -> List[str]:
        """
        Capitalised words that look like a place but match no network node.

        Used to tell "the user named a site we do not have" from "the user named
        no site at all" — the difference between an UNKNOWN_ENTITY clarification
        and simply having no entity in the request.

        Sentence-initial words are skipped: "How many warehouses do we have?"
        begins with a capital purely because sentences do, and treating "How" as
        a missing facility produced a nonsensical clarification. Only a
        capitalised word appearing mid-sentence is a plausible proper noun.

        IDENTIFIER-SHAPED REFERENCES ARE MATCHED SEPARATELY
        ───────────────────────────────────────────────────
        "DC_CHENNAI" is a single regex word — the underscore is a word
        character, so no `\\b` falls between "DC_" and "CHENNAI" and the
        prose pattern above cannot see it. Phase 3.1 evaluation found that
        "Reduce capacity at DC_SHADOW by 10%" therefore produced a confident
        "I don't understand" rather than "there is no such facility": the
        fabricated node was invisible instead of refused. Identifier-shaped
        tokens are now recognised in their own right, and a reference that
        LOOKS like a network id but is not one is exactly the case that most
        needs saying out loud.
        """
        known_tokens: set = set()
        known_ids: set = set()
        for fac in self.network.facilities:
            known_tokens.update(_tokens(f"{fac.id} {fac.name or ''}"))
            known_ids.add(fac.id.lower())

        raw = text or ""
        # Offsets that begin a sentence, so they can be excluded.
        sentence_starts = {0}
        for match in re.finditer(r"[.!?]\s+", raw):
            sentence_starts.add(match.end())

        candidates: List[str] = []

        def add(word: str) -> None:
            if word not in candidates:
                candidates.append(word)

        # TYPED references, case-insensitively: "the bangalore dc", "Pune
        # facility", "Hyderabad warehouse". A role word next to an unrecognised
        # token names a node as plainly as a capitalised proper noun does, and
        # relying on capitalisation meant "close the bangalore dc" was read as
        # naming no facility at all. Users type lowercase.
        # When the message uses capitals at all, a lowercase word before a role
        # noun is an adjective, not a name: "the most exposed facility". When
        # the message is entirely lowercase, capitalisation carries no signal
        # and the word is taken at face value — which is what lets "close the
        # bangalore dc" be recognised.
        mixed_case = any(ch.isupper() for ch in raw)

        for match in _TYPED_REFERENCE_RE.finditer(raw):
            word = match.group("name") or match.group("name2")
            if not word:
                continue
            token = word.lower()
            if token in known_tokens or token in _TYPE_WORDS \
                    or token in _QUESTION_WORDS or token in _ROLE_NOUNS \
                    or token in _NON_NAME_WORDS:
                continue
            if mixed_case and not word[0].isupper():
                continue
            add(word)

        # Identifier-shaped: PREFIX_SUFFIX, where PREFIX is one this network
        # actually uses. Position-independent — an id at the start of a
        # sentence is still an id, unlike a capitalised ordinary word.
        #
        # The prefix set is read from master data rather than hardcoded, so
        # "DC_SHADOW" is recognised as a facility-shaped reference in a network
        # of DC_* nodes while "AUTO_ACTION" is not recognised as anything. A
        # fixed list flagged governance verdicts as missing facilities.
        for match in re.finditer(r"\b([A-Za-z]{1,8}_[A-Za-z0-9_]{1,})\b", raw):
            word = match.group(1)
            if word.lower() in known_ids:
                continue
            if word.split("_", 1)[0].lower() not in self._id_prefixes:
                continue
            add(word)

        # Title Case only. An ALL-CAPS word is an acronym or a code token —
        # "SELECT * FROM facilities;" was reported as a missing facility called
        # "FROM" — whereas a place name a user types is capitalised normally.
        # Identifier-shaped all-caps references are already covered above.
        for match in re.finditer(r"\b[A-Z][a-z]{2,}\b", raw):
            if match.start() in sentence_starts:
                continue
            word = match.group(0)
            token = word.lower()
            if token in known_tokens or token in _TYPE_WORDS or token in _QUESTION_WORDS:
                continue
            add(word)
        return candidates
