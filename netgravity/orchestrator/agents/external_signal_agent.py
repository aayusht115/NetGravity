"""
Orchestrator — External signal interpretation.

Turns outside-world text ("severe flooding expected around Delhi next week")
into a structured `ExternalSignal` carrying event type, location, likelihood and
full provenance.

The governing rule: **external information is EVIDENCE, never observed network
truth.** A signal supplies P for the RF calculation and nothing else. It is
never merged into a network snapshot, and the entities it names are re-matched
against the real network before use — a signal cannot introduce a facility.

Likelihood is only ever taken from what the source actually states. When no
likelihood is available the field stays None and RF is simply not computed for
that entity; a fabricated probability would flow straight into a governance
threshold, which is exactly the kind of invented number this system must not
produce.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List, Optional, Sequence

from netgravity.orchestrator.agents.llm_gateway import LLMGateway, extract_json
from netgravity.orchestrator.exceptions import LLMFailureError
from netgravity.orchestrator.schemas.requests import EventSeverity, ExternalSignal

logger = logging.getLogger(__name__)

#: Severity vocabulary → categorical severity. Note this maps to SEVERITY ONLY.
#:
#: There is deliberately NO severity → probability table here. A previous
#: version derived likelihood from severity ("severe" ⇒ P = 0.7), which
#: conflated two independent variables: a catastrophic event can be very
#: unlikely, and a trivial one near-certain. Feeding severity into RF as P
#: produced numbers with no defensible basis that then drove governance
#: thresholds. Probability is now taken only from an explicit statement in the
#: source; otherwise it stays None and RF reports NOT_COMPUTABLE.
_SEVERITY_WORDS = {
    "catastrophic": EventSeverity.CRITICAL,
    "critical": EventSeverity.CRITICAL,
    "extreme": EventSeverity.CRITICAL,
    "severe": EventSeverity.SEVERE,
    "major": EventSeverity.SEVERE,
    "high": EventSeverity.HIGH,
    "significant": EventSeverity.HIGH,
    "moderate": EventSeverity.MODERATE,
    "medium": EventSeverity.MODERATE,
    "minor": EventSeverity.LOW,
    "low": EventSeverity.LOW,
    "slight": EventSeverity.LOW,
}

#: Phrases that state a probability explicitly. Only these produce a P.
_PROBABILITY_PATTERNS = (
    # "70% chance", "70 % probability of", "70% likelihood"
    r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:chance|probability|likelihood|risk|likely)",
    # "chance of 70%", "probability of 0.7"
    r"(?:chance|probability|likelihood)\s+of\s+(\d{1,3}(?:\.\d+)?)\s*%",
    r"(?:chance|probability|likelihood)\s+of\s+(0?\.\d+)",
    # "probability: 0.72"
    r"(?:probability|p)\s*[:=]\s*(0?\.\d+|1\.0+|\d{1,3}(?:\.\d+)?%)",
)

_EVENT_KEYWORDS = {
    "flood": "FLOOD", "flooding": "FLOOD",
    "storm": "STORM", "cyclone": "CYCLONE", "hurricane": "HURRICANE",
    "typhoon": "TYPHOON", "earthquake": "EARTHQUAKE", "wildfire": "WILDFIRE",
    "fire": "FIRE", "strike": "LABOUR_STRIKE", "protest": "CIVIL_UNREST",
    "blockade": "BLOCKADE", "heatwave": "HEATWAVE", "snow": "SNOW",
    "outage": "POWER_OUTAGE", "cyber": "CYBER_INCIDENT", "pandemic": "PANDEMIC",
}


class ExternalSignalAgent:
    """Interprets external event text into a provenance-carrying signal."""

    def __init__(self, gateway: Optional[LLMGateway] = None) -> None:
        self.gateway = gateway

    def interpret(
        self,
        text: str,
        *,
        known_facility_ids: Sequence[str] = (),
        source: str = "user_report",
        allow_llm: bool = True,
    ) -> ExternalSignal:
        """
        Extract a structured signal from free text.

        Never raises: a failure to interpret yields a low-confidence signal with
        `likelihood=None`, which downstream simply means "RF not computed".
        """
        rule_signal = self._rule_based(text, known_facility_ids, source)

        if not allow_llm or self.gateway is None or not self.gateway.available:
            return rule_signal

        try:
            llm_signal = self._llm_based(text, known_facility_ids, source)
        except LLMFailureError as exc:
            logger.warning("orchestrator.external_signal.llm_failed code=%s", exc.code.value)
            rule_signal.evidence += f" (LLM interpretation failed: {exc.code.value})"
            return rule_signal

        return llm_signal or rule_signal

    # ------------------------------------------------------------------
    # Deterministic extraction
    # ------------------------------------------------------------------

    def _rule_based(
        self, text: str, known_ids: Sequence[str], source: str,
    ) -> ExternalSignal:
        raw = (text or "").strip()
        lowered = raw.lower()

        event_type = "UNKNOWN_EVENT"
        for keyword, mapped in _EVENT_KEYWORDS.items():
            if keyword in lowered:
                event_type = mapped
                break

        # Severity: how bad. Categorical, and it stops here — it never becomes P.
        severity = EventSeverity.UNKNOWN
        for word, mapped_severity in _SEVERITY_WORDS.items():
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                severity = mapped_severity
                break

        # Probability: ONLY from an explicit statement in the source.
        probability, basis = self._extract_probability(lowered)

        affected = [fid for fid in known_ids if self._mentions(fid, lowered)]
        location = affected[0] if affected else self._guess_location(raw)

        return ExternalSignal(
            event_type=event_type,
            location=location,
            severity=severity,
            event_probability=probability,
            probability_basis=basis,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            # Confidence in this EXTRACTION — not the probability of the event.
            confidence=0.5 if event_type != "UNKNOWN_EVENT" else 0.2,
            evidence=raw[:500],
            affected_entity_ids=affected,
        )

    @staticmethod
    def _extract_probability(lowered: str) -> tuple:
        """
        Extract an explicitly stated probability, or (None, None).

        Returns None whenever the text merely describes severity or urgency.
        "Severe flooding is expected" states no probability, and inventing one
        would put a fabricated number into a governance threshold.
        """
        for pattern in _PROBABILITY_PATTERNS:
            match = re.search(pattern, lowered)
            if not match:
                continue
            token = match.group(1)
            try:
                if token.endswith("%"):
                    value = float(token.rstrip("%")) / 100.0
                elif "." in token and float(token) <= 1.0:
                    value = float(token)
                else:
                    value = float(token) / 100.0
            except ValueError:
                continue
            if 0.0 <= value <= 1.0:
                return value, f"explicitly stated in source: '{match.group(0).strip()}'"
        return None, None

    @staticmethod
    def _mentions(facility_id: str, lowered_text: str) -> bool:
        if facility_id.lower() in lowered_text:
            return True
        tokens = [t for t in re.split(r"[^a-z0-9]+", facility_id.lower())
                  if len(t) > 2 and t not in {"dc", "plant", "new"}]
        return any(re.search(rf"\b{re.escape(t)}\b", lowered_text) for t in tokens)

    @staticmethod
    def _guess_location(text: str) -> str:
        """Crude capitalised-token heuristic; the LLM path does this better."""
        match = re.search(r"\b(?:around|near|in|at)\s+([A-Z][A-Za-z_\- ]{2,30})", text)
        return match.group(1).strip() if match else ""

    # ------------------------------------------------------------------
    # Model extraction
    # ------------------------------------------------------------------

    def _llm_based(
        self, text: str, known_ids: Sequence[str], source: str,
    ) -> Optional[ExternalSignal]:
        assert self.gateway is not None
        facility_list = ", ".join(known_ids[:60]) or "(none supplied)"

        prompt = (
            "You extract structured supply-chain risk signals from text.\n"
            "Return ONLY a JSON object, no prose and no code fences.\n\n"
            "{\n"
            '  "event_type": "FLOOD | STORM | EARTHQUAKE | LABOUR_STRIKE | CYBER_INCIDENT '
            '| POWER_OUTAGE | CIVIL_UNREST | OTHER",\n'
            '  "location": "place named in the text",\n'
            '  "severity": "LOW | MODERATE | HIGH | SEVERE | CRITICAL | UNKNOWN",\n'
            '  "event_probability": <number 0..1, or null>,\n'
            '  "probability_quote": "the exact words stating the probability, or null",\n'
            '  "validity_period": "e.g. next 7 days, or null",\n'
            '  "confidence": <number 0..1, your confidence in THIS EXTRACTION>,\n'
            '  "affected_facility_ids": ["exact identifiers from the list below"]\n'
            "}\n\n"
            "CRITICAL RULES — read carefully:\n"
            "- SEVERITY and PROBABILITY are different things. Severity is how BAD the "
            "event would be. Probability is how LIKELY it is. A catastrophic event can "
            "be very unlikely.\n"
            "- Set event_probability ONLY if the text states an explicit probability "
            "(for example '70% chance', 'probability of 0.7'). Words like 'severe', "
            "'expected', 'major' or 'warning' describe severity or urgency, NOT "
            "probability — for those return null.\n"
            "- NEVER convert severity into a probability. NEVER guess a number.\n"
            "- confidence is how sure YOU are about this extraction. It is NOT the "
            "probability of the event.\n"
            "- affected_facility_ids must contain only exact strings from this list, or "
            "be empty. Never invent an identifier.\n"
            f"- Valid facility identifiers: {facility_list}\n\n"
            f"Text: {text}\n"
        )

        response = self.gateway.generate(prompt, purpose="external_signal")
        parsed = extract_json(response.output)
        if not parsed:
            return None

        allowed = set(known_ids)
        affected: List[str] = [
            f for f in parsed.get("affected_facility_ids", []) or [] if f in allowed
        ]

        # A probability is accepted only when the model also quotes the words
        # that state it. No quote ⇒ it inferred rather than read, so we drop it.
        probability = parsed.get("event_probability")
        quote = parsed.get("probability_quote")
        basis: Optional[str] = None
        if probability is not None:
            try:
                probability = float(probability)
            except (TypeError, ValueError):
                probability = None
            if probability is not None and not (0.0 <= probability <= 1.0):
                probability = None
            if probability is not None:
                if quote and str(quote).strip().lower() not in ("null", "none", ""):
                    basis = f"model-extracted, quoting source: '{str(quote)[:160]}'"
                else:
                    logger.warning(
                        "orchestrator.external_signal.probability_without_quote "
                        "dropped=%s", probability,
                    )
                    probability = None

        try:
            severity = EventSeverity(str(parsed.get("severity", "UNKNOWN")).upper())
        except ValueError:
            severity = EventSeverity.UNKNOWN

        try:
            confidence = min(1.0, max(0.0, float(parsed.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5

        return ExternalSignal(
            event_type=str(parsed.get("event_type", "UNKNOWN_EVENT"))[:64],
            location=str(parsed.get("location", ""))[:120],
            severity=severity,
            event_probability=probability,
            probability_basis=basis,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            validity_period=(str(parsed.get("validity_period"))[:120]
                             if parsed.get("validity_period") else None),
            confidence=confidence,
            evidence=(text or "")[:500],
            affected_entity_ids=affected,
        )
