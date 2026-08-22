"""
Phase 3 §21–§24 — the conversational chain end to end.

    USER → CHATBOT → NLU → structured intent → ORCHESTRATOR
         → workflow decision → deterministic engines → reasoning
         → grounding → governance → reply

The MILP, REI, RF, grounding and governance are all REAL here. Only the LLM is
ever faked, and only where a test needs a specific model failure or a specific
hallucination — a real model cannot be relied upon to misbehave on cue.

Numbers come from the hand-calculable `PHASE2_DELHI` fixture:

    C0 = 1,200 ; REI(DELHI) = 0.80 ; with P = 0.70 ⇒ RF = 0.94
"""

from __future__ import annotations

from typing import Any, List

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.conversation import ChatService
from netgravity.orchestrator.engines.deterministic import REIClient
from netgravity.orchestrator.schemas.conversation import (
    AmbiguityKind,
    ChatRequest,
    IntentClarity,
)
from netgravity.orchestrator.schemas.requests import Intent
from netgravity.resilience.service import REIService

from .conftest import build_delhi_network
from .test_failure_propagation import StaleREIClient, TimingOutREIClient

TOL = 1e-9


class SolveCounter:
    """Counts REAL MILP invocations, so 'no unnecessary solve' is measured."""

    def __init__(self) -> None:
        self.scenario_ids: List[str] = []

    def __call__(self, network: Any, config: Any = None, scenario_id: Any = None):
        from netgravity.optimization.milp import solve
        self.scenario_ids.append(scenario_id or "(none)")
        return solve(network, config=config, scenario_id=scenario_id)

    @property
    def count(self) -> int:
        return len(self.scenario_ids)

    def reset(self) -> None:
        self.scenario_ids.clear()


@pytest.fixture
def chat(orch):
    return ChatService(orch)


def _counting_chat(network=None):
    """A chat service whose REI solves are counted at the solver."""
    counter = SolveCounter()
    orch = build_orchestrator(network=network or build_delhi_network(),
                              enable_llm=False)
    orch.services["rei"] = REIClient(service=REIService(solve_fn=counter))
    return ChatService(orch), orch, counter


def _say(chat, message: str, **kwargs):
    return chat.chat(ChatRequest(message=message, disable_llm=True, **kwargs))


# ===========================================================================
# §21 Test 1 — information query, no unnecessary MILP
# ===========================================================================

class TestInformationQuery:

    def test_a_count_question_is_answered_from_the_digital_twin(self):
        chat, orch, counter = _counting_chat()
        response = _say(chat, "How many warehouses do we currently have?")

        assert response.intent == Intent.STATUS_QUERY.value
        assert response.provenance == "OBSERVED"
        assert response.workflow_id == "wf_status"
        assert counter.count == 0, "a facility count must not run the solver"
        assert response.results["facilities"]["DC"] == [
            "DC_DELHI", "DC_KOLKATA", "DC_MUMBAI"
        ]
        assert "3 distribution centres" in response.reply

    def test_a_cost_question_does_solve_because_cost_requires_an_optimum(self, chat):
        """
        The counterpart. "No unnecessary MILP" is not "no MILP" — cost is
        defined by the optimum and cannot be read off the snapshot.
        """
        response = _say(chat, "What is the current transportation cost?")

        assert response.intent == Intent.NETWORK_STATE_QUERY.value
        assert response.provenance == "OBSERVED"
        assert response.results["network"]["business_network_cost"] == pytest.approx(
            1200.0, abs=1e-6
        )
        assert "1,200.00" in response.reply

    def test_the_status_answer_states_that_nothing_was_optimised(self):
        chat, _, _ = _counting_chat()
        response = _say(chat, "How many warehouses do we have?")
        assert "no optimization was run" in response.reply.lower()


# ===========================================================================
# §21 Test 2 — what-if scenario
# ===========================================================================

