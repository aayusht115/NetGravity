"""Pluggable runtime boundary for OpenAI Agents SDK reasoning."""

from __future__ import annotations

import importlib.util
import os
from typing import Any, Callable, Dict, Optional, Protocol

from netgravity.orchestrator.reasoning.prompts import REASONING_AGENT_INSTRUCTIONS
from netgravity.orchestrator.reasoning.tools import (
    evidence_manifest,
    list_missing_evidence,
    lookup_evidence,
)
from netgravity.orchestrator.schemas.reasoning import ReasoningDraft, ReasoningEvidencePack


class ReasoningRuntime(Protocol):
    """The only model capability the reasoning layer accepts."""

    @property
    def available(self) -> bool: ...

    def run(self, evidence: ReasoningEvidencePack) -> ReasoningDraft: ...

    @property
    def stats(self) -> Dict[str, Any]: ...


class StubReasoningRuntime:
    """Deterministic test double. It never performs network I/O."""

    def __init__(
        self,
        output: ReasoningDraft | Callable[[ReasoningEvidencePack], ReasoningDraft],
        *,
        available: bool = True,
    ) -> None:
        self.output = output
        self._available = available
        self.calls: list[ReasoningEvidencePack] = []

    @property
    def available(self) -> bool:
        return self._available

    def run(self, evidence: ReasoningEvidencePack) -> ReasoningDraft:
        self.calls.append(evidence)
        result = self.output(evidence) if callable(self.output) else self.output
        return result.model_copy(deep=True)

    @property
    def stats(self) -> Dict[str, Any]:
        return {"provider": "stub", "calls": len(self.calls)}


class OpenAIAgentsReasoningRuntime:
    """
    OpenAI Agents SDK adapter.

    The adapter is inert unless explicitly selected, an API key is available,
    and the optional ``openai-agents`` dependency is installed. Tools are
    closures over one immutable evidence pack and expose no write capability.
    """

    def __init__(self, *, model: str = "gpt-5", enabled: bool = True) -> None:
        self.model = model
        self.enabled = enabled
        self._calls = 0
        self._failures = 0

    @classmethod
    def from_environment(cls, *, enabled: bool = True) -> "OpenAIAgentsReasoningRuntime":
        selected = os.getenv("NETGRAVITY_REASONING_RUNTIME", "").strip().lower()
        disabled = os.getenv("NETGRAVITY_DISABLE_LLM", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        return cls(
            model=os.getenv("NETGRAVITY_REASONING_MODEL", "gpt-5").strip() or "gpt-5",
            enabled=enabled and not disabled and selected == "agents",
        )

    @property
    def available(self) -> bool:
        return bool(
            self.enabled
            and os.getenv("OPENAI_API_KEY")
            and importlib.util.find_spec("agents") is not None
        )

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "provider": "openai_agents_sdk",
            "model": self.model,
            "calls": self._calls,
            "failures": self._failures,
            "available": self.available,
        }

    def run(self, evidence: ReasoningEvidencePack) -> ReasoningDraft:
        if not self.available:
            raise RuntimeError(
                "OpenAI Agents reasoning is unavailable. Set "
                "NETGRAVITY_REASONING_RUNTIME=agents and OPENAI_API_KEY, and install "
                "the 'llm' optional dependencies."
            )

        # Imported only on the explicitly enabled live path. Offline test and
        # deterministic deployments do not need the SDK installed.
        from agents import Agent, Runner, function_tool  # type: ignore[import-not-found]

        def get_evidence(metric_ref: str) -> str:
            """Read one deterministic metric by its exact reference."""
            return lookup_evidence(evidence, metric_ref)

        def get_missing_evidence() -> str:
            """Read analyses explicitly marked unavailable for this run."""
            return list_missing_evidence(evidence)

        agent = Agent(
            name="NetGravity Reasoning Agent",
            instructions=REASONING_AGENT_INSTRUCTIONS,
            model=self.model,
            tools=[function_tool(get_evidence), function_tool(get_missing_evidence)],
            output_type=ReasoningDraft,
        )
        user_input = (
            f"Scope: {evidence.scope.value}\n"
            f"Entity: {evidence.entity_id or 'network'}\n"
            f"User question: {evidence.user_question or 'Give me the executive briefing.'}\n"
            f"Provenance: {evidence.provenance}\n"
            "Evidence catalogue (use get_evidence before citing a metric):\n"
            f"{evidence_manifest(evidence)}"
        )

        self._calls += 1
        try:
            result = Runner.run_sync(agent, user_input)
            final = result.final_output
            if isinstance(final, ReasoningDraft):
                return final
            return ReasoningDraft.model_validate(final)
        except Exception:
            self._failures += 1
            raise
