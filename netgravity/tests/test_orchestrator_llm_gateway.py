"""
NetGravity Orchestrator — Text Generation Gateway client tests.

Verifies the client speaks the documented protocol exactly, WITHOUT spending
shared gateway capacity. The gateway's budget is cumulative and shared across
all consumers ($10 total, 100 requests/day, 20/minute), so a test suite that
made real calls would be both flaky and antisocial.

Every test here mocks the HTTP transport. The one thing that cannot be mocked —
that the endpoint exists — is covered by the unauthenticated `/health` probe,
which consumes no budget and needs no credential.

No credential appears anywhere in this file.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from netgravity.orchestrator.agents.llm_gateway import (
    CONNECT_TIMEOUT,
    MAX_PROMPT_CHARS,
    READ_TIMEOUT,
    RETRYABLE_STATUS,
    LLMGateway,
    LLMGatewayConfig,
)
from netgravity.orchestrator.exceptions import (
    FailureClass,
    LLMFailureError,
    LLMNonRetryableError,
)


class FakeResponse:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, status_code: int, payload: Optional[Dict[str, Any]] = None,
                 text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> Dict[str, Any]:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def gateway():
    return LLMGateway(LLMGatewayConfig(
        base_url="https://gateway.example", token="test-token",
        enabled=True, max_attempts=3, backoff_seconds=0.0,
    ))


def _patch_post(monkeypatch, responses, captured):
    """Install a fake `requests.post` returning queued responses."""
    import requests

    queue = list(responses)

    def fake_post(url, headers=None, json=None, timeout=None, **kwargs):
        captured.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return queue.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestRequestShape:

    def test_sends_documented_request(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {
            "output": "hello", "request_id": "req-1",
            "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
        })], captured)

        result = gateway.generate("Explain demand forecasting.", purpose="test")

        assert len(captured) == 1
        call = captured[0]
        assert call["url"] == "https://gateway.example/v1/generate"
        assert call["headers"]["Authorization"] == "Bearer test-token"
        assert call["headers"]["Content-Type"] == "application/json"
        # The gateway accepts EXACTLY one field.
        assert call["json"] == {"prompt": "Explain demand forecasting."}
        assert list(call["json"].keys()) == ["prompt"]
        # Client timeout must exceed the gateway's 60s processing timeout.
        assert call["timeout"] == (CONNECT_TIMEOUT, READ_TIMEOUT)
        assert READ_TIMEOUT > 60.0

        assert result.output == "hello"
        assert result.request_id == "req-1"
        assert result.usage.total_tokens == 7

    def test_no_unsupported_options_are_sent(self, gateway, monkeypatch):
        """No model, temperature, tools or max_tokens — the API rejects them."""
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {"output": "x"})], captured)
        gateway.generate("hi")
        body = captured[0]["json"]
        for forbidden in ("model", "temperature", "max_tokens", "tools",
                          "messages", "system", "stream"):
            assert forbidden not in body

    def test_usage_accounting_tracked_locally(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [
            FakeResponse(200, {"output": "a", "usage": {"total_tokens": 10}}),
            FakeResponse(200, {"output": "b", "usage": {"total_tokens": 15}}),
        ], captured)
        gateway.generate("one")
        gateway.generate("two")
        stats = gateway.stats()
        assert stats["requests_made"] == 2
        assert stats["total_tokens"] == 25


# ---------------------------------------------------------------------------
# Retry discipline
# ---------------------------------------------------------------------------

class TestRetryDiscipline:

    @pytest.mark.parametrize("status", sorted(RETRYABLE_STATUS))
    def test_transient_statuses_are_retried(self, gateway, monkeypatch, status):
        captured: list = []
        _patch_post(monkeypatch, [
            FakeResponse(status, {"error": "transient", "message": "retry me"}),
            FakeResponse(200, {"output": "recovered"}),
        ], captured)
        assert gateway.generate("hi").output == "recovered"
        assert len(captured) == 2

    def test_retries_are_bounded(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [
            FakeResponse(500, {"error": "server_error", "message": "boom"})
            for _ in range(5)
        ], captured)
        with pytest.raises(LLMFailureError):
            gateway.generate("hi")
        assert len(captured) == gateway.config.max_attempts == 3

    @pytest.mark.parametrize("status,code", [
        (400, "invalid_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (413, "prompt_too_large"),
        (402, "budget_exceeded"),
    ])
    def test_client_errors_are_not_retried(self, gateway, monkeypatch, status, code):
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(status, {"error": code, "message": "no"})],
                    captured)
        with pytest.raises(LLMNonRetryableError) as exc:
            gateway.generate("hi")
        assert len(captured) == 1, "client errors must be attempted exactly once"
        assert exc.value.failure_class == FailureClass.NON_RETRYABLE

    def test_client_timeout_is_never_retried(self, gateway, monkeypatch):
        """
        The gateway accepts no idempotency key, so a client-side timeout may
        already have been processed. Retrying could duplicate output and spend
        shared budget twice.
        """
        import requests
        calls = {"n": 0}

        def timing_out(*args, **kwargs):
            calls["n"] += 1
            raise requests.Timeout("read timed out")

        monkeypatch.setattr(requests, "post", timing_out)

        with pytest.raises(LLMNonRetryableError) as exc:
            gateway.generate("hi")
        assert calls["n"] == 1
        assert "idempotency" in exc.value.message.lower()
        assert exc.value.failure_class == FailureClass.NON_RETRYABLE

    def test_backoff_is_exponential_jittered_and_capped(self):
        """
        The guide requires exponential backoff with jitter. Uses its own
        gateway because the shared fixture sets backoff to 0 for test speed.
        """
        gw = LLMGateway(LLMGatewayConfig(
            base_url="https://gateway.example", token="t",
            backoff_seconds=1.0, max_backoff_seconds=8.0,
        ))
        delays = {gw._backoff(2) for _ in range(30)}
        assert len(delays) > 1, "backoff must be jittered, not constant"
        assert all(0.0 <= d <= 8.0 for d in delays)
        # Ceiling grows exponentially, then caps.
        assert max(gw._backoff(1) for _ in range(50)) <= 1.0
        assert max(gw._backoff(10) for _ in range(50)) <= 8.0


# ---------------------------------------------------------------------------
# Local guards — protect shared capacity
# ---------------------------------------------------------------------------

class TestLocalGuards:

    def test_oversized_prompt_refused_before_sending(self, gateway, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))
        with pytest.raises(LLMNonRetryableError):
            gateway.generate("x" * (MAX_PROMPT_CHARS + 1))

    def test_empty_prompt_refused(self, gateway, monkeypatch):
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))
        with pytest.raises(LLMNonRetryableError, match="empty"):
            gateway.generate("   ")

    def test_per_instance_call_budget_enforced(self, monkeypatch):
        """One runaway execution must not exhaust shared daily capacity."""
        gw = LLMGateway(LLMGatewayConfig(
            base_url="https://gateway.example", token="t", enabled=True,
            max_requests_per_execution=2, backoff_seconds=0.0,
        ))
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {"output": "x"}) for _ in range(5)],
                    captured)
        gw.generate("1")
        gw.generate("2")
        with pytest.raises(LLMNonRetryableError, match="budget"):
            gw.generate("3")
        assert len(captured) == 2

    def test_the_per_execution_budget_is_restored_for_the_next_execution(
            self, monkeypatch):
        """
        The budget is per EXECUTION, and the orchestrator says when one starts.

        It was counted on the gateway instance and never reset. One gateway is
        built per orchestrator and the orchestrator lives as long as the
        server, so the fifth question anyone asked the assistant — for the
        whole life of the process — was refused and silently answered from the
        deterministic template instead.
        """
        gw = LLMGateway(LLMGatewayConfig(
            base_url="https://gateway.example", token="t", enabled=True,
            max_requests_per_execution=2, backoff_seconds=0.0,
        ))
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {"output": "x"}) for _ in range(9)],
                    captured)

        for _ in range(3):
            gw.begin_execution()
            gw.generate("a")
            gw.generate("b")
            with pytest.raises(LLMNonRetryableError, match="per execution"):
                gw.generate("c")

        assert len(captured) == 6, "each execution gets its own budget"
        assert gw.stats()["requests_made_total"] == 6

    def test_a_cumulative_cap_still_guards_a_long_lived_process(self, monkeypatch):
        """Resetting per execution must not remove the runaway guard entirely."""
        gw = LLMGateway(LLMGatewayConfig(
            base_url="https://gateway.example", token="t", enabled=True,
            max_requests_per_execution=2, max_requests_total=3,
            backoff_seconds=0.0,
        ))
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {"output": "x"}) for _ in range(9)],
                    captured)

        gw.begin_execution()
        gw.generate("a")
        gw.generate("b")
        gw.begin_execution()
        gw.generate("c")
        with pytest.raises(LLMNonRetryableError, match="Cumulative"):
            gw.generate("d")
        assert len(captured) == 3


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------

class TestResponseHandling:

    def test_non_json_success_body_rejected(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, None, text="not json")], captured)
        with pytest.raises(LLMFailureError, match="non-JSON"):
            gateway.generate("hi")

    def test_missing_output_field_rejected(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(200, {"request_id": "r"})], captured)
        with pytest.raises(LLMFailureError, match="output"):
            gateway.generate("hi")

    def test_error_body_parsed_for_request_id(self, gateway, monkeypatch):
        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(
            400, {"error": "invalid_request", "message": "bad", "request_id": "req-9"})],
            captured)
        with pytest.raises(LLMNonRetryableError) as exc:
            gateway.generate("hi")
        assert exc.value.context.get("request_id") == "req-9"


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------

class TestCredentialHygiene:

    def test_token_read_only_from_environment(self, monkeypatch):
        monkeypatch.setenv("TEXT_API_TOKEN", "env-token-123")
        monkeypatch.setenv("TEXT_API_URL", "https://configured.example")
        cfg = LLMGatewayConfig.from_env()
        assert cfg.token == "env-token-123"
        assert cfg.base_url == "https://configured.example"

    def test_missing_token_degrades_rather_than_crashes(self, monkeypatch):
        monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
        gw = LLMGateway(LLMGatewayConfig.from_env())
        assert gw.available is False
        assert "deterministic results are unaffected" in gw.unavailable_reason()

    def test_explicit_disable_switch(self, monkeypatch):
        monkeypatch.setenv("TEXT_API_TOKEN", "t")
        monkeypatch.setenv("NETGRAVITY_DISABLE_LLM", "true")
        assert LLMGateway(LLMGatewayConfig.from_env()).available is False

    def test_token_absent_from_stats_and_errors(self, monkeypatch):
        monkeypatch.setenv("TEXT_API_TOKEN", "super-secret-token")
        gw = LLMGateway(LLMGatewayConfig.from_env())
        assert "super-secret-token" not in json.dumps(gw.stats())

        captured: list = []
        _patch_post(monkeypatch, [FakeResponse(401, {"error": "unauthorized",
                                                     "message": "bad token"})], captured)
        with pytest.raises(LLMNonRetryableError) as exc:
            gw.generate("hi")
        blob = json.dumps({"msg": exc.value.message, "ctx": exc.value.context})
        assert "super-secret-token" not in blob

    def test_usage_endpoint_never_raises(self, monkeypatch):
        """Usage is a diagnostic; it must not be able to break a run."""
        monkeypatch.delenv("TEXT_API_TOKEN", raising=False)
        gw = LLMGateway(LLMGatewayConfig.from_env())
        assert "error" in gw.usage()
