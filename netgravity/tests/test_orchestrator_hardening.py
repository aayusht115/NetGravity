"""
NetGravity Orchestrator — Hardening Sprint Test Suite
======================================================

Covers the four hardening changes as a control plane, not as isolated
functions:

  1. Dependency execution semantics — HARD blocks, SOFT degrades
  2. Comprehensive orchestrator coverage incl. failure injection & concurrency
  3. Deterministic numeric-claim grounding
  4. External likelihood semantics — severity is never probability

Organised as UNIT → INTEGRATION → E2E within one module, matching the existing
flat `netgravity/tests/` layout rather than imposing a new directory tree.

All tests run offline (`enable_llm=False`) unless they are specifically
exercising model-output handling, which is done with injected fakes so the
shared gateway budget is never spent.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.agents.external_signal_agent import ExternalSignalAgent
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.core.planner import WorkflowPlanner
from netgravity.orchestrator.exceptions import (
    EngineFailureError,
    EngineTimeoutError,
    SolverInfeasibleError,
    ValidationFailureError,
)
from netgravity.orchestrator.governance.action_classifier import (
    ActionClassifier,
    GovernancePolicy,
)
from netgravity.orchestrator.risk.risk_factor import (
    RF_FORMULA,
    assess_network_risk,
    compute_risk_factor,
)
from netgravity.orchestrator.schemas.actions import ActionClassification, ActionType
from netgravity.orchestrator.schemas.plans import (
    DependencyType,
    EvidenceStatus,
    ExecutionMode,
    ExecutionPlan,
    PlanStep,
    StepStatus,
)
from netgravity.orchestrator.schemas.requests import (
    EventSeverity,
    ExternalSignal,
    Intent,
    IntentResolution,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.schemas.risk import (
    RFNotComputableReason,
    RFStatus,
    ReasoningResult,
)
from netgravity.orchestrator.tools.base import NO_RETRY, Capability
from netgravity.orchestrator.validation.numeric_grounding import (
    ClaimKind,
    ClaimVerdict,
    build_authoritative_facts,
    extract_numeric_claims,
    ground_narrative,
    strip_ungrounded_claims,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


# ---------------------------------------------------------------------------
# Fixtures & failure-injection helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def network():
    return build_case16_network()


@pytest.fixture
def orch(network):
    return build_orchestrator(network=network, enable_llm=False)


def inject_failure(orchestrator, capability: str, exc: Exception) -> None:
    """Replace a capability with one that always raises `exc`."""
    async def failing(ctx, req):
        raise exc
    orchestrator.registry.register(
        Capability(name=capability, handler=failing,
                   execution_mode=ExecutionMode.DETERMINISTIC, retry_policy=NO_RETRY),
        replace=True,
    )


def inject_invalid_output(orchestrator, capability: str) -> None:
    """Replace a capability with one returning output that fails validation."""
    async def invalid(ctx, req):
        raise ValidationFailureError(f"{capability} produced malformed output")
    orchestrator.registry.register(
        Capability(name=capability, handler=invalid,
                   execution_mode=ExecutionMode.DETERMINISTIC, retry_policy=NO_RETRY),
        replace=True,
    )


def steps_by_id(response) -> Dict[str, str]:
    return {s["step_id"]: s["status"] for s in response.steps}


# ===========================================================================
# CHANGE 1 — UNIT: dependency semantics
# ===========================================================================

class TestDependencyTypeModel:

    def test_edges_are_hard_by_default(self):
        plan = ExecutionPlan(workflow_id="w", intent="T", steps=[
            PlanStep(step_id="a", capability="c"),
            PlanStep(step_id="b", capability="c", depends_on=["a"]),
        ])
        assert plan.dependency_type("a", plan.step("b")) == DependencyType.HARD

    def test_explicit_soft_edge(self):
        plan = ExecutionPlan(workflow_id="w", intent="T", steps=[
            PlanStep(step_id="a", capability="c"),
            PlanStep(step_id="b", capability="c",
                     depends_on=["a"], soft_depends_on=["a"]),
        ])
        assert plan.dependency_type("a", plan.step("b")) == DependencyType.SOFT

    def test_edges_from_optional_steps_are_soft_automatically(self):
        """An advisory step's output cannot be mandatory for anyone."""
        plan = ExecutionPlan(workflow_id="w", intent="T", steps=[
            PlanStep(step_id="a", capability="c", optional=True),
            PlanStep(step_id="b", capability="c", depends_on=["a"]),
        ])
        assert plan.dependency_type("a", plan.step("b")) == DependencyType.SOFT

    def test_soft_dep_must_be_a_real_dependency(self):
        with pytest.raises(ValueError, match="not in depends_on"):
            PlanStep(step_id="b", capability="c",
                     depends_on=["a"], soft_depends_on=["ghost"])

    def test_classify_splits_blocking_from_degrading(self):
        plan = ExecutionPlan(workflow_id="w", intent="T", steps=[
            PlanStep(step_id="hard", capability="c"),
            PlanStep(step_id="soft", capability="c", optional=True),
            PlanStep(step_id="target", capability="c", depends_on=["hard", "soft"]),
        ])
        target = plan.step("target")

        both_ok = plan.classify_dependencies(target, {"hard", "soft"})
        assert both_ok.runnable and not both_ok.is_degraded

        soft_gone = plan.classify_dependencies(target, {"hard"})
        assert soft_gone.runnable is True
        assert soft_gone.degraded == ["soft"]

        hard_gone = plan.classify_dependencies(target, {"soft"})
        assert hard_gone.runnable is False
        assert hard_gone.blocking == ["hard"]

        both_gone = plan.classify_dependencies(target, set())
        assert both_gone.runnable is False
        assert both_gone.blocking == ["hard"] and both_gone.degraded == ["soft"]


