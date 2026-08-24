"""
Orchestrator — Text Generation Gateway client.

Client for the NetGravity text-generation gateway (`POST /v1/generate`).

CREDENTIAL HANDLING
───────────────────
The token is read from the environment ONLY:

    export TEXT_API_URL="https://<gateway-host>"
    export TEXT_API_TOKEN="<token>"

It is deliberately never hardcoded, never defaulted to a literal, never logged
and never echoed into a prompt or error message, per the gateway's own security
guidance. If `TEXT_API_TOKEN` is unset the gateway reports itself unavailable
and the orchestrator degrades to its deterministic path.

GATEWAY CONSTRAINTS (these shape the design)
────────────────────────────────────────────
* The request body accepts exactly one field, `prompt`. There is no system
  role, no temperature, no tool calling, no model selection. All instruction
  must therefore be inlined into the prompt string, and every response must be
  parsed defensively.
* Limits are SHARED across all consumers: a cumulative USD budget, 100
  requests/day, 20 requests/rolling-minute, 100k prompt characters, 2000 output
  tokens, 60s processing timeout.
* Because capacity is shared and small, the LLM is treated as a best-effort
  enhancement. Every deterministic result stays valid without it.

RETRY RULES (from the integration guide)
────────────────────────────────────────
* Client timeout ~90s, slightly above the gateway's 60s processing timeout.
* Retry ONLY explicit transient 429 / 500 / 502, with exponential backoff and
  jitter, few attempts.
* NEVER auto-retry a client-side network timeout: the original call may have
  completed, and the gateway accepts no idempotency key, so retrying can
  duplicate work and consume shared budget twice.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from netgravity.llm import gateway_contract
from netgravity.telemetry import record_call
from netgravity.orchestrator.exceptions import LLMFailureError, LLMNonRetryableError

logger = logging.getLogger(__name__)

# The gateway's own facts — endpoints, limits, retry policy — are shared with
# the ingestion client rather than declared twice. The budget is cumulative
# and shared across every holder of the token, so a limit corrected in one
# file and not the other spends someone else's remaining capacity. See
# netgravity/llm/gateway_contract.py for why the CONTRACT is shared and the
# transports are not.
DEFAULT_BASE_URL = gateway_contract.DEFAULT_BASE_URL
DEFAULT_MODEL_NAME = gateway_contract.DEFAULT_MODEL_NAME
MAX_PROMPT_CHARS = gateway_contract.MAX_PROMPT_CHARS
RETRYABLE_STATUS = gateway_contract.RETRYABLE_STATUS

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 90.0


@dataclass
class LLMUsage:
    """Token accounting from one call."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMResponse:
    """One successful generation."""
    output: str
    request_id: Optional[str] = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_seconds: float = 0.0
    #: Provider/model provenance, recorded on the intent for audit. Optional
    #: because the shared gateway does not report a model identifier.
    model_name: Optional[str] = None
    model_version: Optional[str] = None


@runtime_checkable
class LLMClient(Protocol):
    """
    The narrow provider interface the conversational layer depends on.

    Deliberately minimal: language in, language out, plus enough metadata to
    audit which model said it. Retry, timeout, backoff and budget accounting
    belong to the implementation, not to callers.

    `LLMGateway` satisfies this structurally, so nothing is bound to one
    provider — a different backend needs only these three members. Note what is
    NOT here: no tool-calling, no function invocation, no callbacks. A provider
    reached through this interface has no mechanism by which to invoke MILP,
    REI, RF or governance, which is the point.
    """

    @property
    def available(self) -> bool:
        """True when a call could plausibly succeed."""
        ...

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:
        """Send one prompt; return generated text. Raises LLMFailureError."""
        ...

    def stats(self) -> Dict[str, Any]:
        """Call accounting. Must never contain credential material."""
        ...


