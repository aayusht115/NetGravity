"""
The assistant, doing the basic things a user does first.

Every test here is one thing that was broken when the chat surface was used the
way a person uses it — typing "hello", asking what something costs, asking what
happens if demand grows. The engine underneath was working the whole time; each
failure was in the layer between the question and it.

WHAT WAS WRONG, in the order a user would have met it:

  * "hello" was classified UNKNOWN and answered "I could not work out what you
    would like me to do", followed by a list of every distribution centre with
    its internal identifier;
  * "what is my total network cost?" was answered "The total network cost is
    reported as the business network cost." — no figure, because the narrative
    layer is told to write none. That rule is correct for a dashboard card,
    where the screen prints the figures beside the sentence. The assistant has
    nothing beside it;
  * "what happens if demand grows 20%?" returned HTTP 500. The model had
    classified it correctly and proposed a runnable scenario; the parser threw
    it away for naming no facility, `scenario.create` then failed, and the
    browser told the user the analysis engine was unreachable — while the
    engine had solved the network twice and put the answer in the 500's body;
  * "should I open a new distribution centre?" returned HTTP 500 for the same
    reason, on a message that should simply have been asked a question back.
"""

from __future__ import annotations

import pathlib

import pytest

from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.conversation.nlu import (
    _GREETING_RE,
    _SCENARIO_INTENTS_NEEDING_A_SPEC,
    ConversationalNLU,
)
from netgravity.orchestrator.schemas.conversation import AmbiguityKind
from netgravity.orchestrator.schemas.requests import (
    NETWORK_WIDE_ACTIONS,
    Intent,
    ScenarioActionType,
    ScenarioIntentSpec,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


class TestAGreetingIsAnswered:
    """
    "hello" is the first thing anyone types, and it was the worst answer the
    assistant gave: a refusal, plus a dump of internal facility identifiers.

    A greeting is a request for orientation, and the orientation answer already
    existed — it is what "what can you do?" returns.
    """

    @pytest.mark.parametrize("message", [
        "hello", "Hi", "hey", "hey there", "Good morning", "hiii",
        "namaste", "hello!", "Hi there",
    ])
    def test_it_is_recognised(self, message):
        assert _GREETING_RE.match(message.strip().rstrip("!?.,")), message

    @pytest.mark.parametrize("message", [
        "hi, why is Delhi at 97%?",
        "which site is highest?",
        "hello world dataset upload",
        "high utilisation at the western DC",
        "open a new DC",
    ])
    def test_it_does_not_swallow_a_real_question(self, message):
        """
        Matched against the WHOLE message, never searched for inside it. A
        greeting with a question attached is the question.
        """
        assert not _GREETING_RE.match(message.strip().rstrip("!?.,")), message

    def test_a_greeting_routes_to_the_capability_answer(self):
        from netgravity.tests.fixtures.case16_synthetic import (
            build_case16_network,
        )

        nlu = ConversationalNLU(intent_agent=IntentAgent(gateway=None))
        intent, scenarios, source, _confidence, _rationale, _e, _f = \
            nlu._classify("hello", build_case16_network(), [], allow_llm=False)
        assert intent is Intent.CAPABILITY_QUERY
        assert source == "rules", "a greeting must never cost a model call"
        assert scenarios == []


class TestAScenarioThatNamesNoSiteIsNotAnEmptyScenario:
    """
    The single line that made every network-wide what-if return HTTP 500.

    `_llm_based` dropped any proposed scenario whose `facility_ids` was empty,
    which is the natural shape of a demand change, a freight-rate change or a
    change to the delivery promise. The schema has said so for as long as
    `NETWORK_WIDE_ACTIONS` has existed, and the REST scenario API and the
    scenario validator both already read it.
    """

    def _agent(self, payload):
        class _Gateway:
            def generate(self, prompt, purpose=""):
                class _R:
                    output = payload
                    request_id = "test"
                return _R()
        return IntentAgent(gateway=_Gateway())

    def test_a_network_wide_demand_change_survives(self):
        agent = self._agent(
            '{"intent":"SCENARIO_ANALYSIS","confidence":0.9,'
            '"facility_ids":[],'
            '"scenarios":[{"action":"CHANGE_DEMAND","facility_ids":[],'
            '"demand_multiplier":1.2,"label":"demand +20%"}]}')
        resolved = agent._llm_based("what happens if demand grows 20%?",
                                    ["DC_WEST", "DC_EAST"])
        assert resolved is not None
        assert len(resolved.scenarios) == 1, "the scenario was discarded again"
        spec = resolved.scenarios[0]
        assert spec.action is ScenarioActionType.CHANGE_DEMAND
        assert spec.demand_multiplier == pytest.approx(1.2)
        assert spec.is_runnable, "runnable is the whole point of keeping it"

    def test_the_other_two_network_wide_actions_survive_too(self):
        agent = self._agent(
            '{"intent":"SCENARIO_ANALYSIS","confidence":0.9,"facility_ids":[],'
            '"scenarios":['
            '{"action":"CHANGE_TRANSPORT_COST","facility_ids":[],'
            ' "transport_cost_multiplier":1.1,"label":"freight +10%"},'
            '{"action":"CHANGE_SLA","facility_ids":[],'
            ' "sla_days_delta":-1,"label":"a day tighter"}]}')
        resolved = agent._llm_based("freight up 10% and a day tighter",
                                    ["DC_WEST"])
        assert resolved is not None
        kinds = {s.action for s in resolved.scenarios}
        assert kinds == {ScenarioActionType.CHANGE_TRANSPORT_COST,
                         ScenarioActionType.CHANGE_SLA}
        # The quantities were read. Without them each is an action with no
        # magnitude, which is the same dead end as no scenario at all.
        for spec in resolved.scenarios:
            assert spec.is_runnable, spec.action

    def test_an_action_that_needs_a_site_is_still_dropped_without_one(self):
        """
        The rule is `NETWORK_WIDE_ACTIONS`, not "keep everything". Closing a
        facility nobody named is not a scenario.
        """
        agent = self._agent(
            '{"intent":"SCENARIO_ANALYSIS","confidence":0.9,"facility_ids":[],'
            '"scenarios":[{"action":"CLOSE_FACILITY","facility_ids":[],'
            '"label":"close it"}]}')
        resolved = agent._llm_based("close it", ["DC_WEST"])
        assert resolved is not None
        assert resolved.scenarios == []

    def test_an_invented_facility_is_still_dropped(self):
        agent = self._agent(
            '{"intent":"SCENARIO_ANALYSIS","confidence":0.9,'
            '"facility_ids":["DC_ATLANTIS"],'
            '"scenarios":[{"action":"CLOSE_FACILITY",'
            '"facility_ids":["DC_ATLANTIS"],"label":"x"}]}')
        resolved = agent._llm_based("close DC_ATLANTIS", ["DC_WEST"])
        assert resolved is not None
        assert resolved.entities == []
        assert resolved.scenarios == []

    def test_the_model_is_told_these_fields_exist(self):
        """
        A parser that reads a field the prompt never offered reads nothing.
        """
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
                  / "intent_agent.py").read_text(encoding="utf-8")
        block = source[source.index("JSON schema:"):source.index("User request:")]
        for field in ("demand_multiplier", "demand_region",
                      "demand_product_category", "transport_cost_multiplier",
                      "sla_days_delta"):
            assert field in block, field
        assert "WHOLE NETWORK" in block, \
            "nothing tells the model these actions need no facility"