class TestPlannerDependencyDeclarations:

    def test_reasoning_and_governance_never_hard_depend(self, orch):
        """Both must survive any analytic failure."""
        planner = WorkflowPlanner(orch.registry)
        for intent in (Intent.SCENARIO_ANALYSIS, Intent.RESILIENCE_QUERY,
                       Intent.EXTERNAL_EVENT, Intent.NETWORK_STATE_QUERY):
            plan = planner.plan(IntentResolution(intent=intent))
            for step_id in ("reason", "govern"):
                step = plan.step(step_id)
                if step is None:
                    continue
                for dep in step.depends_on:
                    assert plan.dependency_type(dep, step) == DependencyType.SOFT, (
                        f"{intent.value}: {dep} -> {step_id} must be SOFT"
                    )

    def test_scenario_validation_is_hard_for_the_solve(self, orch):
        """Solving an unvalidated scenario would produce untrustworthy numbers."""
        planner = WorkflowPlanner(orch.registry)
        plan = planner.plan(IntentResolution(intent=Intent.SCENARIO_ANALYSIS))
        solve = plan.step("optimize_scenario")
        assert plan.dependency_type("validate_scenario", solve) == DependencyType.HARD
        # ...but the baseline is only needed for the delta.
        assert plan.dependency_type("baseline", solve) == DependencyType.SOFT

    def test_rf_inputs_are_soft_so_it_can_report_not_computable(self, orch):
        planner = WorkflowPlanner(orch.registry)
        plan = planner.plan(IntentResolution(intent=Intent.EXTERNAL_EVENT))
        risk = plan.step("risk")
        assert plan.dependency_type("rei", risk) == DependencyType.SOFT
        assert plan.dependency_type("interpret_signal", risk) == DependencyType.SOFT


# ===========================================================================
# CHANGE 1 — INTEGRATION: cases A–G from the brief
# ===========================================================================

class TestDependencyExecutionSemantics:

    def test_case_a_optional_dependency_succeeds(self, orch):
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))
        statuses = steps_by_id(resp)
        assert statuses["rei"] == "COMPLETED"
        assert statuses["reason"] == "COMPLETED"
        assert statuses["govern"] == "COMPLETED"

    def test_case_b_optional_failure_does_not_block_downstream(self, orch):
        inject_failure(orch, "resilience.assess", EngineFailureError("REI engine down"))
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))
        statuses = steps_by_id(resp)

        assert statuses["rei"] == "FAILED"
        assert statuses["reason"] == "COMPLETED", "soft failure must not block reasoning"
        assert statuses["govern"] == "COMPLETED", "soft failure must not block governance"
        assert resp.summary, "a narrative must still be produced"

    def test_case_c_required_failure_blocks_downstream(self, orch):
        inject_failure(orch, "scenario.validate", EngineFailureError("validator down"))
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))
        statuses = steps_by_id(resp)

        assert statuses["validate_scenario"] == "FAILED"
        assert statuses["optimize_scenario"] == "BLOCKED", (
            "MILP must not run on an unvalidated scenario"
        )
        # Reasoning/governance still run — they depend softly.
        assert statuses["govern"] == "COMPLETED"
        assert resp.status == "FAILED", "a required step failing fails the run"

    def test_case_d_optional_timeout_yields_explicit_unavailable_evidence(self, orch):
        inject_failure(orch, "resilience.assess", EngineTimeoutError("REI exceeded 600s"))
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))

        assert steps_by_id(resp)["reason"] == "COMPLETED"
        ctx = orch.state_store.get(resp.execution_id)
        evidence = ctx.unavailable_evidence["resilience.assess"]
        assert evidence.status == EvidenceStatus.TIMEOUT
        assert "600s" in evidence.reason
        # And it is visible in the narrative, not silently dropped.
        assert "UNKNOWN" in resp.summary or "not complete" in resp.summary

    def test_case_e_invalid_output_recorded_as_invalid(self, orch):
        inject_invalid_output(orch, "resilience.assess")
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))

        ctx = orch.state_store.get(resp.execution_id)
        evidence = ctx.unavailable_evidence["resilience.assess"]
        assert evidence.status == EvidenceStatus.INVALID
        assert "malformed" in evidence.reason
        assert steps_by_id(resp)["govern"] == "COMPLETED"

    def test_case_f_all_optional_failures_still_reach_a_final_state(self, orch):
        inject_failure(orch, "resilience.assess", EngineFailureError("REI down"))
        inject_failure(orch, "reasoning.synthesise", EngineFailureError("reasoning down"))
        resp = orch.run_sync(OrchestratorRequest(input="What happens if we close DC_EAST?"))

        statuses = steps_by_id(resp)
        assert statuses["rei"] == "FAILED"
        assert statuses["reason"] == "FAILED"
        assert statuses["govern"] == "COMPLETED", "governance must always run"
        assert resp.status in ("REQUIRES_HUMAN", "REQUIRES_APPROVAL", "COMPLETED")
        assert resp.status != "FAILED", "optional failures are degradation, not failure"
        assert resp.governance is not None

    def test_case_g_governance_is_conservative_without_rei(self, network):
        """
        Missing risk information must make the system MORE conservative.

        Uses a reversible action that would otherwise be auto-approved, so the
        missing-evidence rule is what changes the outcome.
        """
        orch = build_orchestrator(
            network=network, enable_llm=False,
            governance_policy=GovernancePolicy(min_confidence_for_auto="LOW"),
        )
        inject_failure(orch, "resilience.assess", EngineFailureError("REI down"))

        resp = orch.run_sync(OrchestratorRequest(
            input="Shift DC_EAST volume to DC_WEST",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.SHIFT_VOLUME,
                facility_ids=["DC_EAST"], target_facility_id="DC_WEST")],
        ))

        assert resp.governance.classification != ActionClassification.AUTO_ACTION
        assert "R7B_MISSING_CRITICAL_EVIDENCE" in resp.governance.triggered_rules
        assert "resilience.assess" in resp.governance.evaluated["missing_critical_evidence"]

    def test_missing_evidence_never_becomes_zero(self, orch):
        inject_failure(orch, "resilience.assess", EngineFailureError("REI down"))
        resp = orch.run_sync(OrchestratorRequest(input="Which facility is most exposed?"))
        ctx = orch.state_store.get(resp.execution_id)

        # REI is absent, not zero.
        assert "resilience.assess" in ctx.unavailable_evidence
        assert ctx.output_of("resilience.assess") is None
        assert resp.governance.evaluated["rei"] is None


