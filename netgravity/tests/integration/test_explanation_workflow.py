"""
Phase 2 — Workflow C: explanation query (§12, §21).

    User → Orchestrator → existing evidence (REI, RF, KPI, external P)
      → Reasoning → Numeric Grounding → Explanation

The defining constraint: an explanation reads what has already been computed. It
must not launch a fresh optimization the user did not ask for, and §21 makes
that measurable — REI solve count must be 0 when valid cached evidence exists.

Solve counting is done by wrapping the real MILP entry point, so the count is of
actual solver invocations rather than of anything the cache reports about itself.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.core.planner import WORKFLOW_TEMPLATES
from netgravity.orchestrator.engines.deterministic import REIClient
from netgravity.orchestrator.schemas.plans import StepStatus
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.orchestrator.schemas.risk import RFNotComputableReason
from netgravity.resilience.service import REIService

from .conftest import flood_signal

TOL = 1e-9


class SolveCounter:
    """
    Counts real MILP invocations.

    Wraps `optimization.milp.solve` itself, so nothing between the orchestrator
    and the solver can under-report. `scenario_ids` records what each solve was
    for, which distinguishes an REI batch from a baseline evaluation.
    """

    def __init__(self) -> None:
        self.scenario_ids: List[str] = []

    def __call__(self, network: Any, config: Any = None, scenario_id: Any = None):
        from netgravity.optimization.milp import solve
        self.scenario_ids.append(scenario_id or "(none)")
        return solve(network, config=config, scenario_id=scenario_id)

    @property
    def count(self) -> int:
        return len(self.scenario_ids)


def _counting_orchestrator(network, **kwargs):
    """An orchestrator whose REI service routes every solve through a counter."""
    counter = SolveCounter()
    service = REIService(solve_fn=counter)
    orch = build_orchestrator(network=network, enable_llm=False, **kwargs)
    orch.services["rei"] = REIClient(service=service)
    return orch, counter


def _explain(orch, text="Why is DC_DELHI considered high risk?", **kwargs):
    return orch.run_sync(OrchestratorRequest(input=text, disable_llm=True, **kwargs))


# ===========================================================================
# §12 — the query is recognised and routed to the right workflow
# ===========================================================================

class TestExplanationRouting:

    def test_a_why_question_resolves_to_the_explanation_intent(self, orch):
        resolution = orch.services["intent_agent"].resolve(
            "Why is DC_DELHI considered high risk?",
            known_facility_ids=["DC_DELHI", "DC_MUMBAI"], allow_llm=False,
        )
        assert resolution.intent == Intent.EXPLANATION
        assert resolution.source == "rules"
        assert "DC_DELHI" in resolution.entities

    def test_the_explanation_workflow_contains_no_optimization_step(self):
        """
        Structural proof of §12: the graph has no `optimization.solve`, so an
        explanation CANNOT trigger one regardless of what the request says.
        """
        template = WORKFLOW_TEMPLATES[Intent.EXPLANATION]
        capabilities = {s.capability for s in template.build(None)}  # type: ignore[arg-type]

        assert "optimization.solve" not in capabilities
        assert "optimization.solve_scenario" not in capabilities
        assert "scenario.create" not in capabilities
        assert capabilities == {
            "network.load_snapshot", "resilience.assess",
            "risk.compute_rf", "reasoning.synthesise", "governance.classify",
        }

    def test_an_explanation_query_runs_that_workflow_end_to_end(self, orch):
        response = _explain(orch)
        assert response.intent == Intent.EXPLANATION.value

        by_step = {s["step_id"]: s["status"] for s in response.steps}
        assert by_step == {
            "load": StepStatus.COMPLETED.value,
            "rei": StepStatus.COMPLETED.value,
            "risk": StepStatus.COMPLETED.value,
            "reason": StepStatus.COMPLETED.value,
            "govern": StepStatus.COMPLETED.value,
        }

    def test_a_resilience_query_is_still_routed_separately(self, orch):
        """The new intent must not swallow the existing one."""
        resolution = orch.services["intent_agent"].resolve(
            "Which facility is most exposed?",
            known_facility_ids=["DC_DELHI"], allow_llm=False,
        )
        assert resolution.intent == Intent.RESILIENCE_QUERY


# ===========================================================================
# §21 — no unnecessary recomputation
# ===========================================================================

class TestExplanationUsesExistingEvidence:

    def test_rei_solve_count_is_zero_when_valid_evidence_exists(self, delhi_network):
        """§21's headline assertion, measured at the solver."""
        orch, counter = _counting_orchestrator(delhi_network)

        # Populate the evidence with a genuine assessment first.
        first = orch.run_sync(OrchestratorRequest(
            input="Which facility is most exposed?", disable_llm=True,
        ))
        assert first.intent == Intent.RESILIENCE_QUERY.value
        solves_after_warmup = counter.count
        assert solves_after_warmup > 0, "the first assessment really did solve"

        # Now ask WHY. This must consult evidence, not rebuild it.
        counter.scenario_ids.clear()
        explanation = _explain(orch)

        assert counter.count == 0, (
            f"the explanation query triggered {counter.count} MILP solves: "
            f"{counter.scenario_ids}"
        )
        assert explanation.results["resilience"]["served_from_cache"] is True
        assert explanation.results["resilience"]["n_milp_solves"] == 0

    def test_the_explanation_still_reports_the_real_numbers(self, delhi_network):
        """Zero solves must not mean zero content."""
        orch, _ = _counting_orchestrator(delhi_network)
        orch.run_sync(OrchestratorRequest(
            input="Which facility is most exposed?", disable_llm=True,
        ))
        explanation = _explain(orch)

        registry = explanation.results["resilience"]
        assert registry["rei_by_facility"]["DC_DELHI"] == pytest.approx(0.8, abs=TOL)
        assert registry["baseline_business_cost"] == pytest.approx(1200.0, abs=1e-6)
        assert registry["batch_id"], "cached evidence keeps its batch identity"

    def test_a_cold_explanation_computes_rather_than_inventing(self, delhi_network):
        """
        With no prior evidence there is nothing to read, so the explanation
        assesses. Refusing to compute at all would be as wrong as recomputing
        needlessly — the rule is "don't recompute what you have", not "never
        compute".
        """
        orch, counter = _counting_orchestrator(delhi_network)
        response = _explain(orch)

        assert counter.count > 0
        assert response.results["resilience"]["served_from_cache"] is False
        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)


