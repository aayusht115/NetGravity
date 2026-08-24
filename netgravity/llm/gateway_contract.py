"""
NetGravity — the text-generation gateway's contract, in one place.

WHY THIS EXISTS
---------------
Two clients talk to the same gateway:

    netgravity/ingestion/ai/client.py         one provider among several
    netgravity/orchestrator/agents/llm_gateway.py   the orchestrator's only one

They were written independently and had independently declared the same
limits, the same retryable statuses and the same endpoint paths. That is a
drift hazard with teeth: the gateway's budget is SHARED and CUMULATIVE across
everyone holding the token, so a limit corrected in one file and not the other
does not produce a local bug — it spends someone else's remaining capacity.

WHY THE CONTRACT AND NOT THE TRANSPORT
--------------------------------------
The obvious move is to merge the two HTTP loops into one. That was considered
and rejected, for three reasons:

  1. They use different HTTP libraries, deliberately. The ingestion client is
     hand-rolled on stdlib `urllib` so that ingestion adds no dependency; the
     orchestrator uses `requests`, and its tests mock `requests`. Unifying
     means either adding a dependency ingestion avoided on purpose, or
     rewriting the orchestrator's transport and every test that fakes it.

  2. They raise different exceptions, and those exceptions are contracts.
     `LLMFailureError` / `LLMNonRetryableError` are part of how the
     orchestrator's agents and workflows behave; the ingestion client degrades
     to labelled stub data instead of raising at all. Neither behaviour is
     wrong — they serve different callers — and a shared transport would have
     to satisfy both, which means it would satisfy neither cleanly.

  3. The orchestrator's `LLMClient` Protocol has exactly three members —
     `available()`, `generate()`, `stats()` — and that narrowness is a
     documented security boundary: there is no tool-invocation mechanism, so
     no path by which a model could reach the MILP, REI, RF or governance
     whatever a prompt instructs. Giving it the ingestion client's richer
     surface (PDF handling, JSON extraction, provider switching) would widen a
     wall that was built narrow on purpose.

What actually drifts is the FACTS — a limit, a path, an error code. Those live
here, are imported by both, and cost nothing to share.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Credentials, and any default that resolves one. Each client reads its own
environment variables, and this module never touches `os.environ`.
"""

from __future__ import annotations

from typing import Optional, Set

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

#: Default host. Overridden per client from its own environment variable.
DEFAULT_BASE_URL = "https://rapidinsights-openai-gateway-dev.azurewebsites.net"

#: The gateway accepts exactly one field on this path: {"prompt": "..."}.
#: There is no model selection, no system prompt, no JSON mode and no
#: temperature — any instruction has to be inlined into the prompt itself.
GENERATE_PATH = "/v1/generate"

#: Shared budget remaining. Free, and worth calling before a batch.
USAGE_PATH = "/v1/usage"

#: Unauthenticated liveness probe. Consumes no budget.
HEALTH_PATH = "/health"

#: The gateway reports no model identifier, so provenance is configured rather
#: than observed. An audit record saying only "an LLM said so" cannot be
#: re-evaluated when the backing model changes.
DEFAULT_MODEL_NAME = "gpt-5-mini"

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

#: Above this the gateway answers 413. Checked LOCALLY before sending, so an
#: oversized prompt costs nothing instead of one guaranteed-failed request
#: against a shared allowance.
MAX_PROMPT_CHARS = 100_000

#: Fixed server-side. A caller asking for more does not get more, so asking is
#: worth a warning rather than a silent disappointment.
MAX_OUTPUT_TOKENS = 2_000

# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

#: Statuses worth another attempt: rate limiting and transient server faults.
RETRYABLE_STATUS: Set[int] = {429, 500, 502}

#: Errors that arrive AS a retryable status and must never be retried anyway.
#:
#: This is the subtlety the whole module earns its place for. Both are 429s.
#: A rolling-minute rate limit clears by waiting, so retrying is correct. A
#: cumulative budget or daily quota does NOT clear by waiting — the money is
#: spent — so a retry can only burn attempts and delay an honest failure. The
#: status code alone cannot tell them apart; the error code can.
TERMINAL_ERRORS: Set[str] = {"daily_limit_exceeded", "budget_exceeded"}

#: Hostnames that mean a gateway URL has been misconfigured with a vendor
#: endpoint — usually by reusing an existing base-URL variable. Sending a
#: gateway-shaped body to a vendor endpoint leaks a prompt to an unintended
#: destination and fails confusingly; refusing names the real problem.
_VENDOR_HOST_MARKERS = (
    "googleapis", "openai.com", "anthropic.com", "openrouter.ai",
    "groq.com", "cerebras.ai", "inference.ai.azure.com",
)


def should_retry(status: int, error_code: str = "", *,
                 attempts_left: bool = True) -> bool:
    """
    Whether a failed gateway call is worth repeating.

    A terminal error is never retried however retryable its status looks —
    see `TERMINAL_ERRORS`.
    """
    if not attempts_left:
        return False
    if (error_code or "").strip().lower() in TERMINAL_ERRORS:
        return False
    return status in RETRYABLE_STATUS


def oversized_prompt_reason(prompt_chars: int) -> Optional[str]:
    """
    A message explaining a locally-refused prompt, or None if it will fit.

    Returned rather than raised so each client can fail in its own idiom: the
    orchestrator raises, the ingestion client degrades to labelled stub data.
    """
    if prompt_chars <= MAX_PROMPT_CHARS:
        return None
    return (
        f"Prompt is {prompt_chars:,} characters, above the gateway's "
        f"{MAX_PROMPT_CHARS:,} limit. Refused locally: sending it would spend "
        f"a request from a shared allowance on a guaranteed HTTP 413."
    )


def looks_like_vendor_endpoint(url: str) -> bool:
    """True when a gateway URL is actually a model vendor's endpoint."""
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _VENDOR_HOST_MARKERS)


def describe_limits() -> str:
    """One-line summary for diagnostics and verify scripts."""
    return (f"prompt <= {MAX_PROMPT_CHARS:,} chars, output <= "
            f"{MAX_OUTPUT_TOKENS:,} tokens, budget is shared and cumulative")
