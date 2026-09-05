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

from netgravity.orchestrator.agents.intent_agent import (
    IntentAgent,
    _MARKET_DOWN_WORDS,
    _MARKET_SUBJECTS,
    _MARKET_UP_WORDS,
)
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
    MarketSignalSpec,
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
#: Asking what the assistant is for.
#:
#: Deliberately narrow. Every phrase here is about the ASSISTANT — "you", "I
#: ask", "this assistant" — so it cannot swallow a question about the network
#: that happens to contain "what can". "What can I do about Delhi's capacity?"
#: is a scenario question and matches none of these.
_CAPABILITY_WORDS = (
    "what can you do", "what do you do", "what can you help",
    "what are you able to do", "what are your capabilities",
    "what can i ask", "what questions can i ask", "what should i ask",
    "what can this assistant do", "how can you help", "what are you for",
    "what kind of questions", "what sort of questions", "help me get started",
)

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
#: Outcomes that exist ONLY in a solve. An explanatory question about one of
#: these needs the optimum, not the resilience registry — see `_classify`.
_SOLVED_OUTCOME_WORDS = ("unserved", "unmet", "not served", "shortfall",
                         "stranded", "infeasible", "capacity breach",
                         "open facilit", "closed facilit", "below 100")

#: Words that point back at the previous turn, making a short message a genuine
#: follow-up rather than a new request. See `_is_elliptical`.
_BACK_REFERENCES = ("it", "that", "this", "these", "those", "them", "there",
                    "same", "instead", "again", "one")

#: Verbs that carry a request of their own. A short message containing one is
#: NOT elliptical — it stands by itself, whether or not this system can answer
#: it. Kept deliberately small: every addition makes the follow-up path
#: narrower, and the failure it guards against (answering a question nobody
#: asked) is worse than declining to inherit context.
_STANDALONE_VERBS = {
    "tell", "give", "write", "sing", "draw", "make", "translate", "define",
    "recommend", "suggest", "help", "teach", "send", "email", "call",
    "book", "buy", "order", "play", "search", "google", "remind",
}

#: Words that open a COMPLETE question. A message starting with one of these
#: carries its own subject and verb, so it is a new request rather than a
#: fragment leaning on the previous turn — see `_is_elliptical`. Closed set:
#: these are the English interrogatives, not a vocabulary that grows with the
#: domain.
_INTERROGATIVES = {
    "who", "whom", "whose", "what", "when", "where", "which", "how", "why",
    "is", "are", "was", "were", "do", "does", "did", "can", "could", "should",
    "would", "will", "has", "have", "had",
}
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
#: The magnitude EXACTLY as typed — "6%", "INR 2 per kg", "12 percent". Kept
#: as text on purpose; see `_extract_market_signal`.
_MAGNITUDE_RE = re.compile(
    r"(?:(?:INR|Rs\.?|₹|USD|\$)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:per|/)\s?\w+)?"
    r"|\d+(?:\.\d+)?\s?(?:%|percent|per cent))",
    re.IGNORECASE,
)

#: Which guardrail bucket a subject belongs to. The guardrail owns the
#: THRESHOLDS; this only names the family, so a signal is scored under the
#: right policy row.
_MARKET_BUCKETS = {
    "CARRIER": ("trucking rate", "transport rate", "shipping rate", "haulage",
                "carrier capacity", "carrier rate", "ocean freight",
                "air freight", "freight rate", "freight cost", "surcharge",
                "port charge", "port handling", "terminal handling",
                "handling charge", "handling fee", "handling rate",
                "demurrage", "detention"),
    "SUPPLIER": ("wage", "labour cost", "labor cost", "warehousing rate",
                 "storage cost", "lease rate", "rent"),
    "MACRO": ("diesel", "petrol", "fuel", "crude", "oil price", "toll",
              "tariff", "duty", "customs", "gst", "excise", "levy",
              "exchange rate", "currency", "rupee", "forex", "fx"),
}


def _bucket_for(subject: str) -> str:
    """Name the guardrail family for a subject. UNKNOWN when unmatched."""
    for bucket, subjects in _MARKET_BUCKETS.items():
        if subject in subjects:
            return bucket
    return "UNKNOWN"


