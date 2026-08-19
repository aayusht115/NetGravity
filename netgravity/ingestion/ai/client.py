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


@dataclass
class LLMResponse:
    """One model response, with provenance attached."""
    data: Dict[str, Any]
    stubbed: bool
    model: str
    notes: str = ""

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
                model=f"{self.config.llm_provider}:{self.config.llm_model}",
                notes=f"{task}: live extraction",
            )
        except Exception as exc:
            # Never let a provider outage break ingestion — degrade to stub
            # and say so loudly in the report.
            from netgravity.ingestion.ai import stubs
            return LLMResponse(
                data=stubs.get(stub_key, stub_context or {}),
                stubbed=True,
                model="stub",
                notes=f"{task}: LLM call failed ({type(exc).__name__}: {exc}); "
                      f"fell back to stub",
            )

    # -- provider-specific ------------------------------------------------

    def _call_live(self, prompt: str, *, max_tokens: int) -> str:
        """
        The ONLY provider-specific code in the ingestion package.
        Swapping providers means rewriting this method and nothing else.
        """
        provider = (self.config.llm_provider or "anthropic").lower()

        if provider == "anthropic":
            if self._sdk is None:
                from anthropic import Anthropic  # lazy: not needed in stub mode
                self._sdk = Anthropic(api_key=self.config.llm_api_key)

            message = self._sdk.messages.create(
                model=self.config.llm_model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text for block in message.content
                if getattr(block, "type", None) == "text"
            )

        raise NotImplementedError(
            f"LLM provider '{provider}' is not wired up. "
            f"Implement it in LLMClient._call_live()."
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
