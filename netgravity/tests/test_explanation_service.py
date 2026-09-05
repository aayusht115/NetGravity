"""
One model request per completed analysis. Zero per view.

The rule, agreed: **a new analysis can justify a new call; viewing the same
analysis cannot.** These tests hold it as a measured number rather than a
convention — a counting gateway, and assertions on how far it moved.

Two failure modes are guarded specifically:

  * the agent runtime. `OpenAIAgentsReasoningRuntime` instructs the model to
    call `get_evidence` before citing each metric, so a briefing quoting six
    figures costs seven or more requests. "One call" must not mean "one agent
    run that quietly makes several".
  * a stale hit. An explanation keyed to a project rather than to the RESULT
    would survive a re-solve and describe a network that no longer exists.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from netgravity.ingestion.storage.local import LocalStorage
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse
from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
from netgravity.orchestrator.explanation_service import ExplanationService
from netgravity.orchestrator.explanations import (
    KIND_SCENARIO,
    ExplanationStore,
    SavedExplanation,
    fingerprint,
)
from netgravity.orchestrator.schemas.reasoning import ReasoningScope


class CountingGateway(LLMGateway):
    """A model that answers once and counts how often it was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.call_count = 0

    @property
    def available(self) -> bool:            # type: ignore[override]
        return True

    def unavailable_reason(self) -> str:    # type: ignore[override]
        return ""

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:  # type: ignore[override]
        self.call_count += 1
        return LLMResponse(output=json.dumps({
            "summary": "The network serves 98.2% of demand.",
            "key_drivers": ["Demand concentrated in the west"],
            "risks": [],
            "recommendation": "Test added capacity at the tightest site.",
            "confidence": "MEDIUM",
            "evidence": [],
        }))


class LoopingRuntime:
    """Stands in for the agents runtime — the thing that must never be used."""

    def __init__(self) -> None:
        self._calls = 0

    @property
    def available(self) -> bool:
        return True

    def run(self, evidence):        # pragma: no cover - must never be reached
        self._calls += 1
        raise AssertionError(
            "the agent runtime was used for a single_request explanation")


@pytest.fixture
def storage(tmp_path):
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)
    return LocalStorage(tmp_path)


@pytest.fixture
def store(storage):
    return ExplanationStore(storage)


PAYLOAD = {
    "network_state": {
        "business_network_cost": 4_200_000.0,
        "demand_fill_rate": 0.982,
        "total_demand": 100_000.0,
        "served_demand": 98_200.0,
    },
}


def _words(content):
    """The content a reader sees, with the cache marker set aside.

    A stored explanation is byte-identical to the one that produced it EXCEPT
    for `cached`, which is deliberately different — a screen says whether it
    is showing a fresh answer or a stored one.
    """
    stripped = {k: v for k, v in content.items() if k != "cached"}
    card = dict(stripped.get("card") or {})
    card.pop("cached", None)
    if card:
        stripped["card"] = card
    return stripped


def _service(store, *, gateway=None, runtime=None):
    agent = ReasoningAgent(gateway, runtime=runtime)
    return ExplanationService(agent, store)


def _explain(service, calls, *, subject="proj1", result=("exec_1",), allow_llm=True):
    calls.append(1)
    return service.explain(
        subject_id=subject, kind=KIND_SCENARIO, scope=ReasoningScope.SCENARIO,
        result_parts=list(result), build_payload=lambda: dict(PAYLOAD),
        allow_llm=allow_llm,
    )