class TestGovernanceMissingEvidenceRules:

    def setup_method(self):
        self.classifier = ActionClassifier()

    def test_missing_critical_evidence_blocks_automation(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW,
            is_feasible=True, cost_impact_pct=0.5, unserved_demand_rate=0.0,
            risk_factor=0.05, confidence="HIGH",
            missing_evidence={"resilience.assess": "UNAVAILABLE: engine down"},
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R7B_MISSING_CRITICAL_EVIDENCE" in decision.triggered_rules

    def test_analytical_output_loses_autonomy_when_critical_evidence_is_missing(self):
        """
        POLICY CHANGE (R7/R7B precedence). This test previously asserted
        AUTO_ACTION here, on the reasoning that "a report that lost one input is
        still just a report". That reasoning was wrong in one specific way: it
        made the system MORE autonomous as evidence got worse, because R7
        returned before R7B could be evaluated.

        A report is the vehicle that carries risk findings to a decision-maker.
        Clearing one for unattended emission while the exposure analysis behind
        it failed is precisely "absence of evidence read as absence of risk".

        The report itself is NOT blocked — see
        `test_missing_evidence_constrains_autonomy_not_information_delivery`.
        Only its autonomy is withdrawn.
        """
        decision = self.classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            missing_evidence={"resilience.assess": "UNAVAILABLE: engine down"},
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R7B_MISSING_CRITICAL_EVIDENCE" in decision.triggered_rules
        assert decision.blocked_by_missing_evidence is True

    def test_analytical_output_is_auto_when_evidence_is_intact(self):
        """The other half: the rule constrains missing evidence, not reports."""
        decision = self.classifier.classify(
            action_type=ActionType.REPORT, confidence="HIGH",
            rei=0.3, risk_factor=0.3,
        )
        assert decision.classification == ActionClassification.AUTO_ACTION
        assert decision.governing_rule == "R7_ANALYTICAL_ONLY"
        assert decision.blocked_by_missing_evidence is False

    def test_structural_still_human_even_with_full_evidence(self):
        decision = self.classifier.classify(
            action_type=ActionType.CLOSE_FACILITY, confidence="HIGH",
            rei=0.01, risk_factor=0.01, cost_impact_pct=0.1,
        )
        assert decision.classification == ActionClassification.HUMAN_ONLY

    def test_non_critical_missing_evidence_does_not_escalate(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH",
            cost_impact_pct=0.5, risk_factor=0.05,
            missing_evidence={"reasoning.synthesise": "UNAVAILABLE: llm down"},
        )
        assert decision.classification == ActionClassification.AUTO_ACTION

    def test_grounding_failure_blocks_automation(self):
        decision = self.classifier.classify(
            action_type=ActionType.REROUTE_FLOW, confidence="HIGH",
            cost_impact_pct=0.5, risk_factor=0.05, grounding_failed=True,
        )
        assert decision.classification == ActionClassification.APPROVAL_REQUIRED
        assert "R7C_GROUNDING_FAILED" in decision.triggered_rules


# ===========================================================================
# CHANGE 3 — UNIT: numeric grounding
# ===========================================================================

class TestNumericExtraction:

    @pytest.mark.parametrize("text,expected_value,expected_kind", [
        ("cost rises 14.3%", 14.3, ClaimKind.PERCENTAGE),
        ("service at 96.8 percent", 96.8, ClaimKind.PERCENTAGE),
        ("a change of 14.3 percentage points", 14.3, ClaimKind.PERCENTAGE),
        ("12,400 units unserved", 12400.0, ClaimKind.UNITS),
        ("$1,234.56 total", 1234.56, ClaimKind.CURRENCY),
        ("REI of 0.94", 0.94, ClaimKind.RATIO),
    ])
    def test_extracts_common_forms(self, text, expected_value, expected_kind):
        claims = extract_numeric_claims(text)
        assert claims, f"nothing extracted from {text!r}"
        match = next((c for c in claims if abs(c.value - expected_value) < 1e-6), None)
        assert match is not None, f"{expected_value} not found in {[c.value for c in claims]}"
        assert match.kind == expected_kind

    def test_grouped_number_parsed_whole(self):
        """Regression: 1,000.00 was once mangled into 0.00."""
        claims = extract_numeric_claims("Business network cost is 1,000.00 per period.")
        assert [c.value for c in claims] == [1000.0]

    def test_indian_currency_scale_applied(self):
        claims = extract_numeric_claims("costs ₹12.4 crore this year")
        assert any(abs(c.value - 124_000_000.0) < 1.0 for c in claims), (
            f"crore scale not applied: {[c.value for c in claims]}"
        )

    def test_no_numbers_yields_no_claims(self):
        assert extract_numeric_claims("Costs increased materially.") == []


class TestAuthoritativeFacts:

    def test_facts_collected_from_nested_payload(self):
        facts = build_authoritative_facts({
            "scenario": {
                "business_network_cost": 162665.58,
                "business_cost_delta_pct": 7.99,
                "cost_components": {"transport_cost": 45890.0},
            },
            "rei": {"max_rei": 1.0},
        })
        keys = set(facts)
        assert any(k.endswith("business_network_cost") for k in keys)
        assert any(k.endswith("business_cost_delta_pct") for k in keys)
        assert any(k.endswith("transport_cost") for k in keys)
        assert any(k.endswith("max_rei") for k in keys)

    def test_sources_are_attributed(self):
        facts = build_authoritative_facts({
            "a": {"business_network_cost": 1.0},
            "b": {"max_rei": 0.5},
            "c": {"risk_factor": 0.9},
            "d": {"pct_demand_in_sla": 96.8},
        })
        by_source = {f.source for f in facts.values()}
        assert {"optimization_result", "rei_engine", "risk_engine", "kpi_engine"} <= by_source

    def test_unlisted_fields_are_not_citable(self):
        facts = build_authoritative_facts({"x": {"some_random_number": 42.0}})
        assert facts == {}


class TestClaimGrounding:

    PAYLOAD = {"scenario": {"business_cost_delta_pct": 14.3,
                            "business_network_cost": 162665.58}}

    def test_exact_match_passes(self):
        report = ground_narrative("Cost increases by 14.3%.", self.PAYLOAD)
        assert report.status == "GROUNDED"
        assert report.grounded and not report.failed

    def test_rounding_passes(self):
        for text in ("14.30%", "14.300%", "14%"):
            report = ground_narrative(f"Cost increases by {text}.", self.PAYLOAD)
            assert report.status == "GROUNDED", f"{text} should ground against 14.3"

    @pytest.mark.parametrize("wrong", ["50%", "15.8%", "1.4%"])
    def test_wrong_number_fails(self, wrong):
        report = ground_narrative(f"Cost increases by {wrong}.", self.PAYLOAD)
        assert report.status == "GROUNDING_FAILED"
        assert report.contradicted
        claim = report.contradicted[0]
        assert claim.matched_value == pytest.approx(14.3)
        assert claim.verdict == ClaimVerdict.CONTRADICTED

    def test_unsupported_number_rejected(self):
        """No cost value exists at all, so any cost figure is fabricated."""
        report = ground_narrative("Cost increases by 12.4%.",
                                  {"rei": {"max_rei": 1.0}})
        assert report.status == "GROUNDING_FAILED"
        assert report.unsupported
        assert report.unsupported[0].verdict == ClaimVerdict.UNSUPPORTED

    def test_multiple_claims_are_adjudicated_individually(self):
        payload = {"scenario": {"business_cost_delta_pct": 14.3},
                   "kpis": {"pct_demand_in_sla": 96.8}}
        report = ground_narrative(
            "Cost increases by 14.3% and service holds at 55.5%.", payload,
        )
        assert report.status == "GROUNDING_FAILED"
        grounded = {c.value for c in report.grounded}
        contradicted = {c.value for c in report.contradicted}
        assert 14.3 in grounded, "the correct claim must still be accepted"
        assert 55.5 in contradicted, "the incorrect claim must not slip through"

    def test_ratio_and_percentage_are_interchangeable(self):
        payload = {"kpis": {"demand_fill_rate": 0.968}}
        assert ground_narrative("Fill rate is 96.8%.", payload).status == "GROUNDED"
        assert ground_narrative("Fill rate is 0.968.", payload).status == "GROUNDED"

    def test_qualitative_reasoning_does_not_fail(self):
        report = ground_narrative(
            "Closing this facility materially increases cost and reduces resilience.",
            self.PAYLOAD,
        )
        assert not report.failed
        assert report.status in ("NO_CLAIMS", "GROUNDED")

    def test_bare_counts_are_ignored_not_policed(self):
        """"3 facilities" is an ordinal, not an assertion about results."""
        report = ground_narrative("There are 3 facilities open.", self.PAYLOAD)
        assert not report.failed
        assert all(c.verdict == ClaimVerdict.IGNORED for c in report.claims)

    def test_accepted_claims_carry_provenance(self):
        report = ground_narrative(
            "Cost increases by 14.3%.", self.PAYLOAD,
            provenance={"execution_id": "e1", "snapshot_id": "s1", "scenario_id": "sc1"},
        )
        claim = report.grounded[0]
        assert claim.provenance["execution_id"] == "e1"
        assert claim.provenance["snapshot_id"] == "s1"
        assert claim.provenance["scenario_id"] == "sc1"
        assert claim.provenance["source"] == "optimization_result"
        assert claim.matched_fact.endswith("business_cost_delta_pct")

    def test_structured_claims_are_validated_too(self):
        report = ground_narrative(
            "Cost rose substantially.", self.PAYLOAD,
            structured_claims=[{"type": "cost_change_pct", "value": 50.0,
                                "unit": "percent", "text": "50%"}],
        )
        assert report.status == "GROUNDING_FAILED"

    def test_strip_replaces_rather_than_deletes(self):
        report = ground_narrative("Cost increases by 50%.", self.PAYLOAD)
        cleaned = strip_ungrounded_claims("Cost increases by 50%.", report)
        assert "50%" not in cleaned
        assert "UNGROUNDED CLAIM REMOVED" in cleaned


class TestReasoningAgentGrounding:

    class FakeGateway:
        """Stand-in returning a fixed model response. Spends no budget."""
        def __init__(self, output: str):
            self._output = output
            self.available = True

        def unavailable_reason(self) -> str:
            return ""

        def generate(self, prompt: str, *, purpose: str = "generic"):
            from netgravity.orchestrator.agents.llm_gateway import LLMResponse
            return LLMResponse(output=self._output, request_id="fake")

    PAYLOAD = {"scenario": {"business_cost_delta_pct": 14.3, "is_feasible": True}}

    def test_correct_model_claim_is_accepted(self):
        agent = ReasoningAgent(self.FakeGateway(
            '{"summary": "Closing Delhi increases cost by 14.3%.", '
            '"recommendation": "Review.", "confidence": "HIGH", "evidence": ["14.3%"]}'
        ))
        result = agent.reason(self.PAYLOAD)
        assert result.source == "llm"
        assert result.grounding_status == "GROUNDED"
        assert "14.3%" in result.summary

    def test_wrong_model_claim_is_removed_not_returned_as_fact(self):
        agent = ReasoningAgent(self.FakeGateway(
            '{"summary": "Closing Delhi increases cost by 50%.", '
            '"recommendation": "Proceed.", "confidence": "HIGH", "evidence": ["50%"]}'
        ))
        result = agent.reason(self.PAYLOAD)

        assert result.grounding_status == "GROUNDING_FAILED"
        assert "50%" not in result.summary, "the false figure must not survive"
        assert "UNGROUNDED CLAIM REMOVED" in result.summary
        assert result.confidence == "LOW"
        assert any("CONTRADICTED" in w for w in result.validation_warnings)

    def test_fabricated_number_rejected_when_no_such_fact_exists(self):
        agent = ReasoningAgent(self.FakeGateway(
            '{"summary": "Cost increases by 12%.", "recommendation": "x", '
            '"confidence": "HIGH", "evidence": []}'
        ))
        result = agent.reason({"rei": {"max_rei": 1.0}})
        assert result.grounding_status == "GROUNDING_FAILED"
        assert "12%" not in result.summary
        assert any("UNSUPPORTED" in w for w in result.validation_warnings)

    def test_template_path_grounds_cleanly(self):
        """The deterministic fallback only cites payload values, so it must pass."""
        agent = ReasoningAgent(gateway=None)
        result = agent.reason({
            "network_state": {"business_network_cost": 162665.58,
                              "unserved_demand": 0.0, "is_feasible": True},
        })
        assert result.source == "template"
        assert result.is_grounded

    def test_template_names_missing_evidence(self):
        agent = ReasoningAgent(gateway=None)
        result = agent.reason(
            {"network_state": {"business_network_cost": 1000.0, "is_feasible": True}},
            unavailable_evidence={"resilience.assess":
                                  {"status": "TIMEOUT", "reason": "engine timeout"}},
        )
        assert "UNKNOWN" in result.summary
        assert "resilience.assess" in result.summary
        assert result.unavailable_evidence


# ===========================================================================
# CHANGE 4 — UNIT: external likelihood semantics
# ===========================================================================

class TestExternalSignalSemantics:

    def test_severity_and_probability_are_separate_fields(self):
        signal = ExternalSignal(
            event_type="FLOOD", severity=EventSeverity.SEVERE,
            event_probability=0.4, confidence=0.95,
        )
        assert signal.severity == EventSeverity.SEVERE
        assert signal.event_probability == 0.4
        assert signal.confidence == 0.95
        # High confidence in a LOW probability is entirely coherent.
        assert signal.confidence > signal.event_probability

    def test_legacy_likelihood_field_is_rejected_with_migration_message(self):
        with pytest.raises(ValueError, match="event_probability"):
            ExternalSignal(event_type="FLOOD", likelihood=0.7)

    def test_probability_range_enforced(self):
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            ExternalSignal(event_type="FLOOD", event_probability=1.2)
        with pytest.raises(ValueError, match=r"\[0, 1\]"):
            ExternalSignal(event_type="FLOOD", event_probability=-0.1)

    def test_probability_defaults_to_none_not_zero(self):
        signal = ExternalSignal(event_type="FLOOD", severity=EventSeverity.CRITICAL)
        assert signal.event_probability is None
        assert signal.has_defensible_probability is False


class TestSignalInterpretation:

    def setup_method(self):
        self.agent = ExternalSignalAgent(gateway=None)

    def test_severity_words_never_produce_a_probability(self):
        """The core correction: 'severe' is not 0.7."""
        for text in ("Severe flooding is expected around DC_EAST.",
                     "A major storm is forecast.",
                     "Critical infrastructure failure warning.",
                     "High risk of disruption reported."):
            signal = self.agent.interpret(text, known_facility_ids=["DC_EAST"])
            assert signal.event_probability is None, (
                f"severity wrongly converted to probability for: {text!r}"
            )

    def test_severity_is_still_captured(self):
        signal = self.agent.interpret("Severe flooding expected.", known_facility_ids=[])
        assert signal.severity == EventSeverity.SEVERE

    @pytest.mark.parametrize("text,expected", [
        ("There is a 70% chance of flooding.", 0.7),
        ("Flooding with 45% probability.", 0.45),
        ("probability of 0.72 for disruption", 0.72),
        ("chance of 30% reported", 0.3),
    ])
    def test_explicit_probability_is_used(self, text, expected):
        signal = self.agent.interpret(text, known_facility_ids=[])
        assert signal.event_probability == pytest.approx(expected)
        assert signal.probability_basis and "stated" in signal.probability_basis

    def test_confidence_is_not_probability(self):
        signal = self.agent.interpret("Severe flooding expected.", known_facility_ids=[])
        assert signal.confidence > 0.0
        assert signal.event_probability is None, (
            "extraction confidence must never leak into event probability"
        )

    def test_no_severity_prior_table_exists(self):
        """Guard against the mapping being reintroduced."""
        import inspect
        from netgravity.orchestrator.agents import external_signal_agent as mod
        src = inspect.getsource(mod)
        assert "_SEVERITY_PRIOR" not in src


class TestRFComputability:

    def test_explicit_probability_and_rei_computes(self):
        result = compute_risk_factor(0.7, 0.8)
        assert result.status == RFStatus.COMPUTED
        assert result.risk_factor == pytest.approx(0.94)
        assert result.formula == RF_FORMULA

    def test_missing_probability_is_not_computable(self):
        result = compute_risk_factor(None, 0.8)
        assert result.status == RFStatus.NOT_COMPUTABLE
        assert result.not_computable_reason == RFNotComputableReason.NO_EVENT_PROBABILITY
        assert result.risk_factor is None
        assert any("never substituted" in n for n in result.notes)

    def test_missing_rei_is_not_computable(self):
        result = compute_risk_factor(0.7, None)
        assert result.status == RFStatus.NOT_COMPUTABLE
        assert result.not_computable_reason == RFNotComputableReason.NO_REI
        assert result.risk_factor is None

    def test_both_missing(self):
        result = compute_risk_factor(None, None)
        assert result.not_computable_reason == RFNotComputableReason.NO_INPUTS

    def test_invalid_probability_still_raises(self):
        """Present-but-invalid is a defect, not missing data."""
        with pytest.raises(ValidationFailureError):
            compute_risk_factor(1.2, 0.5)
        with pytest.raises(ValidationFailureError):
            compute_risk_factor(-0.1, 0.5)

    def test_severity_cannot_be_passed_as_probability(self):
        with pytest.raises(ValidationFailureError):
            compute_risk_factor("SEVERE", 0.5)  # type: ignore[arg-type]

    def test_network_assessment_separates_computed_from_not_computable(self):
        assessment = assess_network_risk(
            rei_by_facility={"A": 0.8, "B": 0.5},
            likelihood_by_facility={"A": 0.7},
        )
        assert [r.facility_id for r in assessment.results] == ["A"]
        assert [r.facility_id for r in assessment.not_computable] == ["B"]
        assert assessment.max_risk_factor == pytest.approx(0.94)


# ===========================================================================
# E2E — acceptance scenarios
# ===========================================================================

class TestEndToEndScenarios:

    def test_scenario_1_facility_closure(self, orch):
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?"))
        statuses = steps_by_id(resp)

        assert resp.intent == "SCENARIO_ANALYSIS"
        assert resp.is_hypothetical is True
        assert statuses["optimize_scenario"] == "COMPLETED"
        assert statuses["kpi"] == "COMPLETED"
        assert statuses["rei"] == "COMPLETED"
        assert statuses["reason"] == "COMPLETED"
        assert resp.governance.classification == ActionClassification.HUMAN_ONLY
        # Base network untouched.
        assert orch.snapshots.current().network.get_facility("DC_EAST").is_forced_closed is False

    def test_scenario_2_compare_scenarios(self, orch):
        resp = orch.run_sync(OrchestratorRequest(
            input="Compare closing DC_EAST versus DC_WEST"))
        assert resp.intent == "SCENARIO_COMPARISON"

        scenario_ids = orch.scenarios.list_ids()
        assert len(scenario_ids) >= 2
        records = [orch.scenarios.get(s) for s in scenario_ids]
        # Same baseline, independent overrides.
        assert len({r.parent_snapshot_id for r in records}) == 1
        overrides = sorted(o for r in records for o in r.overrides)
        assert "CLOSE_FACILITY DC_EAST" in overrides
        assert "CLOSE_FACILITY DC_WEST" in overrides
        for record in records:
            assert len(record.overrides) == 1, "scenarios must not cross-contaminate"

    def test_scenario_3_external_event_with_probability(self, orch):
        resp = orch.run_sync(OrchestratorRequest(
            input="Severe flooding is expected around DC_EAST. Should we act?",
            external_signal=ExternalSignal(
                event_type="FLOOD", location="DC_EAST",
                severity=EventSeverity.SEVERE, event_probability=0.7,
                probability_basis="met office 70% chance",
                confidence=0.9, source="met_office",
                affected_entity_ids=["DC_EAST"]),
        ))
        assert resp.intent == "EXTERNAL_EVENT"
        assert resp.risk["results"], "RF should be computed when P is defensible"
        rf = resp.risk["results"][0]
        assert rf["status"] == "COMPUTED"
        assert rf["likelihood"] == pytest.approx(0.7)

    def test_scenario_3b_external_event_without_probability(self, orch):
        """Severity alone must never manufacture a probability."""
        resp = orch.run_sync(OrchestratorRequest(
            input="Severe flooding is expected around DC_EAST. Should we act?",
            external_signal=ExternalSignal(
                event_type="FLOOD", location="DC_EAST",
                severity=EventSeverity.SEVERE, event_probability=None,
                confidence=0.9, source="met_office",
                affected_entity_ids=["DC_EAST"]),
        ))
        assert resp.risk is not None
        assert resp.risk["results"] == [], "no RF may be produced without P"
        assert resp.risk["not_computable"], "the absence must be reported explicitly"
        reasons = {r["not_computable_reason"] for r in resp.risk["not_computable"]}
        assert "NO_EVENT_PROBABILITY" in reasons
        # And the narrative says so.
        assert "NOT calculated" in resp.summary or "NO defensible probability" in resp.summary

    def test_scenario_4_milp_infeasible(self, orch, network):
        dcs = [f.id for f in network.facilities if f.role.value == "DC"]
        resp = orch.run_sync(OrchestratorRequest(
            input="close every DC",
            explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CLOSE_FACILITY, facility_ids=dcs)],
        ))
        assert resp.status == "INFEASIBLE"
        assert "no feasible solution" in resp.summary.lower()
        # No fabricated KPIs and no retry.
        assert not resp.results.get("kpis")
        solve = [s for s in resp.steps if s["capability"] == "optimization.solve_scenario"]
        assert solve and solve[0]["attempts"] == 1

    def test_scenario_5_llm_unavailable(self, orch):
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?", disable_llm=True))
        assert resp.status == "REQUIRES_HUMAN"
        assert resp.reasoning.source == "template"
        assert resp.results["network"]["business_network_cost"] > 0
        assert resp.reasoning.is_grounded, "the fallback must not invent numbers"

    def test_scenario_6_rei_unavailable(self, orch):
        inject_failure(orch, "resilience.assess", EngineFailureError("REI engine down"))
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?"))

        assert resp.results["network"]["business_network_cost"] > 0, "MILP continues"
        assert steps_by_id(resp)["reason"] == "COMPLETED"
        assert steps_by_id(resp)["govern"] == "COMPLETED"
        assert "resilience.assess" in resp.summary or "UNKNOWN" in resp.summary
        assert any("resilience.assess" in w for w in resp.warnings)

    def test_scenario_7_stale_snapshot(self, orch, network):
        stale_id = orch.snapshots.current_id
        modified = network.model_copy(deep=True)
        modified.facilities[0].capacity_units_per_period += 1234
        modified = modified.model_copy(
            update={"data_version": modified.compute_data_version()})
        orch.snapshots.register(modified)

        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?", network_snapshot_id=stale_id))
        assert resp.status == "STALE"
        assert not orch.scenarios.list_ids(), "no scenario may be created on stale data"

    def test_scenario_8_duplicate_execution(self, orch):
        req = OrchestratorRequest(input="What happens if we close DC_EAST?")
        first = orch.run_sync(req)
        scenarios_after_first = len(orch.scenarios.list_ids())
        second = orch.run_sync(req)

        assert first.execution_id == second.execution_id
        assert len(orch.scenarios.list_ids()) == scenarios_after_first, (
            "a replayed request must not create a second scenario"
        )