class TestAnUnrunnableWhatIfAsksRatherThanFails:
    """
    A scenario intent with no runnable spec used to reach `scenario.create`,
    fail there, and take the whole execution to FAILED — which the endpoint
    answers 500 and the browser reports as an unreachable engine.

    The check existed. It was inside the branch that requires a resolved
    facility, so it fired for every case except the ones that resolve none.
    """

    def _nlu(self):
        return ConversationalNLU(intent_agent=IntentAgent(gateway=None))

    def test_a_what_if_naming_nobody_is_a_question_not_a_failure(self):
        kind = self._nlu()._detect_intent_ambiguity(
            "should I open a new distribution centre?",
            Intent.SCENARIO_ANALYSIS, [], [])
        assert kind is AmbiguityKind.MISSING_PARAMETER

    def test_a_comparison_with_nothing_to_compare_asks_too(self):
        kind = self._nlu()._detect_intent_ambiguity(
            "compare the options", Intent.SCENARIO_COMPARISON, [], [])
        assert kind is AmbiguityKind.MISSING_PARAMETER

    def test_a_runnable_spec_is_left_alone(self):
        spec = ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                  demand_multiplier=1.2)
        assert spec.is_runnable
        kind = self._nlu()._detect_intent_ambiguity(
            "what happens if demand grows 20%?",
            Intent.SCENARIO_ANALYSIS, [spec], [])
        assert kind is None

    def test_which_operation_is_still_the_better_question(self):
        """
        ORDER. "What if we close it instead?" has no runnable spec either — but
        the subject is known and only the operation is in doubt, which is asked
        as three concrete options rather than as an open question. Putting the
        runnable check first stole that diagnosis; the NLU evaluation caught it
        as a new residual failure (fu08).
        """
        kind = self._nlu()._detect_intent_ambiguity(
            "what if we close it instead?",
            Intent.SCENARIO_ANALYSIS, [], ["DC_DELHI"])
        assert kind is AmbiguityKind.AMBIGUOUS_INTENT

    def test_a_non_scenario_intent_is_not_policed_by_this(self):
        for intent in (Intent.NETWORK_STATE_QUERY, Intent.EXPLANATION,
                       Intent.STATUS_QUERY):
            assert intent not in _SCENARIO_INTENTS_NEEDING_A_SPEC
            assert self._nlu()._detect_intent_ambiguity(
                "what is my total cost?", intent, [], []) is None

    def test_the_question_offers_changes_this_product_can_model(self):
        """
        An open question earns an answer the engine cannot run. The examples
        are the network-wide actions the scenario builder accepts.
        """
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "conversation"
                  / "nlu.py").read_text(encoding="utf-8")
        block = source[source.index("elif not resolved_ids:"):]
        block = block[:block.index("elif mentions_capacity:")]
        # THE COMMENT ABOVE THE CODE EXPLAINS THE DEFECT IN THE WORDS THE CODE
        # MUST NOT USE, so an assertion about the source has to read the source
        # rather than the prose around it. Stripped, not trusted.
        code = "\n".join(line for line in block.splitlines()
                         if not line.strip().startswith("#"))
        assert "demand grows" in code
        assert "freight" in code
        assert "that facility" not in code