@dataclass
class LLMGatewayConfig:
    """
    Gateway configuration, resolved from the environment.

    `max_requests_per_execution` is a local guard: because daily capacity is
    shared and small, one runaway execution must not exhaust it for everyone.
    """
    base_url: str = ""
    token: str = ""
    enabled: bool = True
    connect_timeout: float = CONNECT_TIMEOUT
    read_timeout: float = READ_TIMEOUT
    max_attempts: int = 3
    backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    max_requests_per_execution: int = 4
    #: The gateway offers no model selection and reports no model identifier, so
    #: provenance has to be configured rather than observed. Recorded on every
    #: intent: an audit record saying only "an LLM said so" cannot be
    #: re-evaluated when the backing model changes.
    model_name: str = DEFAULT_MODEL_NAME

    @classmethod
    def from_env(cls) -> "LLMGatewayConfig":
        token = os.environ.get("TEXT_API_TOKEN", "").strip()
        base = os.environ.get("TEXT_API_URL", DEFAULT_BASE_URL).strip().rstrip("/")
        disabled = os.environ.get("NETGRAVITY_DISABLE_LLM", "").strip().lower() in {"1", "true", "yes"}
        model = os.environ.get("TEXT_API_MODEL", DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
        return cls(base_url=base, token=token, model_name=model,
                   enabled=bool(token) and not disabled)


class LLMGateway:
    """
    Thin, defensive HTTP client for the text-generation gateway.

    Deliberately NOT an agent: it transports prompts and returns text. All
    semantics live in the agents that use it, and none of its output is ever
    treated as authoritative.
    """

    def __init__(self, config: Optional[LLMGatewayConfig] = None) -> None:
        self.config = config or LLMGatewayConfig.from_env()
        self._lock = threading.Lock()
        self._request_count = 0
        self._total_tokens = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """
        True when a call could plausibly succeed.

        False whenever the token is absent — which is the normal state in CI and
        for offline runs, and must degrade cleanly rather than raise.
        """
        if not self.config.enabled or not self.config.token or not self.config.base_url:
            return False
        try:
            import requests  # noqa: F401
        except ImportError:
            return False
        return True

    def unavailable_reason(self) -> str:
        if not self.config.token:
            return (
                "TEXT_API_TOKEN is not set. The orchestrator will use rule-based intent "
                "parsing and template reasoning; deterministic results are unaffected."
            )
        if not self.config.enabled:
            return "LLM disabled via NETGRAVITY_DISABLE_LLM."
        if not self.config.base_url:
            return "TEXT_API_URL is not set."
        try:
            import requests  # noqa: F401
        except ImportError:
            return "The 'requests' package is not installed."
        return ""

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:
        """
        Send one prompt and return the generated text.

        Args:
            prompt:  The complete prompt. The gateway accepts no other fields,
                     so any instruction must already be inlined here.
            purpose: Short label for logs/metrics. Never sent to the gateway.

        Raises:
            LLMNonRetryableError: unavailable, oversized prompt, per-execution
                budget exhausted, auth failure, or a 4xx other than 429.
            LLMFailureError: transient failure after exhausting retries.
        """
        if not self.available:
            raise LLMNonRetryableError(
                f"LLM gateway unavailable: {self.unavailable_reason()}",
                context={"purpose": purpose},
            )

        if not prompt or not prompt.strip():
            raise LLMNonRetryableError("Refusing to send an empty prompt.", context={"purpose": purpose})

        oversized = gateway_contract.oversized_prompt_reason(len(prompt))
        if oversized is not None:
            raise LLMNonRetryableError(
                oversized, context={"purpose": purpose, "prompt_chars": len(prompt)},
            )

        with self._lock:
            if self._request_count >= self.config.max_requests_per_execution:
                raise LLMNonRetryableError(
                    f"Local LLM call budget ({self.config.max_requests_per_execution}) "
                    f"exhausted for this gateway instance. Daily gateway capacity is "
                    f"shared, so runaway usage is capped locally.",
                    context={"purpose": purpose, "calls_made": self._request_count},
                )

        import requests

        url = f"{self.config.base_url}{gateway_contract.GENERATE_PATH}"
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }
        # The gateway accepts exactly one field.
        payload = {"prompt": prompt}

        attempt = 0
        last_exc: Optional[Exception] = None

        while attempt < self.config.max_attempts:
            attempt += 1
            started = time.perf_counter()
            try:
                resp = requests.post(
                    url, headers=headers, json=payload,
                    timeout=(self.config.connect_timeout, self.config.read_timeout),
                )
            except requests.Timeout as exc:
                # Deliberately NOT retried: the gateway may already have
                # processed the call, and there is no idempotency key.
                self._last_error = "client_timeout"
                raise LLMNonRetryableError(
                    "Client-side timeout calling the text gateway. Not retried: the "
                    "request may have completed server-side and the gateway accepts no "
                    "idempotency key, so a retry could duplicate work and spend shared "
                    "budget twice.",
                    context={"purpose": purpose, "attempt": attempt},
                    cause=exc,
                ) from exc
            except requests.RequestException as exc:
                last_exc = exc
                self._last_error = type(exc).__name__
                if attempt >= self.config.max_attempts:
                    break
                time.sleep(self._backoff(attempt))
                continue

            latency = round(time.perf_counter() - started, 4)

            if resp.status_code == 200:
                return self._parse_success(resp, purpose, latency)

            error_code, message, request_id = self._parse_error(resp)
            # Never log the token; request_id is the safe correlation handle.
            logger.warning(
                "orchestrator.llm.error purpose=%s status=%s code=%s request_id=%s attempt=%d",
                purpose, resp.status_code, error_code, request_id, attempt,
            )
            self._last_error = error_code

            # A cumulative-budget or daily-quota refusal arrives AS a 429 and
            # must not be retried: waiting does not restore spent budget, so a
            # retry only burns attempts and delays an honest failure. The
            # status alone cannot tell that apart from a rolling-minute rate
            # limit; the error code can.
            retryable = gateway_contract.should_retry(
                resp.status_code, error_code,
                attempts_left=attempt < self.config.max_attempts,
            )
            if retryable:
                time.sleep(self._backoff(attempt))
                continue

            if (resp.status_code in RETRYABLE_STATUS
                    and error_code.strip().lower()
                    not in gateway_contract.TERMINAL_ERRORS):
                raise LLMFailureError(
                    f"Text gateway returned {resp.status_code} ({error_code}) after "
                    f"{attempt} attempts: {message}",
                    context={"purpose": purpose, "status": resp.status_code,
                             "request_id": request_id},
                )

            raise LLMNonRetryableError(
                f"Text gateway returned {resp.status_code} ({error_code}): {message}",
                context={"purpose": purpose, "status": resp.status_code,
                         "request_id": request_id},
            )

        raise LLMFailureError(
            f"Text gateway unreachable after {attempt} attempts: {last_exc}",
            context={"purpose": purpose}, cause=last_exc,
        )

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, as the guide recommends."""
        raw = min(
            self.config.backoff_seconds * (2 ** (attempt - 1)),
            self.config.max_backoff_seconds,
        )
        return random.uniform(0.0, raw)

    def _parse_success(self, resp: Any, purpose: str, latency: float) -> LLMResponse:
        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMFailureError(
                "Text gateway returned a 200 with a non-JSON body.",
                context={"purpose": purpose}, cause=exc,
            ) from exc

        output = body.get("output")
        if not isinstance(output, str):
            raise LLMFailureError(
                "Text gateway response is missing a string 'output' field.",
                context={"purpose": purpose, "request_id": body.get("request_id")},
            )

        usage_raw = body.get("usage") or {}
        usage = LLMUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("output_tokens", 0) or 0),
            total_tokens=int(usage_raw.get("total_tokens", 0) or 0),
        )

        with self._lock:
            self._request_count += 1
            self._total_tokens += usage.total_tokens

        # Record into the SHARED token ledger, not only this object's private
        # counter.
        #
        # This was a real accounting gap. The gateway's budget is cumulative
        # and shared across every holder of the token, and until now the two
        # clients that spend it kept separate tallies that did not know about
        # each other — so neither view was ever complete, and "how much is
        # left?" had no answer anywhere in the system. The private counter is
        # kept as well: it is what `max_requests_per_execution` enforces, and
        # that guard must not depend on a module whose recording is
        # best-effort.
        record_call(
            task=f"orchestrator:{purpose}",
            model=str(body.get("model") or self.config.model_name),
            usage={"prompt_tokens": usage.input_tokens,
                   "completion_tokens": usage.output_tokens,
                   "total_tokens": usage.total_tokens},
        )

        logger.info(
            "orchestrator.llm.success purpose=%s request_id=%s tokens=%d latency=%.3fs",
            purpose, body.get("request_id"), usage.total_tokens, latency,
        )
        return LLMResponse(
            output=output,
            request_id=body.get("request_id"),
            usage=usage,
            latency_seconds=latency,
            # Prefer what the gateway reports, if it ever starts reporting it.
            model_name=str(body.get("model") or self.config.model_name),
            model_version=body.get("model_version"),
        )

    @staticmethod
    def _parse_error(resp: Any) -> tuple:
        try:
            body = resp.json()
            return (
                str(body.get("error", "unknown_error")),
                str(body.get("message", "")),
                body.get("request_id"),
            )
        except Exception:  # noqa: BLE001 - error bodies are not guaranteed JSON
            return ("non_json_error", (resp.text or "")[:200], None)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def usage(self) -> Dict[str, Any]:
        """
        Query shared gateway capacity (`GET /v1/usage`).

        Returns a dict with an `error` key rather than raising: this is a
        diagnostic, and must never break a run.
        """
        if not self.available:
            return {"error": self.unavailable_reason()}
        import requests
        try:
            resp = requests.get(
                f"{self.config.base_url}{gateway_contract.USAGE_PATH}",
                headers={"Authorization": f"Bearer {self.config.token}"},
                timeout=(self.config.connect_timeout, 30.0),
            )
            if resp.status_code != 200:
                return {"error": f"usage endpoint returned {resp.status_code}"}
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    def health(self) -> bool:
        """Unauthenticated availability probe. Consumes no budget."""
        if not self.config.base_url:
            return False
        try:
            import requests
            resp = requests.get(f"{self.config.base_url}{gateway_contract.HEALTH_PATH}", timeout=(5.0, 10.0))
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def stats(self) -> Dict[str, Any]:
        """Local call accounting. Contains no credential material."""
        return {
            "available": self.available,
            "requests_made": self._request_count,
            "total_tokens": self._total_tokens,
            "last_error": self._last_error,
            "base_url": self.config.base_url,
            "token_configured": bool(self.config.token),
        }


# ---------------------------------------------------------------------------
# Defensive JSON extraction
# ---------------------------------------------------------------------------

def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort extraction of a JSON object from model output.

    The gateway offers no structured-output mode, so responses may arrive
    wrapped in prose or fenced code blocks. Returns None rather than raising —
    callers must always have a non-LLM fallback.
    """
    if not text:
        return None

    candidate = text.strip()

    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost brace-balanced span.
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    for idx in range(start, len(candidate)):
        if candidate[idx] == "{":
            depth += 1
        elif candidate[idx] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(candidate[start:idx + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None