class TestConcurrency:

    def test_three_scenarios_run_independently(self, orch):
        async def drive():
            reqs = [
                OrchestratorRequest(
                    input=f"close {fid}",
                    explicit_intent=Intent.SCENARIO_ANALYSIS,
                    explicit_scenarios=[ScenarioIntentSpec(
                        action=ScenarioActionType.CLOSE_FACILITY, facility_ids=[fid])],
                )
                for fid in ("DC_EAST", "DC_WEST", "DC_CENTRAL")
            ]
            return await asyncio.gather(*(orch.run(r) for r in reqs))

        results = asyncio.run(drive())

        assert len({r.execution_id for r in results}) == 3
        assert len({r.scenario_id for r in results}) == 3
        snapshots = {r.network_snapshot_id for r in results}
        assert len(snapshots) == 1, "all must share one baseline snapshot"

        for resp, fid in zip(results, ("DC_EAST", "DC_WEST", "DC_CENTRAL")):
            record = orch.scenarios.get(resp.scenario_id)
            assert record.overrides == [f"CLOSE_FACILITY {fid}"], "no result leakage"

        for fid in ("DC_EAST", "DC_WEST", "DC_CENTRAL"):
            assert orch.snapshots.current().network.get_facility(fid).is_forced_closed is False

    def test_concurrent_failure_does_not_leak_between_runs(self, orch):
        """One run failing must not degrade a sibling."""
        async def drive():
            good = OrchestratorRequest(input="Which facility is most exposed?")
            bad = OrchestratorRequest(
                input="close ghost",
                explicit_intent=Intent.SCENARIO_ANALYSIS,
                explicit_scenarios=[ScenarioIntentSpec(
                    action=ScenarioActionType.CLOSE_FACILITY,
                    facility_ids=["DC_NONEXISTENT"])])
            return await asyncio.gather(orch.run(good), orch.run(bad))

        good_resp, bad_resp = asyncio.run(drive())
        assert good_resp.status == "COMPLETED"
        assert good_resp.results["resilience"]["highest_exposure_facility"]
        assert bad_resp.status in ("FAILED", "REQUIRES_HUMAN")
        assert good_resp.execution_id != bad_resp.execution_id