class TestAnAnswerCarriesItsFigure:
    """
    "RULES: no figures, amounts, percentages or currency symbols" is right for
    a dashboard card and wrong for the assistant, and the difference is stated
    in the rule's own justification: the screen prints the figures beside the
    sentence. In chat the sentence is the whole answer.
    """

    def test_the_card_rule_is_unchanged(self):
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
                  / "reasoning_agent.py").read_text(encoding="utf-8")
        assert "RULES: no figures, amounts, percentages or currency symbols." \
            in source

    def test_answering_a_question_turns_the_rule_off(self):
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
                  / "reasoning_agent.py").read_text(encoding="utf-8")
        assert 'answering = bool(question) and not payload.get("kpi_chart")' \
            in source
        assert "quote the figure asked for" in source
        # A chart card has its own vocabulary and must not be re-routed here.
        block = source[source.index("answering = bool(question)"):]
        assert 'not payload.get("kpi_chart")' in block[:120]

    def test_the_rule_stays_short(self):
        """
        The binding constraint is not clarity, it is the output budget: the
        backing model bills its internal reasoning to the same 2,000 tokens as
        its text, so a long rule is paid for twice. A fuller version of this
        one was measured at output_tokens=1984 with ZERO characters emitted on
        two calls out of three — the reasoning layer degrading to templates.
        """
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
                  / "reasoning_agent.py").read_text(encoding="utf-8")
        start = source.index('"RULES: quote the figure asked for')
        rule = source[start:source.index("if answering else", start)]
        # The literal, without the Python quoting around it.
        text = "".join(part.strip().strip('"') for part in rule.splitlines())
        assert len(text) < 200, f"{len(text)} chars: {text}"


