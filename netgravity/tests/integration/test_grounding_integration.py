"""
Phase 2 — §13, §14: reasoning and numeric grounding over REAL evidence.

    deterministic evidence → reasoning agent → generated claims
      → numeric grounding → grounded output

The LLM is faked here — deliberately and necessarily. Grounding exists to catch
a model asserting a number the deterministic engines never produced, and a real
model cannot be relied upon to hallucinate on cue. What is NOT faked is the
evidence: every authoritative figure comes from the real MILP and the real REI
engine, so the grounding layer is adjudicating against genuine values.

The §14 case: authoritative cost increase is 16.67%, the model claims 50%.
"""

from __future__ import annotations

import json

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.validation.numeric_grounding import (
    ClaimVerdict,
    build_authoritative_facts,
    ground_narrative,
)

from .conftest import FakeGateway, build_delhi_network, flood_signal, reasoning_json

TOL = 1e-9

#: 5,000 → 50 units. Real MILP outcome: 1,200 → 1,400, i.e. +16.67%.
BINDING_CUT = ScenarioIntentSpec(
    action=ScenarioActionType.CHANGE_CAPACITY,
    facility_ids=["DC_DELHI"], capacity_delta_units=-4_950.0,
    label="Cut DC_DELHI to 50 units",
)


def _scenario_with_gateway(gateway, planner_actor):
    orch = build_orchestrator(network=build_delhi_network(), gateway=gateway)
    return orch, orch.run_sync(OrchestratorRequest(
        input="What happens if we cut Delhi capacity hard?",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[BINDING_CUT],
        actor=planner_actor,
    ))


# ===========================================================================
# §13 — the agent receives real deterministic evidence
# ===========================================================================

class TestReasoningReceivesRealEvidence:

    def test_the_prompt_carries_authoritative_figures(self, planner_actor):
        gateway = FakeGateway({
            "reasoning": reasoning_json(summary="Cost rose by 16.67%.",
                                        evidence=["business_cost_delta_pct = 16.67"]),
        })
        _scenario_with_gateway(gateway, planner_actor)

        [prompt] = [p for c, p in zip(gateway.calls, gateway.prompts)
                    if c == "reasoning"]
        assert '"business_network_cost": 1400.0' in prompt
        assert '"business_cost_delta": 200.0' in prompt
        assert '"business_cost_delta_pct": 16.666667' in prompt
        assert "authoritative" in prompt.lower()

    def test_risk_evidence_reaches_the_agent_in_structured_form(self, planner_actor):
        """§13's example payload: P, REI, RF, node, snapshot and source."""
        gateway = FakeGateway({
            "reasoning": reasoning_json(summary="Delhi risk factor is 0.940."),
        })
        orch = build_orchestrator(network=build_delhi_network(), gateway=gateway)
        orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(), actor=planner_actor,
        ))

        [prompt] = [p for c, p in zip(gateway.calls, gateway.prompts)
                    if c == "reasoning"]
        payload = prompt[prompt.index("DETERMINISTIC RESULTS:"):]

        assert '"risk_factor": 0.94' in payload
        assert '"rei": 0.8' in payload
        assert '"likelihood": 0.7' in payload
        assert '"facility_id": "DC_DELHI"' in payload
        assert "rei_registry:" in payload
        assert orch.snapshots.current_id in payload

    def test_authoritative_facts_are_extracted_from_the_real_result(self, orch,
                                                                    planner_actor):
        response = orch.run_sync(OrchestratorRequest(
            input="Cut Delhi.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[BINDING_CUT], actor=planner_actor, disable_llm=True,
        ))
        facts = build_authoritative_facts({"scenario": response.results["network"]})
        values = {f.value for f in facts.values()}

        assert 1400.0 in values
        assert 200.0 in values
        assert any(abs(v - 16.6667) < 0.01 for v in values)


# ===========================================================================
# §14 — the deliberate hallucination
# ===========================================================================

class TestHallucinationIsCaught:

    def test_a_50_percent_claim_against_a_16_67_percent_truth_is_rejected(
        self, planner_actor,
    ):
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="This change increases cost by 50%, a severe deterioration.",
            recommendation="Reject the proposal outright.",
            confidence="HIGH",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)
        reasoning = response.reasoning

        assert reasoning.source == "llm"
        assert reasoning.grounding_status == "GROUNDING_FAILED"

        verdicts = {c["claim"]: c["verdict"] for c in reasoning.grounded_claims}
        assert ClaimVerdict.CONTRADICTED.value in verdicts.values()

    def test_the_fabricated_figure_never_reaches_the_reader(self, planner_actor):
        """
        A warning is not enough. Someone reading the summary would never see it,
        so the number itself is removed from the text.
        """
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="This change increases cost by 50%, a severe deterioration.",
            recommendation="Reject the proposal outright.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)

        assert "50%" not in response.reasoning.summary
        assert "[unverified]" in response.reasoning.summary.lower() or \
               "50" not in response.reasoning.summary

    def test_confidence_is_downgraded_after_a_failed_grounding(self, planner_actor):
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.", confidence="HIGH",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)
        assert response.reasoning.confidence == "LOW"

    def test_the_deterministic_numbers_are_untouched_by_the_hallucination(
        self, planner_actor,
    ):
        """The model got it wrong; the MILP did not, and its result stands."""
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)
        result = response.results["network"]

        assert result["business_network_cost"] == pytest.approx(1400.0, abs=1e-6)
        assert result["business_cost_delta_pct"] == pytest.approx(16.6667, abs=1e-3)


