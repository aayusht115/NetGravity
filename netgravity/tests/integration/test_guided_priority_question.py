"""
Guided business questions — asking what to optimise FOR, before optimising.

"Optimise my network" is not one question. Cost, carbon and resilience are
three different networks, and picking one silently answers a question the user
did not ask. So the system asks — once, with concrete options.

WHAT MATTERS MOST HERE is what is NOT offered. The natural three-way "cost,
service, or resilience?" is not buildable: ObjectiveMode.COST_SERVICE is
declared in netgravity/schemas/network.py and never referenced by the MILP,
and V1_SUPPORTED_OBJECTIVE_MODES says so in the code. Offering it would ask a
planner to choose something that changes nothing, which is worse than not
asking. These tests pin that boundary, and pin that every option that IS
offered reaches the solver's config.
"""

from __future__ import annotations

import json
from typing import List, Optional

import pytest

from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
from netgravity.orchestrator.conversation import priorities
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.schemas.conversation import AmbiguityKind, IntentClarity
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.schemas.network import (
    ObjectiveMode,
    SourcingPolicy,
    V1_SUPPORTED_OBJECTIVE_MODES,
)

from .conftest import build_delhi_network


class StubModel(LLMGateway):
    """Always returns one chosen intent, naming no facility."""

    def __init__(self, intent: str) -> None:
        super().__init__()
        self.intent = intent

    @property
    def available(self) -> bool:            # type: ignore[override]
        return True

    def unavailable_reason(self) -> str:    # type: ignore[override]
        return ""

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:  # type: ignore[override]
        if purpose != "intent":
            return LLMResponse(output=json.dumps({
                "summary": "Deterministic values only.", "key_drivers": [],
                "risks": [], "recommendation": "Review.", "confidence": "LOW",
                "evidence": [], "claims": [],
            }))
        return LLMResponse(output=json.dumps({
            "intent": self.intent, "confidence": 0.95, "facility_ids": [],
            "scenarios": [], "rationale": "stub",
        }))


def _nlu(intent: str = "OPTIMIZATION_REQUEST") -> ConversationalNLU:
    return ConversationalNLU(intent_agent=IntentAgent(StubModel(intent)))


@pytest.fixture
def network():
    return build_delhi_network()


@pytest.fixture
def carbon_network(network):
    """A network whose data gives a carbon objective something to weigh."""
    config = network.config.model_copy(update={"carbon_price": 2.5})
    return network.model_copy(update={"config": config})


class TestTheQuestionIsAsked:

    def test_an_optimisation_request_with_no_stated_priority_asks(self, carbon_network):
        intent = _nlu().understand("Optimise my network.", carbon_network,
                                   allow_llm=True)

        assert intent.ambiguity == AmbiguityKind.UNSTATED_PRIORITY
        assert intent.needs_clarification
        assert intent.clarification is not None
        assert intent.clarification.missing_parameter == "optimisation_priority"

    def test_the_question_names_its_options(self, carbon_network):
        intent = _nlu().understand("Optimise my network.", carbon_network,
                                   allow_llm=True)
        question = intent.clarification.question

        assert "?" in question
        for option in intent.clarification.options:
            assert option["label"].lower() in question.lower(), (
                f"the question offers {option['id']!r} without naming it")

    def test_a_stated_priority_is_not_asked_about(self, network):
        for text in ("Optimise my network for the lowest cost.",
                     "Give me the most resilient network.",
                     "Optimise for carbon emissions."):
            intent = _nlu().understand(text, network, allow_llm=True)
            assert intent.ambiguity != AmbiguityKind.UNSTATED_PRIORITY, text
            assert intent.clarity == IntentClarity.CLEAR, text

    def test_a_what_if_is_not_asked_about(self, network):
        """
        The user already said what to change. Interrupting a stated scenario
        with "but what should I optimise for?" is an interruption, not a
        clarification.
        """
        intent = _nlu("SCENARIO_ANALYSIS").understand(
            "What if we close DC_DELHI?", network, allow_llm=True)
        assert intent.ambiguity != AmbiguityKind.UNSTATED_PRIORITY

    def test_a_state_query_is_not_asked_about(self, network):
        intent = _nlu("STATUS_QUERY").understand(
            "How many distribution centres do we have?", network, allow_llm=True)
        assert intent.ambiguity != AmbiguityKind.UNSTATED_PRIORITY


