"""
NetGravity — the model connection for explanations
====================================================
One resolver, one credential, shared by every result-screen explanation:
optimized results, a scenario, a comparison, a forecast.

THE CREDENTIAL IS THE SWITCH. There is no separate on/off flag. The shared
gateway is configured by three variables and nothing else:

    TEXT_API_URL     https://rapidinsights-openai-gateway-dev.azurewebsites.net
    TEXT_API_MODEL   gpt-5-mini
    TEXT_API_TOKEN   blank => every explanation is written by the
                     deterministic template; set => written by the model.

A second switch alongside them would be a second answer to one question, and
the two would eventually disagree — a token present with the flag off looks
exactly like a token that is missing.

WHY THIS MODULE EXISTS AT ALL. The forecast and comparison flows used to
build a bare `ReasoningAgent()`, which holds no gateway and therefore
produced templates however anything was configured. Every flow now takes its
connection from here, so they cannot diverge.

WHAT A CONFIGURED TOKEN BUYS:

  * one model request per COMPLETED ANALYSIS, and none for viewing one
    already explained. The request goes through `ExplanationService`, which
    reads the saved explanation first and passes `single_request=True` so the
    agent runtime — which reaches the model once per metric it cites — is
    never used;
  * the deterministic template as the fallback on every path. A gateway that
    is unavailable, over budget, or answers in the wrong shape degrades to
    templates and says so in `source`;
  * grounding either way. `numeric_grounding` re-checks every numeric claim
    and strips what it cannot source, so a token cannot turn on unchecked
    numbers.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def explanation_gateway() -> Optional[Any]:
    """
    The gateway every explanation flow shares, or None when none is usable.

    None is the normal state with `TEXT_API_TOKEN` blank, and callers then run
    the deterministic template rather than failing.
    """
    try:
        from netgravity.orchestrator.agents.llm_gateway import LLMGateway

        gateway = LLMGateway()
        if gateway.available:
            return gateway
        logger.debug("explanation gateway unavailable: %s",
                     gateway.unavailable_reason())
    except Exception as exc:  # noqa: BLE001 — explanations are advisory
        logger.debug("explanation gateway could not be built: %s", exc)
    return None


def explanations_llm_enabled() -> bool:
    """
    Whether explanations reach a model.

    Asks the CONNECTION, not the environment. Looking for a variable name
    instead would answer a different question — and did: it reported "AI is
    on" while all four flows produced templates, because the name it looked
    for was one no gateway reads.
    """
    return explanation_gateway() is not None


def explanation_reasoning_agent() -> Any:
    """
    A `ReasoningAgent` with that connection attached.

    Use this instead of `ReasoningAgent()`. A bare one has no gateway and
    degrades to the template whatever is configured — which is precisely what
    left the forecast and comparison flows silent.

    `runtime=None` is deliberate: the agents runtime reaches the model once
    per metric it cites, and every explanation flow is budgeted at one
    request.
    """
    from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent

    return ReasoningAgent(explanation_gateway(), runtime=None)


def explanation_mode() -> str:
    """"llm" or "template" — for a status endpoint, and for logs."""
    return "llm" if explanations_llm_enabled() else "template"


def explanation_status() -> dict:
    """
    What is actually wired, for `/api/status` and for a human asking.

    Reports the connection rather than a setting, because a setting that
    disagrees with the connection is the failure this module prevents.
    """
    gateway = explanation_gateway()
    stats = {}
    try:
        stats = gateway.stats() if gateway is not None else {}
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "available": gateway is not None,
        "mode": explanation_mode(),
        "base_url": stats.get("base_url"),
        "token_configured": bool(stats.get("token_configured")),
    }