class TestScenarioQuery:

    def test_the_full_chain_runs_and_the_milp_produces_the_answer(self, chat):
        response = _say(
            chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?",
        )

        assert response.intent == Intent.SCENARIO_ANALYSIS.value
        assert response.clarity == IntentClarity.CLEAR.value
        assert response.provenance == "SCENARIO"
        assert response.scenario_id is not None
        # Real MILP: 1,200 → 1,400, +16.67%.
        assert response.results["network"]["business_network_cost"] == pytest.approx(
            1400.0, abs=1e-6
        )
        assert response.results["network"]["business_cost_delta_pct"] == pytest.approx(
            16.6667, abs=1e-3
        )

    def test_the_reply_marks_the_result_as_hypothetical(self, chat):
        response = _say(
            chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?",
        )
        assert "SCENARIO result" in response.reply
        assert "observed network is unchanged" in response.reply
        assert response.results["network"]["result_kind"] == "SCENARIO_RESULT"

    def test_the_baseline_is_byte_for_byte_unchanged(self, orch):
        """§8 — a conversational what-if must never contaminate observed state."""
        chat = ChatService(orch)
        before = orch.snapshots.current().network.model_dump_json()
        before_ids = orch.snapshots.list_ids()

        _say(chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?")

        assert orch.snapshots.current().network.model_dump_json() == before
        assert orch.snapshots.list_ids() == before_ids

    def test_the_scenario_override_came_from_the_user_not_the_model(self, chat):
        response = _say(chat, "Reduce DC_DELHI capacity by 2,000 units/day.")
        assert response.results["network"]["scenario_overrides"] == [
            "CHANGE_CAPACITY DC_DELHI -2,000 units/period"
        ]

    def test_the_narrative_is_grounded(self, chat):
        response = _say(
            chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?",
        )
        assert response.grounding_status in ("GROUNDED", "NO_CLAIMS")


# ===========================================================================
# §21 Test 3 — explanation, no unnecessary MILP
# ===========================================================================

class TestExplanationQuery:

    def test_an_explanation_reuses_cached_evidence(self):
        chat, orch, counter = _counting_chat()

        first = _say(chat, "What is DC_DELHI risk exposure?")
        assert counter.count > 0, "the first assessment genuinely solved"
        counter.reset()

        response = _say(chat, "Why is DC_DELHI considered high risk?")

        assert response.intent == Intent.EXPLANATION.value
        assert counter.count == 0, (
            f"the explanation re-solved: {counter.scenario_ids}"
        )
        assert response.results["resilience"]["served_from_cache"] is True

    def test_a_cost_explanation_does_not_launch_an_optimization(self):
        chat, _, counter = _counting_chat()
        _say(chat, "What is DC_DELHI risk exposure?")
        counter.reset()

        response = _say(chat, "Why did transportation cost increase?")
        assert response.intent == Intent.EXPLANATION.value
        assert counter.count == 0

    def test_the_explanation_carries_real_deterministic_figures(self):
        chat, _, _ = _counting_chat()
        _say(chat, "What is DC_DELHI risk exposure?")
        response = _say(chat, "Why is DC_DELHI considered high risk?")

        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)
        assert response.grounding_status in ("GROUNDED", "NO_CLAIMS")


# ===========================================================================
# §21 Test 4 — risk query, cached REI reused
# ===========================================================================

class TestRiskQuery:

    def test_the_first_risk_query_computes(self):
        chat, _, counter = _counting_chat()
        response = _say(chat, "What is the risk exposure of DC_DELHI?")

        assert response.intent == Intent.RESILIENCE_QUERY.value
        assert counter.count == 6      # 1 baseline + 4 nodes + 1 diagnostic
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)

    def test_the_second_identical_query_performs_zero_solves(self):
        """§24 — the headline performance requirement, measured at the solver."""
        chat, _, counter = _counting_chat()
        _say(chat, "What is the risk exposure of DC_DELHI?")
        counter.reset()

        response = _say(chat, "What is DC_DELHI's REI?")

        assert counter.count == 0, f"unnecessary solves: {counter.scenario_ids}"
        assert response.results["resilience"]["served_from_cache"] is True
        assert response.results["resilience"]["n_milp_solves"] == 0

    def test_cached_and_fresh_answers_agree(self):
        chat, _, _ = _counting_chat()
        first = _say(chat, "What is the risk exposure of DC_DELHI?")
        second = _say(chat, "What is DC_DELHI's REI?")

        assert first.results["resilience"]["rei_by_facility"] == \
            second.results["resilience"]["rei_by_facility"]


# ===========================================================================
# §21 Test 5 — external signal → RF
# ===========================================================================

