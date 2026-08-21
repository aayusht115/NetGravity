"""
NetGravity Orchestrator — Control plane.

Interprets requests, plans work, coordinates deterministic engines and
specialised agents, manages execution state and scenario isolation, combines
outputs, applies risk and governance logic, and decides what happens next.

    LLM              semantic intelligence
    Orchestrator     coordination + control
    MILP             optimization truth
    REI              network exposure truth
    RF               deterministic risk combination
    Reasoning Agent  synthesis and explanation
    Governance       action authorization
    Audit            traceability

Quick start::

    from netgravity.orchestrator import build_orchestrator
    from netgravity.orchestrator.schemas.requests import OrchestratorRequest

    orch = build_orchestrator(network=my_network)
    response = orch.run_sync(OrchestratorRequest(input="What if we close DC_EAST?"))
    print(response.status, response.summary)

The orchestrator runs fully offline. Without a configured text gateway it uses
rule-based intent parsing and template reasoning; every deterministic result is
identical either way.
"""

from netgravity.orchestrator.core.execution_context import ExecutionContext
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.core.orchestrator import Orchestrator
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.risk.risk_factor import compute_risk_factor
from netgravity.orchestrator.schemas.actions import (
    ActionClassification,
    ActionType,
    FinalResponse,
)
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
)

__all__ = [
    "build_orchestrator",
    "Orchestrator",
    "ExecutionContext",
    "ExecutionState",
    "OrchestratorRequest",
    "FinalResponse",
    "Actor",
    "ActorRole",
    "Intent",
    "ActionType",
    "ActionClassification",
    "compute_risk_factor",
]
