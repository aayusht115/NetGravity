"""
NetGravity — shared model-gateway definitions.

`gateway_contract` holds the FACTS about the text-generation gateway: its
endpoints, its environment variables, its limits, and which of its errors are
worth retrying. Every client that talks to the gateway imports them from here.

See `gateway_contract` for why this package holds the contract rather than the
transport.
"""

from netgravity.llm.gateway_contract import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_NAME,
    GENERATE_PATH,
    HEALTH_PATH,
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARS,
    RETRYABLE_STATUS,
    TERMINAL_ERRORS,
    USAGE_PATH,
    describe_limits,
    looks_like_vendor_endpoint,
    oversized_prompt_reason,
    should_retry,
)

__all__ = [
    "DEFAULT_BASE_URL", "DEFAULT_MODEL_NAME", "GENERATE_PATH", "HEALTH_PATH",
    "MAX_OUTPUT_TOKENS", "MAX_PROMPT_CHARS", "RETRYABLE_STATUS",
    "TERMINAL_ERRORS", "USAGE_PATH", "describe_limits",
    "looks_like_vendor_endpoint", "oversized_prompt_reason", "should_retry",
]
