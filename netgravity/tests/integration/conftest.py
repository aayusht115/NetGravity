"""
Phase 2 integration — shared fixtures.

Two rules govern everything here:

1. **The deterministic chain is REAL.** MILP, REI, RF, numeric grounding and
   governance all run their production code paths. Nothing in this package
   replaces them with a mock; a test that passed against a fake MILP would prove
   nothing about integration.

2. **Only the outside world is faked.** The LLM gateway and the external signal
   source are injected doubles, because one costs shared budget and the other is
   a third party. Both are boundaries of the system, not parts of it.

The Delhi network is engineered to be hand-calculable so the Phase 2 acceptance
figures (REI = 0.8, RF = 0.94) are arithmetic, not whatever the solver happens
to produce.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMResponse, LLMUsage
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    EventSeverity,
    ExternalSignal,
)
from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    TransportMode,
)


# ---------------------------------------------------------------------------
# The Delhi network — engineered so REI(DC_DELHI) is exactly 0.8
# ---------------------------------------------------------------------------

def _config(**overrides: Any) -> OptimizationConfig:
    base = dict(
        solver_name="HiGHS", enable_inventory=False, enforce_sla=False,
        enable_carbon_cost=False, minimum_throughput_enabled=False,
        allow_shortage=False, cost_period=CostPeriod.MONTH,
        mip_gap=0.0, time_limit_seconds=60, verbose=False,
    )
    base.update(overrides)
    return OptimizationConfig(**base)


def build_delhi_network(
    *,
    config: Optional[OptimizationConfig] = None,
    delhi_capacity: float = 5_000.0,
) -> CanonicalNetwork:
    """
    Three DCs, three markets, each market with exactly one primary and one
    backup DC. Every cost is a round number so the whole chain is checkable by
    hand.

        PLANT_N --(1.0)--> DC_DELHI   --(2.0)--> MKT_NORTH   (demand 100)
                           DC_MUMBAI  --(6.0)--> MKT_NORTH   backup
                --(1.0)--> DC_MUMBAI  --(3.0)--> MKT_WEST    (demand 100)
                           DC_KOLKATA --(8.0)--> MKT_WEST    backup
                --(1.0)--> DC_KOLKATA --(4.0)--> MKT_EAST    (demand 100)
                           DC_DELHI   --(6.0)--> MKT_EAST    backup

    Fixed costs are zero, so business network cost is pure transport and the
    arithmetic is transparent:

        baseline C0   = 100·(1+2) + 100·(1+3) + 100·(1+4) = 1,200
        DC_DELHI  out = MKT_NORTH falls back to Mumbai @6 → 700   PI =  400
        DC_MUMBAI out = MKT_WEST  falls back to Kolkata @8 → 900  PI =  500
        DC_KOLKATA out= MKT_EAST  falls back to Delhi @6 → 700    PI =  200

        max EI = 500  ⇒  REI(DELHI) = 400/500 = 0.80   ← the Phase 2 figure
                         REI(MUMBAI)  = 1.00
                         REI(KOLKATA) = 0.40

    With an event probability of 0.7 at DC_DELHI:

        RF = 0.7 + 0.8 − (0.7 × 0.8) = 0.94
    """
    facilities = [
        FacilityRecord(id="PLANT_N", name="North Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=99_999,
                       is_mandatory=True, is_closable=False,
                       fixed_cost_per_year=0.0),
        FacilityRecord(id="DC_DELHI", name="Delhi NCR DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=delhi_capacity,
                       fixed_cost_per_year=0.0),
        FacilityRecord(id="DC_MUMBAI", name="Mumbai DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=5_000.0,
                       fixed_cost_per_year=0.0),
        FacilityRecord(id="DC_KOLKATA", name="Kolkata DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING,
                       capacity_units_per_period=5_000.0,
                       fixed_cost_per_year=0.0),
        FacilityRecord(id="MKT_NORTH", name="North Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
        FacilityRecord(id="MKT_WEST", name="West Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
        FacilityRecord(id="MKT_EAST", name="East Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
    ]

    def lane(origin: str, dest: str, rate: float) -> LaneRecord:
        return LaneRecord(origin_id=origin, destination_id=dest,
                          mode=TransportMode.ROAD, rate_per_unit=rate,
                          distance_km=100.0, lead_time_days=1.0)

    lanes = [
        lane("PLANT_N", "DC_DELHI", 1.0),
        lane("PLANT_N", "DC_MUMBAI", 1.0),
        lane("PLANT_N", "DC_KOLKATA", 1.0),
        # MKT_NORTH: Delhi primary, Mumbai backup.
        lane("DC_DELHI", "MKT_NORTH", 2.0),
        lane("DC_MUMBAI", "MKT_NORTH", 6.0),
        # MKT_WEST: Mumbai primary, Kolkata backup.
        lane("DC_MUMBAI", "MKT_WEST", 3.0),
        lane("DC_KOLKATA", "MKT_WEST", 8.0),
        # MKT_EAST: Kolkata primary, Delhi backup.
        lane("DC_KOLKATA", "MKT_EAST", 4.0),
        lane("DC_DELHI", "MKT_EAST", 6.0),
    ]

    demands = [
        DemandRecord(market_id="MKT_NORTH", product_id="P1", quantity=100.0, std_dev=0.0),
        DemandRecord(market_id="MKT_WEST", product_id="P1", quantity=100.0, std_dev=0.0),
        DemandRecord(market_id="MKT_EAST", product_id="P1", quantity=100.0, std_dev=0.0),
    ]

    net = CanonicalNetwork(
        network_id="PHASE2_DELHI",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=demands,
        lanes=lanes,
        config=config or _config(),
    )
    return net.model_copy(update={"data_version": net.compute_data_version()})


def build_infeasible_network() -> CanonicalNetwork:
    """A market no DC can reach. The MILP proves INFEASIBLE, it does not error."""
    net = build_delhi_network()
    orphan = FacilityRecord(id="MKT_ORPHAN", name="Unreachable Market",
                            role=NodeRole.MARKET, status=FacilityStatus.EXISTING,
                            is_closable=False)
    demand = DemandRecord(market_id="MKT_ORPHAN", product_id="P1",
                          quantity=50.0, std_dev=0.0)
    updated = net.model_copy(update={
        "network_id": "PHASE2_INFEASIBLE",
        "facilities": [*net.facilities, orphan],
        "demands": [*net.demands, demand],
    })
    return updated.model_copy(update={"data_version": updated.compute_data_version()})


# ---------------------------------------------------------------------------
# External signals — the outside world, injected
# ---------------------------------------------------------------------------

def flood_signal(
    *,
    probability: Optional[float] = 0.7,
    nodes: Optional[List[str]] = None,
    location: str = "Delhi",
    severity: EventSeverity = EventSeverity.SEVERE,
) -> ExternalSignal:
    """
    A flood warning. `probability=None` models a source that stated severity but
    no probability — the case where RF must refuse rather than infer.
    """
    return ExternalSignal(
        event_type="FLOOD",
        location=location,
        severity=severity,
        event_probability=probability,
        probability_basis=("stated in the met office bulletin"
                           if probability is not None else None),
        confidence=0.9,
        source="india_met_department",
        source_quality="OFFICIAL",
        evidence="Monsoon flood warning issued for the Delhi NCR region.",
        affected_entity_ids=list(nodes) if nodes is not None else ["DC_DELHI"],
    )


# ---------------------------------------------------------------------------
# Fake LLM gateway
# ---------------------------------------------------------------------------

class FakeGateway(LLMGateway):
    """
    An LLM gateway that returns scripted output and makes no network call.

    Subclasses the real gateway so every consumer sees the genuine interface —
    `available`, `generate`, `stats`, the same `LLMResponse` shape. Only the
    transport is replaced.

    `responses` maps a purpose ("intent", "reasoning", "external_signal") to the
    raw string the model would have returned. An unmapped purpose raises, which
    is deliberate: a test that silently gets a default response is not testing
    what it thinks it is.
    """

    def __init__(self, responses: Dict[str, str], *, available: bool = True) -> None:
        super().__init__()
        self._responses = dict(responses)
        self._available = available
        self.calls: List[str] = []
        self.prompts: List[str] = []

    @property
    def available(self) -> bool:      # type: ignore[override]
        return self._available

    def unavailable_reason(self) -> str:   # type: ignore[override]
        return "fake gateway configured as unavailable"

    def generate(self, prompt: str, *, purpose: str = "generic") -> LLMResponse:  # type: ignore[override]
        self.calls.append(purpose)
        self.prompts.append(prompt)
        if purpose not in self._responses:
            raise AssertionError(
                f"FakeGateway received an unscripted call for purpose '{purpose}'. "
                f"Scripted: {sorted(self._responses)}"
            )
        return LLMResponse(
            output=self._responses[purpose],
            request_id=f"fake-{len(self.calls)}",
            usage=LLMUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            latency_seconds=0.001,
        )


def reasoning_json(
    *,
    summary: str,
    recommendation: str = "Review with a planner.",
    confidence: str = "HIGH",
    evidence: Optional[List[str]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the JSON blob the reasoning agent expects back from a model."""
    return json.dumps({
        "summary": summary,
        "key_drivers": ["driver"],
        "risks": ["risk"],
        "recommendation": recommendation,
        "confidence": confidence,
        "evidence": evidence if evidence is not None else ["cited figure"],
        "claims": claims or [],
    })


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def delhi_network() -> CanonicalNetwork:
    return build_delhi_network()


@pytest.fixture
def orch(delhi_network):
    """A fully wired orchestrator with the LLM disabled — the offline default."""
    return build_orchestrator(network=delhi_network, enable_llm=False)


@pytest.fixture
def planner_actor() -> Actor:
    return Actor(actor_id="planner_1", role=ActorRole.PLANNER,
                 display_name="Integration Planner")