# ===========================================================================
# §12 — the explanation preserves the categories of knowledge
# ===========================================================================

class TestExplanationContent:

    def test_it_separates_deterministic_results_from_missing_evidence(self, orch):
        response = _explain(orch)
        summary = response.reasoning.summary

        # A deterministic fact, stated as one.
        assert "DC_MUMBAI" in summary
        assert "exposure" in summary.lower()
        # An absent input, named as absent rather than passed over.
        assert "NOT calculated" in summary
        assert RFNotComputableReason.NO_EVENT_PROBABILITY.value in summary

    def test_external_evidence_supplied_with_the_query_is_used(self, orch):
        """
        "Why is Delhi high risk?" asked alongside a live flood warning: the
        explanation combines the existing REI with the supplied P and reports the
        real RF, without gathering any NEW external evidence.
        """
        response = _explain(
            orch, external_signal=flood_signal(probability=0.7, nodes=["DC_DELHI"]),
        )

        row = response.risk["results"][0]
        assert row["facility_id"] == "DC_DELHI"
        assert row["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert "0.940" in response.reasoning.summary
        # No signal-interpretation step ran — the workflow does not gather.
        assert not any(s["capability"] == "external.interpret_signal"
                       for s in response.steps)

    def test_without_a_signal_it_says_so_instead_of_inventing_one(self, orch):
        response = _explain(orch)
        assert response.risk["max_risk_factor"] is None
        assert all(r["likelihood"] is None for r in response.risk["not_computable"])
        assert any("no event probability" in w.lower()
                   for w in response.risk["warnings"])

    def test_the_narrative_is_grounded(self, orch):
        response = _explain(orch)
        assert response.reasoning.grounding_status in ("GROUNDED", "NO_CLAIMS")
        assert response.reasoning.is_grounded

    def test_the_explanation_never_writes_to_observed_state(self, orch):
        before = orch.snapshots.current().network.model_dump_json()
        _explain(orch)
        assert orch.snapshots.current().network.model_dump_json() == before
        assert orch.scenarios.list_ids() == []