class TestExternalSignal:

    def test_rf_is_computed_deterministically_from_natural_language(self, chat):
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )

        assert response.intent == Intent.EXTERNAL_EVENT.value
        [row] = response.risk["results"]
        assert row["facility_id"] == "DC_DELHI"
        assert row["likelihood"] == pytest.approx(0.70, abs=TOL)
        assert row["rei"] == pytest.approx(0.80, abs=TOL)
        assert row["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert row["formula"] == "RF = P + REI - P*REI"

    def test_the_arithmetic_reproduces_from_the_recorded_inputs(self, chat):
        """RF = P + REI − P·REI, verifiable from the response alone."""
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        row = response.risk["results"][0]
        p, rei = row["likelihood"], row["rei"]
        assert p + rei - p * rei == pytest.approx(row["risk_factor"], abs=1e-9)

    def test_the_llm_did_not_produce_the_risk_factor(self, chat):
        """
        Structural: the run is offline (`disable_llm=True`), so no model was
        consulted at all, and RF is still exactly 0.94. The number cannot have
        come from a model that never ran.
        """
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        assert response.intent_source == "rules"
        assert response.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

    def test_governance_runs_and_escalates_on_the_measured_risk(self, chat):
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        assert response.governance["classification"] == "HUMAN_ONLY"
        assert "R6_RISK_FACTOR_HUMAN" in response.governance["triggered_rules"]
        assert response.governance["blocked_by_missing_evidence"] is False
        assert "requires a human decision" in response.reply

    def test_the_user_asserted_probability_is_labelled_as_such(self, orch):
        """Provenance: the P came from a person, not from a data feed."""
        chat = ChatService(orch)
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        row = response.risk["results"][0]
        assert "user_conversation" in row["provenance"]["likelihood"]


# ===========================================================================
# §21 Test 6 — ambiguity
# ===========================================================================

class TestAmbiguousRequest:

    def test_close_delhi_asks_rather_than_guessing(self):
        chat, _, counter = _counting_chat()
        response = _say(chat, "Close Delhi.")

        assert response.status == "AWAITING_CLARIFICATION"
        assert response.clarity == IntentClarity.AMBIGUOUS.value
        assert response.clarification is not None
        assert counter.count == 0, "an unanswered question must not run the solver"

    def test_no_scenario_and_no_execution_were_created(self, orch):
        chat = ChatService(orch)
        response = _say(chat, "Close Delhi.")

        assert response.execution_id is None
        assert response.scenario_id is None
        assert orch.scenarios.list_ids() == []

    def test_answering_the_clarification_produces_a_real_scenario(self, orch):
        """The clarification returns to the intent layer and completes."""
        chat = ChatService(orch)
        first = _say(chat, "Close Delhi.")
        assert first.status == "AWAITING_CLARIFICATION"

        second = _say(chat, "Simulate closure of the DC_DELHI facility.",
                      conversation_id=first.conversation_id)

        assert second.clarity == IntentClarity.CLEAR.value
        assert second.provenance == "SCENARIO"
        assert second.scenario_id is not None

    def test_a_missing_quantity_asks_for_the_number(self, chat):
        response = _say(chat, "Reduce DC_DELHI capacity.")
        assert response.status == "AWAITING_CLARIFICATION"
        assert response.clarification.missing_parameter == "capacity"


# ===========================================================================
# §21 Test 7 — unknown node
# ===========================================================================

class TestUnknownNode:

    def test_an_unknown_facility_is_not_fabricated(self):
        chat, orch, counter = _counting_chat()
        response = _say(chat, "What if we close Bangalore DC?")

        assert response.status == "AWAITING_CLARIFICATION"
        assert response.clarification.kind.value == AmbiguityKind.UNKNOWN_ENTITY.value
        assert response.resolved_entity_ids == []
        assert counter.count == 0
        assert orch.scenarios.list_ids() == []

    def test_the_clarification_offers_only_real_facilities(self, chat):
        response = _say(chat, "What if we close Bangalore DC?")
        offered = {o["id"] for o in response.clarification.options}
        assert offered == {"DC_DELHI", "DC_KOLKATA", "DC_MUMBAI"}

    def test_an_event_naming_an_unknown_node_is_refused_not_partially_answered(self, chat):
        """
        BEHAVIOUR CHANGED IN PHASE 3.2 — this test previously asserted the
        opposite, and the reversal is deliberate.

        The old behaviour computed RF for DC_DELHI and dropped DC_ATLANTIS with
        a warning. Nothing was fabricated, so it looked safe. But the user asked
        about two facilities and got an answer about one, presented as complete:
        a partial answer with the omission recorded somewhere they were not
        looking. That is the same failure mode Phase 3.2 exists to remove —
        a named site that master data does not contain must be *reported*, not
        quietly skipped.

        The system now says which name it could not find and runs nothing.
        """
        response = _say(
            chat,
            "There is a 70% probability of flooding around DC_DELHI and DC_ATLANTIS.",
        )
        assert response.status == "AWAITING_CLARIFICATION"
        assert response.clarification.kind.value == "UNKNOWN_ENTITY"
        assert "DC_ATLANTIS" in response.reply
        # Nothing ran: no solve, no REI, no RF, no governance.
        assert response.risk is None
        assert response.governance is None
        assert response.execution_id is None


# ===========================================================================
# §21 Test 8 — missing probability
# ===========================================================================

class TestMissingProbability:

    def test_severity_is_never_converted_into_a_probability(self, chat):
        response = _say(chat, "There may be severe flooding around DC_DELHI.")

        assert response.risk["results"] == []
        rows = response.risk["not_computable"]
        assert rows[0]["not_computable_reason"] == "NO_EVENT_PROBABILITY"
        assert rows[0]["likelihood"] is None, "P is UNKNOWN, never 0"
        assert response.risk["max_risk_factor"] is None

    def test_the_reply_explains_why_rf_is_absent(self, chat):
        response = _say(chat, "There may be severe flooding around DC_DELHI.")
        assert "NOT calculated" in response.reply
        assert "NO_EVENT_PROBABILITY" in response.reply

    def test_exposure_is_still_reported(self, chat):
        """Losing P costs RF, not the exposure analysis."""
        response = _say(chat, "There may be severe flooding around DC_DELHI.")
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)


