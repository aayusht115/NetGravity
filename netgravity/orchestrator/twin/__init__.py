"""
NetGravity — Digital Twin.

A representation layer over authoritative results. It holds what the network
looks like under a given snapshot and scenario, and it can say how two such
states differ. It computes nothing.

    User / Workflow
          ↓
      Orchestrator
          ↓
    ┌─────┼─────┐
    ↓     ↓     ↓
   MILP  REI    RF          ← authoritative engines
    └─────┼─────┘
          ↓
      Orchestrator          ← composes the authoritative result
          ↓
    DigitalTwinState        ← the contract
          ↓
      Digital Twin
          ↓
 Visualization / Comparison

**The Orchestrator is the sole upstream integration point.** No engine calls
this package, and this package calls no engine — `builder` accepts only frozen
result contracts (`NetworkStateResult`, `FacilityResilienceRegistry`,
`RiskAssessment`), never a `CanonicalNetwork` and never a solver. A test walks
the compiled source of every module here and asserts the absence of those
imports, because the guarantee has to be checkable rather than promised.

Usage::

    from netgravity.orchestrator.twin import DigitalTwinService

    twin = DigitalTwinService()
    ref  = twin.update(state)                       # publish
    view = twin.get(snapshot_id, scenario_id)       # read
    diff = twin.compare_scenario(snapshot_id, scenario_id)
"""

from netgravity.orchestrator.twin.builder import (
    apply_delta,
    build_flow_aggregate,
    build_twin_state,
    build_unavailable_state,
    make_state_id,
    to_delta,
)
from netgravity.orchestrator.twin.service import DEFAULT_FLOW_LIMIT, DigitalTwinService
from netgravity.orchestrator.twin.store import DigitalTwinStore, TwinStateNotFound

__all__ = [
    "DigitalTwinService",
    "DigitalTwinStore",
    "TwinStateNotFound",
    "DEFAULT_FLOW_LIMIT",
    "build_twin_state",
    "build_unavailable_state",
    "build_flow_aggregate",
    "make_state_id",
    "to_delta",
    "apply_delta",
]
