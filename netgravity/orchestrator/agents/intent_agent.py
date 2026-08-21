"""
Orchestrator — Intent interpretation.

Turns natural language into a structured, validated `IntentResolution`.

Two-tier by design:

  1. **Rule-based parser** — always runs. Deterministic, free, offline, and
     sufficient for the common phrasings.
  2. **LLM interpretation** — used only when the rules are not confident and a
     gateway is configured. Shared gateway capacity is small, so a model call
     must earn its place.

Whatever the source, the output is only a PROPOSAL. Facility identifiers are
re-validated against the real network before any engine runs, so a hallucinated
site name fails validation rather than reaching the MILP.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence

from netgravity.orchestrator.agents.llm_gateway import LLMGateway, extract_json
from netgravity.orchestrator.exceptions import LLMFailureError
from netgravity.orchestrator.schemas.requests import (
    Intent,
    IntentResolution,
    ScenarioActionType,
    ScenarioIntentSpec,
)

logger = logging.getLogger(__name__)

#: Confidence at or above which the rule parser is trusted outright.
RULES_CONFIDENT = 0.75

_CLOSE_WORDS = ("close", "closing", "shut", "shutdown", "shut down", "lose", "losing",
                "remove", "offline", "down", "disrupt", "disruption", "outage", "fail")
_COMPARE_WORDS = ("compare", "versus", " vs ", "vs.", "against", "either")
_RESILIENCE_WORDS = ("most exposed", "exposure", "resilien", "vulnerab", "riskiest",
                     "most critical", "single point", "rei")
_EXTERNAL_WORDS = ("flood", "storm", "cyclone", "hurricane", "earthquake", "strike",
                   "protest", "typhoon", "wildfire", "heatwave", "expected", "forecast",
                   "warning", "alert")
_OPTIMIZE_WORDS = ("optimi", "best configuration", "cheapest", "minimise cost",
                   "minimize cost", "improve the network")
_STATE_WORDS = ("current state", "how does the network", "what does the network",
                "network state", "status of the network", "show me the network")
_EXPLAIN_WORDS = ("why is", "why are", "why does", "why did", "explain",
                  "what makes", "reason for", "how come")

#: Capacity-change phrasing. Split into direction and the quantity so an
#: absolute ("2,000 units/day") and a relative ("20%") change are told apart —
#: they mean materially different things and only one can be a multiplier.
_CAPACITY_WORDS = ("capacity", "throughput")
_CAPACITY_DOWN = ("reduce", "reduced", "reducing", "cut", "decrease", "decreased",
                  "lower", "drop", "shrink", "de-rate", "derate")
_CAPACITY_UP = ("increase", "increased", "increasing", "expand", "add", "raise",
                "boost", "uplift", "grow")
#: "by 2,000 units" / "by 2000 units per day" — absolute.
_CAPACITY_ABS_RE = re.compile(
    r"\bby\s+([\d,]+(?:\.\d+)?)\s*(?:units?|unit/day|units?\s*(?:/|per)\s*\w+)",
    re.IGNORECASE,
)
#: "by 20%" / "by 20 percent" — relative.
_CAPACITY_PCT_RE = re.compile(
    r"\bby\s+([\d,]+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", re.IGNORECASE,
)


class IntentAgent:
    """Interprets a request into a structured intent."""

    def __init__(self, gateway: Optional[LLMGateway] = None) -> None:
        self.gateway = gateway

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def resolve(
        self,
        text: str,
        *,
        known_facility_ids: Sequence[str] = (),
        allow_llm: bool = True,
    ) -> IntentResolution:
        """
        Resolve free text into an intent.

        Args:
            text:               The user's request.
            known_facility_ids: Real facility ids, used both to ground the rule
                                parser and to constrain the LLM prompt.
            allow_llm:          False forces the deterministic path.

        Returns:
            IntentResolution. Never raises — an unparseable request resolves to
            Intent.UNKNOWN with a rationale rather than failing the run.
        """
        rules = self._rule_based(text, known_facility_ids)

        if rules.confidence >= RULES_CONFIDENT:
            return rules

        if not allow_llm or self.gateway is None or not self.gateway.available:
            reason = "LLM not used" if not allow_llm else (
                self.gateway.unavailable_reason() if self.gateway else "no gateway configured"
            )
            rules.rationale = (
                f"{rules.rationale} (rule-based only; {reason})".strip()
            )
            return rules

        try:
            llm = self._llm_based(text, known_facility_ids)
        except LLMFailureError as exc:
            logger.warning("orchestrator.intent.llm_failed error=%s", exc.message)
            rules.rationale = f"{rules.rationale} (LLM interpretation failed: {exc.code.value})"
            return rules

        if llm is None or llm.intent == Intent.UNKNOWN:
            return rules
        return llm

    # ------------------------------------------------------------------
    # Tier 1 — deterministic rules
    # ------------------------------------------------------------------

    def _rule_based(self, text: str, known_ids: Sequence[str]) -> IntentResolution:
        raw = (text or "").strip()
        lowered = f" {raw.lower()} "

        mentioned = self._match_facilities(raw, known_ids)
        has = lambda words: any(w in lowered for w in words)  # noqa: E731

        # Comparison must be checked before plain scenario analysis: "compare
        # closing Delhi vs Mumbai" contains close-words too.
        if has(_COMPARE_WORDS) and len(mentioned) >= 2:
            return IntentResolution(
                intent=Intent.SCENARIO_COMPARISON,
                confidence=0.9,
                source="rules",
                entities=mentioned,
                scenarios=[
                    ScenarioIntentSpec(
                        action=ScenarioActionType.CLOSE_FACILITY,
                        facility_ids=[fid],
                        label=f"Close {fid}",
                    )
                    for fid in mentioned
                ],
                rationale=f"Comparison language with {len(mentioned)} named facilities.",
            )

        # Capacity change is checked early: "reduce Delhi capacity by 2,000
        # units/day" contains "reduce", which is close-language, but reducing
        # capacity is emphatically NOT closing a facility — one is a reversible
        # operational change, the other is a structural one governed HUMAN_ONLY.
        capacity_spec = self._parse_capacity_change(raw, lowered, mentioned)
        if capacity_spec is not None:
            return IntentResolution(
                intent=Intent.SCENARIO_ANALYSIS,
                confidence=0.85,
                source="rules",
                entities=mentioned,
                scenarios=[capacity_spec],
                rationale="Capacity-change language with a named facility and a quantity.",
            )

        if has(_EXTERNAL_WORDS):
            return IntentResolution(
                intent=Intent.EXTERNAL_EVENT,
                confidence=0.8 if mentioned else 0.6,
                source="rules",
                entities=mentioned,
                rationale="External hazard or forecast language detected.",
            )

        # An explanation request must be recognised BEFORE the resilience rule:
        # "why is Delhi high risk?" is a question about existing evidence, and
        # answering it must not launch a fresh assessment it did not ask for.
        if has(_EXPLAIN_WORDS):
            return IntentResolution(
                intent=Intent.EXPLANATION,
                confidence=0.85,
                source="rules",
                entities=mentioned,
                rationale="Explanation request; answered from existing evidence.",
            )

        if has(_RESILIENCE_WORDS):
            return IntentResolution(
                intent=Intent.RESILIENCE_QUERY,
                confidence=0.9,
                source="rules",
                entities=mentioned,
                rationale="Exposure/resilience language detected.",
            )

        if has(_CLOSE_WORDS) and mentioned:
            return IntentResolution(
                intent=Intent.SCENARIO_ANALYSIS,
                confidence=0.85,
                source="rules",
                entities=mentioned,
                scenarios=[
                    ScenarioIntentSpec(
                        action=ScenarioActionType.CLOSE_FACILITY,
                        facility_ids=list(mentioned),
                        label=f"Close {', '.join(mentioned)}",
                    )
                ],
                rationale="Closure language with a named facility.",
            )

        if has(_OPTIMIZE_WORDS):
            return IntentResolution(
                intent=Intent.OPTIMIZATION_REQUEST,
                confidence=0.8,
                source="rules",
                entities=mentioned,
                rationale="Optimization language detected.",
            )

        if has(_STATE_WORDS):
            return IntentResolution(
                intent=Intent.NETWORK_STATE_QUERY,
                confidence=0.8,
                source="rules",
                entities=mentioned,
                rationale="Current-state query language detected.",
            )

        # "what if" without a recognised facility — likely a scenario, but the
        # target is unresolved, so confidence stays low and the LLM may help.
        if "what if" in lowered or "what happens if" in lowered:
            return IntentResolution(
                intent=Intent.SCENARIO_ANALYSIS,
                confidence=0.4,
                source="rules",
                entities=mentioned,
                rationale="Hypothetical phrasing, but no facility matched.",
            )

        return IntentResolution(
            intent=Intent.UNKNOWN,
            confidence=0.0,
            source="rules",
            entities=mentioned,
            rationale="No rule matched the request.",
        )

    @staticmethod
    def _parse_capacity_change(
        raw: str, lowered: str, mentioned: Sequence[str],
    ) -> Optional[ScenarioIntentSpec]:
        """
        Parse "reduce <facility> capacity by 2,000 units/day" and its variants.

        Returns None unless ALL of a facility, capacity language, a direction and
        a quantity are present. A partial match is deliberately not guessed at:
        the difference between −2,000 units and −20% is the whole answer, and
        inventing either would put a fabricated number into the MILP.
        """
        if not mentioned:
            return None
        if not any(w in lowered for w in _CAPACITY_WORDS):
            return None

        down = any(w in lowered for w in _CAPACITY_DOWN)
        up = any(w in lowered for w in _CAPACITY_UP)
        if down == up:          # neither stated, or contradictory
            return None
        sign = -1.0 if down else 1.0
        facility_ids = list(mentioned)
        verb = "Reduce" if down else "Increase"

        abs_match = _CAPACITY_ABS_RE.search(raw)
        if abs_match:
            units = float(abs_match.group(1).replace(",", ""))
            return ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=facility_ids,
                capacity_delta_units=sign * units,
                label=f"{verb} {', '.join(facility_ids)} capacity by {units:,.0f} units",
            )

        pct_match = _CAPACITY_PCT_RE.search(raw)
        if pct_match:
            pct = float(pct_match.group(1).replace(",", ""))
            multiplier = 1.0 + sign * (pct / 100.0)
            if multiplier < 0:
                return None
            return ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=facility_ids,
                capacity_multiplier=round(multiplier, 6),
                label=f"{verb} {', '.join(facility_ids)} capacity by {pct:g}%",
            )

        return None

    @staticmethod
    def _match_facilities(text: str, known_ids: Sequence[str]) -> List[str]:
        """
        Match facility ids by exact id or by their human-readable fragments.

        Only ever returns ids that genuinely exist in `known_ids`, so the parser
        cannot invent a facility.
        """
        if not known_ids:
            return []
        lowered = text.lower()
        hits: List[str] = []
        for fid in known_ids:
            if fid.lower() in lowered:
                hits.append(fid)
                continue
            # "DC_DELHI_NCR" → tokens {dc, delhi, ncr}; match distinctive ones.
            tokens = [t for t in re.split(r"[^a-z0-9]+", fid.lower())
                      if len(t) > 2 and t not in {"dc", "plant", "wh", "new"}]
            if tokens and any(re.search(rf"\b{re.escape(t)}\b", lowered) for t in tokens):
                hits.append(fid)
        seen: set = set()
        return [h for h in hits if not (h in seen or seen.add(h))]

    # ------------------------------------------------------------------
    # Tier 2 — model interpretation
    # ------------------------------------------------------------------

    def _llm_based(self, text: str, known_ids: Sequence[str]) -> Optional[IntentResolution]:
        """
        Ask the model to classify. Constrained to the real facility list.

        The gateway takes a single `prompt` field, so the whole instruction —
        role, schema, allowed values — is inlined here.
        """
        assert self.gateway is not None
        facility_list = ", ".join(known_ids[:60]) or "(none supplied)"
        intents = ", ".join(i.value for i in Intent if i != Intent.UNKNOWN)
        actions = ", ".join(a.value for a in ScenarioActionType)

        prompt = (
            "You are an intent classifier for a supply-chain network optimization system.\n"
            "Classify the user request and return ONLY a JSON object. No prose, no code fences.\n\n"
            f"Valid intents: {intents}\n"
            f"Valid scenario actions: {actions}\n"
            f"Valid facility identifiers (use these EXACT strings, never invent one): {facility_list}\n\n"
            "JSON schema:\n"
            "{\n"
            '  "intent": "<one valid intent>",\n'
            '  "confidence": <number 0..1>,\n'
            '  "facility_ids": ["<exact identifiers mentioned>"],\n'
            '  "scenarios": [{"action": "<valid action>", "facility_ids": ["..."], '
            '"target_facility_id": null, "capacity_multiplier": null, '
            '"capacity_delta_units": null, "demand_multiplier": null, '
            '"label": "short label"}],\n'
            '  "rationale": "one short sentence"\n'
            "}\n\n"
            "Rules:\n"
            "- If no listed facility is clearly referenced, return an empty facility_ids list.\n"
            "- Never output a facility identifier that is not in the list above.\n"
            "- Use SCENARIO_COMPARISON when two or more alternatives are contrasted.\n"
            "- Use EXTERNAL_EVENT for weather, disaster, strike or other outside events.\n"
            "- Use EXPLANATION when the user asks WHY something is the case, rather than\n"
            "  asking for a new analysis.\n"
            "- For CHANGE_CAPACITY set capacity_delta_units for an absolute change stated\n"
            "  in units (negative to reduce), or capacity_multiplier for a percentage\n"
            "  change (0.8 = 20% reduction). Never set both, and never guess a quantity\n"
            "  the user did not state.\n\n"
            f"User request: {text}\n"
        )

        response = self.gateway.generate(prompt, purpose="intent")
        parsed = extract_json(response.output)
        if not parsed:
            logger.warning("orchestrator.intent.llm_unparseable request_id=%s", response.request_id)
            return None

        try:
            intent = Intent(str(parsed.get("intent", "")).strip().upper())
        except ValueError:
            return None

        allowed = set(known_ids)
        # Drop anything the model invented — the network is the authority.
        entities = [f for f in parsed.get("facility_ids", []) or [] if f in allowed]

        scenarios: List[ScenarioIntentSpec] = []
        for raw in parsed.get("scenarios", []) or []:
            if not isinstance(raw, dict):
                continue
            try:
                action = ScenarioActionType(str(raw.get("action", "")).strip().upper())
            except ValueError:
                continue
            fids = [f for f in raw.get("facility_ids", []) or [] if f in allowed]
            if not fids:
                continue
            target = raw.get("target_facility_id")

            def number(key: str) -> Optional[float]:
                """A quantity the model stated, or None. Never a substituted default."""
                value = raw.get(key)
                if value is None or isinstance(value, bool):
                    return None
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None

            multiplier = number("capacity_multiplier")
            delta = number("capacity_delta_units")
            if multiplier is not None and delta is not None:
                # Ambiguous. Drop both rather than picking one — the validator
                # would reject the pair anyway, and guessing here would hide
                # which quantity the model actually meant.
                logger.warning(
                    "orchestrator.intent.capacity_ambiguous multiplier=%s delta=%s",
                    multiplier, delta,
                )
                multiplier = delta = None

            scenarios.append(ScenarioIntentSpec(
                action=action,
                facility_ids=fids,
                target_facility_id=target if target in allowed else None,
                capacity_multiplier=multiplier,
                capacity_delta_units=delta,
                demand_multiplier=number("demand_multiplier"),
                label=str(raw.get("label") or f"{action.value} {', '.join(fids)}")[:120],
            ))

        try:
            confidence = float(parsed.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        return IntentResolution(
            intent=intent,
            confidence=min(1.0, max(0.0, confidence)),
            source="llm",
            entities=entities,
            scenarios=scenarios,
            rationale=str(parsed.get("rationale", ""))[:300],
            raw_model_output=response.output[:2000],
        )