class TestOnlyRealLeversAreOffered:
    """The rule this feature exists to keep."""

    def test_service_is_never_offered(self, carbon_network):
        for net in (carbon_network,):
            intent = _nlu().understand("Optimise my network.", net, allow_llm=True)
            ids = {o["id"] for o in intent.clarification.options}
            assert "service" not in ids
            labels = " ".join(o["label"].lower() for o in intent.clarification.options)
            assert "service" not in labels
            assert "fill rate" not in labels

    def test_every_declared_objective_mode_is_one_the_milp_implements(self):
        declared = priorities.declared_objective_modes()
        assert declared, "no lever declares an objective mode; the guard is inert"
        for lever_id, mode in declared.items():
            assert mode in V1_SUPPORTED_OBJECTIVE_MODES, (
                f"{lever_id} selects {mode}, which the V1 MILP does not implement")

    def test_cost_service_would_be_refused_if_it_were_added(self):
        """
        The guard is not decorative. COST_SERVICE is declared in the schema and
        inert in the solver; a lever selecting it must be rejected.
        """
        assert ObjectiveMode.COST_SERVICE.value not in V1_SUPPORTED_OBJECTIVE_MODES

    def test_carbon_is_not_offered_without_a_basis_for_a_weight(self, network):
        """
        WEIGHTED_COST_CARBON multiplies CO₂ by `carbon_weight`. With no weight
        and no price, that term is zero and choosing "lowest carbon" would
        change nothing — so it is not offered, and with cost the only lever
        left there is nothing to ask.
        """
        assert network.config.carbon_weight == 0.0
        assert network.config.carbon_price == 0.0
        assert network.config.carbon_cap_kg is None

        assert "carbon" not in {
            l.id for l in priorities.available_levers(network, network.config)}
        intent = _nlu().understand("Optimise my network.", network, allow_llm=True)
        assert intent.ambiguity != AmbiguityKind.UNSTATED_PRIORITY

    def test_carbon_is_offered_once_the_data_prices_it(self, carbon_network):
        intent = _nlu().understand("Optimise my network.", carbon_network, allow_llm=True)
        assert "carbon" in {o["id"] for o in intent.clarification.options}

    def test_resilience_is_never_shown_to_a_user(self, network, carbon_network):
        """
        The solver always solves for cost. Dual sourcing stays in the engine —
        `SourcingPolicy.DUAL` is really enforced — but it is not a choice a
        planner is asked to make, and "most resilient" overclaims what the
        constraint delivers.
        """
        for net in (network, carbon_network):
            assert "resilience" not in {
                l.id for l in priorities.available_levers(net, net.config)}

        intent = _nlu().understand("Optimise my network.", carbon_network,
                                   allow_llm=True)
        ids = {o["id"] for o in intent.clarification.options}
        assert "resilience" not in ids
        labels = " ".join(o["label"].lower() for o in intent.clarification.options)
        assert "resilient" not in labels

    def test_the_solver_capability_is_kept(self):
        """
        Removed from the question, not from the engine. A caller that knows
        what it is asking for can still ask.
        """
        lever = priorities.by_id("resilience")
        assert lever is not None and lever.offered is False
        assert priorities.config_overrides_for("resilience", None)[
            "sourcing_policy"] == SourcingPolicy.DUAL

    def test_cost_is_always_offered(self, carbon_network):
        intent = _nlu().understand("Optimise my network.", carbon_network,
                                   allow_llm=True)
        assert "cost" in {o["id"] for o in intent.clarification.options}

    def test_one_option_is_not_a_question(self, network):
        """
        With cost the only lever, asking "what should I optimise for: lowest
        total cost?" is an interruption wearing a question mark. The default
        objective simply runs.
        """
        assert not priorities.worth_asking(network, network.config)
        intent = _nlu().understand("Optimise my network.", network, allow_llm=True)
        assert intent.ambiguity == AmbiguityKind.NONE
        assert not intent.needs_clarification


