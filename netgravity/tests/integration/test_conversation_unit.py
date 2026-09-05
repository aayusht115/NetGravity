"""
Phase 3 §20 — unit tests for the conversational layer.

Covers the intent schema, its validators, entity resolution, ambiguity
detection, extraction and conversation context, in isolation from the
orchestrator. The end-to-end chain is covered by
`test_conversational_workflows.py`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.llm_gateway import LLMClient, LLMGateway
from netgravity.orchestrator.conversation.entity_resolver import EntityResolver
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.conversation.store import MAX_TURNS, ConversationStore
from netgravity.orchestrator.schemas.conversation import (
    INTENT_SCHEMA_VERSION,
    AmbiguityKind,
    ChatTurn,
    ClarificationRequest,
    ConversationalIntent,
    EntityMention,
    ExternalEventSpec,
    IntentClarity,
)
from netgravity.orchestrator.schemas.requests import (
    EventSeverity,
    Intent,
    ScenarioActionType,
)
from netgravity.schemas.network import (
    CanonicalNetwork,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    ProductRecord,
    TransportMode,
)

from .conftest import build_delhi_network

TOL = 1e-9


def build_two_delhi_network() -> CanonicalNetwork:
    """A network with TWO Delhi DCs, so 'Delhi' is genuinely ambiguous."""
    facilities = [
        FacilityRecord(id="PLANT_N", name="North Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=99_999,
                       is_mandatory=True, is_closable=False),
        FacilityRecord(id="DC_DELHI_NORTH", name="Delhi North DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=5_000),
        FacilityRecord(id="DC_DELHI_EAST", name="Delhi East DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=5_000),
        FacilityRecord(id="MKT_N", name="North Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
    ]
    lanes = [
        LaneRecord(origin_id="PLANT_N", destination_id="DC_DELHI_NORTH",
                   mode=TransportMode.ROAD, rate_per_unit=1.0, distance_km=100.0),
        LaneRecord(origin_id="PLANT_N", destination_id="DC_DELHI_EAST",
                   mode=TransportMode.ROAD, rate_per_unit=1.0, distance_km=100.0),
        LaneRecord(origin_id="DC_DELHI_NORTH", destination_id="MKT_N",
                   mode=TransportMode.ROAD, rate_per_unit=2.0, distance_km=50.0),
        LaneRecord(origin_id="DC_DELHI_EAST", destination_id="MKT_N",
                   mode=TransportMode.ROAD, rate_per_unit=3.0, distance_km=50.0),
    ]
    net = CanonicalNetwork(
        network_id="TWO_DELHI", facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=[DemandRecord(market_id="MKT_N", product_id="P1", quantity=100.0)],
        lanes=lanes,
    )
    return net.model_copy(update={"data_version": net.compute_data_version()})


@pytest.fixture
def nlu():
    return ConversationalNLU(intent_agent=IntentAgent(None))


# ===========================================================================
# Intent schema and its validators
# ===========================================================================

class TestIntentSchema:

    def test_a_minimal_intent_validates(self):
        intent = ConversationalIntent(intent=Intent.STATUS_QUERY, confidence=0.9)
        assert intent.schema_version == INTENT_SCHEMA_VERSION
        assert intent.is_actionable
        assert not intent.needs_clarification

    def test_confidence_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError, match=r"\[0, 1\]"):
            ConversationalIntent(intent=Intent.STATUS_QUERY, confidence=1.4)

    def test_extra_fields_are_forbidden(self):
        """A model cannot smuggle a field the schema does not define."""
        with pytest.raises(ValidationError):
            ConversationalIntent(intent=Intent.STATUS_QUERY, milp_cost=1234.0)

    @pytest.mark.parametrize("key", [
        "cost", "total_cost", "business_network_cost", "rei", "rf",
        "risk_factor", "sla", "utilization", "savings", "objective_value",
        "governance", "action_tier",
    ])
    def test_deterministic_values_are_rejected_from_parameters(self, key):
        """
        The hard boundary in code. `parameters` exists for quantities the USER
        supplied; it must not become a route for a model-asserted result.
        """
        with pytest.raises(ValidationError, match="deterministic result values"):
            ConversationalIntent(
                intent=Intent.RESILIENCE_QUERY, parameters={key: 0.0},
            )

    def test_the_rejection_is_case_and_spacing_insensitive(self):
        for key in ("REI", "Risk_Factor", "total cost", "  RF  "):
            with pytest.raises(ValidationError, match="deterministic result values"):
                ConversationalIntent(intent=Intent.RESILIENCE_QUERY,
                                     parameters={key: 1.0})

    def test_legitimate_user_parameters_are_allowed(self):
        """A capacity the user typed is not a result. It must pass."""
        intent = ConversationalIntent(
            intent=Intent.SCENARIO_ANALYSIS,
            parameters={"capacity_delta_units": -2000.0, "horizon_months": 6},
        )
        assert intent.parameters["capacity_delta_units"] == -2000.0

    def test_ambiguous_intent_must_carry_a_question(self):
        with pytest.raises(ValidationError, match="requires a clarification"):
            ConversationalIntent(
                intent=Intent.UNKNOWN, clarity=IntentClarity.AMBIGUOUS,
                ambiguity=AmbiguityKind.AMBIGUOUS_ENTITY,
            )

    def test_clear_intent_must_not_claim_ambiguity(self):
        with pytest.raises(ValidationError, match="contradicts ambiguity"):
            ConversationalIntent(
                intent=Intent.STATUS_QUERY, clarity=IntentClarity.CLEAR,
                ambiguity=AmbiguityKind.AMBIGUOUS_ENTITY,
            )

    def test_event_probability_out_of_range_is_rejected(self):
        with pytest.raises(ValidationError, match=r"\[0, 1\]"):
            ExternalEventSpec(event_type="FLOOD", event_probability=1.7)

    def test_severity_and_probability_are_independent_fields(self):
        spec = ExternalEventSpec(
            event_type="FLOOD", severity=EventSeverity.CRITICAL,
            event_probability=None,
        )
        assert spec.severity == EventSeverity.CRITICAL
        assert spec.event_probability is None, "severity must never become P"

    def test_an_unclear_intent_is_not_actionable(self):
        intent = ConversationalIntent(
            intent=Intent.SCENARIO_ANALYSIS, clarity=IntentClarity.AMBIGUOUS,
            ambiguity=AmbiguityKind.AMBIGUOUS_INTENT,
            clarification=ClarificationRequest(
                kind=AmbiguityKind.AMBIGUOUS_INTENT, question="Which?"),
        )
        assert not intent.is_actionable
        assert intent.needs_clarification


# ===========================================================================
# Entity resolution
# ===========================================================================

class TestEntityResolver:

    def test_exact_id_resolves(self, delhi_network):
        mention = EntityResolver(delhi_network).resolve_phrase("DC_DELHI")
        assert mention.resolved_ids == ["DC_DELHI"]
        assert mention.method == "exact_id"
        assert mention.is_resolved

    def test_display_name_resolves(self, delhi_network):
        mention = EntityResolver(delhi_network).resolve_phrase("Delhi NCR DC")
        assert mention.resolved_ids == ["DC_DELHI"]
        assert mention.method == "name"

    @pytest.mark.parametrize("phrase", [
        "Delhi warehouse", "the Delhi DC", "DC in Delhi",
        "Delhi distribution centre",
    ])
    def test_natural_phrasings_resolve_to_the_same_node(self, delhi_network, phrase):
        mention = EntityResolver(delhi_network).resolve_phrase(phrase)
        assert mention.resolved_ids == ["DC_DELHI"], phrase

    def test_an_unknown_place_resolves_to_nothing(self, delhi_network):
        mention = EntityResolver(delhi_network).resolve_phrase("Bangalore DC")
        assert mention.resolved_ids == []
        assert mention.is_unknown

    def test_a_resolver_can_never_invent_an_id(self, delhi_network):
        """
        Every id it returns came out of the network. Asserted exhaustively over
        a range of inputs including deliberately misleading ones.
        """
        resolver = EntityResolver(delhi_network)
        real_ids = {f.id for f in delhi_network.facilities}
        for phrase in ("DC_ATLANTIS", "Delhi", "warehouse", "DC_DELHI_2",
                       "the new Bangalore hub", "", "   ", "12345"):
            assert set(resolver.resolve_phrase(phrase).resolved_ids) <= real_ids

    def test_two_matches_are_reported_as_ambiguous_not_resolved(self):
        resolver = EntityResolver(build_two_delhi_network())
        mention = resolver.resolve_phrase("Delhi")

        assert mention.is_ambiguous
        assert not mention.is_resolved
        assert set(mention.resolved_ids) == {"DC_DELHI_EAST", "DC_DELHI_NORTH"}

    def test_a_type_word_alone_does_not_match_everything(self, delhi_network):
        """'warehouse' describes a kind of node; it identifies none."""
        mention = EntityResolver(delhi_network).resolve_phrase("warehouse")
        assert mention.resolved_ids == []

    def test_a_role_hint_narrows_but_never_widens(self, delhi_network):
        resolver = EntityResolver(delhi_network)
        assert resolver.resolve_phrase("North plant").resolved_ids == ["PLANT_N"]
        # The hint cannot introduce a node the tokens did not find.
        assert resolver.resolve_phrase("Atlantis plant").resolved_ids == []

    def test_sentence_initial_capitals_are_not_treated_as_places(self, delhi_network):
        resolver = EntityResolver(delhi_network)
        assert resolver.find_unknown_candidates("How many warehouses do we have?") == []
        assert resolver.find_unknown_candidates("What is the current cost?") == []

    def test_a_genuine_unknown_place_is_detected(self, delhi_network):
        candidates = EntityResolver(delhi_network).find_unknown_candidates(
            "What if we close the Bangalore DC?"
        )
        assert "Bangalore" in candidates

    def test_extract_mentions_finds_real_nodes_only(self, delhi_network):
        mentions = EntityResolver(delhi_network).extract_mentions(
            "Compare DC_DELHI with DC_ATLANTIS."
        )
        resolved = {m.resolved_ids[0] for m in mentions if m.is_resolved}
        assert resolved == {"DC_DELHI"}

    def test_facility_catalogue_reads_from_the_snapshot(self, delhi_network):
        resolver = EntityResolver(delhi_network)
        assert resolver.facilities_of_role(NodeRole.DC) == [
            "DC_DELHI", "DC_KOLKATA", "DC_MUMBAI"
        ]
        assert resolver.facilities_of_role(NodeRole.PLANT) == ["PLANT_N"]


# ===========================================================================
# NLU — classification, ambiguity, extraction
# ===========================================================================

class TestNLUClassification:

    @pytest.mark.parametrize("message,expected", [
        ("How many warehouses do we have?", Intent.STATUS_QUERY),
        ("List the facilities.", Intent.STATUS_QUERY),
        ("What is the current transportation cost?", Intent.NETWORK_STATE_QUERY),
        ("Why is DC_DELHI considered high risk?", Intent.EXPLANATION),
        ("Why did transportation cost increase?", Intent.EXPLANATION),
        ("Forecast demand for the next six months.", Intent.FORECAST),
        ("What will capacity utilization look like next quarter?", Intent.FORECAST),
        ("What is DC_DELHI risk exposure?", Intent.RESILIENCE_QUERY),
        ("There is a 70% probability of flooding around DC_DELHI.",
         Intent.EXTERNAL_EVENT),
    ])
    def test_intents_classify_deterministically(self, nlu, delhi_network,
                                                message, expected):
        """All of these resolve offline, with no model involved."""
        intent = nlu.understand(message, delhi_network, allow_llm=False)
        assert intent.intent == expected, message
        assert intent.source == "rules"

    def test_a_cost_question_is_not_answered_from_the_snapshot(self, nlu,
                                                               delhi_network):
        """
        Cost is a property of the optimum, not of the snapshot. Routing it to
        STATUS_QUERY would answer a different question than the one asked.
        """
        intent = nlu.understand("What is the current transportation cost?",
                                delhi_network, allow_llm=False)
        assert intent.intent != Intent.STATUS_QUERY
        assert intent.intent == Intent.NETWORK_STATE_QUERY

    def test_the_nlu_never_names_a_workflow(self, nlu, delhi_network):
        """
        §4 — the NLP layer does not select workflows. Its output carries an
        Intent enum value and nothing that could name a graph or a capability.
        """
        intent = nlu.understand("What if DC_DELHI capacity drops by 2,000 units?",
                                delhi_network, allow_llm=False)
        serialised = intent.model_dump_json()
        for forbidden in ("wf_", "capability", "optimization.solve",
                          "resilience.assess", "risk.compute_rf"):
            assert forbidden not in serialised


class TestAmbiguityDetection:

    def test_close_delhi_is_ambiguous(self, nlu, delhi_network):
        """The canonical §6 case: the node is clear, the operation is not."""
        intent = nlu.understand("Close Delhi.", delhi_network, allow_llm=False)

        assert intent.clarity == IntentClarity.AMBIGUOUS
        assert intent.ambiguity == AmbiguityKind.AMBIGUOUS_INTENT
        assert intent.clarification is not None
        assert "DC_DELHI" in intent.clarification.question
        assert {o["id"] for o in intent.clarification.options} == {
            "CLOSE_FACILITY", "SHIFT_VOLUME", "CHANGE_CAPACITY"
        }
        assert not intent.is_actionable

    def test_an_explicit_closure_is_not_ambiguous(self, nlu, delhi_network):
        intent = nlu.understand("Simulate closure of the DC_DELHI facility.",
                                delhi_network, allow_llm=False)
        assert intent.clarity == IntentClarity.CLEAR
        assert intent.scenario_overrides
        assert intent.scenario_overrides[0].action == ScenarioActionType.CLOSE_FACILITY

    def test_a_capacity_change_with_no_quantity_asks_for_one(self, nlu,
                                                             delhi_network):
        intent = nlu.understand("Reduce DC_DELHI capacity.", delhi_network,
                                allow_llm=False)
        assert intent.clarity == IntentClarity.INSUFFICIENT_INFORMATION
        assert intent.ambiguity == AmbiguityKind.MISSING_PARAMETER
        assert intent.clarification.missing_parameter == "capacity"
        assert "2,000 units" in intent.clarification.question

    def test_a_capacity_change_with_a_quantity_is_clear(self, nlu, delhi_network):
        intent = nlu.understand(
            "Reduce DC_DELHI capacity by 2,000 units/day.", delhi_network,
            allow_llm=False,
        )
        assert intent.clarity == IntentClarity.CLEAR
        [spec] = intent.scenario_overrides
        assert spec.action == ScenarioActionType.CHANGE_CAPACITY
        assert spec.capacity_delta_units == pytest.approx(-2000.0)

    def test_two_matching_facilities_produce_an_entity_question(self, nlu):
        intent = nlu.understand("What if we close Delhi?", build_two_delhi_network(),
                                allow_llm=False)
        assert intent.ambiguity == AmbiguityKind.AMBIGUOUS_ENTITY
        assert "DC_DELHI_EAST" in intent.clarification.question
        assert "DC_DELHI_NORTH" in intent.clarification.question
        assert len(intent.clarification.options) == 2

    def test_an_unknown_facility_is_never_fabricated(self, nlu, delhi_network):
        intent = nlu.understand("What if we close Bangalore DC?", delhi_network,
                                allow_llm=False)
        assert intent.ambiguity == AmbiguityKind.UNKNOWN_ENTITY
        assert intent.resolved_entity_ids == []
        assert intent.scenario_overrides == []
        assert "Bangalore" in intent.clarification.question

    def test_an_empty_message_is_unsupported_not_a_crash(self, nlu, delhi_network):
        intent = nlu.understand("", delhi_network, allow_llm=False)
        assert intent.clarity == IntentClarity.UNSUPPORTED
        assert not intent.is_actionable


class TestExtraction:

    def test_a_stated_probability_is_extracted(self, nlu, delhi_network):
        intent = nlu.understand(
            "There is a 70% probability of flooding around DC_DELHI.",
            delhi_network, allow_llm=False,
        )
        assert intent.external_event is not None
        assert intent.external_event.event_probability == pytest.approx(0.70)
        assert intent.external_event.event_type == "FLOOD"

    def test_severity_without_probability_yields_no_probability(self, nlu,
                                                                delhi_network):
        """§22-adjacent: 'severe' must not become 0.7."""
        intent = nlu.understand(
            "There is severe flooding expected around DC_DELHI.",
            delhi_network, allow_llm=False,
        )
        assert intent.external_event is not None
        assert intent.external_event.event_probability is None
        assert intent.external_event.severity == EventSeverity.SEVERE

    def test_entities_come_from_the_network(self, nlu, delhi_network):
        intent = nlu.understand("What is DC_DELHI risk exposure?", delhi_network,
                                allow_llm=False)
        assert intent.resolved_entity_ids == ["DC_DELHI"]
        assert all(m.method != "none" for m in intent.mentions if m.resolved_ids)

    def test_prompt_and_schema_versions_are_recorded(self, nlu, delhi_network):
        intent = nlu.understand("How many warehouses?", delhi_network,
                                allow_llm=False)
        assert intent.schema_version == INTENT_SCHEMA_VERSION
        assert intent.prompt_version


# ===========================================================================
# Conversation context
# ===========================================================================

class TestConversationContext:

    def test_an_elliptical_follow_up_inherits_the_subject(self, nlu, delhi_network):
        intent = nlu.understand(
            "Why?", delhi_network, allow_llm=False,
            prior_entity_ids=["DC_DELHI"], prior_intent=Intent.RESILIENCE_QUERY,
        )
        assert intent.resolved_entity_ids == ["DC_DELHI"]

    def test_a_fresh_request_does_not_accumulate_the_previous_subject(
        self, nlu, delhi_network,
    ):
        """
        The failure this prevents: turn 3 silently analysing "Delhi AND Mumbai"
        because turn 1 mentioned Delhi.
        """
        intent = nlu.understand(
            "What happens if DC_MUMBAI capacity is reduced by 1,000 units/day?",
            delhi_network, allow_llm=False,
            prior_entity_ids=["DC_DELHI"], prior_intent=Intent.SCENARIO_ANALYSIS,
        )
        assert intent.resolved_entity_ids == ["DC_MUMBAI"]
        assert "DC_DELHI" not in intent.resolved_entity_ids
        [spec] = intent.scenario_overrides
        assert spec.facility_ids == ["DC_MUMBAI"]

    def test_the_store_keeps_turn_order_and_bounds_growth(self):
        store = ConversationStore()
        conversation = store.start("conv_test")

        for i in range(MAX_TURNS + 10):
            store.append("conv_test", ChatTurn(user_input=f"m{i}", reply="r"))

        turns = store.history("conv_test")
        assert len(turns) == MAX_TURNS
        assert turns[-1].user_input == f"m{MAX_TURNS + 9}"

    def test_the_store_reports_the_last_substantive_subject(self):
        store = ConversationStore()
        store.append("c", ChatTurn(user_input="a", intent=Intent.RESILIENCE_QUERY,
                                   resolved_entity_ids=["DC_DELHI"]))
        store.append("c", ChatTurn(user_input="why", intent=Intent.EXPLANATION))

        conversation = store.get("c")
        assert conversation.last_entity_ids == ["DC_DELHI"]
        assert conversation.last_intent == Intent.EXPLANATION

    def test_conversations_are_isolated_from_each_other(self):
        store = ConversationStore()
        store.append("a", ChatTurn(user_input="x", resolved_entity_ids=["DC_DELHI"]))
        store.append("b", ChatTurn(user_input="y", resolved_entity_ids=["DC_MUMBAI"]))

        assert store.get("a").last_entity_ids == ["DC_DELHI"]
        assert store.get("b").last_entity_ids == ["DC_MUMBAI"]

    def test_no_scenario_overrides_are_carried_in_conversation_state(self):
        """
        §13 — a conversation must not accumulate overrides. `ChatTurn` has no
        field capable of carrying one, which is how that is guaranteed.
        """
        assert "scenario_overrides" not in ChatTurn.model_fields
        assert "overrides" not in ChatTurn.model_fields


# ===========================================================================
# LLM client abstraction
# ===========================================================================

class TestLLMClientAbstraction:

    def test_the_existing_gateway_satisfies_the_protocol(self):
        """§10 — reuse the existing abstraction rather than replacing it."""
        assert isinstance(LLMGateway(), LLMClient)

    def test_an_alternative_provider_satisfies_it_too(self):
        """Nothing is bound to one vendor: three members is the whole contract."""

        class OtherProvider:
            @property
            def available(self) -> bool:
                return True

            def generate(self, prompt, *, purpose="generic"):
                from netgravity.orchestrator.agents.llm_gateway import LLMResponse
                return LLMResponse(output="{}", model_name="other/v1")

            def stats(self):
                return {"available": True}

        assert isinstance(OtherProvider(), LLMClient)

    def test_the_protocol_offers_no_tool_invocation(self):
        """
        §23 — a provider reached through this interface has no mechanism by
        which to call MILP, REI, RF or governance. Asserted on the members.
        """
        members = {m for m in dir(LLMClient) if not m.startswith("_")}
        assert members == {"available", "generate", "stats"}

    def test_gateway_stats_carry_no_credential_material(self):
        stats = LLMGateway().stats()
        assert "token" not in {k.lower() for k in stats} - {"token_configured"}
        assert all("bearer" not in str(v).lower() for v in stats.values())


# ===========================================================================
# Out-of-domain questions must not be answered as follow-ups
# ===========================================================================

class TestAnOffTopicQuestionIsNotTreatedAsAFollowUp:
    """
    The worst failure this layer can produce is a confident, correct figure
    about something nobody asked about.

    A message the classifier cannot place is UNKNOWN, and mid-conversation an
    UNKNOWN that "looks elliptical" is promoted to EXPLANATION and answered
    from the previous turn's subject. The test for "looks elliptical" was a
    whitelist of imperative verbs — it caught "tell me a joke" and could not,
    even in principle, catch "who won the cricket world cup": six words, no
    listed verb, therefore a bare fragment. Asked in a conversation about the
    network, it was answered with the network's REI, unserved demand and
    baseline cost.

    A question that opens with an interrogative carries its own subject and
    verb, so it depends on nothing and is not a follow-up.
    """

    OFF_TOPIC = [
        "Who won the cricket world cup?",
        "What is the capital of France?",
        "When did India gain independence?",
        "How do I bake bread?",
        "Tell me a joke",
        "Which airline flies to Tokyo?",
    ]

    FOLLOW_UPS = [
        "and the cost?",
        "what about Mumbai?",
        "how much did that cost?",
        "the cost impact?",
        "what if we close it?",
        "that one",
    ]

    @pytest.mark.parametrize("text", OFF_TOPIC)
    def test_a_complete_off_topic_question_is_not_elliptical(self, text):
        assert ConversationalNLU._is_elliptical(text) is False, (
            f"{text!r} stands on its own; treating it as a follow-up answers "
            f"the previous question instead of declining this one"
        )

    @pytest.mark.parametrize("text", FOLLOW_UPS)
    def test_a_genuine_follow_up_still_is(self, text):
        assert ConversationalNLU._is_elliptical(text) is True, (
            f"{text!r} depends on the previous turn and must keep inheriting it"
        )

    @pytest.mark.parametrize("text", OFF_TOPIC)
    def test_it_stays_unknown_mid_conversation(self, nlu, delhi_network, text):
        """
        With a prior intent on the conversation — the condition that made the
        promotion reachable at all — the intent must still come back UNKNOWN so
        the assistant says it did not understand.
        """
        intent = nlu.understand(
            text, delhi_network, allow_llm=False,
            prior_entity_ids=["DC_DELHI"], prior_intent=Intent.RESILIENCE_QUERY,
        )
        assert intent.intent == Intent.UNKNOWN, (
            f"{text!r} was classified {intent.intent.value}: {intent.rationale}"
        )

    def test_a_bare_why_is_still_an_explanation(self, nlu, delhi_network):
        """The interrogative rule must not swallow the genuine ellipsis."""
        intent = nlu.understand(
            "Why?", delhi_network, allow_llm=False,
            prior_entity_ids=["DC_DELHI"], prior_intent=Intent.RESILIENCE_QUERY,
        )
        assert intent.intent == Intent.EXPLANATION


# ===========================================================================
# A scenario with no magnitude is a question, not a failure
# ===========================================================================

class TestAnIncompleteScenarioIsAskedAboutNotAttempted:
    """
    "A major customer is expanding in Delhi" resolves to CHANGE_DEMAND with
    nothing to multiply by.

    A spec existed, so the MISSING_PARAMETER check — which tested only for the
    ABSENCE of a spec — passed it through. Three steps later `ScenarioBuilder`
    refused it correctly ("CHANGE_DEMAND requires a demand_multiplier"), the
    step failed NON_RETRYABLE, and the execution settled FAILED with "The
    analysis produced no narrative result". The user needed one question — by
    how much? — and got a dead run instead.
    """

    def test_a_spec_with_no_magnitude_is_not_runnable(self):
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )
        bare = ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                  facility_ids=["DC_DELHI"])
        assert bare.is_runnable is False
        assert bare.missing_parameter == "demand_multiplier"

        stated = ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                    demand_multiplier=1.2)
        assert stated.is_runnable is True
        assert stated.missing_parameter is None

    def test_a_closure_needs_no_magnitude(self):
        """The instruction is complete in the action and the facility."""
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )
        spec = ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                                  facility_ids=["DC_DELHI"])
        assert spec.is_runnable is True

    def test_every_capacity_form_counts_as_stated(self):
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )
        A = ScenarioActionType.CHANGE_CAPACITY
        assert ScenarioIntentSpec(action=A).is_runnable is False
        for field, value in (("capacity_multiplier", 0.8),
                             ("capacity_delta_units", -2000.0),
                             ("capacity_set_units", 8000.0)):
            assert ScenarioIntentSpec(action=A, **{field: value}).is_runnable, field

    def test_the_nlu_asks_rather_than_planning_an_unbuildable_scenario(
        self, nlu, delhi_network,
    ):
        intent = nlu.understand(
            "Reduce DC_DELHI capacity.", delhi_network, allow_llm=False,
        )
        assert intent.ambiguity == AmbiguityKind.MISSING_PARAMETER
        assert intent.needs_clarification

    def test_a_complete_scenario_still_runs(self, nlu, delhi_network):
        """The gate must not start refusing scenarios that are perfectly clear."""
        intent = nlu.understand(
            "What happens if we reduce DC_DELHI capacity by 2,000 units/day?",
            delhi_network, allow_llm=False,
        )
        assert intent.ambiguity != AmbiguityKind.MISSING_PARAMETER
        assert intent.scenario_overrides
        assert all(s.is_runnable for s in intent.scenario_overrides)
