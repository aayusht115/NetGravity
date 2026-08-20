"""
NetGravity — LLM Client (single integration point)
===================================================
EVERY LLM call in the ingestion pipeline goes through this file.

WHY THAT MATTERS
----------------
1. API KEY PLACEHOLDER. The key is read from NETGRAVITY_LLM_API_KEY. It is
   deliberately not required. With no key set the client runs in STUB MODE
   and returns canned responses from ai/stubs.py, so the whole pipeline —
   and its entire test suite — runs end to end without credentials.
   Supplying the key later changes nothing but an environment variable.

2. PROVIDER ISOLATION. The team has not finalised the LLM provider. Swapping
   from Anthropic to anything else is a change to _call_live() in this file
   only. No adapter imports a vendor SDK directly.

3. AUDITABILITY. Every response records whether it came from a live model or
   a stub, and that flag is surfaced in the ingestion report — so nobody can
   mistake canned demo output for a real extraction.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from netgravity.ingestion.config import IngestionConfig

# Providers that speak the OpenAI chat-completions API. "codex" is tolerated
# as a synonym for the same thing.
#
# NOTE: Azure OpenAI is deliberately NOT listed. It needs a different client
# class (AzureOpenAI, with azure_endpoint / api_version / azure_deployment),
# so routing it here would silently call api.openai.com instead of the Azure
# deployment. When Azure OpenAI is actually needed, it gets its own branch.
_OPENAI_PROVIDERS = {"openai", "codex"}

# Sentinel embedded in the note when a live call failed and we degraded to
# stub data. Adapters look for this to set FileResult.ai_failed, so the
# report can distinguish "no key configured" from "the API call broke".
LLM_FAILURE_MARKER = "LLM CALL FAILED"


def _is_unsupported_param(exc: Exception, param: str) -> bool:
    """True when an API error is specifically 'this model rejects <param>'."""
    text = str(exc).lower()
    return param.lower() in text and (
        "unsupported" in text or "unrecognized" in text
        or "not supported" in text or "unknown" in text
    )


class LLMCallError(RuntimeError):
    """A live model call failed and strict mode forbids falling back to stubs."""


@dataclass
class LLMResponse:
    """One model response, with provenance attached."""
    data: Dict[str, Any]
    stubbed: bool
    model: str
    notes: str = ""
    # True only when a LIVE call was attempted and failed. Distinguishes
    # "deliberately running without a key" from "tried to call the API and
    # could not" — the second is a problem, the first is not.
    failed: bool = False
    # Token usage the provider reported for this call: prompt_tokens,
    # completion_tokens, total_tokens. None for stub responses, or if the
    # provider's response didn't include usage. This is what makes cost
    # visible per call instead of only on the provider's own dashboard.
    tokens: Optional[Dict[str, int]] = None

    @property
    def provenance(self) -> str:
        return "stub" if self.stubbed else self.model


class LLMClient:
    """Thin wrapper over whichever provider is configured."""

    def __init__(self, config: IngestionConfig):
        self.config = config
        self._sdk = None
        # Set by _call_openai/_call_anthropic as a side effect of the most
        # recent live call. Read by extract_json() right after _call_live()
        # returns. Not part of the public API — a transient handoff, since
        # _call_live() itself only returns text (changing that return shape
        # would touch every test that calls it directly).
        self._last_usage: Optional[Dict[str, int]] = None

    @property
    def stub_mode(self) -> bool:
        return self.config.stub_mode

    # -- public API ------------------------------------------------------

    def extract_json(self, *, task: str, prompt: str, stub_key: str,
                     stub_context: Optional[Dict[str, Any]] = None,
                     max_tokens: int = 2000) -> LLMResponse:
        """
        Ask the model for a JSON object.

        `stub_key` selects the canned response used when no API key is set,
        so each call site has a realistic offline equivalent.
        """
        if self.stub_mode:
            from netgravity.ingestion.ai import stubs
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True,
                model="stub",
                notes=f"{task}: stubbed (no API key configured)",
            )

        try:
            self._last_usage = None
            raw = self._call_live(prompt, max_tokens=max_tokens)
            usage = self._last_usage
            tokens_note = ""
            if usage:
                tokens_note = (
                    f" [{usage.get('total_tokens', '?')} tokens: "
                    f"{usage.get('prompt_tokens', '?')} in + "
                    f"{usage.get('completion_tokens', '?')} out]"
                )
            return LLMResponse(
                data=_parse_json(raw),
                stubbed=False,
                model=f"{self.config.llm_provider}:{self.config.resolved_model}",
                notes=f"{task}: live extraction{tokens_note}",
                tokens=usage,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"

            # In strict mode a failed call is a failed run. Silently serving
            # canned demo data in place of a real extraction is the one
            # outcome worse than stopping.
            if self.config.llm_strict:
                raise LLMCallError(
                    f"{task}: live LLM call failed and NETGRAVITY_LLM_STRICT "
                    f"is set — refusing to substitute stub data. {detail}"
                ) from exc

            # Otherwise degrade to stub so a demo survives an outage, but mark
            # it as a FAILURE, not as ordinary stub mode.
            from netgravity.ingestion.ai import stubs
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True,
                model="stub",
                failed=True,
                notes=f"{task}: {LLM_FAILURE_MARKER} ({detail}) — fell back to stub "
                      f"data. These numbers are NOT a real extraction. Set "
                      f"NETGRAVITY_LLM_STRICT=true to fail the run instead.",
            )

    # -- provider-specific ------------------------------------------------

    def _call_live(self, prompt: str, *, max_tokens: int) -> str:
        """
        The ONLY provider-specific code in the ingestion package.
        Adding a provider means adding a branch here and nothing else.

        Both SDKs handle their own retry/backoff on transient errors (429,
        5xx) when given max_retries, so we do not hand-roll a retry loop.
        """
        provider = (self.config.llm_provider or "openai").lower()

        if provider in _OPENAI_PROVIDERS:
            return self._call_openai(prompt, max_tokens=max_tokens)
        if provider == "anthropic":
            return self._call_anthropic(prompt, max_tokens=max_tokens)

        raise NotImplementedError(
            f"LLM provider '{provider}' is not wired up. Supported: "
            f"{', '.join(sorted(_OPENAI_PROVIDERS | {'anthropic'}))}. "
            f"Add a branch in LLMClient._call_live()."
        )

    # -- OpenAI ------------------------------------------------------------

    def _create_chat_completion(self, prompt: str, max_tokens: int):
        """
        Call chat.completions.create(), degrading two independent optional
        features one at a time on the specific error that says a provider
        doesn't support them — rather than assuming every OpenAI-compatible
        provider (OpenRouter, Groq, Cerebras, GitHub Models, ...) supports
        every OpenAI feature identically:

          - JSON mode (response_format): guarantees syntactically valid
            JSON back. Real OpenAI models support it; some alternate
            providers/models don't and reject the parameter.
          - max_completion_tokens vs max_tokens: newer OpenAI models renamed
            this and reject the old name; most other providers still expect
            the old name.

        At most 4 attempts, each one only ever removing a param that was
        just rejected, so this always terminates.
        """
        base: Dict[str, Any] = {
            "model": self.config.resolved_model,
            "messages": [{"role": "user", "content": prompt}],
        }
        use_json_mode = True
        token_param = "max_completion_tokens"

        for _ in range(4):
            kwargs = dict(base)
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            kwargs[token_param] = max_tokens
            try:
                return self._sdk.chat.completions.create(**kwargs)
            except Exception as exc:
                if token_param == "max_completion_tokens" and \
                        _is_unsupported_param(exc, "max_completion_tokens"):
                    token_param = "max_tokens"
                    continue
                if use_json_mode and _is_unsupported_param(exc, "response_format"):
                    use_json_mode = False
                    continue
                raise
        raise RuntimeError("unreachable: fallback loop exhausted")  # pragma: no cover

    def _call_openai(self, prompt: str, *, max_tokens: int) -> str:
        if self._sdk is None:
            try:
                from openai import OpenAI   # lazy: not needed in stub mode
            except ImportError as exc:
                raise ImportError(
                    "The 'openai' package is required for live extraction with "
                    "this provider. Install it with `pip install openai`."
                ) from exc
            self._sdk = OpenAI(
                api_key=self.config.llm_api_key,
                # None => SDK default (api.openai.com). Set this to point at
                # any OpenAI-compatible server instead — OpenRouter, Groq,
                # Cerebras, GitHub Models — with no other code change.
                base_url=self.config.llm_base_url,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )

        resp = self._create_chat_completion(prompt, max_tokens)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        choice = resp.choices[0]
        # A truncated response is invalid JSON and would fail parsing with a
        # confusing error. Name the real cause instead.
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError(
                f"response hit the {max_tokens}-token limit and was truncated; "
                f"raise max_tokens for this call site"
            )
        return choice.message.content or ""

    # -- Anthropic ---------------------------------------------------------

    def _call_anthropic(self, prompt: str, *, max_tokens: int) -> str:
        if self._sdk is None:
            try:
                from anthropic import Anthropic   # lazy: not needed in stub mode
            except ImportError as exc:
                raise ImportError(
                    "The 'anthropic' package is required for live extraction "
                    "with this provider. Install it with `pip install anthropic`."
                ) from exc
            self._sdk = Anthropic(
                api_key=self.config.llm_api_key,
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )

        message = self._sdk.messages.create(
            model=self.config.resolved_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        usage = getattr(message, "usage", None)
        if usage is not None:
            input_tokens = getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output_tokens", None)
            self._last_usage = {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": (input_tokens + output_tokens)
                if input_tokens is not None and output_tokens is not None else None,
            }
        if getattr(message, "stop_reason", None) == "max_tokens":
            raise ValueError(
                f"response hit the {max_tokens}-token limit and was truncated; "
                f"raise max_tokens for this call site"
            )
        return "".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        )


def _parse_json(text: str) -> Dict[str, Any]:
    """Pull a JSON object out of a model response, tolerating prose or fences."""
    text = text.strip()

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first != -1 and last > first:
            text = text[first:last + 1]

    return json.loads(text)


def get_client(config: IngestionConfig) -> LLMClient:
    return LLMClient(config)