class TestTheAnswerReachesTheSolver:
    """
    A question whose answer changes nothing is the thing this feature is not
    allowed to be.
    """

    def test_a_stated_priority_is_carried_on_the_intent(self, carbon_network):
        intent = _nlu().understand(
            "Optimise my network for the lowest carbon footprint.",
            carbon_network, allow_llm=True)
        assert intent.optimisation_priority == "carbon"

    def test_it_survives_the_request_to_the_execution_context(self):
        request = OrchestratorRequest(
            input="Optimise for resilience.",
            explicit_intent=Intent.OPTIMIZATION_REQUEST,
            metadata={"optimisation_priority": "resilience"},
        )
        ctx = ExecutionContext.from_request(request, "snap_1")
        assert ctx.optimisation_priority == "resilience"

    def test_no_stated_priority_leaves_the_context_clear(self):
        ctx = ExecutionContext.from_request(
            OrchestratorRequest(input="Optimise my network."), "snap_1")
        assert ctx.optimisation_priority is None

    def test_each_lever_changes_the_solver_config(self, carbon_network):
        config = carbon_network.config

        cost = priorities.config_overrides_for("cost", config)
        assert cost["objective_mode"] == ObjectiveMode.COST_MIN

        carbon = priorities.config_overrides_for("carbon", config)
        assert carbon["objective_mode"] == ObjectiveMode.WEIGHTED_COST_CARBON
        # Taken from the data's own carbon price, never invented.
        assert carbon["carbon_weight"] == 2.5

        resilience = priorities.config_overrides_for("resilience", config)
        assert resilience["sourcing_policy"] == SourcingPolicy.DUAL

    def test_an_override_actually_applies_to_the_config(self, carbon_network):
        overrides = priorities.config_overrides_for("resilience", carbon_network.config)
        updated = carbon_network.config.model_copy(update=overrides)

        assert updated.sourcing_policy == SourcingPolicy.DUAL
        assert carbon_network.config.sourcing_policy != SourcingPolicy.DUAL, (
            "the original config must not be mutated")

    def test_an_unknown_priority_changes_nothing(self, network):
        assert priorities.config_overrides_for("service", network.config) == {}
        assert priorities.config_overrides_for("", network.config) == {}


class TestTheOptionSetIsData:
    """
    Adding COST_SERVICE later must be one entry, not a redesign — so nothing
    outside the registry may enumerate the levers.
    """

    def test_the_question_is_built_from_whatever_levers_exist(self):
        one = [priorities.by_id("cost")]
        question = priorities.clarification_question(one)
        assert "lowest total cost" in question.lower()
        assert "resilient" not in question.lower()

        two = [priorities.by_id("cost"), priorities.by_id("resilience")]
        assert "or most resilient" in priorities.clarification_question(two).lower()

    def test_options_are_built_from_whatever_levers_exist(self):
        options = priorities.clarification_options([priorities.by_id("carbon")])
        assert [o["id"] for o in options] == ["carbon"]
        assert options[0]["label"] and options[0]["description"]

    def test_every_lever_states_where_it_is_real_in_the_solver(self):
        for lever in priorities.LEVERS:
            assert lever.evidence, f"{lever.id} claims no basis in the solver"
            assert lever.phrases, f"{lever.id} can never be recognised as stated"


# ---------------------------------------------------------------------------
# Answering the question
#
# The failure this closes: picking "Lowest cost" sent those two words as a
# fresh message. The NLU read them as a new (different) request, so the
# optimisation the user had actually asked for never ran — and "Most
# resilient" was worse, because "resilien" reads as a resilience query and
# the assistant confidently answered a question nobody asked.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402

from netgravity.orchestrator import build_orchestrator  # noqa: E402
from netgravity.orchestrator.conversation.chat_service import ChatService  # noqa: E402
from netgravity.orchestrator.conversation.nlu import (  # noqa: E402
    apply_clarification_answer,
)
from netgravity.orchestrator.schemas.conversation import ChatRequest  # noqa: E402


class _OptimiseStub(LLMGateway):
    """A model that always reads a message as an optimisation request."""

    @property
    def available(self) -> bool:            # type: ignore[override]
        return True

    def unavailable_reason(self) -> str:    # type: ignore[override]
        return ""

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:  # type: ignore[override]
        if purpose != "intent":
            return LLMResponse(output=_json.dumps({
                "summary": "Deterministic values only.", "key_drivers": [],
                "risks": [], "recommendation": "Review.", "confidence": "LOW",
                "evidence": [], "claims": [],
            }))
        return LLMResponse(output=_json.dumps({
            "intent": "OPTIMIZATION_REQUEST", "confidence": 0.95,
            "facility_ids": [], "scenarios": [], "rationale": "stub",
        }))


@pytest.fixture
def chat(carbon_network):
    """A network where a real choice exists, so the question is asked."""
    return ChatService(
        orchestrator=build_orchestrator(network=carbon_network),
        nlu=ConversationalNLU(intent_agent=IntentAgent(_OptimiseStub())),
    )


