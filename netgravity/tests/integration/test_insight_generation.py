"""
Insights: whether the engine says anything about a network, and whether it is true.

The reported symptom was "insights are not visible for the uploaded dataset".
There were two causes, and only one of them was the wiring.

**Nothing fetched them.** `HOME_INSIGHTS` and `HOME_ACTION_ITEMS` were
initialised empty, read by the Home feed and the deep dive, and written by
nothing — so the feed rendered "No insights have been generated for this network
yet" permanently. `/api/insights` and its client now close that, and the browser
harness covers it.

**And there was almost nothing to fetch.** The deterministic template emitted a
`KPIInsight` for exactly two themes, Cost and Scenario impact, so a solved
baseline network produced ONE insight — "I see the current cost position
clearly" — whatever the network said. An overloaded DC, a missed SLA and
stranded demand all reached the reader as one cost card. The evidence for all of
them was already in the payload and already narrated in prose; nothing was being
made of it.

This module tests the second half: that the themes exist, that each appears only
when its evidence does, that severity is stated by the engine rather than
guessed from wording, and that every figure quoted survives numeric grounding.
"""

from __future__ import annotations

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.reasoning.evidence import twin_reasoning_payload
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.schemas.reasoning import (
    InsightSeverity,
    ReasoningScope,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


@pytest.fixture(scope="module")
def solved():
    """A real solved network and its Digital Twin state."""
    orc = build_orchestrator()
    snapshot = orc.snapshots.register(build_case16_network(), label="insights")
    orc.run_sync(OrchestratorRequest(
        input="baseline", explicit_intent=Intent.NETWORK_STATE_QUERY,
        actor=Actor(actor_id="u", role=ActorRole.PLANNER),
        network_snapshot_id=snapshot.snapshot_id, disable_llm=True))
    refs = orc.twin.list_states(snapshot.snapshot_id)
    assert refs, "the baseline workflow must publish a twin state"
    return orc, orc.twin.materialize(refs[-1].state_id)


def briefing_for(orc, state, scope=ReasoningScope.NETWORK, entity_id=None):
    payload = twin_reasoning_payload(state, scope=scope, entity_id=entity_id,
                                     comparison=None)
    return orc.services["reasoning_agent"].reason(
        payload, unavailable_evidence={},
        provenance={"state_id": state.state_id}, allow_llm=False,
        scope=scope, entity_id=entity_id, user_question="")


def bare_agent() -> ReasoningAgent:
    """An agent with no gateway, for exercising one theme at a time."""
    return ReasoningAgent(gateway=None, runtime=None)


def no_refs(_field):
    return []


# ===========================================================================

class TestASolvedNetworkProducesMoreThanACostCard:

    def test_several_themes_are_reported(self, solved):
        orc, state = solved
        result = briefing_for(orc, state)
        themes = [i.theme for i in result.briefing.kpi_insights]
        assert len(themes) >= 4, f"only produced {themes}"
        assert "Cost" in themes
        assert len(set(themes)) == len(themes), f"themes repeat: {themes}"

    def test_every_quoted_figure_is_grounded(self, solved):
        """
        Each insight states values taken from the evidence pack, so the numeric
        grounding check must pass by construction rather than by luck.
        """
        orc, state = solved
        result = briefing_for(orc, state)
        assert result.grounding_status in {"GROUNDED", "NO_CLAIMS"}, \
            result.validation_warnings
        assert result.validation_warnings == []

    def test_a_cited_threshold_is_not_read_as_a_measurement(self, solved):
        """
        "No open site reaches the 90% threshold" was adjudicated CONTRADICTED
        against `pct_demand_in_sla = 100` — a percentage measured on something
        else entirely — and the 90 was stripped out mid-sentence. A threshold is
        a fact about the configuration, and it now travels with the evidence.
        """
        orc, state = solved
        result = briefing_for(orc, state)
        text = " ".join(i.narrative for i in result.briefing.kpi_insights)
        assert "UNGROUNDED CLAIM REMOVED" not in text

    def test_a_threshold_grounds_wherever_the_payload_came_from(self):
        """
        The thresholds must travel with EVERY payload, not just the twin's.

        They were added to `twin_reasoning_payload` alone, so the orchestrator's
        own payload for `reasoning.synthesise` — the one a scenario comparison
        uses — quoted a threshold it could not ground. Four contradicted claims
        on a real client network, on the path a planner uses most.
        """
        from netgravity.orchestrator.reasoning.evidence import with_policy_thresholds

        payload = {"network_state": {"business_network_cost": 1000.0,
                                     "avg_utilization_pct": 40.0,
                                     "max_utilization_pct": 55.0},
                   "facilities": []}
        result = bare_agent().reason(payload, allow_llm=False)
        assert result.grounding_status in {"GROUNDED", "NO_CLAIMS"}, \
            result.validation_warnings
        assert with_policy_thresholds({})["thresholds"]["utilization_over_pct"] == 90.0

    def test_a_threshold_is_citable_but_never_contradicts_a_measurement(self):
        """
        A configured threshold is a percentage, so once it became citable a
        fabricated "cost increases by 12%" was reported as CONTRADICTED by
        `utilization_under_pct = 30` — a threshold about something else. That
        verdict tells a reader nothing and destroys the distinction the
        validator is careful about: CONTRADICTED means a real measurement was
        misreported, UNSUPPORTED means a figure was invented.
        """
        from netgravity.orchestrator.reasoning.evidence import with_policy_thresholds
        from netgravity.orchestrator.validation.numeric_grounding import (
            build_authoritative_facts, extract_numeric_claims, ground_claims,
        )

        facts = build_authoritative_facts(
            with_policy_thresholds({"rei": {"max_rei": 1.0}}))

        invented = ground_claims(extract_numeric_claims("Cost increases by 12%."), facts)
        verdicts = {c.verdict.value for c in invented.claims}
        assert "UNSUPPORTED" in verdicts, verdicts
        assert "CONTRADICTED" not in verdicts, \
            "a threshold must not stand in for a measurement"

        quoted = ground_claims(
            extract_numeric_claims("No site reaches the 90% threshold."), facts)
        assert all(c.verdict.value in {"GROUNDED", "IGNORED"}
                   for c in quoted.claims), \
            "a narrative must still be able to name the line it is drawing"

    def test_a_cost_reducing_scenario_grounds(self):
        """
        The magnitude of a signed fact.

        The narrative states a direction in words and a quantity in digits —
        "the scenario DECREASES business cost by 8,506,746.48" — while the fact
        holds −8,506,746.48. The two did not match, so the nearest same-kind
        currency fact was picked and the claim reported as CONTRADICTED. That
        happened on EVERY cost-reducing scenario: the briefing was marked
        GROUNDING_FAILED, its confidence dropped to LOW, and the figure was
        stripped out of the sentence — for the outcome a planner is looking for.
        """
        payload = {
            "network_state": {"business_network_cost": 18067793.96},
            "scenario": {"business_cost_delta": -8506746.48,
                         "business_cost_delta_pct": -47.08,
                         "cost_components": {"facility_cost": 8411800.0}},
        }
        result = bare_agent().reason(payload, allow_llm=False)
        assert result.grounding_status == "GROUNDED", result.validation_warnings
        assert "8,506,746.48" in result.summary, \
            "the figure must survive, not be stripped as ungrounded"
        assert "decreases" in result.summary
        assert result.confidence != "LOW"

    def test_a_wrong_magnitude_is_still_caught(self):
        """Accepting a magnitude must not accept the wrong magnitude."""
        from netgravity.orchestrator.validation.numeric_grounding import (
            build_authoritative_facts, extract_numeric_claims, ground_claims,
        )
        facts = build_authoritative_facts(
            {"scenario": {"business_cost_delta": -8506746.48}})
        report = ground_claims(
            extract_numeric_claims("Cost decreases by 9,000,000.00."), facts)
        assert report.status == "GROUNDING_FAILED"
        assert any(c.verdict.value == "CONTRADICTED" for c in report.claims)

    def test_a_facility_scope_speaks_about_that_facility(self, solved):
        orc, state = solved
        facility_id = state.facilities[0].facility_id
        result = briefing_for(orc, state, ReasoningScope.FACILITY, facility_id)
        capacity = [i for i in result.briefing.kpi_insights if i.theme == "Capacity"]
        assert capacity, "a facility view must say something about that facility"
        name = state.facilities[0].facility_name or facility_id
        assert name in capacity[0].headline or name in capacity[0].narrative
        assert result.grounding_status in {"GROUNDED", "NO_CLAIMS"}


class TestSeverityIsStatedByTheEngine:

    def test_unserved_demand_is_a_risk(self):
        agent = bare_agent()
        state = {"total_demand": 1000.0, "unserved_demand": 200.0,
                 "demand_fill_rate": 0.8}
        insights = agent._service_insights(state, no_refs)
        assert insights[0].severity is InsightSeverity.RISK

    def test_demand_fully_served_is_information_not_a_risk(self):
        agent = bare_agent()
        insights = agent._service_insights(
            {"total_demand": 100.0, "unserved_demand": 0.0,
             "demand_fill_rate": 1.0}, no_refs)
        assert insights[0].severity is InsightSeverity.INFORMATION

    def test_an_sla_miss_is_a_risk_even_when_everything_is_served(self):
        agent = bare_agent()
        insights = agent._service_insights(
            {"demand_fill_rate": 1.0, "pct_demand_in_sla": 82.5}, no_refs)
        risks = [i for i in insights if i.severity is InsightSeverity.RISK]
        assert risks, "served late is still a service finding"
        assert "82.50%" in risks[0].narrative

    def test_an_overloaded_site_is_a_risk_and_an_idle_one_an_opportunity(self):
        agent = bare_agent()
        payload = {"facilities": [
            {"facility_id": "D1", "facility_name": "DC One", "is_open": True,
             "utilization_pct": 97.0},
            {"facility_id": "D2", "facility_name": "DC Two", "is_open": True,
             "utilization_pct": 12.0},
            {"facility_id": "D3", "facility_name": "DC Three", "is_open": True,
             "utilization_pct": 8.0},
        ]}
        insights = agent._utilization_insights(
            {"avg_utilization_pct": 39.0, "max_utilization_pct": 97.0},
            payload, no_refs)
        by_theme = {i.theme: i for i in insights}
        assert by_theme["Capacity"].severity is InsightSeverity.RISK
        assert "DC One" in by_theme["Capacity"].narrative
        assert by_theme["Utilisation"].severity is InsightSeverity.OPPORTUNITY
        assert "DC Two" in by_theme["Utilisation"].narrative


class TestAnAbsentMetricProducesNoInsight:
    """
    The rule that keeps this from becoming a filler generator: no metric, no
    insight. Never a zero, never a hedge, and never a sentence built round a
    number nobody measured.
    """

    def test_no_service_metric_yields_no_service_insight(self):
        assert bare_agent()._service_insights({}, no_refs) == []

    def test_no_utilization_yields_no_capacity_insight(self):
        assert bare_agent()._utilization_insights({}, {"facilities": []}, no_refs) == []

    def test_a_single_cost_component_is_not_a_cost_structure(self):
        """Naming the largest of one line says nothing."""
        agent = bare_agent()
        assert agent._cost_structure_insights(
            {"cost_components": {"transport_cost": 100.0}}, no_refs) == []
        assert agent._cost_structure_insights(
            {"cost_components": {"transport_cost": 100.0,
                                 "facility_cost": 40.0}}, no_refs)

    def test_zero_carbon_is_not_reported_as_a_carbon_finding(self):
        agent = bare_agent()
        assert agent._carbon_insights({"total_carbon_kg": 0.0}, no_refs) == []
        assert agent._carbon_insights({"total_carbon_kg": 12.5}, no_refs)

    def test_a_footprint_with_nothing_unselected_is_not_a_finding(self):
        agent = bare_agent()
        assert agent._footprint_insights(
            {"n_facilities_open": 5, "n_facilities_closed": 0}, no_refs) == []
        assert agent._footprint_insights(
            {"n_facilities_open": 5, "n_facilities_closed": 2}, no_refs)


class TestTheRecommendationFollowsFromTheEvidence:
    """
    Every branch used to produce the same sentence — "I recommend reviewing the
    quantified impact above before moving to a formal option appraisal" —
    whether the network stranded a fifth of its demand or ran comfortably.
    """

    def test_unserved_demand_comes_first(self):
        text = bare_agent()._recommendation(
            infeasible=False,
            state={"unserved_demand": 200.0, "total_demand": 1000.0},
            payload={"facilities": []}, negatives=[], insights=[object()])
        assert "unserved demand" in text.lower()

    def test_an_overloaded_site_is_named_next(self):
        payload = {"facilities": [
            {"facility_id": "D1", "is_open": True, "utilization_pct": 96.0}]}
        text = bare_agent()._recommendation(
            infeasible=False, state={"unserved_demand": 0.0},
            payload=payload, negatives=[], insights=[object()])
        assert "utilisation threshold" in text

    def test_a_negative_rei_becomes_a_footprint_review(self):
        text = bare_agent()._recommendation(
            infeasible=False, state={"unserved_demand": 0.0},
            payload={"facilities": []},
            negatives=[{"facility_id": "DC_X", "performance_impact": -5.0}],
            insights=[object()])
        assert "footprint review" in text.lower()
        assert "irreversible" in text.lower()

    def test_idle_sites_become_a_consolidation_test(self):
        payload = {"facilities": [
            {"facility_id": "D1", "is_open": True, "utilization_pct": 9.0},
            {"facility_id": "D2", "is_open": True, "utilization_pct": 11.0},
        ]}
        text = bare_agent()._recommendation(
            infeasible=False, state={"unserved_demand": 0.0},
            payload=payload, negatives=[], insights=[object()])
        assert "consolidation" in text.lower()

    def test_a_healthy_network_is_told_so_explicitly(self):
        text = bare_agent()._recommendation(
            infeasible=False, state={"unserved_demand": 0.0},
            payload={"facilities": [
                {"facility_id": "D1", "is_open": True, "utilization_pct": 55.0}]},
            negatives=[], insights=[object()])
        assert "no structural change" in text.lower()

    def test_no_evidence_recommends_supplying_data_rather_than_acting(self):
        text = bare_agent()._recommendation(
            infeasible=False, state={}, payload={}, negatives=[], insights=[])
        assert "more of the network's data" in text

    def test_an_infeasible_network_is_not_offered_an_option_appraisal(self):
        text = bare_agent()._recommendation(
            infeasible=True, state={}, payload={}, negatives=[], insights=[])
        assert "constraint conflict" in text.lower()

    def test_no_recommendation_states_a_saving_it_has_not_computed(self):
        """
        Naming the next test is a recommendation; naming its result would be an
        invention. No branch may quote a figure, because no scenario has run.
        """
        agent = bare_agent()
        cases = [
            dict(infeasible=True, state={}, payload={}, negatives=[], insights=[]),
            dict(infeasible=False, state={"unserved_demand": 5.0},
                 payload={}, negatives=[], insights=[object()]),
            dict(infeasible=False, state={"unserved_demand": 0.0},
                 payload={"facilities": [
                     {"facility_id": "D", "is_open": True, "utilization_pct": 99.0}]},
                 negatives=[], insights=[object()]),
            dict(infeasible=False, state={"unserved_demand": 0.0},
                 payload={"facilities": []},
                 negatives=[{"facility_id": "X", "performance_impact": -1.0}],
                 insights=[object()]),
        ]
        import re
        # A currency symbol, or a figure attached to a saving or a cost. A
        # recommendation may — and should — say it is NOT stating one; what it
        # may never do is state one.
        forbidden = re.compile(
            r"[₹$€£]|(?:sav(?:e|es|ing)|cost)\s+(?:of\s+)?[\d,.]+", re.IGNORECASE)
        for case in cases:
            text = agent._recommendation(**case)
            match = forbidden.search(text)
            assert match is None, f"{match.group(0)!r} in {text!r}"
