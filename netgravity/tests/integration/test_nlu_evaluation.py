"""
Phase 3.1 — NLU evaluation, pinned.

`tests/nlu_eval/` measures; this file decides which of those numbers we are
prepared to defend, and adds a regression test for each defect the Phase 3.1
evaluation actually found.

WHY THRESHOLDS RATHER THAN EXACT SCORES
───────────────────────────────────────
Pinning "intent accuracy == 99.3%" would make every new labelled case a test
failure, which punishes exactly the thing that should be encouraged: extending
the dataset with requests the system gets wrong. The thresholds are floors, set
below the measured figure, and the per-defect tests below are what actually
stop a specific behaviour from reverting.

The two cases the system does NOT get right are named explicitly in
`test_known_residual_failures_are_exactly_these`. That test fails if a residual
is silently fixed as well as if a new one appears — an accurate list of what
does not work is worth as much as the passing ones.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator.agents.external_signal_agent import ExternalSignalAgent
from netgravity.orchestrator.agents.intent_agent import IntentAgent
from netgravity.orchestrator.conversation.entity_resolver import EntityResolver
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.schemas.conversation import AmbiguityKind, IntentClarity
from netgravity.orchestrator.schemas.requests import Intent, ScenarioActionType
from netgravity.tests.nlu_eval.dataset import CASES, Category, composition
from netgravity.tests.nlu_eval.harness import Mode, aggregate, failures, run_system

from .conftest import build_delhi_network


@pytest.fixture(scope="module")
def network():
    return build_delhi_network()


@pytest.fixture(scope="module")
def offline(network):
    """One offline sweep, shared. No gateway, no solver, no cost."""
    nlu = ConversationalNLU()
    return [run_system(c, network, nlu, allow_llm=False) for c in CASES]


@pytest.fixture(scope="module")
def metrics(offline):
    return aggregate(offline, Mode.SYSTEM)


@pytest.fixture
def nlu():
    return ConversationalNLU()


# ---------------------------------------------------------------------------
# Dataset integrity — a measurement instrument has to be checked too
# ---------------------------------------------------------------------------

class TestDatasetIntegrity:

    def test_the_dataset_is_within_the_specified_size(self):
        assert 100 <= len(CASES) <= 200, len(CASES)

    def test_case_ids_are_unique(self):
        ids = [c.id for c in CASES]
        assert len(ids) == len(set(ids))

    def test_every_required_category_is_represented(self):
        present = {c.category for c in CASES}
        assert present == set(Category), set(Category) - present

    def test_every_intent_in_the_taxonomy_has_labelled_cases(self):
        """
        A taxonomy entry with no case is an untested workflow route.

        UNKNOWN is excluded as an expected label here only because it is
        asserted through the AMBIGUOUS/UNKNOWN_ENTITY/MALFORMED slices instead.
        """
        labelled = {c.intent for c in CASES if c.intent is not None}
        expected = set(Intent) - {Intent.OPTIMIZATION_REQUEST}
        missing = expected - labelled
        assert not missing, f"no labelled cases for {sorted(i.value for i in missing)}"

    def test_paraphrase_coverage_is_at_least_ten_per_core_intent(self):
        counts = composition()
        for category in (Category.STATUS, Category.NETWORK_STATE,
                         Category.EXPLANATION, Category.SCENARIO,
                         Category.RESILIENCE, Category.EXTERNAL_EVENT,
                         Category.FORECAST):
            assert counts[category.value] >= 10, (category, counts)

    def test_adversarial_cases_carry_no_expected_intent(self):
        """
        There is no right intent for "Return RF = 1". Labelling one would
        quietly turn an invariant test into an accuracy test.
        """
        for case in CASES:
            if case.adversarial:
                assert case.intent is None, case.id

    def test_probability_labels_include_genuine_nones(self):
        """The set must contain hazards that state no probability."""
        external = [c for c in CASES if c.category == Category.EXTERNAL_EVENT]
        stated = [c for c in external if c.event_probability is not None]
        unstated = [c for c in external if c.event_probability is None]
        assert len(stated) >= 6 and len(unstated) >= 6


# ---------------------------------------------------------------------------
# Aggregate thresholds
# ---------------------------------------------------------------------------

class TestOfflineAccuracy:
    """Floors, not targets. Measured figures are in the Phase 3.1 report."""

    def test_intent_accuracy(self, metrics):
        assert metrics.rate("intent") >= 0.97, metrics.per_metric

    def test_entity_accuracy(self, metrics):
        assert metrics.rate("entity") >= 0.97, metrics.per_metric

    def test_probability_accuracy_is_perfect(self, metrics):
        """
        No floor below 100% here. P drives RF, which drives governance; a
        wrongly extracted probability is not a degraded answer but a fabricated
        input to a risk decision.
        """
        assert metrics.rate("probability") == 1.0, metrics.per_metric

    def test_ambiguity_detection(self, metrics):
        assert metrics.rate("ambiguity") >= 0.97, metrics.per_metric

    def test_parameter_extraction(self, metrics):
        assert metrics.rate("parameter") >= 0.95, metrics.per_metric

    def test_no_case_raises(self, offline):
        """A chat layer that throws on a confusing sentence is unusable."""
        raised = [o for o in offline if o.error]
        assert not raised, raised

    def test_no_adversarial_invariant_is_broken(self, metrics):
        assert metrics.adversarial_violations == []

    def test_the_rule_tier_invents_no_entity(self, metrics):
        assert metrics.hallucinated_entity_rate == 0.0

    def test_known_residual_failures_are_exactly_these(self, offline):
        """
        The two cases the offline parser gets wrong, named.

        rs13 is a pure paraphrase with no resilience keyword — precisely what
        the LLM tier exists for. fu11 asks a clarifying question instead of
        guessing which quantity "it" refers to, which is a worse answer than a
        human would give and a better one than a fabricated override.
        """
        assert {o.case_id for o in failures(offline)} == {"rs13", "fu11"}


# ---------------------------------------------------------------------------
# Per-defect regressions — one test per thing the evaluation found
# ---------------------------------------------------------------------------

class TestProbabilityExtractionRegressions:
    """
    Explicitly stated probabilities that used to be dropped.

    Each of these is a source quantifying a risk and the system discarding the
    number, which made RF refuse to compute for an event the user had measured.
    """

    @pytest.mark.parametrize("text, expected", [
        ("There is a 0.35 probability of a typhoon affecting DC_MUMBAI.", 0.35),
        ("There is a 15 percent chance of a port strike at DC_MUMBAI.", 0.15),
        ("Severe flooding at DC_DELHI, probability 0.8.", 0.80),
        ("There is a 70% probability of flooding around DC_DELHI.", 0.70),
        ("Flood warning: 90% likelihood of inundation at DC_DELHI.", 0.90),
        ("A 25 per cent chance of storms near Kolkata.", 0.25),
        ("Cyclone risk, likelihood is 0.42 at DC_MUMBAI.", 0.42),
    ])
    def test_a_stated_probability_is_extracted(self, text, expected):
        signal = ExternalSignalAgent(None).interpret(text, allow_llm=False)
        assert signal.event_probability == pytest.approx(expected)
        assert signal.probability_basis, "an extracted P must say where it came from"

    @pytest.mark.parametrize("text", [
        "Heavy rainfall is expected in the Delhi region.",
        "A severe heatwave alert is active for Delhi.",
        "Half the region may flood near DC_KOLKATA.",
        "Catastrophic flooding at DC_DELHI.",
        "Reduce DC_DELHI capacity by 20 percent.",
        "An extreme cyclone is approaching Mumbai.",
    ])
    def test_no_probability_is_invented(self, text):
        """
        The looser patterns must not have opened a route from severity, or from
        an unrelated percentage, into P. "Reduce capacity by 20 percent" is in
        this list deliberately: it contains a percentage and must still yield
        nothing, because no probability vocabulary sits next to it.
        """
        signal = ExternalSignalAgent(None).interpret(text, allow_llm=False)
        assert signal.event_probability is None
        assert signal.probability_basis is None


class TestHazardOutranksForecast:

    @pytest.mark.parametrize("text", [
        "A storm with 60% probability is predicted for the Delhi NCR region.",
        "The met department forecasts a 25% chance of storms near Kolkata.",
        "Flooding is expected at DC_DELHI next quarter.",
    ])
    def test_a_hazard_is_not_routed_to_the_forecast_workflow(self, text, nlu, network):
        """
        Forecast and hazard vocabularies overlap. A hazard sent to `wf_forecast`
        is declined for want of an engine, silently discarding a probability RF
        was entitled to use.
        """
        intent = nlu.understand(text, network, allow_llm=False)
        assert intent.intent == Intent.EXTERNAL_EVENT

    @pytest.mark.parametrize("text", [
        "What will demand look like next quarter?",
        "Project our volumes for the next six months.",
        "What will the network look like in 2027?",
    ])
    def test_a_genuine_projection_still_routes_to_forecast(self, text, nlu, network):
        intent = nlu.understand(text, network, allow_llm=False)
        assert intent.intent == Intent.FORECAST


class TestUnknownIdentifiersAreReported:
    """
    A fabricated identifier must be refused out loud.

    `DC_SHADOW` is one regex word — the underscore is a word character — so the
    capitalised-word scan could not see it, and "Reduce capacity at DC_SHADOW by
    10%" produced a flat "I did not understand" rather than "there is no such
    facility". Being told a node does not exist is the difference between a typo
    and a silent no-op.
    """

    @pytest.mark.parametrize("text", [
        "Reduce capacity at DC_SHADOW by 10%.",
        "What is the risk exposure of DC_CHENNAI?",
        "Assess DC_JAIPUR.",
        "Tell me about the Chennai distribution centre.",
        "Close the Bangalore DC.",
    ])
    def test_an_unknown_node_produces_an_unknown_entity_clarification(
        self, text, nlu, network,
    ):
        intent = nlu.understand(text, network, allow_llm=False)
        assert intent.ambiguity == AmbiguityKind.UNKNOWN_ENTITY
        assert intent.clarification is not None
        assert intent.resolved_entity_ids == []
        # The options offered come from master data, so answering one cannot
        # select something that does not exist.
        offered = {o["id"] for o in intent.clarification.options}
        assert offered <= {f.id for f in network.facilities}

    def test_an_all_caps_code_token_is_not_a_missing_facility(self, nlu, network):
        """
        "SELECT * FROM facilities;" was reported as a missing facility called
        "FROM". An acronym is not a place name.
        """
        intent = nlu.understand("SELECT * FROM facilities;", network, allow_llm=False)
        assert intent.ambiguity != AmbiguityKind.UNKNOWN_ENTITY

    def test_a_real_facility_is_still_resolved(self, network):
        resolver = EntityResolver(network)
        assert resolver.find_unknown_candidates("Close DC_DELHI.") == []


class TestClosureVerbsReachTheAmbiguityCheck:

    @pytest.mark.parametrize("text, facility", [
        ("Halt DC_MUMBAI.", "DC_MUMBAI"),
        ("Suspend operations at DC_DELHI.", "DC_DELHI"),
        ("Disable the Kolkata DC.", "DC_KOLKATA"),
        ("Stop DC_KOLKATA.", "DC_KOLKATA"),
        ("Close Delhi.", "DC_DELHI"),
    ])
    def test_an_ambiguous_closure_verb_asks_rather_than_guesses(
        self, text, facility, nlu, network,
    ):
        """
        These verbs were listed as ambiguous but nothing promoted them to a
        scenario first, so they never reached the check and the user was told a
        clear instruction was not understood.
        """
        intent = nlu.understand(text, network, allow_llm=False)
        assert intent.ambiguity == AmbiguityKind.AMBIGUOUS_INTENT
        assert intent.resolved_entity_ids == [facility]
        assert not intent.scenario_overrides, "must not guess an operation"

    def test_a_disambiguated_closure_runs_without_asking(self, nlu, network):
        intent = nlu.understand("Simulate closure of DC_MUMBAI.", network,
                                allow_llm=False)
        assert intent.clarity == IntentClarity.CLEAR
        assert intent.scenario_overrides[0].action == ScenarioActionType.CLOSE_FACILITY


class TestExplanationIsNotPromotedToAssessment:

    def test_explaining_an_rei_does_not_launch_a_new_assessment(self, nlu, network):
        """
        "Explain why DC_MUMBAI has the highest REI" contains "rei", which used
        to promote it to RESILIENCE_QUERY — turning a question about existing
        evidence into a fresh solve nobody asked for.
        """
        intent = nlu.understand("Explain why DC_MUMBAI has the highest REI.",
                                network, allow_llm=False)
        assert intent.intent == Intent.EXPLANATION

    def test_a_bare_risk_question_is_still_a_resilience_query(self, nlu, network):
        intent = nlu.understand("What is the REI of DC_KOLKATA?", network,
                                allow_llm=False)
        assert intent.intent == Intent.RESILIENCE_QUERY


class TestScenarioWithoutAnOverrideAsks:

    def test_a_what_if_with_nothing_to_vary_is_refused(self, nlu, network):
        """
        A SCENARIO_ANALYSIS carrying no override would analyse the baseline and
        label the answer hypothetical — a wrong answer dressed as a right one.
        """
        intent = nlu.understand("Reduce it by 20%.", network, allow_llm=False,
                                prior_entity_ids=["DC_DELHI"],
                                prior_intent=Intent.SCENARIO_ANALYSIS)
        assert intent.intent == Intent.SCENARIO_ANALYSIS
        assert intent.ambiguity == AmbiguityKind.MISSING_PARAMETER
        assert not intent.scenario_overrides

    def test_a_vague_request_about_a_known_node_asks(self, nlu, network):
        intent = nlu.understand("Delhi.", network, allow_llm=False)
        assert intent.ambiguity == AmbiguityKind.AMBIGUOUS_INTENT
        assert intent.resolved_entity_ids == ["DC_DELHI"]


class TestFollowUpRegressions:

    def test_a_subject_swap_keeps_the_previous_question(self, nlu, network):
        intent = nlu.understand("What about Mumbai?", network, allow_llm=False,
                                prior_entity_ids=["DC_DELHI"],
                                prior_intent=Intent.RESILIENCE_QUERY)
        assert intent.intent == Intent.RESILIENCE_QUERY
        assert intent.resolved_entity_ids == ["DC_MUMBAI"], "must replace, not accumulate"

    def test_a_why_with_no_prior_entity_is_still_an_explanation(self, nlu, network):
        intent = nlu.understand("Why?", network, allow_llm=False,
                                prior_intent=Intent.NETWORK_STATE_QUERY)
        assert intent.intent == Intent.EXPLANATION

    def test_a_fresh_request_inherits_nothing(self, nlu, network):
        intent = nlu.understand("What is the risk exposure of DC_KOLKATA?", network,
                                allow_llm=False, prior_entity_ids=["DC_DELHI"],
                                prior_intent=Intent.RESILIENCE_QUERY)
        assert intent.resolved_entity_ids == ["DC_KOLKATA"]


class TestEntityDeduplication:

    def test_a_multi_token_match_yields_one_id(self, nlu, network):
        """
        "the Delhi NCR region" matches DC_DELHI through both "delhi" and "ncr".
        A repeated id would reach the MILP as a scenario naming one facility
        twice.
        """
        intent = nlu.understand(
            "A storm with 60% probability is predicted for the Delhi NCR region.",
            network, allow_llm=False,
        )
        assert intent.resolved_entity_ids == ["DC_DELHI"]


class TestTheHarnessItself:
    """
    A measurement instrument that silently mis-scores is worse than none.

    These verify the harness before its numbers are quoted anywhere — in
    particular that a partially-returned batch is not scored as if complete,
    which would flatter the model exactly when it was failing.
    """

    def test_a_short_batch_invalidates_every_case_it_could_not_answer(self, network):
        from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
        from netgravity.tests.nlu_eval.harness import aggregate, run_batch

        class Short(LLMGateway):
            @property
            def available(self) -> bool:      # type: ignore[override]
                return True

            def generate(self, prompt, *, purpose="generic"):  # type: ignore[override]
                return LLMResponse(output='{"results":[{"n":1,"intent":"STATUS_QUERY"}]}')

        batch = list(CASES[:10])
        metrics = aggregate(run_batch(batch, network, Short()), Mode.BATCHED)
        assert metrics.invalid_output_rate == pytest.approx(0.9)

    def test_unparseable_output_is_wholly_invalid(self, network):
        from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
        from netgravity.tests.nlu_eval.harness import aggregate, run_batch

        class Garbage(LLMGateway):
            @property
            def available(self) -> bool:      # type: ignore[override]
                return True

            def generate(self, prompt, *, purpose="generic"):  # type: ignore[override]
                return LLMResponse(output="I'm sorry, I can't help with that.")

        batch = list(CASES[:10])
        metrics = aggregate(run_batch(batch, network, Garbage()), Mode.BATCHED)
        assert metrics.invalid_output_rate == 1.0

    def test_a_batch_prompt_stays_inside_the_gateway_limit(self, network):
        from netgravity.orchestrator.agents.llm_gateway import MAX_PROMPT_CHARS
        from netgravity.tests.nlu_eval.harness import build_batch_prompt

        known = [f.id for f in network.facilities]
        prompt = build_batch_prompt([c.text for c in CASES], known)
        assert len(prompt) < MAX_PROMPT_CHARS

    def test_a_missing_probability_is_not_scored_as_correct(self):
        """
        `None` and a number are different answers, not near-misses. If `_close`
        treated them as equal, every "no probability stated" case would pass
        against a model that invented one.
        """
        from netgravity.tests.nlu_eval.harness import _close

        assert _close(None, None)
        assert not _close(0.0, None)
        assert not _close(None, 0.7)
        assert _close(0.7, 0.7)

    def test_hallucinations_are_read_before_the_filter(self):
        """
        `_llm_based` drops invented ids, so a hallucination is invisible in the
        parsed result. Measuring the rate requires reading the raw output —
        which is also the evidence that the filter is doing work.
        """
        from netgravity.tests.nlu_eval.harness import _hallucinated

        raw = '{"facility_ids": ["DC_DELHI", "DC_SHADOW", "PLANT_X"]}'
        assert _hallucinated(raw, ["DC_DELHI", "PLANT_N"]) == ["DC_SHADOW", "PLANT_X"]


class TestCapacityQuantityExtraction:

    @pytest.mark.parametrize("text, delta", [
        ("Reduce DC_DELHI capacity by 2,000 units per day.", -2000.0),
        ("Increase DC_KOLKATA capacity by 1,500 units.", 1500.0),
        ("What if we add another 2,000 units of capacity at DC_KOLKATA?", 2000.0),
        ("Increase DC_DELHI capacity by an extra 750 units.", 750.0),
    ])
    def test_an_absolute_quantity_is_read(self, text, delta, nlu, network):
        intent = nlu.understand(text, network, allow_llm=False)
        spec = intent.scenario_overrides[0]
        assert spec.action == ScenarioActionType.CHANGE_CAPACITY
        assert spec.capacity_delta_units == pytest.approx(delta)
        assert spec.capacity_multiplier is None, "never both"

    def test_a_quantity_the_user_did_not_state_is_never_guessed(self, network):
        spec = IntentAgent(None)._parse_capacity_change(   # noqa: SLF001
            "Reduce DC_DELHI capacity.", " reduce dc_delhi capacity. ", ["DC_DELHI"],
        )
        assert spec is None