# ===========================================================================
# §21 Test 9 / 10 — REI unavailable and stale
# ===========================================================================

class TestDegradedEvidence:

    def test_rei_timeout_yields_rf_not_computable(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        chat = ChatService(orch)

        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )

        assert response.risk["results"] == []
        assert response.risk["not_computable"][0]["not_computable_reason"] == "NO_REI"
        assert response.risk["not_computable"][0]["rei"] is None

    def test_missing_evidence_never_becomes_zero_in_the_reply(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        chat = ChatService(orch)

        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        assert "UNKNOWN" in response.reply
        assert "resilience.assess" in response.reply

    def test_governance_stays_conservative_when_evidence_is_missing(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = TimingOutREIClient()
        chat = ChatService(orch)

        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        assert response.governance["classification"] != "AUTO_ACTION"
        assert response.governance["blocked_by_missing_evidence"] is True
        assert "not because measured risk is high" in response.reply

    def test_stale_rei_is_not_silently_consumed(self):
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.services["rei"] = StaleREIClient("snap_V17")
        chat = ChatService(orch)

        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )

        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            "STALE_REI"
        assert response.risk["max_risk_factor"] is None
        assert response.governance["classification"] != "AUTO_ACTION"


# ===========================================================================
# §15 — LLM failure
# ===========================================================================

class TestLLMFailure:

    def test_a_failing_model_does_not_fabricate_an_intent(self, orch):
        """
        The gateway raises on every call. The deterministic rule tier still
        classifies, so the answer is produced WITHOUT a model — and no intent is
        invented to keep the conversation moving.
        """
        from netgravity.orchestrator.exceptions import LLMNonRetryableError

        class ExplodingGateway:
            @property
            def available(self) -> bool:
                return True

            def generate(self, prompt, *, purpose="generic"):
                raise LLMNonRetryableError("gateway is down")

            def stats(self):
                return {"available": False}

            def unavailable_reason(self):
                return "gateway is down"

        from netgravity.orchestrator.agents.intent_agent import IntentAgent
        from netgravity.orchestrator.conversation.nlu import ConversationalNLU

        chat = ChatService(orch, nlu=ConversationalNLU(
            intent_agent=IntentAgent(ExplodingGateway()),
        ))
        response = chat.chat(ChatRequest(
            message="What is the risk exposure of DC_DELHI?", disable_llm=False,
        ))

        assert response.intent == Intent.RESILIENCE_QUERY.value
        assert response.intent_source == "rules"
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)

    def test_an_uninterpretable_request_is_declined_not_guessed(self, chat):
        response = _say(chat, "zxcvbnm qwerty asdfgh")
        assert response.status == "UNSUPPORTED"
        assert response.intent == Intent.UNKNOWN.value
        assert response.execution_id is None

    def test_deterministic_results_are_unaffected_by_the_llm_being_off(self, orch):
        """The same question, with and without the model, gives the same numbers."""
        chat = ChatService(orch)
        offline = chat.chat(ChatRequest(
            message="What is the risk exposure of DC_DELHI?", disable_llm=True,
        ))
        online = chat.chat(ChatRequest(
            message="What is the risk exposure of DC_DELHI?", disable_llm=False,
        ))
        assert offline.results["resilience"]["rei_by_facility"] == \
            online.results["resilience"]["rei_by_facility"]


