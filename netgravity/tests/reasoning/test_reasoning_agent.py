from __future__ import annotations

from types import SimpleNamespace

import pytest

from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.reasoning.evidence import build_evidence_pack
from netgravity.orchestrator.reasoning.runtime import StubReasoningRuntime
from netgravity.orchestrator.reasoning.validation import validate_reasoning_draft
from netgravity.orchestrator.schemas.reasoning import (
    KPIInsight,
    ReasoningDraft,
    ReasoningScope,
)


def _draft(**changes):
    values = dict(
        scope=ReasoningScope.NETWORK,
        opening="I see a clear cost opportunity in the current network.",
        context="I am comparing the current optimizer result with the available evidence.",
        kpi_insights=[KPIInsight(
            theme="Cost",
            headline="I see business network cost at ₹1,000.00",
            narrative=(
                "I read ₹1,000.00 as the current operating-cost baseline; it is "
                "the reference point for testing future scenarios."
            ),
            metric_refs=["network_state.business_network_cost"],
        )],
        key_drivers=["I see transport as the largest recorded cost component."],
        recommendation="I recommend testing the highest-impact lane next.",
        suggested_questions=["Which lane contributes most to this result?"],
        confidence="HIGH",
        evidence_refs=["network_state.business_network_cost"],
    )
    values.update(changes)
    return ReasoningDraft(**values)


def test_stub_runtime_produces_ui_briefing_and_never_calls_a_live_api():
    runtime = StubReasoningRuntime(_draft())
    agent = ReasoningAgent(runtime=runtime)

    result = agent.reason(
        {"network_state": {"business_network_cost": 1000.0,
                           "transport_cost": 700.0}},
        provenance={"state_id": "tws_1", "snapshot_id": "snap_1"},
    )

    assert len(runtime.calls) == 1
    assert result.source == "openai_agents"
    assert result.briefing is not None
    assert result.briefing.opening.startswith("I ")
    assert result.briefing.kpi_insights[0].metric_refs == [
        "network_state.business_network_cost"
    ]
    assert result.grounding_status == "GROUNDED"


def test_invalid_collective_voice_fails_closed_to_deterministic_template():
    runtime = StubReasoningRuntime(_draft(
        opening="We found a clear cost opportunity.",
        recommendation="We recommend testing it.",
    ))
    result = ReasoningAgent(runtime=runtime).reason(
        {"network_state": {"business_network_cost": 1000.0}}
    )

    assert result.source == "template"
    assert result.summary.startswith("I ")
    assert any("reasoning contract" in warning
               for warning in result.validation_warnings)


def test_unknown_evidence_reference_is_rejected():
    pack = build_evidence_pack({"network_state": {"business_network_cost": 1000.0}})
    draft = _draft(evidence_refs=["invented.metric"])

    errors = validate_reasoning_draft(draft, pack)

    assert any("unknown evidence refs" in error for error in errors)


def test_entity_scope_is_preserved_for_map_level_explanations():
    pack = build_evidence_pack(
        {"facilities": [{"facility_id": "DC_DELHI", "utilization_pct": 72.0}]},
        scope=ReasoningScope.FACILITY,
        entity_id="DC_DELHI",
        user_question="Why is this node important?",
    )

    assert pack.scope is ReasoningScope.FACILITY
    assert pack.entity_id == "DC_DELHI"
    assert pack.user_question == "Why is this node important?"
    assert pack.metrics["facilities.0.utilization_pct"].source == "kpi_engine"


def test_agents_runtime_is_off_by_default(monkeypatch):
    from netgravity.orchestrator.reasoning.runtime import OpenAIAgentsReasoningRuntime

    monkeypatch.delenv("NETGRAVITY_REASONING_RUNTIME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime = OpenAIAgentsReasoningRuntime.from_environment()

    assert runtime.available is False
    assert runtime.stats["calls"] == 0


def test_real_agents_sdk_wiring_uses_structured_output_and_stubbed_runner(monkeypatch):
    agents = pytest.importorskip("agents")
    from netgravity.orchestrator.reasoning.runtime import OpenAIAgentsReasoningRuntime

    captured = {}

    def fake_run_sync(agent, user_input):
        captured["agent"] = agent
        captured["input"] = user_input
        return SimpleNamespace(final_output=_draft())

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-sent")
    monkeypatch.setattr(agents.Runner, "run_sync", staticmethod(fake_run_sync))
    runtime = OpenAIAgentsReasoningRuntime(model="gpt-5", enabled=True)
    pack = build_evidence_pack(
        {"network_state": {"business_network_cost": 1000.0}}
    )

    result = runtime.run(pack)

    assert result.opening.startswith("I ")
    assert captured["agent"].output_type is ReasoningDraft
    assert len(captured["agent"].tools) == 2
    assert "network_state.business_network_cost" in captured["input"]
    assert runtime.stats["calls"] == 1


# ---------------------------------------------------------- horizon phrasing

class TestACostFigureSaysWhatSpanItCovers:
    """
    "per period" was asserted unconditionally about `business_network_cost`.

    That was true while every solve modelled one period. Once a horizon is
    modelled the same sentence describes a twelve-month total as a monthly one
    — a twelvefold overstatement, in the prose a planner acts on, next to a
    figure that is itself correct.
    """

    def _reason(self, network_state):
        agent = ReasoningAgent()
        return agent.reason({"network_state": network_state},
                            allow_llm=False, scope=ReasoningScope.NETWORK)

    def test_a_single_period_solve_still_says_per_period(self):
        result = self._reason({
            "business_network_cost": 1000.0, "periods_modelled": 1,
        })
        assert "1,000.00 per period" in result.summary

    def test_a_horizon_states_the_span_and_the_per_period_figure(self):
        result = self._reason({
            "business_network_cost": 12000.0,
            "periods_modelled": 12,
            "cost_per_period": 1000.0,
        })
        assert "across the 12 periods modelled" in result.summary
        assert "1,000.00 per period" in result.summary
        # The horizon total is never restated as a per-period figure.
        assert "12,000.00 per period" not in result.summary

    def test_the_span_is_stated_even_with_no_per_period_figure(self):
        """
        The period count alone still corrects the sentence. Computing the
        per-period figure here instead would make this a second cost engine.
        """
        result = self._reason({
            "business_network_cost": 12000.0, "periods_modelled": 12,
        })
        assert "across the 12 periods modelled" in result.summary
        assert "per period)" not in result.summary

    def test_the_per_period_figure_survives_numeric_grounding(self):
        """
        The regression this class exists for. `cost_per_period` was not a
        citable fact, so the validator adjudicated the new figure against the
        nearest currency it did know — transport cost — and marked the whole
        narrative CONTRADICTED, which drops the insight from the feed.
        """
        result = self._reason({
            "business_network_cost": 216594606.26,
            "periods_modelled": 12,
            "cost_per_period": 18049550.52,
            "cost_components": {"transport_cost": 10138267.14},
        })
        assert result.grounding_status == "GROUNDED"
        assert result.validation_warnings == []
        assert "18,049,550.52 per period" in result.summary
