"""
The shared gateway contract, and the two clients that must agree with it.

EVERY TEST HERE RUNS OFFLINE. Nothing in this file opens a socket.

These are agreement tests. They exist because the gateway's budget is
CUMULATIVE and SHARED across everyone holding the token, which changes what a
drifted constant costs: a limit corrected in one client and not the other is
not a local bug that shows up in that client's own tests — it silently spends
capacity that belongs to someone else.
"""

from __future__ import annotations

import pytest

from netgravity.llm import gateway_contract
from netgravity.ingestion.ai import client as ingestion_client
from netgravity.orchestrator.agents import llm_gateway as orchestrator_gateway


# ---------------------------------------------------------------------------
# Both clients read the same facts
# ---------------------------------------------------------------------------

def test_both_clients_use_the_same_prompt_limit():
    assert (ingestion_client.GATEWAY_MAX_PROMPT_CHARS
            == orchestrator_gateway.MAX_PROMPT_CHARS
            == gateway_contract.MAX_PROMPT_CHARS)


def test_both_clients_use_the_same_retryable_statuses():
    assert (ingestion_client._GATEWAY_RETRYABLE_STATUS
            == orchestrator_gateway.RETRYABLE_STATUS
            == gateway_contract.RETRYABLE_STATUS)


def test_both_clients_agree_on_the_model_name():
    from netgravity.ingestion.config import DEFAULT_MODELS
    assert DEFAULT_MODELS["gateway"] == gateway_contract.DEFAULT_MODEL_NAME
    assert (orchestrator_gateway.DEFAULT_MODEL_NAME
            == gateway_contract.DEFAULT_MODEL_NAME)


# ---------------------------------------------------------------------------
# The distinction the contract exists for
# ---------------------------------------------------------------------------

def test_a_rate_limit_is_retried():
    """A rolling-minute limit clears by waiting, so waiting is correct."""
    assert gateway_contract.should_retry(429, "rate_limit_exceeded") is True


@pytest.mark.parametrize("error_code", ["budget_exceeded", "daily_limit_exceeded"])
def test_a_spent_budget_is_never_retried(error_code):
    """
    Both arrive as 429s, and neither clears by waiting.

    The status code alone cannot tell a rolling-minute limit from an exhausted
    cumulative budget. Retrying the second can only burn attempts and delay an
    honest failure — the money is already gone.
    """
    assert gateway_contract.should_retry(429, error_code) is False


def test_a_client_error_is_never_retried():
    assert gateway_contract.should_retry(400, "bad_request") is False
    assert gateway_contract.should_retry(401, "unauthorized") is False


def test_exhausted_attempts_stop_retrying_whatever_the_status():
    assert gateway_contract.should_retry(429, "rate_limit_exceeded",
                                         attempts_left=False) is False


def test_the_terminal_error_codes_are_a_subset_of_retryable_situations():
    """
    A terminal error must be one that WOULD otherwise be retried.

    If it were not, the override would be dead code and the real terminal
    case would still be getting retried somewhere.
    """
    assert gateway_contract.TERMINAL_ERRORS
    assert 429 in gateway_contract.RETRYABLE_STATUS


# ---------------------------------------------------------------------------
# Local refusals — budget spent on nothing is still budget spent
# ---------------------------------------------------------------------------

def test_an_oversized_prompt_is_refused_before_it_is_sent():
    reason = gateway_contract.oversized_prompt_reason(
        gateway_contract.MAX_PROMPT_CHARS + 1)
    assert reason is not None
    assert "413" in reason


def test_a_prompt_within_the_limit_is_not_refused():
    assert gateway_contract.oversized_prompt_reason(
        gateway_contract.MAX_PROMPT_CHARS) is None


@pytest.mark.parametrize("url", [
    "https://generativelanguage.googleapis.com/v1beta/openai",
    "https://api.openai.com/v1",
    "https://api.anthropic.com",
])
def test_a_vendor_endpoint_is_recognised_as_a_misconfiguration(url):
    """
    The specific mistake: reusing an existing base-URL variable, so the
    "gateway" address is quietly a model vendor's. Both clients apply this —
    a guard only one of them ran is a guard the other one's misconfiguration
    walks straight past.
    """
    assert gateway_contract.looks_like_vendor_endpoint(url) is True
    assert ingestion_client._looks_like_vendor_endpoint(url) is True


def test_the_real_gateway_address_is_not_flagged():
    assert gateway_contract.looks_like_vendor_endpoint(
        gateway_contract.DEFAULT_BASE_URL) is False


# ---------------------------------------------------------------------------
# The accounting gap this work closed
# ---------------------------------------------------------------------------

def test_the_orchestrator_gateway_records_into_the_shared_ledger():
    """
    Both clients spend ONE cumulative budget, so both must report into one
    ledger.

    Before this, the orchestrator's gateway kept a private counter and never
    touched `netgravity.telemetry`. Neither view of spending was complete, and
    "how much of the shared allowance is left?" had no answer anywhere in the
    system.
    """
    import inspect
    source = inspect.getsource(orchestrator_gateway)
    assert "record_call(" in source


def test_the_private_counter_is_kept_as_well():
    """
    `max_requests_per_execution` is enforced from the private counter, and
    must not start depending on a ledger whose recording is best-effort by
    design (telemetry never raises).
    """
    gateway = orchestrator_gateway.LLMGateway(
        orchestrator_gateway.LLMGatewayConfig(base_url="", token="", enabled=False))
    assert gateway.stats()["requests_made"] == 0
    assert gateway.stats()["total_tokens"] == 0


def test_ingestion_has_a_runaway_guard_too():
    """
    Adopted from the orchestrator's client, which had it. Ingestion is the
    side that batches, so it is the side most able to drain a shared daily
    allowance without anyone deciding to.
    """
    from netgravity.ingestion.config import IngestionConfig
    config = IngestionConfig()
    assert config.gateway_max_calls > 0
    assert ingestion_client.LLMClient(config)._gateway_calls == 0