# ===========================================================================
# §14 — correct, unsupported, and missing-evidence cases
# ===========================================================================

class TestGroundingVerdicts:

    def test_a_correct_claim_passes(self, planner_actor):
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="The scenario increases business cost by 16.67%.",
            recommendation="Review the reroute with a planner.",
            evidence=["business_cost_delta_pct = 16.67"],
            claims=[{"type": "business_cost_delta_pct", "value": 16.6667,
                     "unit": "percent", "text": "16.67%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)

        assert response.reasoning.grounding_status == "GROUNDED"
        assert "16.67%" in response.reasoning.summary
        verdicts = [c["verdict"] for c in response.reasoning.grounded_claims]
        assert ClaimVerdict.GROUNDED.value in verdicts
        assert ClaimVerdict.CONTRADICTED.value not in verdicts

    def test_rounding_to_the_claim_precision_is_allowed(self, planner_actor):
        """"17%" against 16.6667 is legitimate rounding, not a fabrication."""
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost rises about 17% under this scenario.",
            claims=[{"type": "business_cost_delta_pct", "value": 16.6667,
                     "unit": "percent", "text": "17%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)
        assert response.reasoning.grounding_status == "GROUNDED"

    def test_an_unsupported_claim_is_distinguished_from_a_contradicted_one(self, orch):
        """
        Different failures. CONTRADICTED means a real figure was misreported;
        UNSUPPORTED means one was invented where none exists. Both are rejected,
        and the audit must be able to tell them apart.

        The verdict keys on whether a fact of the same KIND exists. With a
        percentage fact present, a wrong percentage is CONTRADICTED; with none
        present, any percentage is UNSUPPORTED.
        """
        with_a_percentage = {"scenario": {"business_network_cost": 1400.0,
                                          "business_cost_delta_pct": 16.666667}}
        contradicted = ground_narrative("Cost rose by 50%.", with_a_percentage)
        assert contradicted.contradicted
        assert not contradicted.unsupported

        no_percentage_at_all = {"scenario": {"business_network_cost": 1400.0}}
        unsupported = ground_narrative(
            "Service level reached 99.4% under this plan.", no_percentage_at_all,
        )
        assert unsupported.unsupported
        assert not unsupported.contradicted
        assert unsupported.failed, "both failure modes block the claim"

    def test_missing_evidence_yields_no_claims_rather_than_invented_ones(self, orch):
        report = ground_narrative("No analysis was produced.", {})
        assert report.status in ("NO_CLAIMS", "GROUNDED")
        assert not report.failed

    def test_the_template_path_is_grounded_too(self, orch, planner_actor):
        """
        "Trust me, the template only states payload values" is not a validation
        strategy. It is checked like anything else.
        """
        response = orch.run_sync(OrchestratorRequest(
            input="Cut Delhi.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[BINDING_CUT], actor=planner_actor, disable_llm=True,
        ))
        assert response.reasoning.source == "template"
        assert response.reasoning.grounding_status in ("GROUNDED", "NO_CLAIMS")
        assert response.reasoning.grounding_status != "GROUNDING_FAILED"


# ===========================================================================
# §14 / §26 — grounding is observable and feeds governance
# ===========================================================================

class TestGroundingObservability:

    def test_the_grounding_event_reports_the_verdict_counts(self, planner_actor):
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        orch, response = _scenario_with_gateway(gateway, planner_actor)
        [event] = orch.get_trace(response.execution_id).events_of(
            events.GROUNDING_COMPLETED
        )

        assert event.detail["grounding_status"] == "GROUNDING_FAILED"
        assert event.detail["claims_contradicted"] >= 1
        assert event.detail["execution_id"] == response.execution_id

    def test_a_failed_grounding_withholds_automation(self, planner_actor):
        """§14 → §15: an unreliable narrative cannot authorise anything."""
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        _, response = _scenario_with_gateway(gateway, planner_actor)

        assert response.governance.evaluated["grounding_failed"] is True
        assert response.governance.classification.value != "AUTO_ACTION"

    def test_no_model_prompt_is_stored_in_the_audit_trail(self, planner_actor):
        """Prompts carry business data and add little forensic value."""
        gateway = FakeGateway({"reasoning": reasoning_json(summary="Fine.")})
        orch, response = _scenario_with_gateway(gateway, planner_actor)
        trace = orch.get_trace(response.execution_id)

        serialised = json.dumps(trace.to_dict(), default=str)
        assert "You are a supply-chain network analyst" not in serialised
        assert all("prompt" not in entry for entry in trace.llm_outputs)


# ===========================================================================
# §3 — the model is never the source of truth
# ===========================================================================

class TestModelIsNeverAuthoritative:

    def test_the_agent_cannot_mutate_the_payload_it_is_given(self, orch):
        payload = {"scenario": {"business_network_cost": 1400.0,
                                "business_cost_delta_pct": 16.6667}}
        before = json.dumps(payload, sort_keys=True)

        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        ReasoningAgent(gateway).reason(payload)

        assert json.dumps(payload, sort_keys=True) == before

    def test_a_model_asserting_feasibility_against_an_infeasible_solver_is_flagged(
        self, orch,
    ):
        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="The network remains feasible and healthy.", confidence="HIGH",
        )})
        result = ReasoningAgent(gateway).reason({
            "scenario": {"solver_status": "INFEASIBLE", "is_feasible": False},
        })

        assert result.confidence == "LOW"
        assert any("solver is authoritative" in w.lower()
                   for w in result.validation_warnings)
