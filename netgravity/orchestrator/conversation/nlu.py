"""
Orchestrator — Natural-language understanding.

Converts user text into a validated `ConversationalIntent`. That is the whole
job. This module does not choose a workflow, does not call an engine, and does
not produce a number.

    text → IntentAgent (existing rules + LLM tiers)
         → EntityResolver (authoritative master data)
         → ambiguity adjudication
         → ConversationalIntent

The existing `IntentAgent` is REUSED rather than reimplemented — it already has
a tested rule parser, a constrained LLM prompt, capacity extraction and
facility-id filtering. What it lacks, and what this layer adds, is the ability
to say "I am not sure" and ask.

WHY AMBIGUITY IS ADJUDICATED HERE AND NOT IN THE MODEL
──────────────────────────────────────────────────────
A model asked "is this ambiguous?" will answer confidently either way. Whether
"close Delhi" is ambiguous is a fact about the NETWORK — how many Delhi nodes
exist, and what operations are defined on them — not a matter of language. So
it is decided deterministically, from the resolver's output, and the model's
opinion is not consulted.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional, Sequence, Tuple

from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.external_signal_agent import ExternalSignalAgent
from netgravity.orchestrator.conversation.entity_resolver import EntityResolver
from netgravity.orchestrator.exceptions import LLMFailureError
from netgravity.orchestrator.schemas.conversation import (
    INTENT_SCHEMA_VERSION,
    AmbiguityKind,
    ClarificationRequest,
    ConversationalIntent,
    ConversationContext,
    EntityMention,
    ExternalEventSpec,
    IntentClarity,
)
from netgravity.orchestrator.schemas.requests import (
    Intent,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.schemas.network import CanonicalNetwork

logger = logging.getLogger(__name__)

#: Bumped when the prompt or interpretation rules change materially. Recorded
#: on every intent so an old answer can be traced to how it was interpreted.
PROMPT_VERSION = "p3.1"

#: Verbs that are ambiguous ON THEIR OWN for a facility: each could mean a
#: structural closure, a temporary stand-down, or a demand reallocation, and the
#: three have very different consequences.
_AMBIGUOUS_CLOSURE_VERBS = ("close", "closing", "shut", "shut down", "shutdown",
                            "stop", "halt", "suspend", "disable")

#: Phrasing that disambiguates a closure verb into a genuine facility closure.
_CLOSURE_DISAMBIGUATORS = (
    "close the facility", "close facility", "close down the", "closure of",
    "shut the facility", "permanently", "decommission", "simulate closure",
    "closure scenario", "close the warehouse", "close the dc",
    "close the plant", "close the site", "if we lose", "if .* goes down",
    "goes offline", "is offline", "fails", "failure of", "disruption at",
)

#: Countable/inventory phrasing — answerable from the digital twin alone.
_STATUS_WORDS = ("how many", "list the", "show me the list", "which facilities",
                 "what facilities", "count of", "number of", "do we have",
                 "inventory of", "which warehouses", "what warehouses")

#: Quantities that only an OPTIMUM defines. A question mentioning one of these
#: is not a snapshot lookup however it is phrased — "what is the current
#: transportation cost?" needs a solve, and answering it with a facility count
#: would be answering a different question.
_METRIC_WORDS = ("cost", "spend", "utilisation", "utilization", "sla",
                 "service level", "fill rate", "carbon", "emission",
                 "throughput", "savings", "objective")

#: Phrasing that asks about the CURRENT state of such a metric.
_CURRENT_STATE_WORDS = ("current", "today", "right now", "at the moment",
                        "what is the", "what are the", "how much")
_FORECAST_WORDS = ("forecast", "project", "projection", "next quarter",
                   "next month", "next six months", "next year", "will look like",
                   "predict", "expected demand")

#: A future PERIOD named without a projection verb: "what will the network look
#: like in 2027?". Kept separate from the vocabulary above because it is only
#: safe in combination with the hazard guard below — "close DC_DELHI in 2027" is
#: a scenario, not a projection.
_FUTURE_PERIOD_RE = re.compile(r"\bin\s+20[2-9]\d\b|\bby\s+20[2-9]\d\b")

#: Outside-world hazard vocabulary. Checked BEFORE forecast language because
#: the two overlap and the consequence of confusing them is asymmetric: a hazard
#: mistaken for a projection is a risk signal thrown away. Evaluation found "a
#: storm with 60% probability is predicted for Delhi" routed to the forecast
#: workflow, which has no engine and declines — silently discarding a stated
#: probability that RF was entitled to use.
_HAZARD_WORDS = ("flood", "flooding", "storm", "cyclone", "hurricane", "typhoon",
                 "earthquake", "wildfire", "heatwave", "strike", "protest",
                 "blockade", "outage", "monsoon", "snow", "landslide",
                 "curfew", "unrest", "disaster", "tsunami")
_EXPLAIN_WORDS = ("why is", "why are", "why does", "why did", "explain",
                  "what makes", "reason for", "how come", "why")
_RISK_WORDS = ("risk exposure", "exposure of", "risk of", "how exposed",
               "resilience of", "impact if", "rei", "how critical",
               "how important", "criticality")

#: Elliptical follow-ups that swap the SUBJECT while keeping the question.
#: "What about Mumbai?" after a resilience query is another resilience query;
#: without this the phrase classified as UNKNOWN and the user was told their
#: perfectly clear follow-up was not understood.
_FOLLOWUP_COMPARE = ("what about", "compare that", "and for", "how about",
                     "what if instead", "instead of", "and what about")

#: Phrasing that makes a request a what-if about a specific node, beyond the
#: base agent's narrower closure vocabulary.
#: Includes every verb in `_AMBIGUOUS_CLOSURE_VERBS`. Evaluation found "Halt
#: DC_MUMBAI.", "Suspend operations at DC_DELHI." and "Disable the Kolkata DC."
#: never reaching the ambiguity check at all: the verbs were listed as ambiguous
#: but nothing promoted them to SCENARIO_ANALYSIS first, so the request fell
#: through as UNKNOWN and the system answered a clear instruction with "I did
#: not understand" instead of asking which operation was meant.
_SCENARIO_LANGUAGE = (
    "what if", "what happens if", "capacity", "closure", "open", "add another",
    "simulate", "scenario", "reduce", "increase", "expand", "throughput",
    "decommission", "take offline", "offline", "mothball",
    *_AMBIGUOUS_CLOSURE_VERBS,
)

#: Phrasing that makes a request UNAMBIGUOUSLY a what-if about a named node.
#: Deliberately much narrower than `_SCENARIO_LANGUAGE`: this list is used to
#: refuse a model's reclassification, so it must contain only phrases whose
#: scenario reading is not in genuine doubt. "capacity" and "increase" appear in
#: ordinary questions and are excluded for that reason.
_EXPLICIT_SCENARIO_LANGUAGE = (
    "what if", "what happens if", "simulate", "closure of", "close the",
    "shut down", "decommission", "if we lose", "goes offline", "scenario",
    "take offline", "failure of",
)

#: Intents about the network AS A WHOLE rather than a particular node. For
#: these, ambiguity BETWEEN KNOWN NODES is irrelevant — a count of warehouses
#: names no warehouse, so "which Delhi did you mean?" would be nonsense.
#:
#: These intents do NOT skip unknown-entity detection. They used to, and the
#: live evaluation found the hole: the model classified "Tell me about the
#: Chennai distribution centre" as NETWORK_STATE_QUERY, entity adjudication was
#: skipped wholesale, and the system solved the MILP and answered about the
#: whole network as though Chennai were part of it. Whether a named site exists
#: is a question about master data, not about which workflow is running.
_AMBIGUITY_FREE_INTENTS = frozenset({
    Intent.STATUS_QUERY,
    Intent.FORECAST,
    Intent.NETWORK_STATE_QUERY,
    Intent.OPTIMIZATION_REQUEST,
})


def _unique(ids: Sequence[str]) -> List[str]:
    """Order-preserving de-duplication."""
    seen: set = set()
    return [i for i in ids if not (i in seen or seen.add(i))]


class ConversationalNLU:
    """
    Understands a user message against a specific network snapshot.

    Args:
        intent_agent:  existing two-tier intent parser. Reused, not replaced.
        signal_agent:  existing external-signal parser, whose probability
                       extraction is deterministic and already correct.
    """

    def __init__(
        self,
        intent_agent: Optional[IntentAgent] = None,
        signal_agent: Optional[ExternalSignalAgent] = None,
    ) -> None:
        self.intent_agent = intent_agent or IntentAgent(None)
        self.signal_agent = signal_agent or ExternalSignalAgent(None)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def understand(
        self,
        message: str,
        network: CanonicalNetwork,
        *,
        conversation_id: Optional[str] = None,
        allow_llm: bool = True,
        prior_entity_ids: Sequence[str] = (),
        prior_intent: Optional[Intent] = None,
        context: Optional[ConversationContext] = None,
    ) -> ConversationalIntent:
        """
        Interpret one message.

        Args:
            prior_entity_ids: entities from the previous turn, used ONLY to
                resolve elliptical follow-ups ("why?", "what about Mumbai?").
                Never used to fill in a facility the user did not refer to in a
                fresh request — see `_inherit_context`.
            context: structured conversation state shown to the MODEL so it can
                interpret "it", "that" and "why". It carries no scenario
                override and no result value, and the deterministic side does
                not read it — `prior_entity_ids` and `prior_intent` remain the
                only inputs to rule-based inheritance. Built from
                `prior_entity_ids`/`prior_intent` when not supplied, so a caller
                that knows nothing of it still gives the model context.

        Returns:
            ConversationalIntent. Never raises for unparseable input: it returns
            UNSUPPORTED or a clarification, because a chat layer that throws on
            a confusing sentence is a chat layer nobody can use.
        """
        text = (message or "").strip()
        resolver = EntityResolver(network)

        if not text:
            return self._unsupported(
                "Empty message.", conversation_id,
                question="What would you like to know about the network?",
            )

        mentions = resolver.extract_mentions(text)
        unknown_candidates = resolver.find_unknown_candidates(text)
        # De-duplicated: "the Delhi NCR region" matches DC_DELHI through both
        # the "delhi" and "ncr" tokens, and a repeated id would reach the MILP
        # as a scenario naming the same facility twice.
        resolved_ids = _unique([m.resolved_ids[0] for m in mentions if m.is_resolved])

        # ---- classify FIRST ------------------------------------------------
        # Entity problems are only problems for intents that need an entity.
        # "How many warehouses do we have?" names no facility and needs none;
        # adjudicating entities before knowing the intent produced a nonsensical
        # "I could not find 'How' in the network".
        if context is None and (prior_entity_ids or prior_intent):
            context = ConversationContext(
                current_entity_ids=list(prior_entity_ids),
                previous_intent=prior_intent,
            )

        intent, scenarios, source, confidence, rationale, raw, llm_used = \
            self._classify(text, network, resolved_ids, allow_llm, context)

        if intent not in _AMBIGUITY_FREE_INTENTS:
            # Entity-level ambiguity, decided from the network rather than by
            # the model: whether "Delhi" is ambiguous is a fact about how many
            # Delhi nodes exist.
            ambiguous = [m for m in mentions if m.is_ambiguous]
            if ambiguous:
                return self._ambiguous_entity(ambiguous[0], resolver, conversation_id,
                                              mentions, text)

        # ---- the deterministic entity boundary -----------------------------
        # Runs for EVERY intent, including the network-wide ones. A named site
        # that master data does not contain is a fact about master data, and no
        # workflow may proceed on it — see `_AMBIGUITY_FREE_INTENTS`.
        unresolved = self._unresolved_references(
            text, resolver, resolved_ids, unknown_candidates,
        )
        if unresolved:
            return self._unknown_entity(unresolved, resolver, conversation_id,
                                        mentions, text)

        # ---- follow-ups inherit context, fresh requests do not -------------
        inherited = self._inherit_context(
            text, intent, resolved_ids, prior_entity_ids, prior_intent,
        )
        resolved_ids = inherited

        # A bare "Why?" carries no vocabulary of its own — the explanation words
        # are phrases like "why is" and "why did". The request IS an explanation
        # of the previous answer whether or not an entity carried over: "Why?"
        # after a network-cost answer has no subject to inherit and is still a
        # request to explain.
        if intent == Intent.UNKNOWN and self._is_explanatory_fragment(text):
            intent = Intent.EXPLANATION
            rationale = "Elliptical follow-up explaining the previous answer."

        # A follow-up that swaps only the subject keeps the previous question:
        # "What about Mumbai?" after a resilience query is a resilience query.
        elif intent == Intent.UNKNOWN and prior_intent is not None and resolved_ids \
                and self._is_subject_swap(text):
            intent = prior_intent
            rationale = (f"Elliptical follow-up; carried the previous intent "
                         f"({prior_intent.value}) onto a new subject.")

        # Context can supply the subject a classifier needed. "Reduce it by 20%"
        # names no facility, so the first pass saw scenario language with
        # nothing to apply it to; once inheritance has run, re-ask.
        elif intent == Intent.UNKNOWN and resolved_ids and \
                any(w in f" {text.lower()} " for w in _SCENARIO_LANGUAGE):
            intent = Intent.SCENARIO_ANALYSIS
            rationale = "Scenario language resolved against the inherited subject."

        # Anything else elliptical, with context behind it, is a further
        # question about the previous answer rather than a fresh request.
        elif intent == Intent.UNKNOWN and prior_intent is not None and \
                self._is_elliptical(text):
            intent = Intent.EXPLANATION
            rationale = "Elliptical follow-up about the previous answer."

        if not scenarios and intent == Intent.SCENARIO_ANALYSIS and resolved_ids:
            scenarios = self._scenarios_for(text, resolved_ids)

        # ---- intent-level ambiguity ---------------------------------------
        ambiguity = self._detect_intent_ambiguity(text, intent, scenarios, resolved_ids)
        if ambiguity is not None:
            return self._with_clarification(
                intent, ambiguity, mentions, resolved_ids, conversation_id,
                source, confidence, raw,
                mentions_capacity=any(w in f" {text.lower()} "
                                      for w in ("capacity", "throughput")),
            )

        external_event = self._extract_event(text, intent, network, resolved_ids)

        return ConversationalIntent(
            schema_version=INTENT_SCHEMA_VERSION,
            intent=intent,
            clarity=IntentClarity.CLEAR,
            ambiguity=AmbiguityKind.NONE,
            mentions=mentions,
            resolved_entity_ids=resolved_ids,
            scenario_overrides=scenarios,
            external_event=external_event,
            confidence=confidence,
            source=source,
            rationale=rationale,
            conversation_id=conversation_id,
            prompt_version=PROMPT_VERSION,
            model_name=self._model_name() if llm_used else None,
            raw_model_output=raw,
        )

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    def _classify(
        self,
        text: str,
        network: CanonicalNetwork,
        resolved_ids: List[str],
        allow_llm: bool,
        context: Optional[ConversationContext] = None,
    ) -> Tuple[Intent, List[ScenarioIntentSpec], str, float, str, Optional[str], bool]:
        """
        Decide the intent, preferring deterministic rules.

        Conversational intents the existing agent has no concept of (STATUS,
        FORECAST) are matched here first, because they must NOT be routed into a
        workflow that solves. Everything else delegates to the existing agent.
        """
        lowered = f" {text.lower()} "

        # A hazard outranks projection language. "Predicted", "expected" and
        # "forecast" appear in both vocabularies, and the two errors are not
        # symmetric: a projection misread as a hazard merely runs an assessment,
        # while a hazard misread as a projection discards a stated probability
        # into a workflow that has no engine and declines.
        is_hazard = any(w in lowered for w in _HAZARD_WORDS)

        if not is_hazard and (any(w in lowered for w in _FORECAST_WORDS)
                              or _FUTURE_PERIOD_RE.search(lowered)):
            return (Intent.FORECAST, [], "rules", 0.85,
                    "Forecast/projection language detected.", None, False)

        mentions_metric = any(w in lowered for w in _METRIC_WORDS)
        is_explanatory = any(w in lowered for w in _EXPLAIN_WORDS)

        # An inventory question, and NOT one about a computed quantity.
        if any(w in lowered for w in _STATUS_WORDS) and not mentions_metric:
            return (Intent.STATUS_QUERY, [], "rules", 0.85,
                    "Inventory/count question answerable from the digital twin.",
                    None, False)

        # A metric question about current state genuinely needs the optimum.
        # Checked before delegating, because the base agent's state vocabulary
        # does not cover "what is the current transportation cost?".
        if (mentions_metric and not is_explanatory
                and any(w in lowered for w in _CURRENT_STATE_WORDS)):
            return (Intent.NETWORK_STATE_QUERY, [], "rules", 0.8,
                    "Question about a computed metric; requires an optimum.",
                    None, False)

        known_ids = [
            f.id for f in network.facilities
            if f.role.value not in ("MARKET", "CUSTOMER")
        ]
        # Context is rendered once and given to the model only. It names the
        # selectable entities so a follow-up such as "what about Mumbai?" is
        # interpreted against master data rather than guessed at.
        block = ""
        if context is not None:
            block = context.model_copy(
                update={"available_entity_ids": list(known_ids)}
            ).as_prompt_block()

        try:
            resolution = self.intent_agent.resolve(
                text, known_facility_ids=known_ids, allow_llm=allow_llm,
                context_block=block,
            )
        except LLMFailureError:
            # The gateway already retries; a failure here is terminal for the
            # LLM tier. Fall back to rules rather than fabricating an intent.
            logger.warning("nlu.llm_failed falling back to rule-based parsing")
            resolution = self.intent_agent.resolve(
                text, known_facility_ids=known_ids, allow_llm=False,
            )

        intent = resolution.intent
        llm_used = resolution.source == "llm"

        # A model may resolve what the rules could not. It may not overrule what
        # they read plainly.
        #
        # Phase 3.1 evaluation, with a deliberately compromised model, found
        # "Simulate closure of DC_DELHI" returned as OPTIMIZATION_REQUEST. No
        # fabricated value resulted — entity filtering, the intent schema and
        # grounding all held — but the workflow changed, and with it the
        # governed action type: a structural closure analysis (HUMAN_ONLY under
        # R2) became a report (APPROVAL_REQUIRED under R7C). The intent is not a
        # value, so nothing downstream re-checks it; this is the only place the
        # substitution can be refused.
        if llm_used and resolved_ids \
                and intent not in (Intent.SCENARIO_ANALYSIS, Intent.SCENARIO_COMPARISON) \
                and any(w in lowered for w in _EXPLICIT_SCENARIO_LANGUAGE):
            logger.warning(
                "nlu.model_intent_overridden model_said=%s rules_read=SCENARIO_ANALYSIS",
                intent.value,
            )
            intent = Intent.SCENARIO_ANALYSIS

        # A risk question about a specific node is a resilience query, which the
        # base agent only recognises through a narrower vocabulary.
        #
        # Applied to UNKNOWN only. It previously also caught EXPLANATION, which
        # inverted a correct answer: "Explain why DC_MUMBAI has the highest REI"
        # contains "rei", so an explicit request to explain existing evidence was
        # promoted into a fresh assessment the user had not asked for — and one
        # that runs the solver.
        if intent == Intent.UNKNOWN and \
                any(w in lowered for w in _RISK_WORDS) and resolved_ids:
            intent = Intent.RESILIENCE_QUERY

        # The base agent's vocabulary is narrower than conversational phrasing:
        # it does not recognise "closure of", or a capacity change with no
        # quantity. Promoting these to SCENARIO_ANALYSIS is what lets the
        # ambiguity and extraction logic below see them at all — without it,
        # "Reduce Delhi capacity." falls through as UNKNOWN and the user is told
        # the request was not understood, when in fact the only thing missing is
        # a number we should ask for.
        if intent == Intent.UNKNOWN and resolved_ids and \
                any(w in lowered for w in _SCENARIO_LANGUAGE):
            intent = Intent.SCENARIO_ANALYSIS

        return (intent, list(resolution.scenarios), resolution.source,
                resolution.confidence, resolution.rationale,
                resolution.raw_model_output, llm_used)

    def _model_name(self) -> Optional[str]:
        gateway = getattr(self.intent_agent, "gateway", None)
        if gateway is None:
            return None
        return getattr(getattr(gateway, "config", None), "model_name", None) or "gateway"

    # ------------------------------------------------------------------
    # Ambiguity
    # ------------------------------------------------------------------

    @staticmethod
    def _unresolved_references(
        text: str,
        resolver: EntityResolver,
        resolved_ids: List[str],
        weak_candidates: List[str],
    ) -> List[str]:
        """
        Named sites the network does not contain.

        Two tiers, because the evidence differs in strength:

        * **Strong** — a typed reference ("the Bangalore DC") or an identifier
          in this network's own shape ("DC_SHADOW"). These name a node and
          nothing else, so an unresolved one is a missing facility even when the
          sentence also names a real one.
        * **Weak** — a bare capitalised word, only meaningful when the sentence
          resolved nothing at all. "Cyclone Amphan may hit Kolkata" must not be
          refused because no facility is called Amphan.

        Returns the phrases to report. Empty means nothing was named that master
        data cannot account for.
        """
        strong = [
            phrase for phrase in resolver.unknown_node_references(text)
            if phrase.upper() not in {i.upper() for i in resolved_ids}
        ]
        if strong:
            return strong
        if not resolved_ids and weak_candidates and \
                ConversationalNLU._references_a_node(text):
            return list(weak_candidates)
        return []

    @staticmethod
    def _references_a_node(text: str) -> bool:
        """Whether the sentence is *about* a facility at all."""
        lowered = text.lower()
        return any(w in lowered for w in (
            "dc", "warehouse", "facility", "facilit", "plant", "site", "depot",
            "hub", "close", "open", "capacity", "risk", "exposure",
            # "the Chennai distribution centre" names a node as plainly as
            # "the Chennai DC" does; without these the unknown site was never
            # reported and the user simply got "not understood".
            "distribution", "centre", "center", "node", "market", "location",
        ))

    def _detect_intent_ambiguity(
        self,
        text: str,
        intent: Intent,
        scenarios: List[ScenarioIntentSpec],
        resolved_ids: List[str],
    ) -> Optional[AmbiguityKind]:
        """
        Decide whether the ACTION is unclear, given a resolved entity.

        The canonical case is "Close Delhi." — the node is unambiguous, the verb
        is not. It could mean simulate a facility closure, stop serving that
        market, or take a lane out. Those are different scenarios with different
        answers, so the system asks rather than picking.
        """
        lowered = f" {text.lower()} "

        if intent == Intent.SCENARIO_ANALYSIS and resolved_ids:
            has_closure_verb = any(v in lowered for v in _AMBIGUOUS_CLOSURE_VERBS)
            if has_closure_verb:
                disambiguated = any(
                    re.search(pattern, lowered) for pattern in _CLOSURE_DISAMBIGUATORS
                )
                if not disambiguated:
                    return AmbiguityKind.AMBIGUOUS_INTENT

            # A what-if with nothing to vary. "Reduce Delhi capacity" states no
            # quantity; "Reduce it by 20%" states one but not what it applies
            # to. Either way there is no override to give the MILP, and running
            # a scenario workflow with an empty override list would analyse the
            # baseline and label the answer hypothetical — a wrong answer
            # dressed as a right one. The MILP needs a number and we will not
            # invent one.
            if not scenarios:
                return AmbiguityKind.MISSING_PARAMETER

        # A resolved node with no recognisable operation: "Delhi.", "Do
        # something about Delhi." Previously these returned UNKNOWN with
        # clarity=CLEAR, so the user got a flat "I did not understand that"
        # about a message whose subject was perfectly clear. We know WHAT they
        # mean, only not what to do about it — which is precisely a question
        # worth asking rather than a failure worth reporting.
        if intent == Intent.UNKNOWN and resolved_ids:
            return AmbiguityKind.AMBIGUOUS_INTENT

        return None

    # ------------------------------------------------------------------
    # Context inheritance
    # ------------------------------------------------------------------

    @staticmethod
    def _is_explanatory_fragment(text: str) -> bool:
        """
        Whether a short follow-up is asking WHY about the previous answer.

        Matched on the whole fragment rather than on phrases, because these
        utterances are too short to contain one: "Why?", "Why is that?", "How
        come?".
        """
        stripped = (text or "").strip().lower().rstrip("?!. ")
        if not stripped:
            return False
        return (
            stripped.startswith(("why", "how come", "explain", "for what reason"))
            or stripped in ("reason", "reasons", "and why", "but why")
        )

    @staticmethod
    def _is_subject_swap(text: str) -> bool:
        """Whether a follow-up replaces only the subject of the last question."""
        stripped = (text or "").strip().lower()
        return (
            any(stripped.startswith(p) for p in _FOLLOWUP_COMPARE)
            or stripped.startswith(("and ", "what about", "how about"))
        )

    @staticmethod
    def _is_elliptical(text: str) -> bool:
        """
        Whether a message is too short to stand on its own.

        Six words is the same threshold `_inherit_context` uses, deliberately:
        a message that was short enough to inherit a subject is short enough to
        be a further question about it.
        """
        stripped = (text or "").strip()
        return bool(stripped) and len(stripped.split()) <= 6

    @staticmethod
    def _inherit_context(
        text: str,
        intent: Intent,
        resolved_ids: List[str],
        prior_entity_ids: Sequence[str],
        prior_intent: Optional[Intent],
    ) -> List[str]:
        """
        Carry entities forward ONLY for elliptical follow-ups.

        "Why?" and "Show me the cost impact" refer to the previous subject and
        have no subject of their own. "What if we close Mumbai instead?" names
        its own subject and must NOT accumulate Delhi from the previous turn —
        that is how a conversation silently grows a scenario nobody asked for.
        """
        if resolved_ids:
            return resolved_ids
        if not prior_entity_ids:
            return resolved_ids

        lowered = text.strip().lower()
        elliptical = (
            len(lowered.split()) <= 6
            or lowered.startswith(("why", "and why", "how", "what about that",
                                   "show me", "explain"))
        )
        if elliptical:
            logger.info("nlu.context_inherited entities=%s", list(prior_entity_ids))
            return list(prior_entity_ids)
        return resolved_ids

    # ------------------------------------------------------------------
    # Scenario and event extraction
    # ------------------------------------------------------------------

    def _scenarios_for(
        self, text: str, resolved_ids: List[str],
    ) -> List[ScenarioIntentSpec]:
        """
        Build a scenario spec when the rule parser produced none but the intent
        is clearly a what-if against a known node.

        Delegates quantity parsing to the existing `IntentAgent` helper so there
        is exactly one implementation of "reduce by 2,000 units".
        """
        lowered = f" {text.lower()} "
        spec = self.intent_agent._parse_capacity_change(  # noqa: SLF001
            text, lowered, resolved_ids,
        )
        if spec is not None:
            return [spec]

        if any(re.search(p, lowered) for p in _CLOSURE_DISAMBIGUATORS):
            return [ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY,
                facility_ids=list(resolved_ids),
                label=f"Close {', '.join(resolved_ids)}",
            )]
        return []

    def _extract_event(
        self,
        text: str,
        intent: Intent,
        network: CanonicalNetwork,
        resolved_ids: List[str],
    ) -> Optional[ExternalEventSpec]:
        """
        Pull an external event out of the message.

        Probability extraction is delegated to the existing signal agent, whose
        rules only ever produce a P from an explicit statement. Severity is
        carried across as severity and is never converted.
        """
        if intent != Intent.EXTERNAL_EVENT:
            return None

        known = [f.id for f in network.facilities
                 if f.role.value not in ("MARKET", "CUSTOMER")]
        signal = self.signal_agent.interpret(
            text, known_facility_ids=known, allow_llm=False,
        )
        return ExternalEventSpec(
            event_type=signal.event_type,
            location=signal.location or (resolved_ids[0] if resolved_ids else ""),
            severity=signal.severity,
            event_probability=signal.event_probability,
            probability_basis=signal.probability_basis,
        )

    # ------------------------------------------------------------------
    # Clarification builders
    # ------------------------------------------------------------------

    def _ambiguous_entity(
        self,
        mention: EntityMention,
        resolver: EntityResolver,
        conversation_id: Optional[str],
        mentions: List[EntityMention],
        text: str,
    ) -> ConversationalIntent:
        options = [resolver.describe(fid) for fid in mention.resolved_ids]
        names = ", ".join(o["id"] for o in options)
        return ConversationalIntent(
            intent=Intent.UNKNOWN,
            clarity=IntentClarity.AMBIGUOUS,
            ambiguity=AmbiguityKind.AMBIGUOUS_ENTITY,
            clarification=ClarificationRequest(
                kind=AmbiguityKind.AMBIGUOUS_ENTITY,
                question=(
                    f"I found {len(options)} facilities matching "
                    f"'{mention.phrase}': {names}. Which one do you mean?"
                ),
                options=options,
            ),
            mentions=mentions,
            conversation_id=conversation_id,
            prompt_version=PROMPT_VERSION,
            rationale=f"'{mention.phrase}' matches {len(options)} network nodes.",
        )

    def _unknown_entity(
        self,
        candidates: List[str],
        resolver: EntityResolver,
        conversation_id: Optional[str],
        mentions: List[EntityMention],
        text: str,
    ) -> ConversationalIntent:
        from netgravity.schemas.network import NodeRole

        known = resolver.facilities_of_role(NodeRole.DC)
        logger.info(
            "nlu.unknown_entity_refused phrases=%s known_dcs=%d",
            candidates, len(known),
        )
        return ConversationalIntent(
            intent=Intent.UNKNOWN,
            clarity=IntentClarity.AMBIGUOUS,
            ambiguity=AmbiguityKind.UNKNOWN_ENTITY,
            clarification=ClarificationRequest(
                kind=AmbiguityKind.UNKNOWN_ENTITY,
                question=(
                    f"I could not find '{candidates[0]}' in the current network "
                    f"data. The distribution centres I know are: "
                    f"{', '.join(known) or '(none)'}. Which did you mean?"
                ),
                options=[resolver.describe(fid) for fid in known],
            ),
            mentions=mentions,
            # The record that blocks execution. `is_actionable` reads this, so
            # the refusal is a property of the intent rather than of whichever
            # branch happened to produce it.
            unresolved_mentions=list(candidates),
            conversation_id=conversation_id,
            prompt_version=PROMPT_VERSION,
            rationale=(f"'{candidates[0]}' does not correspond to any node in "
                       f"master data. No workflow was run."),
        )

    def _with_clarification(
        self,
        intent: Intent,
        kind: AmbiguityKind,
        mentions: List[EntityMention],
        resolved_ids: List[str],
        conversation_id: Optional[str],
        source: str,
        confidence: float,
        raw: Optional[str],
        *,
        mentions_capacity: bool = True,
    ) -> ConversationalIntent:
        target = resolved_ids[0] if resolved_ids else "that facility"

        if kind == AmbiguityKind.AMBIGUOUS_INTENT:
            clarification = ClarificationRequest(
                kind=kind,
                question=(
                    f"Do you want to simulate closure of the {target} facility, "
                    f"or stop customer allocation from {target}?"
                ),
                options=[
                    {"id": "CLOSE_FACILITY",
                     "label": f"Simulate closing the {target} facility"},
                    {"id": "SHIFT_VOLUME",
                     "label": f"Shift {target}'s volume to another facility"},
                    {"id": "CHANGE_CAPACITY",
                     "label": f"Reduce {target}'s capacity instead"},
                ],
            )
            clarity = IntentClarity.AMBIGUOUS
        elif mentions_capacity:
            clarification = ClarificationRequest(
                kind=kind,
                question=(
                    f"By how much should {target}'s capacity change? "
                    f"You can give an absolute figure (\"reduce by 2,000 units/day\") "
                    f"or a percentage (\"reduce by 20%\")."
                ),
                missing_parameter="capacity",
            )
            clarity = IntentClarity.INSUFFICIENT_INFORMATION
        else:
            # A what-if we could not turn into a concrete override. Naming the
            # options is more useful than asking an open question, and keeps
            # the answer inside what the scenario planner can actually model.
            clarification = ClarificationRequest(
                kind=kind,
                question=(
                    f"What change to {target} should I model? For example: close "
                    f"the facility, reduce its capacity by a stated amount, or "
                    f"shift its volume elsewhere."
                ),
                missing_parameter="scenario_override",
            )
            clarity = IntentClarity.INSUFFICIENT_INFORMATION

        return ConversationalIntent(
            intent=intent,
            clarity=clarity,
            ambiguity=kind,
            clarification=clarification,
            mentions=mentions,
            resolved_entity_ids=resolved_ids,
            confidence=confidence,
            source=source,
            conversation_id=conversation_id,
            prompt_version=PROMPT_VERSION,
            raw_model_output=raw,
            rationale="The target is clear but the requested operation is not.",
        )

    @staticmethod
    def _unsupported(
        rationale: str,
        conversation_id: Optional[str],
        question: str = "",
    ) -> ConversationalIntent:
        return ConversationalIntent(
            intent=Intent.UNKNOWN,
            clarity=IntentClarity.UNSUPPORTED,
            ambiguity=AmbiguityKind.UNSUPPORTED_ACTION,
            mentions=[],
            conversation_id=conversation_id,
            prompt_version=PROMPT_VERSION,
            rationale=rationale or question,
        )
