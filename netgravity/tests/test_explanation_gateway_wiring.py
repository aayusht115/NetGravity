"""
The model connection for explanations: one gateway, one credential.

THE CREDENTIAL IS THE SWITCH. `TEXT_API_TOKEN` blank means every explanation
is written by the deterministic template; set means written by the model.
There is no separate on/off flag, because two settings answering one question
eventually disagree — and a token present with a flag off looks exactly like
a token that is missing.

The defect these tests exist for: the forecast and comparison flows built a
bare `ReasoningAgent()`, which holds no gateway, so they produced templates
however anything was configured — while the status said AI was enabled.

Nothing here makes a real request. The transport is faked at `requests.post`.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.backend.app import app
from netgravity.orchestrator.explanation_llm import (
    explanation_gateway,
    explanation_mode,
    explanation_reasoning_agent,
    explanation_status,
    explanations_llm_enabled,
)

API = pathlib.Path(app.root_path) / "api"
ORCH = pathlib.Path("netgravity/orchestrator")

GATEWAY_URL = "https://rapidinsights-openai-gateway-dev.azurewebsites.net"


@pytest.fixture
def no_token(monkeypatch):
    monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
    monkeypatch.delenv("NETGRAVITY_DISABLE_LLM", raising=False)
    return monkeypatch


@pytest.fixture
def with_token(no_token):
    no_token.setenv("TEXT_API_TOKEN", "test-token")
    no_token.setenv("TEXT_API_URL", GATEWAY_URL)
    no_token.setenv("TEXT_API_MODEL", "gpt-5-mini")
    return no_token


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


#: What the reasoning prompt asks for. Prose here would exercise the fallback
#: rather than the wiring; that path is covered separately.
_REASONING_JSON = json.dumps({
    "summary": "Open Nagpur costs less than Expand Delhi while serving the same demand.",
    "key_drivers": ["Lower fixed cost at Nagpur"],
    "risks": [],
    "recommendation": "Review with the people who would carry it out.",
    "confidence": "MEDIUM",
    "evidence": [],
})


@pytest.fixture
def fake_transport(monkeypatch):
    """Captures every outbound request. Nothing leaves the machine."""
    import requests

    calls = []

    def post(url, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json")})
        return _FakeResponse({"output": _REASONING_JSON})

    monkeypatch.setattr(requests, "post", post)
    return calls


class TestTheCredentialIsTheSwitch:

    def test_a_blank_token_means_templates(self, no_token):
        assert explanation_gateway() is None
        assert explanations_llm_enabled() is False
        assert explanation_mode() == "template"

    def test_a_configured_token_means_the_model(self, with_token):
        assert explanation_gateway() is not None
        assert explanations_llm_enabled() is True
        assert explanation_mode() == "llm"

    def test_there_is_no_second_switch(self):
        """
        One question, one answer. A separate flag would eventually disagree
        with the credential, and a token present with the flag off looks
        exactly like a token that is missing.
        """
        source = (ORCH / "explanation_llm.py").read_text(encoding="utf-8")
        assert "NETGRAVITY_EXPLANATIONS_LLM" not in source
        for path in (API / "scenarios.py", API / "forecast.py", API / "insights.py"):
            assert "NETGRAVITY_EXPLANATIONS_LLM" not in path.read_text(encoding="utf-8")

    def test_the_status_reports_the_connection(self, with_token):
        status = explanation_status()
        assert status["available"] is True
        assert status["token_configured"] is True
        assert status["base_url"] == GATEWAY_URL

    def test_disabling_the_llm_tier_wins_over_a_token(self, with_token):
        with_token.setenv("NETGRAVITY_DISABLE_LLM", "1")
        assert explanations_llm_enabled() is False


class TestOnlyTheseThreeVariables:

    def test_no_other_model_credential_is_read_for_reasoning(self):
        """
        The orchestrator reads TEXT_API_* and nothing else. The
        NETGRAVITY_LLM_* keys belong to the INGESTION AI client — a different
        pipeline on a different provider — and must not be borrowed here.
        """
        source = (ORCH / "agents" / "llm_gateway.py").read_text(encoding="utf-8")
        block = source.split("def from_env")[1].split("\n\nclass ")[0]
        assert "TEXT_API_TOKEN" in block
        for foreign in ("OPENAI_API_KEY", "NETGRAVITY_LLM_API_KEY",
                        "NETGRAVITY_OPENAI_API_KEY", "NETGRAVITY_LLM_MODEL"):
            assert foreign not in block, (
                f"the reasoning gateway reads {foreign}, which belongs to "
                f"another subsystem")

    def test_the_removed_openai_gateway_is_gone(self):
        assert not (ORCH / "agents" / "openai_gateway.py").exists()
        for path in (API / "scenarios.py", API / "forecast.py",
                     ORCH / "explanation_llm.py", ORCH / "registry.py"):
            assert "OpenAIGateway" not in path.read_text(encoding="utf-8")

    def test_the_defaults_match_the_configured_values(self):
        """
        The three values in .env are this codebase's own defaults, so a blank
        URL or model cannot silently point somewhere else.
        """
        from netgravity.llm import gateway_contract

        assert gateway_contract.DEFAULT_BASE_URL == GATEWAY_URL
        assert gateway_contract.DEFAULT_MODEL_NAME == "gpt-5-mini"


class TestNoFlowIsLeftWithoutAConnection:

    @pytest.mark.parametrize("filename", ["scenarios.py", "forecast.py"])
    def test_the_standalone_flows_use_the_shared_resolver(self, filename):
        source = (API / filename).read_text(encoding="utf-8")
        assert "explanation_reasoning_agent()" in source
        assert "ReasoningAgent()," not in source, (
            "a bare ReasoningAgent has no gateway and produces templates "
            "however the token is set")

    def test_the_agent_it_builds_holds_the_connection(self, with_token):
        agent = explanation_reasoning_agent()
        assert agent.gateway is not None and agent.gateway.available
        # The agents runtime reaches the model once per metric it cites.
        assert agent.runtime is None

    def test_the_orchestrator_agent_shares_it(self, with_token):
        from netgravity.orchestrator import build_orchestrator
        from netgravity.tests.integration.conftest import build_delhi_network

        agent = build_orchestrator(
            network=build_delhi_network()).services["reasoning_agent"]
        assert agent.gateway is not None and agent.gateway.available


class TestOneRequestPerAnalysisNonePerView:

    def test_the_gateway_path_returns_a_briefing(self, with_token, fake_transport):
        """
        `/api/insights` reads `result.briefing.kpi_insights` directly. This
        path returned None, so the first successful live call 500ed the
        Overview — invisible while the token was blank, because the template
        path always builds one.
        """
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        result = explanation_reasoning_agent().reason(
            {"network_state": {"business_network_cost": 1.0,
                               "demand_fill_rate": 0.9}},
            allow_llm=True, scope=ReasoningScope.NETWORK, single_request=True)

        assert result.source == "llm"
        assert result.briefing is not None
        assert result.briefing.kpi_insights

    def test_one_analysis_one_request_and_views_are_free(
            self, with_token, fake_transport, tmp_path):
        from netgravity.ingestion.storage.local import LocalStorage
        from netgravity.orchestrator.explanation_service import ExplanationService
        from netgravity.orchestrator.explanations import (
            KIND_COMPARISON,
            ExplanationStore,
        )
        from netgravity.orchestrator.reasoning.comparison_evidence import (
            comparison_reasoning_payload,
        )
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        for zone in ("raw", "standardized", "curated"):
            (tmp_path / zone).mkdir(parents=True, exist_ok=True)

        service = ExplanationService(explanation_reasoning_agent(),
                                     ExplanationStore(LocalStorage(tmp_path)))
        payload = comparison_reasoning_payload(
            ranked=[{"scenario_id": "A", "name": "Open Nagpur", "cost": 38.0,
                     "cost_delta": -2.0, "fill_rate": 0.98, "comparable": True},
                    {"scenario_id": "B", "name": "Expand Delhi", "cost": 41.0,
                     "cost_delta": 1.0, "fill_rate": 0.98, "comparable": True}],
            recommended_scenario_id="A", verdict="Open Nagpur is cheapest.")

        def explain():
            return service.explain(
                subject_id="proj1", kind=KIND_COMPARISON,
                scope=ReasoningScope.COMPARISON, result_parts=[["A", "B"], "A"],
                build_payload=lambda: payload, allow_llm=True)

        first = explain()
        assert first["source"] == "llm", "a token is set but nothing reached a model"
        assert len(fake_transport) == 1
        assert fake_transport[0]["url"].startswith(GATEWAY_URL)

        for _ in range(4):
            again = explain()
            assert {k: v for k, v in again.items() if k != "cached"} \
                == {k: v for k, v in first.items() if k != "cached"} \
                or again["card"]["headline"] == first["card"]["headline"]
            assert again["cached"] is True
        assert len(fake_transport) == 1
        assert service.model_requests == 1

    def test_the_switch_does_not_disable_the_insights_cache(self):
        """
        `cacheable = not allow_llm` was right when `?use_llm=1` was the only
        way in. Once a configured token could set it, every view of Optimized
        Results spent a model request.
        """
        source = (API / "insights.py").read_text(encoding="utf-8")
        assert "cacheable = not question and not per_request_llm" in source
        assert "cacheable = not question and not allow_llm" not in source

    def test_the_ranking_does_not_depend_on_request_order(self):
        """
        Ties were resolved by input order, so comparing A,B named a different
        winner than B,A — the same analysis, and a second request for it.
        """
        from app.backend.api.scenarios import _rank_scenarios

        def record(sid, cost):
            kpi = {"value": cost,
                   "status": "VALID" if cost is not None else "NOT_COMPUTABLE"}
            fill = {"value": 0.98, "status": "VALID"}
            return {"id": sid, "name": sid,
                    "baseline_kpis": {"business_network_cost": {"value": 100.0,
                                                                "status": "VALID"},
                                      "demand_fill_rate": fill},
                    "scenario_kpis": {"business_network_cost": kpi,
                                      "demand_fill_rate": fill}}

        for cost in (90.0, None):
            rows = [record("B", cost), record("A", cost)]
            forward = [r["scenario_id"] for r in
                       _rank_scenarios(rows[0]["baseline_kpis"], rows)]
            backward = [r["scenario_id"] for r in
                        _rank_scenarios(rows[0]["baseline_kpis"], list(reversed(rows)))]
            assert forward == backward == ["A", "B"], (cost, forward, backward)
