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

#: Closure/disruption vocabulary. Extended in Phase 3.1 after evaluation found
#: "Simulate closure of DC_DELHI" matching nothing here — "closure" does not
#: contain "close" — so the rules returned UNKNOWN at confidence 0.0 and the
#: request was handed to the model. Every phrase the rules recognise is a
#: request the model never sees, which is both cheaper and safer: a model asked
#: to classify a closure can answer something else, and a closure re-labelled as
#: an optimization request is governed as a REPORT rather than as the structural
#: change it describes.
_CLOSE_WORDS = ("close", "closing", "closure", "shut", "shutdown", "shut down",
                "lose", "losing", "remove", "offline", "down", "disrupt",
                "disruption", "outage", "fail", "decommission", "mothball",
                "halt", "suspend", "disable", "stop")
_COMPARE_WORDS = ("compare", "versus", " vs ", "vs.", "against", "either")
_RESILIENCE_WORDS = ("most exposed", "exposure", "resilien", "vulnerab", "riskiest",
                     "most critical", "single point")
#: "REI" is matched on a WORD BOUNDARY, not as a substring like the phrases
#: above. As a bare substring it appears inside "f-REI-ght", so every sentence
#: about freight rates — "how much did freight rates increase" — was being
#: classified as a resilience query and answered with an exposure ranking.
#: Found while adding the market-intelligence rule, which is the area the
#: defect most affects: freight is market vocabulary.
_REI_TOKEN_RE = re.compile(r"\brei\b", re.IGNORECASE)
_EXTERNAL_WORDS = ("flood", "storm", "cyclone", "hurricane", "earthquake", "strike",
                   "protest", "typhoon", "wildfire", "heatwave", "expected", "forecast",
                   "warning", "alert")