class TestAnsweringResumesTheOriginalRequest:

    def test_the_question_is_asked_with_pickable_options(self, chat):
        first = chat.chat(ChatRequest(message="Optimise my network."))

        assert first.status == "AWAITING_CLARIFICATION"
        assert first.clarification is not None
        # Every option carries an id, which is what a button sends back.
        assert all(o.get("id") for o in first.clarification.options)

    @pytest.mark.parametrize("option_id", ["cost", "carbon"])
    def test_picking_an_option_runs_the_optimisation_that_was_asked_for(
            self, chat, option_id):
        first = chat.chat(ChatRequest(message="Optimise my network."))
        second = chat.chat(ChatRequest(
            message="whatever the button said",
            clarification_option=option_id,
            conversation_id=first.conversation_id,
        ))

        assert second.status == "COMPLETED", second.reply[:200]
        assert second.intent == Intent.OPTIMIZATION_REQUEST.value, (
            "answering the question ran something other than the request it "
            "was asked about")

    def test_the_words_on_the_button_do_not_become_the_request(self, chat):
        """
        The specific regression: a label sent as a fresh message is read as a
        new question. "Lowest carbon" carries 'carbon', which the NLU reads as
        a stated priority on a request of its own rather than as the answer to
        the one already asked.
        """
        first = chat.chat(ChatRequest(message="Optimise my network."))
        second = chat.chat(ChatRequest(
            message="Lowest carbon",
            clarification_option="carbon",
            conversation_id=first.conversation_id,
        ))
        assert second.intent == Intent.OPTIMIZATION_REQUEST.value
        # And it ran the request that was asked, not the button's words.
        assert chat.store.get(first.conversation_id).last_turn is not None

    def test_the_clarification_turn_remembers_what_it_asked_about(self, chat):
        first = chat.chat(ChatRequest(message="Optimise my network."))
        conversation = chat.store.get(first.conversation_id)

        assert conversation.pending_clarification
        assert conversation.clarification_subject == "Optimise my network.", (
            "the turn recorded the question it asked, not the request it "
            "asked about — so there is nothing to resume")

    def test_an_option_nobody_offered_asks_again_rather_than_guessing(self, chat):
        first = chat.chat(ChatRequest(message="Optimise my network."))
        second = chat.chat(ChatRequest(
            message="service", clarification_option="service",
            conversation_id=first.conversation_id,
        ))
        assert second.status == "AWAITING_CLARIFICATION", (
            "an unoffered option must not run a request under a priority "
            "nothing understood")

    def test_a_picked_option_reaches_the_solver_config(self, chat, carbon_network):
        """
        The whole chain, in one place: pick → intent → request metadata →
        ExecutionContext → OptimizationConfig.
        """
        ambiguous = _nlu().understand("Optimise my network.", carbon_network,
                                      allow_llm=True)
        resumed = apply_clarification_answer(ambiguous, "carbon")

        request = chat._to_orchestrator_request(
            resumed, ChatRequest(message="Lowest carbon",
                                 clarification_option="carbon"),
            "snap_1", message="Optimise my network.")
        assert request.metadata["optimisation_priority"] == "carbon"
        # The request carries what was ASKED, not the words on the button.
        assert request.input == "Optimise my network."

        ctx = ExecutionContext.from_request(request, "snap_1")
        assert ctx.optimisation_priority == "carbon"

        overrides = priorities.config_overrides_for(
            ctx.optimisation_priority, carbon_network.config)
        assert overrides["objective_mode"] == ObjectiveMode.WEIGHTED_COST_CARBON

    def test_applying_an_answer_produces_a_runnable_intent(self, carbon_network):
        ambiguous = _nlu().understand("Optimise my network.", carbon_network,
                                      allow_llm=True)
        assert ambiguous.needs_clarification

        resumed = apply_clarification_answer(ambiguous, "carbon")
        assert resumed.optimisation_priority == "carbon"
        assert resumed.clarity == IntentClarity.CLEAR
        assert resumed.ambiguity == AmbiguityKind.NONE
        assert resumed.clarification is None

    def test_applying_an_unknown_answer_changes_nothing(self, carbon_network):
        ambiguous = _nlu().understand("Optimise my network.", carbon_network,
                                      allow_llm=True)
        unchanged = apply_clarification_answer(ambiguous, "service")

        assert unchanged.needs_clarification
        assert unchanged.optimisation_priority is None

    def test_the_resumer_registry_covers_the_kind_it_claims_to(self):
        from netgravity.orchestrator.conversation.nlu import (
            _CLARIFICATION_RESUMERS,
        )

        assert AmbiguityKind.UNSTATED_PRIORITY in _CLARIFICATION_RESUMERS
        # A kind with no resumer must fall through to asking again, never to
        # running under an answer nothing understood.
        assert AmbiguityKind.UNKNOWN_ENTITY not in _CLARIFICATION_RESUMERS