_AMBIGUITY_FREE_INTENTS = frozenset({
    Intent.STATUS_QUERY,
    # A question about the assistant names no node to disambiguate.
    Intent.CAPABILITY_QUERY,
    Intent.FORECAST,
    Intent.NETWORK_STATE_QUERY,
    Intent.OPTIMIZATION_REQUEST,
    # A market change is about the outside world, not about one of our nodes.
    # "Port charges at Mumbai went up" names a city, not a facility, and
    # asking "which Mumbai did you mean?" would be nonsense. Whether the
    # change actually touches DC_MUMBAI is a guardrail question, answered
    # deterministically against master data when the signal is scored — not a
    # clarification to interrupt the user with.
    #
    # As with the others, this does NOT skip unknown-entity detection: if the
    # user names a site that does not exist, that is still caught.
    Intent.MARKET_INTELLIGENCE,
    # An UNKNOWN request has no action to disambiguate FOR.
    #
    # "What is the weather in Mumbai tomorrow?" is not a question this system
    # can answer, and it was met with "I found 2 facilities matching 'mumbai':
    # F001, M002. Which one do you mean?" — a clarification implying the
    # question was understood and only the subject was unclear. Naming a city
    # is not asking about a facility. Where the intent is unknown, saying so is
    # the honest reply, and it is what `_unsupported_response` already exists
    # to give.
    Intent.UNKNOWN,
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

    Constructed with no agents, this instance is DETERMINISTIC-ONLY: the
    defaults below hold no model client, so `allow_llm=True` has nothing to call
    and every turn is answered by the rule tier. That is deliberate — offline by
    default, and a bare instance in a test can never reach the network.

    The real entry point supplies the configured agents: `ChatService` builds
    its NLU from `orchestrator.services["intent_agent"]`, which
    `build_orchestrator` creates as `IntentAgent(gateway)`. If you are
    constructing this class yourself and expect the model tier, pass the agents
    in — a warning is emitted on the first turn that asks for the LLM without
    one, because the Phase 8.0 validation lost three "live" calls to exactly
    this silent degradation before the ledger's zero call count gave it away.
    """

    def __init__(
        self,
        intent_agent: Optional[IntentAgent] = None,
        signal_agent: Optional[ExternalSignalAgent] = None,
    ) -> None:
        self.intent_agent = intent_agent or IntentAgent(None)
        self.signal_agent = signal_agent or ExternalSignalAgent(None)
        #: Set once the no-client warning has been emitted, so a long
        #: conversation does not repeat it on every turn.
        self._warned_no_client = False

    def _warn_if_llm_unavailable(self, allow_llm: bool) -> None:
        """
        Say so when the model tier was asked for and cannot be reached.

        Deliberately does NOT change behaviour: the rule tier still answers, and
        an offline deployment is unaffected. It only removes the silence.
        """
        if not allow_llm or self._warned_no_client:
            return
        gateway = getattr(self.intent_agent, "gateway", None)
        if gateway is not None and getattr(gateway, "available", False):
            return
        self._warned_no_client = True
        logger.warning(
            "orchestrator.nlu.llm_tier_unavailable allow_llm=True but no usable "
            "model client is configured on this ConversationalNLU; answering "
            "from deterministic rules only. Pass intent_agent=IntentAgent("
            "gateway) (ChatService does this from "
            "orchestrator.services['intent_agent']) if the model tier was "
            "intended."
        )

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

        self._warn_if_llm_unavailable(allow_llm)

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
        market_signal = self._extract_market_signal(text, intent)

        return ConversationalIntent(
            schema_version=INTENT_SCHEMA_VERSION,
            intent=intent,
            clarity=IntentClarity.CLEAR,
            ambiguity=AmbiguityKind.NONE,
            mentions=mentions,
            resolved_entity_ids=resolved_ids,
            scenario_overrides=scenarios,
            external_event=external_event,
            market_signal=market_signal,
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

        # FIRST, because a question about the assistant is not a question
        # about the network and every rule below reads it as one. "What can
        # you do?" carries no metric, status or forecast vocabulary and fell
        # through all of them to UNKNOWN; "what questions can I ask you?"
        # contains "ask" and reached EXPLANATION, which solves.
        if any(w in lowered for w in _CAPABILITY_WORDS):
            return (Intent.CAPABILITY_QUERY, [], "rules", 0.95,
                    "A question about what this assistant can do. Answered "
                    "from the planner's workflow catalogue; no engine runs.",
                    None, False)

        # A hazard outranks projection language. "Predicted", "expected" and
        # "forecast" appear in both vocabularies, and the two errors are not
        # symmetric: a projection misread as a hazard merely runs an assessment,
        # while a hazard misread as a projection discards a stated probability
        # into a workflow that has no engine and declines.
        is_hazard = any(w in lowered for w in _HAZARD_WORDS)

        # A stated MARKET change outranks projection language, and must be
        # tested before it. "Fuel prices are expected to rise 8% next month"
        # contains "expected" and "next month" — both projection vocabulary —
        # but it is a reported market movement with a magnitude, not a request
        # to forecast this network. Routed as FORECAST it would reach a
        # workflow with no engine and be declined, and a real signal would be
        # lost at the door.
        #
        # Safe to place first for the same reason the rule parser can: the test
        # requires a market SUBJECT and a stated CHANGE together, so it cannot
        # match a forecast request that merely mentions the future. A hazard
        # still outranks it — "a strike has pushed rates up" is governed as an
        # event, which is the more conservative of the two readings.
        if not is_hazard and IntentAgent._is_market_report(text, lowered):
            return (Intent.MARKET_INTELLIGENCE, [], "rules", 0.8,
                    "A stated market change (subject and movement both "
                    "present). Recorded as context; no probability inferred.",
                    None, False)

        if not is_hazard and (any(w in lowered for w in _FORECAST_WORDS)
                              or _FUTURE_PERIOD_RE.search(lowered)):
            return (Intent.FORECAST, [], "rules", 0.85,
                    "Forecast/projection language detected.", None, False)

        mentions_metric = any(w in lowered for w in _METRIC_WORDS)
        is_explanatory = any(w in lowered for w in _EXPLAIN_WORDS)
        mentions_risk = any(w in lowered for w in _RISK_WORDS)

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

        # "Why is my demand unserved?" — explanatory, but about a FEASIBILITY
        # outcome that exists nowhere except in a solve.
        #
        # `wf_explanation` is deliberately REI-only and runs no optimization.
        # That is the right default and `test_a_cost_explanation_does_not_launch
        # _an_optimization` pins it: an explanation must not silently start work
        # the user did not ask for, and a cost question can be answered from
        # exposure evidence.
        #
        # Unserved demand cannot. Asked why demand was unserved, the
        # explanation narrator answered about facility F003's relative economic
        # exposure — a real figure, about a different question, which is the
        # most misleading kind of wrong answer available. No amount of REI data
        # contains the reason some demand could not be served; only the solve
        # does.
        #
        # Deliberately NARROW: feasibility vocabulary only, and risk vocabulary
        # still wins. Cost, utilisation and SLA explanations are unchanged.
        if (is_explanatory and not mentions_risk
                and any(w in lowered for w in _SOLVED_OUTCOME_WORDS)):
            return (Intent.NETWORK_STATE_QUERY, [], "rules", 0.75,
                    "Explanation of a feasibility outcome. Unserved demand "
                    "exists only in a solve, so the resilience registry cannot "
                    "answer it.",
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
            #
            # A spec that names an action but no magnitude is in exactly the
            # same position as no spec at all: there is still no override to
            # give the MILP. "A major customer is expanding in Delhi" resolves
            # to CHANGE_DEMAND with nothing to multiply by, and it used to pass
            # this check because a spec existed — then failed three steps later
            # inside `ScenarioBuilder` with "CHANGE_DEMAND requires a
            # demand_multiplier", taking the whole execution to FAILED. The
            # user needed one question, not a dead run.
            if not scenarios or not any(s.is_runnable for s in scenarios):
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
        Whether a message DEPENDS on the previous turn to make sense.

        Being short is not enough, and treating it as enough is what let "Tell
        me a joke" — four words, classified UNKNOWN — be promoted to EXPLANATION
        and answered with a briefing about facility F003's economic exposure. A
        confident, correct figure about something nobody asked about is the
        worst failure this layer can produce, and it was reachable from any
        short sentence at all.

        A genuine follow-up REFERS BACK. It either continues the previous
        sentence ("and the cost?"), points at it ("what about that one?"), or is
        a bare fragment with no verb of its own ("the cost impact?"). A short
        message with its own subject and verb and no back-reference is a new
        request, and is left as UNKNOWN so the assistant says it did not
        understand rather than answering something else.
        """
        stripped = (text or "").strip().lower().rstrip("?!. ")
        if not stripped:
            return False
        words = stripped.split()
        if len(words) > 6:
            return False

        # Continues the previous turn.
        if stripped.startswith(("and ", "but ", "or ", "also ", "then ",
                                "what about", "how about", "what if")):
            return True
        # Points back at it.
        if any(f" {p} " in f" {stripped} " for p in _BACK_REFERENCES):
            return True

        # A question that opens with an interrogative and was not caught above
        # asks something COMPLETE. It has its own subject and its own verb, so
        # it depends on nothing, and reading it as a follow-up answers a
        # question the user did not ask.
        #
        # This is a structural test rather than another vocabulary entry, and
        # that is the point. `_STANDALONE_VERBS` is a whitelist of imperative
        # verbs, so it catches "tell me a joke" and cannot, even in principle,
        # catch "who won the cricket world cup" — six words, no listed verb,
        # therefore "a bare fragment", therefore an EXPLANATION of the previous
        # turn. In a conversation that had been discussing the network that
        # question was answered with the network's REI, unserved demand and
        # baseline cost. Every figure was real and none of them had anything to
        # do with what was asked. Growing the verb list one word at a time
        # cannot fix that; not treating a complete question as a fragment can.
        #
        # The continuation and back-reference tests run FIRST and still win, so
        # "what about Mumbai?" and "how much did that cost?" stay follow-ups.
        # A bare "Why?" never reaches here — `_is_explanatory_fragment` claims
        # it earlier in `understand()`.
        if words[0] in _INTERROGATIVES:
            return False

        # A bare fragment: no verb of its own to make it a request.
        return not any(w in _STANDALONE_VERBS for w in words)

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

    @staticmethod
    def _extract_market_signal(text: str,
                               intent: Intent) -> Optional[MarketSignalSpec]:
        """
        Pull a market change out of the message.

        Deterministic and deliberately shallow. It records WHAT the user named
        and WHICH WAY it moved, and copies the magnitude across as the words
        they typed. It does not parse "6%" into 0.06, and it does not decide
        what the change is worth.

        Two reasons for that restraint. A number parsed here would be one
        assignment away from a solver input, with no unit and nobody having
        computed it. And relevance is not this layer's decision: the guardrail
        scores a signal against a versioned policy, from master data, where the
        reasoning is auditable.

        No probability is produced, and `MarketSignalSpec` has no field that
        could hold one.
        """
        if intent != Intent.MARKET_INTELLIGENCE:
            return None

        lowered = f" {(text or '').lower()} "
        subject = next((s for s in _MARKET_SUBJECTS if s in lowered), "")
        direction = "NEUTRAL"
        if any(w in lowered for w in _MARKET_UP_WORDS):
            direction = "UP"
        elif any(w in lowered for w in _MARKET_DOWN_WORDS):
            direction = "DOWN"

        match = _MAGNITUDE_RE.search(text or "")
        return MarketSignalSpec(
            bucket=_bucket_for(subject),
            direction=direction,
            # The user's own words, verbatim. Not converted, not normalised.
            magnitude=match.group(0).strip() if match else "",
            subject=subject,
            geography="",
            # Never defaulted to today. A signal whose age is assumed is worse
            # than one whose age is unknown.
            effective_date=None,
        )

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
        # The label AND the id. `describe()` has always returned
        # "Delhi Distribution Center (DC)" and the question printed "F004" —
        # so a user asked to choose between "F004, M001" was given two opaque
        # keys, one of which is a distribution centre and the other a demand
        # market. Which they are is the entire content of the question.
        #
        # The id stays because it is the answer: an exact id is what the entity
        # resolver matches first, so naming it tells the user precisely what to
        # type back.
        names = ", ".join(
            f"{o['label']} [{o['id']}]" if o.get("label") and o["label"] != o["id"]
            else o["id"]
            for o in options
        )
        return ConversationalIntent(
            intent=Intent.UNKNOWN,
            clarity=IntentClarity.AMBIGUOUS,
            ambiguity=AmbiguityKind.AMBIGUOUS_ENTITY,
            clarification=ClarificationRequest(
                kind=AmbiguityKind.AMBIGUOUS_ENTITY,
                question=(
                    f"'{mention.phrase}' matches {len(options)} nodes in your "
                    f"network: {names}. Which one do you mean?"
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