class TestFiguresAreRenderedBeforeTheModelSeesThem:
    """
    A model told to copy a figure exactly copies what the JSON says, which is
    `150627.7036`. The answers came back "Total network cost is INR
    150627.7036" — correct to four decimal places and unreadable.

    The fix is not another prompt rule. "Copy it exactly" and "round it" are in
    direct tension, and every clause is paid for out of the output budget.
    Rendering the evidence removes the tension and costs nothing.
    """

    PAYLOAD = {
        "network_state": {
            "currency": "INR",
            "business_network_cost": 150627.7036,
            "avg_utilization_pct": 56.42857,
            "total_demand": 7300.0,
            "n_facilities_open": 5,
            "cost_components": {"facility_cost": 95000.0},
        },
        "facilities": [{
            "facility_id": "DC_WEST",
            "facility_name": "Western Distribution Centre",
            "utilization_pct": 77.142857,
            "capacity_units": 3500.0,
            "is_open": True,
        }],
    }

    def _rendered(self):
        return ReasoningAgent._readable_figures(self.PAYLOAD, "INR")

    def test_money_carries_its_currency_and_no_cents(self):
        state = self._rendered()["network_state"]
        assert state["business_network_cost"] == "₹150,628"
        assert state["cost_components"]["facility_cost"] == "₹95,000"

    def test_percentages_are_rendered_at_the_precision_the_cards_use(self):
        assert self._rendered()["network_state"]["avg_utilization_pct"] == "56.4%"
        assert self._rendered()["facilities"][0]["utilization_pct"] == "77.1%"

    def test_it_reaches_nested_rows(self):
        row = self._rendered()["facilities"][0]
        assert row["capacity_units"] == "3,500 units"
        # Untouched: not a quantity, and a name is not a figure.
        assert row["facility_name"] == "Western Distribution Centre"
        assert row["is_open"] is True

    def test_a_bare_count_stays_a_number(self):
        """
        `_display` has no opinion about a count, and turning 5 into "5" would
        make a facility count look like a rendered quantity.
        """
        assert self._rendered()["network_state"]["n_facilities_open"] == 5

    def test_a_rendered_figure_still_grounds_against_its_fact(self):
        """
        The whole approach rests on this: a figure the model copies from the
        rendered evidence must survive numeric grounding, or the validator
        removes it and the sentence is left with a hole.
        """
        from netgravity.orchestrator.validation.numeric_grounding import (
            ground_narrative,
        )

        rendered = self._rendered()
        narrative = (
            f"Total network cost is {rendered['network_state']['business_network_cost']}. "
            f"Western Distribution Centre runs at "
            f"{rendered['facilities'][0]['utilization_pct']} of its "
            f"{rendered['facilities'][0]['capacity_units']}."
        )
        # The PAYLOAD, not a fact table: `ground_narrative` builds the
        # authoritative facts itself, and handing it a facts dict makes it
        # index a structure with no citable keys in it — every claim then comes
        # back UNSUPPORTED, which looks exactly like the defect this test is
        # here to catch.
        report = ground_narrative(narrative, self.PAYLOAD)
        assert report.status != "GROUNDING_FAILED", [
            (c.raw_text, c.verdict.value) for c in report.claims
        ]

    def test_only_the_answering_path_renders(self):
        """
        A dashboard card is told to write no figures, and a chart card has its
        own registered fact vocabulary. Neither should have its evidence
        rewritten underneath it.
        """
        source = (REPO_ROOT / "netgravity" / "orchestrator" / "agents"
                  / "reasoning_agent.py").read_text(encoding="utf-8")
        block = source[source.index("evidence = self._bounded_evidence(payload)"):]
        block = block[:block.index("missing_block")]
        assert "if answering:" in block
        assert "_readable_figures" in block


class TestAClarificationRendersAsAQuestion:
    """
    The browser's clarification branch was unreachable, and broken if reached.

    `_clarification_response` sets `reply` to the clarification's question, so
    the `if (answer)` branch above it always won: the question was rendered as
    an ANSWER — labelled with the intent, under an "Explore in Digital Twin"
    button, as though the assistant had concluded something.

    And `clarification` is a record, not a string, so the dead branch's
    `escapeChatText(clarification)` would have printed "[object Object]".

    Neither mattered while unrunnable what-ifs were failing with a 500. They
    now come back as clarifications, which makes this the common path.
    """

    def _chatbot(self) -> str:
        return (REPO_ROOT / "app" / "frontend" / "js"
                / "chatbot.js").read_text(encoding="utf-8")

    def test_the_question_is_read_not_the_record_around_it(self):
        js = self._chatbot()
        assert "res.clarification && res.clarification.question" in js

    def test_a_clarification_is_handled_before_the_answer(self):
        js = self._chatbot()
        block = js[js.index("const answer = res && (res.reply"):]
        block = block[:block.index("} catch (err) {")]
        assert block.index("if (clarification) {") < block.index("} else if (answer) {"), \
            "the answer branch wins again and a question renders as a conclusion"

    def test_the_named_options_are_offered(self):
        """
        An open question invites an answer the engine cannot run. Where the
        engine named the choices — close the site, shift its volume, change its
        capacity — those are what the reader should be given.
        """
        js = self._chatbot()
        block = js[js.index("if (clarification) {"):]
        block = block[:block.index("} else if (answer) {")]
        assert "res.clarification.options" in block
        assert "escapeChatText(o)" in block, "an option label reaches innerHTML"

    def test_the_dead_branch_is_gone(self):
        """
        Two copies of this branch would leave the broken one in place for
        whichever call reached it first.
        """
        js = self._chatbot()
        code = "\n".join(line for line in js.splitlines()
                         if not line.strip().startswith("//"))
        assert code.count("topic: 'NEEDS CLARIFICATION'") == 1
        assert "escapeChatText(clarification)," not in code