#: Market-intelligence vocabulary, split into a SUBJECT and a CHANGE.
#:
#: Both halves must be present for the rule to fire, and that compound test is
#: the whole design. `_EXTERNAL_WORDS` already claims "expected", "forecast",
#: "warning" and "alert", so a single-word market rule checked first would
#: swallow "flooding expected near Delhi" and route a hazard as a price story;
#: checked last, it would never see "fuel prices are expected to rise 8%",
#: because "expected" would have routed that to EXTERNAL_EVENT first. Requiring
#: a market subject AND a change lets this rule run BEFORE the hazard rule
#: without being able to steal from it: "flooding" is not a market subject, so
#: the compound test cannot match it however the sentence is phrased.
_MARKET_SUBJECTS = (
    "diesel", "petrol", "fuel", "oil price", "crude",
    "freight rate", "freight cost", "ocean freight", "air freight",
    "surcharge", "haulage", "trucking rate", "transport rate", "shipping rate",
    "toll", "tariff", "duty", "customs", "gst", "excise", "levy",
    "port charge", "port handling", "terminal handling", "handling charge",
    "handling fee", "handling rate", "demurrage", "detention",
    "exchange rate", "currency", "rupee", "forex", "fx",
    "carrier capacity", "carrier rate", "wage", "labour cost", "labor cost",
    "warehousing rate", "storage cost", "lease rate", "rent",
)
#: Movement words, split by direction so one list serves both jobs: detecting
#: that a change was stated, and describing which way it went. Kept in this
#: module — the rule parser's — so the conversational layer imports them
#: rather than keeping a second copy that can drift out of agreement with the
#: rule that fires the intent in the first place.
_MARKET_UP_WORDS = (
    "up", "rise", "risen", "rising", "rose", "increase", "increased",
    "increasing", "hike", "hiked", "surge", "surged", "jump", "jumped",
    "climb", "climbed", "costlier", "dearer", "imposed", "levied", "added",
    "upward", "uplift",
)
_MARKET_DOWN_WORDS = (
    "down", "fall", "fallen", "falling", "fell", "drop", "dropped",
    "decrease", "decreased", "decline", "declined", "cut", "reduced",
    "cheaper", "downward",
)
#: Movement stated without a direction — "revised", "changed". Enough to say a
#: change happened, not enough to say which way, and the spec records NEUTRAL.
_MARKET_NEUTRAL_CHANGES = (
    "revised", "revision", "announced", "introduced", "changed", "% ",
    "percent", "per cent",
)
_MARKET_CHANGES = _MARKET_UP_WORDS + _MARKET_DOWN_WORDS + _MARKET_NEUTRAL_CHANGES
#: Words that mean the sentence is a QUESTION about market data rather than a
#: report of a change. "What has diesel done this year?" is not a signal to
#: record — there is nothing stated to record — so the rule must not fire.
_MARKET_QUESTION_STARTS = ("what", "how much", "how has", "why", "when", "is ",
                           "are ", "did ", "has ", "have ", "show ", "tell ")

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
#:
#: `by` is optional but a lead-in word is not: "add another 2,000 units of
#: capacity" states its quantity as plainly as "by 2,000 units" does, and
#: evaluation found the stricter pattern asking "by how much?" about a request
#: that had already said. The lead-in list stays closed so a bare numeral
#: elsewhere in the sentence can never be read as a capacity change.
_CAPACITY_ABS_RE = re.compile(
    r"\b(?:by|another|a\s+further|an\s+extra|extra|additional)\s+"
    r"([\d,]+(?:\.\d+)?)\s*(?:units?|unit/day|units?\s*(?:/|per)\s*\w+)",
    re.IGNORECASE,
)
#: "by 20%" / "by 20 percent" — relative.
_CAPACITY_PCT_RE = re.compile(
    r"\bby\s+([\d,]+(?:\.\d+)?)\s*(?:%|percent|per\s*cent)", re.IGNORECASE,
)
#: "to 8,000 units" / "at 8000 units per day" — an absolute TARGET, not a
#: change. Requires "set"/"make"/"cap" phrasing or a bare "to N units", both of
#: which state a new total rather than a delta.
_CAPACITY_SET_RE = re.compile(
    r"\b(?:to|at)\s+([\d,]+(?:\.\d+)?)\s*(?:units?|unit/day|units?\s*(?:/|per)\s*\w+)",
    re.IGNORECASE,
)
#: Verbs that make a quantity a new total rather than a change.
_CAPACITY_SET_VERBS = ("set", "make it", "cap at", "cap it", "fix", "change to",
                       "bring to", "take to")


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
        context_block: str = "",
    ) -> IntentResolution:
        """
        Resolve free text into an intent.

        Args:
            text:               The user's request.
            known_facility_ids: Real facility ids, used both to ground the rule
                                parser and to constrain the LLM prompt.
            allow_llm:          False forces the deterministic path.
            context_block:      Pre-rendered conversation context from
                                `ConversationContext.as_prompt_block()`. Used
                                only to interpret references such as "it" and
                                "why"; never a source of facts, and never
                                consulted by the rule tier — an elliptical
                                follow-up that the rules can answer without
                                context is still answered without it.

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
            llm = self._llm_based(text, known_facility_ids, context_block)
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

        # Market intelligence is checked BEFORE the hazard rule. Safe to do so
        # only because it requires a market SUBJECT and a stated CHANGE
        # together — see _MARKET_SUBJECTS. A hazard sentence contains neither.
        if self._is_market_report(raw, lowered):
            return IntentResolution(
                intent=Intent.MARKET_INTELLIGENCE,
                confidence=0.85 if mentioned else 0.8,
                source="rules",
                entities=mentioned,
                rationale=("A stated market change (subject and movement both "
                           "present). Recorded as context; no probability is "
                           "inferred and no solver input is edited."),
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

        if has(_RESILIENCE_WORDS) or _REI_TOKEN_RE.search(lowered):
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
    def _is_market_report(raw: str, lowered: str) -> bool:
        """
        True when the user REPORTED a market change, both halves stated.

        Three conditions, all required:
          - a market subject (diesel, freight rate, duty, port charge, ...)
          - a movement (up, hiked, revised, a percentage, ...)
          - the sentence is not a question ABOUT that subject

        The question test matters as much as the other two. "What has diesel
        done this year?" names a subject and a movement and states no change
        at all; recording it as a signal would invent a fact from a query.
        """
        if not any(subject in lowered for subject in _MARKET_SUBJECTS):
            return False
        if not any(change in lowered for change in _MARKET_CHANGES):
            return False

        stripped = raw.strip().lower()
        if stripped.endswith("?"):
            return False
        if stripped.startswith(_MARKET_QUESTION_STARTS):
            return False
        return True

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

        # An absolute TARGET is checked first, because "set capacity to 2,000"
        # also contains no direction word and would otherwise fall through as
        # "understood but unusable" — asking "by how much?" about a request
        # that already gave a number.
        set_match = _CAPACITY_SET_RE.search(raw)
        if set_match and any(v in lowered for v in _CAPACITY_SET_VERBS):
            total = float(set_match.group(1).replace(",", ""))
            return ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=list(mentioned),
                capacity_set_units=total,
                label=f"Set {', '.join(mentioned)} capacity to {total:,.0f} units",
            )

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

    def _llm_based(
        self,
        text: str,
        known_ids: Sequence[str],
        context_block: str = "",
    ) -> Optional[IntentResolution]:
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
            '"capacity_delta_units": null, "capacity_set_units": null, '
            '"demand_multiplier": null, '
            '"label": "short label"}],\n'
            '  "rationale": "one short sentence"\n'
            "}\n\n"
            "Rules:\n"
            "- If no listed facility is clearly referenced, return an empty facility_ids list.\n"
            "- Never output a facility identifier that is not in the list above.\n"
            "- Use SCENARIO_COMPARISON when two or more alternatives are contrasted.\n"
            "- Use EXTERNAL_EVENT for weather, disaster, strike or other outside events.\n"
            "- Use MARKET_INTELLIGENCE when the user REPORTS a market change that has\n"
            "  happened or been announced — a fuel price, a freight rate, a surcharge,\n"
            "  a duty, a port charge, an exchange rate. The difference from\n"
            "  EXTERNAL_EVENT is likelihood: a hazard MIGHT happen and carries a\n"
            "  probability; a price rise already has and carries a magnitude. Never\n"
            "  output a probability for a market change.\n"
            "- Use EXPLANATION when the user asks WHY something is the case, rather than\n"
            "  asking for a new analysis.\n"
            "- For CHANGE_CAPACITY set capacity_delta_units for an absolute CHANGE\n"
            "  stated in units (negative to reduce), capacity_multiplier for a\n"
            "  percentage change (0.8 = 20% reduction), or capacity_set_units when the\n"
            "  user gives a new TOTAL (\"set capacity to 8,000\"). Set exactly one, and\n"
            "  never guess a quantity the user did not state.\n"
            "- Do not decide whether a facility exists. If the user names a site that is\n"
            "  not in the list above, return an empty facility_ids list and leave it to\n"
            "  the system, which checks against master data.\n"
            + (f"\n{context_block}\n" if context_block else "")
            + f"\nUser request: {text}\n"
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
            set_units = number("capacity_set_units")
            if sum(v is not None for v in (multiplier, delta, set_units)) > 1:
                # Ambiguous. Drop all rather than picking one — the validator
                # would reject the combination anyway, and guessing here would
                # hide which quantity the model actually meant.
                logger.warning(
                    "orchestrator.intent.capacity_ambiguous multiplier=%s delta=%s set=%s",
                    multiplier, delta, set_units,
                )
                multiplier = delta = set_units = None

            scenarios.append(ScenarioIntentSpec(
                action=action,
                facility_ids=fids,
                target_facility_id=target if target in allowed else None,
                capacity_multiplier=multiplier,
                capacity_delta_units=delta,
                capacity_set_units=set_units,
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