# ===========================================================================
# §13 / §14 — conversation context and follow-ups
# ===========================================================================

class TestConversationContext:

    def test_a_follow_up_inherits_the_subject(self, orch):
        chat = ChatService(orch)
        first = _say(chat, "What is the risk exposure of DC_DELHI?")
        second = _say(chat, "Why?", conversation_id=first.conversation_id)

        assert second.conversation_id == first.conversation_id
        assert second.resolved_entity_ids == ["DC_DELHI"]
        assert second.intent == Intent.EXPLANATION.value

    def test_a_new_subject_replaces_rather_than_accumulates(self, orch):
        """
        §13's central risk: turn two must produce a NEW scenario about Mumbai,
        not a compound scenario about Delhi AND Mumbai.
        """
        chat = ChatService(orch)
        first = _say(chat,
                     "What happens if DC_DELHI capacity decreases by 4,950 units/day?")
        second = _say(chat,
                      "What if DC_MUMBAI capacity decreases by 1,000 units/day instead?",
                      conversation_id=first.conversation_id)

        assert second.scenario_id != first.scenario_id
        overrides = second.results["network"]["scenario_overrides"]
        assert overrides == ["CHANGE_CAPACITY DC_MUMBAI -1,000 units/period"]
        assert not any("DC_DELHI" in o for o in overrides)

    def test_each_scenario_branches_from_the_observed_baseline(self, orch):
        chat = ChatService(orch)
        first = _say(chat,
                     "What happens if DC_DELHI capacity decreases by 4,950 units/day?")
        second = _say(chat,
                      "What if DC_MUMBAI capacity decreases by 1,000 units/day instead?",
                      conversation_id=first.conversation_id)

        record = orch.scenarios.get(second.scenario_id)
        capacities = {f.id: f.capacity_units_per_period
                      for f in record.network.facilities if f.id.startswith("DC_")}
        assert capacities["DC_MUMBAI"] == pytest.approx(4_000.0)
        assert capacities["DC_DELHI"] == pytest.approx(5_000.0), (
            "the second scenario inherited the first's override"
        )

    def test_history_is_recorded_per_conversation(self, orch):
        chat = ChatService(orch)
        first = _say(chat, "What is the risk exposure of DC_DELHI?")
        _say(chat, "Why?", conversation_id=first.conversation_id)

        turns = chat.history(first.conversation_id)
        assert len(turns) == 2
        assert turns[0].intent == Intent.RESILIENCE_QUERY
        assert turns[1].intent == Intent.EXPLANATION

    def test_separate_conversations_do_not_share_context(self, orch):
        chat = ChatService(orch)
        a = _say(chat, "What is the risk exposure of DC_DELHI?")
        b = _say(chat, "What is the risk exposure of DC_MUMBAI?")

        assert a.conversation_id != b.conversation_id
        follow_up = _say(chat, "Why?", conversation_id=b.conversation_id)
        assert follow_up.resolved_entity_ids == ["DC_MUMBAI"]

    def test_the_baseline_survives_a_whole_conversation(self, orch):
        chat = ChatService(orch)
        before = orch.snapshots.current().network.model_dump_json()

        first = _say(chat, "What is the risk exposure of DC_DELHI?")
        cid = first.conversation_id
        _say(chat, "Why?", conversation_id=cid)
        _say(chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?",
             conversation_id=cid)
        _say(chat, "What if DC_MUMBAI capacity decreases by 1,000 units/day instead?",
             conversation_id=cid)

        assert orch.snapshots.current().network.model_dump_json() == before


