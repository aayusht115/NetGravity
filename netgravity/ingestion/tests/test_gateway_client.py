"""
Text Generation Gateway transport tests.

The gateway is not OpenAI-compatible, has a SHARED cumulative budget, and
offers no JSON mode. Each of those creates a way to get things quietly
wrong — spending budget on a request that could never work, retrying
something that will never succeed, or paying twice for one answer. These
tests pin the behaviour that prevents each.

No network: urllib is faked.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from netgravity.ingestion.ai import client as client_module
from netgravity.ingestion.ai.client import (
    GATEWAY_MAX_PROMPT_CHARS,
    LLMClient,
)
from netgravity.ingestion.config import IngestionConfig


@pytest.fixture
def gateway_config():
    config = IngestionConfig()
    config.llm_provider = "gateway"
    config.llm_api_key = "test-token"          # never a real credential
    config.gateway_url = "https://gateway.example/"
    config.llm_max_retries = 3
    return config


def _http_error(status, error=None, message=""):
    body = json.dumps({"error": error, "message": message}).encode() if error \
        else b"not json"
    return urllib.error.HTTPError(
        url="https://gateway.example/v1/generate", code=status, msg="err",
        hdrs=None, fp=io.BytesIO(body))


class _FakeHTTP:
    """Stands in for urllib.request.urlopen. Records what was sent."""

    def __init__(self, responses):
        # Each entry is either a dict (a success body) or an Exception.
        self._responses = list(responses)
        self.requests = []
        self.sleeps = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

        class _Response:
            def __enter__(_self):
                return _self

            def __exit__(_self, *args):
                return False

            def read(_self):
                return json.dumps(outcome).encode()
        return _Response()


@pytest.fixture
def fake_http(monkeypatch):
    def _install(responses):
        fake = _FakeHTTP(responses)
        monkeypatch.setattr(client_module.urllib.request, "urlopen", fake)
        # Retry backoff is real seconds; recording them keeps tests instant
        # while still proving the delays grow.
        monkeypatch.setattr(client_module.time, "sleep",
                            lambda s: fake.sleeps.append(s))
        return fake
    return _install


_OK = {"output": '{"vendor_name": "TransCorp"}',
       "request_id": "req-1",
       "usage": {"input_tokens": 100, "output_tokens": 40,
                 "total_tokens": 140}}


# --- the request shape ------------------------------------------------------

def test_the_body_carries_exactly_one_field(gateway_config, fake_http):
    """The gateway rejects any field other than `prompt`."""
    http = fake_http([_OK])
    LLMClient(gateway_config)._call_gateway("read this", max_tokens=2000)

    body = json.loads(http.requests[0].data.decode())
    assert list(body.keys()) == ["prompt"]


def test_the_token_is_sent_as_a_bearer_header(gateway_config, fake_http):
    http = fake_http([_OK])
    LLMClient(gateway_config)._call_gateway("hello", max_tokens=2000)

    headers = {k.lower(): v for k, v in http.requests[0].header_items()}
    assert headers["authorization"] == "Bearer test-token"
    assert http.requests[0].full_url == "https://gateway.example/v1/generate"


def test_json_is_asked_for_in_the_prompt(gateway_config, fake_http):
    """
    There is no response_format on this provider, so the only way to ask for
    JSON is to say so in the prompt.
    """
    http = fake_http([_OK])
    LLMClient(gateway_config)._call_gateway("extract the rates",
                                            max_tokens=2000)

    prompt = json.loads(http.requests[0].data.decode())["prompt"]
    assert "extract the rates" in prompt
    assert "valid JSON object" in prompt


def test_usage_is_translated_to_the_common_shape(gateway_config, fake_http):
    """
    The gateway says input_tokens/output_tokens; everything downstream
    expects prompt_tokens/completion_tokens. Translating at the boundary
    keeps the ledger provider-agnostic.
    """
    fake_http([_OK])
    llm = LLMClient(gateway_config)
    llm._call_gateway("hi", max_tokens=2000)

    assert llm._last_usage == {"prompt_tokens": 100, "completion_tokens": 40,
                               "total_tokens": 140}


# --- guards that spend nothing ---------------------------------------------

def test_an_oversized_prompt_is_refused_before_it_is_sent(gateway_config,
                                                          fake_http):
    """
    Sending it would return a 413 and tell us what we could have known for
    free. A rejected request costs no budget, but it costs a round trip and
    an unclear error.
    """
    http = fake_http([_OK])
    with pytest.raises(ValueError, match="above the gateway"):
        LLMClient(gateway_config)._call_gateway(
            "x" * (GATEWAY_MAX_PROMPT_CHARS + 1), max_tokens=2000)
    assert http.requests == [], "nothing should have been sent"


def test_a_missing_url_fails_before_any_request(gateway_config, fake_http):
    gateway_config.gateway_url = None
    http = fake_http([_OK])
    with pytest.raises(RuntimeError, match="NETGRAVITY_GATEWAY_URL"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert http.requests == []


def test_a_missing_token_fails_before_any_request(gateway_config, fake_http):
    gateway_config.llm_api_key = None
    http = fake_http([_OK])
    with pytest.raises(RuntimeError, match="NETGRAVITY_GATEWAY_TOKEN"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert http.requests == []


def test_a_pdf_is_refused_with_the_real_reason(gateway_config):
    """The gateway takes text only — say that, not a generic 'not implemented'."""
    with pytest.raises(NotImplementedError, match="text prompts only"):
        LLMClient(gateway_config)._call_live_with_pdf(
            "read it", pdf_bytes=b"%PDF-", filename="x.pdf", max_tokens=2000)


# --- retrying, and not retrying --------------------------------------------

def test_a_rolling_minute_limit_is_retried(gateway_config, fake_http):
    """That limit clears by itself, so waiting is the correct response."""
    http = fake_http([_http_error(429, "rate_limit_exceeded"), _OK])
    out = LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)

    assert json.loads(out)["vendor_name"] == "TransCorp"
    assert len(http.requests) == 2
    assert len(http.sleeps) == 1


def test_backoff_grows_between_attempts(gateway_config, fake_http):
    http = fake_http([_http_error(500, "internal_error"),
                      _http_error(500, "internal_error"), _OK])
    LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)

    assert len(http.sleeps) == 2
    assert http.sleeps[1] > http.sleeps[0], "delay must grow, not repeat"


def test_an_exhausted_budget_is_never_retried(gateway_config, fake_http):
    """
    The budget is cumulative and shared. Retrying cannot make money appear,
    and each attempt is one more failure in someone else's logs.
    """
    http = fake_http([_http_error(429, "budget_exceeded")])
    with pytest.raises(RuntimeError, match="cumulative"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert len(http.requests) == 1


def test_a_daily_limit_is_never_retried(gateway_config, fake_http):
    http = fake_http([_http_error(429, "daily_limit_exceeded")])
    with pytest.raises(RuntimeError, match="00:00 UTC"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert len(http.requests) == 1


def test_a_client_error_is_never_retried(gateway_config, fake_http):
    """A malformed request stays malformed however many times it is sent."""
    http = fake_http([_http_error(400, "invalid_request", "bad field")])
    with pytest.raises(RuntimeError, match="do not retry"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert len(http.requests) == 1


def test_a_network_timeout_is_never_retried(gateway_config, fake_http):
    """
    THE EXPENSIVE MISTAKE. The call may already have been processed and
    billed, and the gateway accepts no idempotency key — so an automatic
    retry risks paying twice for one answer.
    """
    http = fake_http([urllib.error.URLError("timed out")])
    with pytest.raises(RuntimeError, match="may already have been processed"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert len(http.requests) == 1


def test_a_bad_token_names_the_variable_to_fix(gateway_config, fake_http):
    fake_http([_http_error(401, "invalid_api_key")])
    with pytest.raises(RuntimeError, match="NETGRAVITY_GATEWAY_TOKEN"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)


def test_an_unparseable_error_body_still_produces_a_usable_message(
        gateway_config, fake_http):
    """Auth failures can happen before the JSON error body is even built."""
    fake_http([_http_error(503)])
    with pytest.raises(RuntimeError, match="HTTP 503"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)


def test_an_empty_output_is_an_error_not_an_empty_extraction(gateway_config,
                                                             fake_http):
    """Returning "" would parse as a failed extraction with no explanation."""
    fake_http([{"output": "", "request_id": "req-9", "usage": {}}])
    with pytest.raises(ValueError, match="no output"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)


# --- end to end through the public API --------------------------------------

def test_extract_json_works_end_to_end_over_the_gateway(gateway_config,
                                                        fake_http):
    fake_http([_OK])
    response = LLMClient(gateway_config).extract_json(
        task="contract extraction", prompt="read this", stub_key="contract")

    assert response.stubbed is False
    assert response.failed is False
    assert response.data["vendor_name"] == "TransCorp"
    assert response.tokens["total_tokens"] == 140
    assert response.model == "gateway:gpt-5-mini"


def test_a_chatty_reply_still_parses(gateway_config, fake_http):
    """
    Without JSON mode the model may wrap its answer in prose or fences. The
    parser already tolerates both — this pins that it still does for the
    gateway, where it is the only defence.
    """
    fake_http([{"output": 'Sure!\n```json\n{"vendor_name": "X"}\n```',
                "request_id": "r", "usage": {}}])
    response = LLMClient(gateway_config).extract_json(
        task="t", prompt="p", stub_key="contract")

    assert response.data["vendor_name"] == "X"


def test_a_gateway_failure_degrades_to_a_labelled_stub(gateway_config,
                                                       fake_http):
    """Same honest degradation as every other provider."""
    fake_http([_http_error(429, "budget_exceeded")])
    response = LLMClient(gateway_config).extract_json(
        task="contract extraction", prompt="p", stub_key="contract")

    assert response.failed is True
    assert response.stubbed is True
    assert client_module.LLM_FAILURE_MARKER in response.notes


def test_a_leftover_vendor_url_is_refused_with_the_real_reason(gateway_config,
                                                               fake_http):
    """
    NETGRAVITY_LLM_BASE_URL usually holds a vendor address (Gemini's
    OpenAI-compatible endpoint, say). If that value reaches the gateway
    transport, POSTing the gateway's request shape at a vendor produces a
    confusing 4xx that reads like a gateway fault. Name the real cause.
    """
    gateway_config.gateway_url = \
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    http = fake_http([_OK])

    with pytest.raises(RuntimeError, match="points at a model vendor"):
        LLMClient(gateway_config)._call_gateway("hi", max_tokens=2000)
    assert http.requests == [], "nothing should have been sent"