class TestOneRequestPerAnalysis:

    def test_a_new_analysis_costs_exactly_one_request(self, store):
        gateway = CountingGateway()
        service = _service(store, gateway=gateway)

        content = _explain(service, [])
        assert content, "no explanation was produced"
        assert gateway.call_count == 1

    def test_viewing_the_same_analysis_again_costs_nothing(self, store):
        gateway = CountingGateway()
        service = _service(store, gateway=gateway)

        first = _explain(service, [])
        for _ in range(5):
            again = _explain(service, [])
            assert _words(again) == _words(first), "a view returned different words"
            assert again["cached"] is True, "a stored answer must say it is stored"
        assert first["cached"] is False

        assert gateway.call_count == 1, (
            f"viewing the same analysis spent {gateway.call_count} requests")
        assert service.model_requests == 1

    def test_a_new_service_still_reads_the_saved_explanation(self, store):
        """A reload, or another worker, must not re-ask."""
        gateway = CountingGateway()
        _explain(_service(store, gateway=gateway), [])
        assert gateway.call_count == 1

        second_gateway = CountingGateway()
        content = _explain(_service(store, gateway=second_gateway), [])

        assert content
        assert second_gateway.call_count == 0, (
            "reopening the project re-asked the model")

    def test_a_different_result_is_a_different_analysis(self, store):
        gateway = CountingGateway()
        service = _service(store, gateway=gateway)

        _explain(service, [], result=("exec_1",))
        _explain(service, [], result=("exec_2",))

        assert gateway.call_count == 2

    def test_a_re_solve_never_serves_the_old_words(self, store):
        """
        The stale-hit guard. An explanation about execution 1 must not be
        returned for execution 2, however convenient that would be.
        """
        store.put(SavedExplanation(
            subject_id="proj1", kind=KIND_SCENARIO,
            result_fingerprint=fingerprint("exec_1"),
            content={"opening": "words about the previous solve"},
        ))
        assert store.get("proj1", KIND_SCENARIO, fingerprint("exec_1")) is not None
        assert store.get("proj1", KIND_SCENARIO, fingerprint("exec_2")) is None

    def test_the_same_scenario_set_in_any_order_is_one_analysis(self, store):
        gateway = CountingGateway()
        service = _service(store, gateway=gateway)

        _explain(service, [], result=(["A", "B"],))
        _explain(service, [], result=(["B", "A"],))

        assert gateway.call_count == 1, (
            "comparing A and B is the same analysis as comparing B and A")

    def test_the_evidence_is_not_even_assembled_on_a_hit(self, store):
        gateway = CountingGateway()
        service = _service(store, gateway=gateway)
        built = []

        def build():
            built.append(1)
            return dict(PAYLOAD)

        for _ in range(3):
            service.explain(
                subject_id="proj1", kind=KIND_SCENARIO,
                scope=ReasoningScope.SCENARIO, result_parts=["exec_1"],
                build_payload=build, allow_llm=True)

        assert len(built) == 1, "the payload was rebuilt for a saved explanation"


class TestTheAgentLoopIsForbidden:

    def test_the_looping_runtime_is_never_used(self, store):
        """
        `LoopingRuntime.run` raises if reached. The agents runtime makes one
        model request per metric cited, so a caller promising one request
        cannot use it whatever the environment selects.
        """
        gateway = CountingGateway()
        service = _service(store, gateway=gateway, runtime=LoopingRuntime())

        content = _explain(service, [])

        assert content
        assert gateway.call_count == 1

    def test_the_guard_is_in_the_reasoning_agent_not_only_the_service(self):
        source = pathlib.Path(
            "netgravity/orchestrator/agents/reasoning_agent.py").read_text(
                encoding="utf-8")
        assert "single_request: bool = False" in source
        assert "and not single_request" in source, (
            "the runtime skip must be a condition, not a convention")


class TestItDegradesRatherThanFails:

    def test_no_gateway_falls_back_to_the_template_and_spends_nothing(self, store):
        service = _service(store, gateway=None)

        content = service.explain(
            subject_id="proj1", kind=KIND_SCENARIO, scope=ReasoningScope.SCENARIO,
            result_parts=["exec_1"], build_payload=lambda: dict(PAYLOAD),
            allow_llm=False)

        assert content, "the deterministic path must still explain"
        assert content["source"] == "template"
        assert service.model_requests == 0

    def test_a_failing_payload_returns_nothing_rather_than_raising(self, store):
        service = _service(store, gateway=CountingGateway())

        def explode():
            raise RuntimeError("evidence unavailable")

        assert service.explain(
            subject_id="proj1", kind=KIND_SCENARIO, scope=ReasoningScope.SCENARIO,
            result_parts=["exec_1"], build_payload=explode, allow_llm=True) == {}

    def test_extras_are_saved_beside_the_briefing(self, store):
        """
        The missing-data wording and the eligible suggestions belong to the
        same analysis, so they come from the same record — not a second call.
        """
        service = _service(store, gateway=CountingGateway())
        extras = {"missing_data": [{"display_label": "Opening cost"}],
                  "suggestions": [{"id": "relieve_hottest"}]}

        service.explain(
            subject_id="proj1", kind=KIND_SCENARIO, scope=ReasoningScope.SCENARIO,
            result_parts=["exec_1"], build_payload=lambda: dict(PAYLOAD),
            allow_llm=True, extras=extras)

        saved = store.get("proj1", KIND_SCENARIO, fingerprint("exec_1"))
        assert saved is not None
        assert saved.content["missing_data"] == extras["missing_data"]
        assert saved.content["suggestions"] == extras["suggestions"]