# ===========================================================================
# §18 / §19 — auditability and observability
# ===========================================================================

class TestConversationalAudit:

    def test_the_chain_is_reconstructable_from_the_execution_trace(self, orch):
        chat = ChatService(orch)
        response = _say(
            chat, "There is a 70% probability of flooding around DC_DELHI.",
        )
        trace = orch.get_trace(response.execution_id)
        record = trace.to_dict()

        [classified] = trace.events_of(events.INTENT_CLASSIFIED)
        assert classified.detail["intent"] == Intent.EXTERNAL_EVENT.value
        assert classified.detail["conversation_id"] == response.conversation_id
        assert classified.detail["intent_schema_version"]
        assert classified.detail["prompt_version"]
        assert classified.detail["resolved_entity_ids"] == ["DC_DELHI"]

        [selected] = trace.events_of(events.WORKFLOW_SELECTED)
        assert selected.detail["workflow_id"] == "wf_external_event"
        assert selected.detail["selected_by"] == "WorkflowPlanner"

        # The deterministic half of the chain is in the same record.
        assert record["risk_calculation"]["results"][0]["risk_factor"] == \
            pytest.approx(0.94, abs=TOL)
        assert record["governance_decision"]["classification"] == "HUMAN_ONLY"

    def test_all_chat_events_are_emitted(self, orch):
        chat = ChatService(orch)
        response = _say(chat, "What is the risk exposure of DC_DELHI?")
        trace = orch.get_trace(response.execution_id)
        emitted = {e.event_type for e in trace.events}

        for required in (events.CHAT_REQUEST_RECEIVED, events.INTENT_CLASSIFIED,
                         events.WORKFLOW_SELECTED, events.CHAT_RESPONSE_GENERATED):
            assert required in emitted, required

    def test_the_trace_carries_no_credential_material(self, orch):
        chat = ChatService(orch)
        response = _say(chat, "What is the risk exposure of DC_DELHI?")
        serialised = orch.get_trace(response.execution_id).to_json().lower()

        for forbidden in ("authorization", "bearer ", "text_api_token", "secret"):
            assert forbidden not in serialised

    def test_provenance_is_always_stated(self, chat):
        observed = _say(chat, "How many warehouses do we have?")
        scenario = _say(
            chat, "What happens if DC_DELHI capacity decreases by 4,950 units/day?")

        assert observed.provenance == "OBSERVED"
        assert scenario.provenance == "SCENARIO"


# ===========================================================================
# §24 — performance
# ===========================================================================

class TestPerformance:

    def test_conversational_queries_do_not_multiply_solver_work(self):
        """
        A whole conversation on one unchanged network costs ONE REI batch.
        Everything after it is served from cache.
        """
        chat, _, counter = _counting_chat()

        _say(chat, "What is the risk exposure of DC_DELHI?")
        first_batch = counter.count
        assert first_batch == 6

        counter.reset()
        cid = None
        for message in (
            "What is DC_MUMBAI's REI?",
            "Why is DC_DELHI considered high risk?",
            "There is a 70% probability of flooding around DC_DELHI.",
            "How many warehouses do we have?",
        ):
            response = _say(chat, message, conversation_id=cid)
            cid = response.conversation_id

        assert counter.count == 0, (
            f"four follow-up questions triggered {counter.count} solves: "
            f"{counter.scenario_ids}"
        )

    def test_an_ambiguous_or_unknown_request_costs_nothing(self):
        chat, _, counter = _counting_chat()
        for message in ("Close Delhi.", "What if we close Bangalore DC?",
                        "Reduce DC_DELHI capacity.", "zxcvbnm"):
            _say(chat, message)
        assert counter.count == 0

    def test_a_status_query_costs_nothing(self):
        chat, _, counter = _counting_chat()
        _say(chat, "How many warehouses do we have?")
        assert counter.count == 0