# ===========================================================================
# INTEGRATION — the four changes working together
# ===========================================================================

class TestIntegratedHardening:

    def test_rei_failure_cascades_to_rf_not_computable_and_conservative_governance(
        self, network,
    ):
        """
        Change 1 + Change 4 together:
        REI unavailable → RF NOT_COMPUTABLE → governance conservative,
        with reasoning still produced from the surviving evidence.
        """
        orch = build_orchestrator(network=network, enable_llm=False)
        inject_failure(orch, "resilience.assess", EngineFailureError("REI engine down"))

        resp = orch.run_sync(OrchestratorRequest(
            input="Severe flooding around DC_EAST",
            external_signal=ExternalSignal(
                event_type="FLOOD", location="DC_EAST",
                severity=EventSeverity.SEVERE, event_probability=0.7,
                probability_basis="stated", confidence=0.9,
                affected_entity_ids=["DC_EAST"]),
        ))

        # Soft dependency: risk still ran.
        assert steps_by_id(resp)["risk"] == "COMPLETED"
        # But produced nothing, because REI is genuinely absent.
        assert resp.risk["results"] == []
        assert resp.risk["max_risk_factor"] is None
        # Reasoning and governance both completed.
        assert steps_by_id(resp)["reason"] == "COMPLETED"
        assert resp.governance is not None
        # And the narrative is grounded despite the degradation.
        assert resp.reasoning.is_grounded

    def test_no_fabrication_anywhere_when_everything_degrades(self, network):
        """
        The hardest case: REI down, LLM off, no event probability.
        Nothing may be invented at any layer.
        """
        orch = build_orchestrator(network=network, enable_llm=False)
        inject_failure(orch, "resilience.assess", EngineFailureError("REI down"))

        resp = orch.run_sync(OrchestratorRequest(
            input="Severe flooding around DC_EAST",
            external_signal=ExternalSignal(
                event_type="FLOOD", location="DC_EAST",
                severity=EventSeverity.CRITICAL, event_probability=None,
                confidence=0.95, affected_entity_ids=["DC_EAST"]),
        ))

        assert resp.risk["max_risk_factor"] is None, "no RF may be invented"
        assert resp.reasoning.is_grounded, "no number may be invented"
        assert resp.governance is not None, "a verdict is always produced"
        assert resp.status != "FAILED"
        # Confidence must reflect how little is actually known.
        assert resp.reasoning.confidence in ("LOW", "MEDIUM")

    def test_audit_trail_records_degradation_and_grounding(self, orch):
        inject_failure(orch, "resilience.assess", EngineTimeoutError("timeout"))
        resp = orch.run_sync(OrchestratorRequest(
            input="What happens if we close DC_EAST?"))

        trace = orch.get_trace(resp.execution_id)
        event_types = {e.event_type for e in trace.events}
        assert "step_degraded" in event_types, "degradation must be auditable"

        rei_invocation = next(
            i for i in trace.tool_invocations if i["capability"] == "resilience.assess")
        assert rei_invocation["success"] is False
        assert rei_invocation["error_code"] == "ENGINE_TIMEOUT"
        assert trace.governance_decision is not None