class TestTheSuggestedQuestionsWorkWithoutAModel:
    """
    Six questions ship on the assistant's opening screen as the things to ask.
    Three of them classified as UNKNOWN at the deterministic tier:

        Which distribution centre is most utilised?
        How much of my demand is served, and how much is not?
        Which of my scenarios has the lowest network cost?

    They worked only because the model tier caught them. That tier is not a
    spare wheel — the gateway's budget is a hundred requests A DAY shared
    across the whole product, so exhausting it is an ordinary afternoon, and
    each of these then returned "I could not work out what you would like me to
    do" about a question this product had put in front of the user itself.
    """

    #: Read from the markup, so adding a suggestion to the screen without a
    #: rule to answer it fails here rather than in front of a user.
    def _suggested(self):
        import re as _re

        html = (REPO_ROOT / "app" / "frontend"
                / "index.html").read_text(encoding="utf-8")
        block = html[html.index('id="chatbot-faq-section"'):]
        block = block[:block.index("</div>\n        </div>")]
        return _re.findall(r'data-action="askChatbotPrompt" data-arg="([^"]+)"',
                           block)

    @pytest.fixture(scope="class")
    def nlu(self):
        return ConversationalNLU(intent_agent=IntentAgent(gateway=None))

    @pytest.fixture(scope="class")
    def network(self):
        from netgravity.tests.fixtures.case16_synthetic import (
            build_case16_network,
        )
        return build_case16_network()

    def test_the_screen_still_suggests_questions(self):
        """
        Both blocks: the FAQ cards and the pill row beneath them. The pills
        were the worse half — all four classified as UNKNOWN, and one of them
        ("More prompts") was a UI affordance wired to the chat endpoint with a
        string that is not a question at all.
        """
        assert len(self._suggested()) >= 9, (
            "a guard that covers fewer suggestions than the screen shows is "
            "not covering the screen")

    def test_every_suggested_question_is_understood_offline(self, nlu, network):
        unknown = []
        for question in self._suggested():
            intent, _s, source, _c, _r, _e, _f = nlu._classify(
                question, network, [], allow_llm=False)
            if intent is Intent.UNKNOWN:
                unknown.append(question)
            assert source == "rules", question
        assert unknown == [], unknown

    def test_no_suggestion_is_a_ui_control(self):
        """
        `data-arg` is sent to the optimisation engine verbatim. A control that
        reveals more of the interface has no business being one.
        """
        for question in self._suggested():
            assert "more recommended" not in question.lower(), question
            assert "show more" not in question.lower(), question

    def test_a_superlative_only_counts_over_a_metric(self, nlu, network):
        """
        "Which site is largest?" asks about the network's SHAPE, not about
        anything a solve produced. Treating every superlative as a metric
        question would route it into an optimisation it does not need.
        """
        intent, _s, _src, _c, _r, _e, _f = nlu._classify(
            "Which site is largest?", network, [], allow_llm=False)
        assert intent is not Intent.NETWORK_STATE_QUERY

    @pytest.mark.parametrize("question,expected", [
        ("How many facilities do I have?", Intent.STATUS_QUERY),
        ("Which facilities are open?", Intent.STATUS_QUERY),
        ("What is the risk exposure of DC_WEST?", Intent.RESILIENCE_QUERY),
        ("Forecast demand for the next quarter", Intent.FORECAST),
        ("Why is my demand unserved?", Intent.NETWORK_STATE_QUERY),
        ("Close DC_WEST", Intent.SCENARIO_ANALYSIS),
    ])
    def test_the_existing_routes_are_untouched(self, nlu, network,
                                               question, expected):
        """
        Widening a vocabulary is how a classifier quietly starts answering the
        wrong question. Each of these belongs to a different branch that the
        new words pass through.
        """
        intent, _s, _src, _c, _r, _e, _f = nlu._classify(
            question, network, [], allow_llm=False)
        assert intent is expected, f"{question} -> {intent.value}"
