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

    @property
    def provenance(self) -> str:
        return "stub" if self.stubbed else self.model


class LLMClient:
    """Thin wrapper over whichever provider is configured."""

    def __init__(self, config: IngestionConfig):
        self.config = config
        self._sdk = None

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
            raw = self._call_live(prompt, max_tokens=max_tokens)
            return LLMResponse(
                data=_parse_json(raw),
                stubbed=False,
                model=f"{self.config.llm_provider}:{self.config.resolved_model}",
                notes=f"{task}: live extraction",
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
                timeout=self.config.llm_timeout_seconds,
                max_retries=self.config.llm_max_retries,
            )

        # JSON mode makes the API itself guarantee syntactically valid JSON,
        # which removes a whole class of "model wrapped it in prose" parse
        # failures. It requires the word "json" in the prompt — every prompt
        # in ai/ says "Return ONLY a JSON object", so that holds.
        kwargs: Dict[str, Any] = {
            "model": self.config.resolved_model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }

        # Newer OpenAI models renamed max_tokens -> max_completion_tokens and
        # reject the old name. Rather than hardcode a model list that will go
        # stale, try one and switch on the specific complaint.
        try:
            resp = self._sdk.chat.completions.create(
                **kwargs, max_completion_tokens=max_tokens
            )
        except Exception as exc:
            if not _is_unsupported_param(exc, "max_completion_tokens"):
                raise
            resp = self._sdk.chat.completions.create(**kwargs, max_tokens=max_tokens)

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
