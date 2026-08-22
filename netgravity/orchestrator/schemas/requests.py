"""
Orchestrator — Request, intent and actor schemas.

These are the API-facing contracts. They are deliberately kept separate from
internal execution classes so the HTTP surface can evolve independently.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActorRole(str, Enum):
    """
    Who is driving the run. Drives authorization, never the LLM.

    SYSTEM is for automated triggers (e.g. an external-signal poller); it
    deliberately has the FEWEST action rights, because an unattended process
    must not be able to authorise structural change.
    """
    VIEWER   = "VIEWER"     # read-only analysis
    PLANNER  = "PLANNER"    # may create and run scenarios
    APPROVER = "APPROVER"   # may approve actions requiring approval
    ADMIN    = "ADMIN"
    SYSTEM   = "SYSTEM"     # automated trigger; analysis only


class Actor(BaseModel):
    """The authenticated principal on whose behalf a run executes."""
    actor_id: str = "anonymous"
    role: ActorRole = ActorRole.VIEWER
    display_name: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class Intent(str, Enum):
    """
    What the caller is asking for.

    The intent selects a workflow template; it never selects code paths
    scattered through the orchestrator core.
    """
    NETWORK_STATE_QUERY   = "NETWORK_STATE_QUERY"    # "what does the network look like now?"
    SCENARIO_ANALYSIS     = "SCENARIO_ANALYSIS"      # "what if we close Delhi?"
    SCENARIO_COMPARISON   = "SCENARIO_COMPARISON"    # "compare closing Delhi vs Mumbai"
    RESILIENCE_QUERY      = "RESILIENCE_QUERY"       # "which facility is most exposed?"
    EXTERNAL_EVENT        = "EXTERNAL_EVENT"         # "flooding expected around Delhi"
    OPTIMIZATION_REQUEST  = "OPTIMIZATION_REQUEST"   # "optimise the current footprint"
    # "why is Delhi DC high risk?" — answered from evidence that ALREADY exists.
    # Distinct from RESILIENCE_QUERY because it must not trigger a fresh
    # optimization: it explains what has been computed, it does not compute more.
    EXPLANATION           = "EXPLANATION"
    # "how many warehouses do we have?" — answerable from the digital twin
    # alone. Distinct from NETWORK_STATE_QUERY, which reports cost and service
    # and therefore genuinely requires a solve.
    STATUS_QUERY          = "STATUS_QUERY"
    # "forecast demand for the next six months". Recognised so the system can
    # answer honestly; no forecasting capability is registered today.
    FORECAST              = "FORECAST"
    UNKNOWN               = "UNKNOWN"


class ScenarioActionType(str, Enum):
    """Structural change a scenario may request. Mirrors the scenario engine."""
    CLOSE_FACILITY  = "CLOSE_FACILITY"
    OPEN_FACILITY   = "OPEN_FACILITY"
    CHANGE_CAPACITY = "CHANGE_CAPACITY"
    CHANGE_DEMAND   = "CHANGE_DEMAND"
    SHIFT_VOLUME    = "SHIFT_VOLUME"


class ScenarioIntentSpec(BaseModel):
    """
    A structured, machine-checkable description of the scenario the caller
    described in natural language.

    This is the ONLY thing the LLM is allowed to produce for scenarios: a
    proposal. Every field is validated against the real network before any
    engine runs, and the LLM never touches the resulting network state.
    """
    action: ScenarioActionType
    facility_ids: List[str] = Field(default_factory=list)
    target_facility_id: Optional[str] = None      # for SHIFT_VOLUME
    capacity_multiplier: Optional[float] = None
    # ABSOLUTE change in units per period, signed: -2000 reduces capacity by
    # 2,000 units/day, +2000 increases it. Present because operators state
    # capacity changes in units, not ratios, and nothing upstream of the network
    # knows a facility's current capacity to convert one into the other.
    # Mutually exclusive with capacity_multiplier — see ScenarioValidator.
    capacity_delta_units: Optional[float] = None
    # An absolute TARGET capacity: "set DC_DELHI capacity to 8,000 units/day".
    # Distinct from capacity_delta_units, and the distinction is the whole
    # point: "reduce by 2,000" and "set to 2,000" are different instructions
    # that coincide only by accident. Representing one as the other requires
    # knowing the current capacity, which the caller may not, and silently
    # guessing wrong changes the answer rather than degrading it. Mutually
    # exclusive with the other two — see ScenarioValidator.
    capacity_set_units: Optional[float] = None
    demand_multiplier: Optional[float] = None
    label: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @property
    def capacity_operation(self) -> Optional[str]:
        """"SET" | "DECREASE" | "INCREASE" | "SCALE", or None."""
        if self.capacity_set_units is not None:
            return "SET"
        if self.capacity_delta_units is not None:
            return "DECREASE" if self.capacity_delta_units < 0 else "INCREASE"
        if self.capacity_multiplier is not None:
            return "SCALE"
        return None


class IntentResolution(BaseModel):
    """
    Result of interpreting a request.

    `source` records HOW the intent was derived, so a downstream reader can
    tell rule-based parsing from model interpretation. `confidence` is
    advisory only — it never gates a deterministic calculation.
    """
    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    source: str = "rules"          # "rules" | "llm" | "explicit"
    scenarios: List[ScenarioIntentSpec] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    rationale: str = ""
    raw_model_output: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class EventSeverity(str, Enum):
    """
    How BAD the event would be if it happened.

    Categorical and deliberately NOT numeric. Severity is a different variable
    from probability and must never be silently converted into one — an event
    can be catastrophic and very unlikely, or trivial and near-certain.
    """
    LOW      = "LOW"
    MODERATE = "MODERATE"
    HIGH     = "HIGH"
    SEVERE   = "SEVERE"
    CRITICAL = "CRITICAL"
    UNKNOWN  = "UNKNOWN"


class ExternalSignal(BaseModel):
    """
    An external-world event with full provenance.

    External information is EVIDENCE, never observed network truth. It carries
    its source and confidence and is never merged into a network snapshot.

    THREE INDEPENDENT VARIABLES
    ───────────────────────────
    These are routinely conflated and must not be:

        event_probability   How LIKELY the event is.        → feeds RF as P
        severity            How BAD it would be.            → never feeds RF
        confidence          How much we trust THIS
                            assessment.                     → never feeds RF

    `probability = 0.4` with `confidence = 0.95` is entirely coherent: we are
    highly confident the chance is low.

    `event_probability` is populated ONLY when the source states or clearly
    implies a probability. It is never derived from severity, confidence or
    source quality. When absent it stays None and RF reports NOT_COMPUTABLE
    rather than a fabricated number.

    SCHEMA CHANGE (v2)
    ──────────────────
    The former `likelihood` field was renamed `event_probability` because it was
    being populated from severity via a lookup table, which conflated the two.
    Passing `likelihood` now raises with a migration message rather than being
    silently reinterpreted.
    """
    schema_version: int = 2

    event_type: str
    location: str = ""

    # How bad, if it happens. Categorical. NEVER converted to probability.
    severity: EventSeverity = EventSeverity.UNKNOWN

    # How likely, in [0, 1]. The ONLY field that may feed RF as P.
    # None means "no defensible probability available" — not "zero chance".
    event_probability: Optional[float] = None
    # Free text describing where the probability came from. Present only when
    # event_probability is set, so provenance is always inspectable.
    probability_basis: Optional[str] = None

    source: str = "unspecified"
    source_quality: Optional[str] = None
    timestamp: Optional[str] = None
    validity_period: Optional[str] = None

    # Confidence in this ASSESSMENT. Not a probability of the event.
    confidence: float = 0.0

    evidence: str = ""
    # Network entities the signal plausibly affects. Always re-validated
    # against the real network before use.
    affected_entity_ids: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("event_probability")
    @classmethod
    def probability_in_range(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not (0.0 <= v <= 1.0):
            raise ValueError(
                f"event_probability must lie in [0, 1], got {v}. Out-of-range values "
                f"are rejected rather than clamped."
            )
        return v

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must lie in [0, 1], got {v}.")
        return v

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_likelihood(cls, data: Any) -> Any:
        """
        Refuse the removed `likelihood` field with an explicit migration message.

        Silently mapping it to `event_probability` would preserve exactly the
        bug this rename exists to fix, because callers were feeding it
        severity-derived values.
        """
        if isinstance(data, dict) and "likelihood" in data:
            raise ValueError(
                "ExternalSignal.likelihood was removed in schema v2 because it "
                "conflated event probability with severity. Use 'event_probability' "
                "for a genuine probability in [0, 1], and 'severity' "
                "(LOW/MODERATE/HIGH/SEVERE/CRITICAL) for impact. Severity must never "
                "be supplied as a probability."
            )
        return data

    @property
    def has_defensible_probability(self) -> bool:
        """True only when a real probability is available for RF."""
        return self.event_probability is not None


class OrchestratorRequest(BaseModel):
    """
    One inbound request to the control plane.

    `request_id` is the idempotency key: two requests carrying the same
    request_id resolve to the same execution rather than running twice.
    """
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    input: str = ""
    actor: Actor = Field(default_factory=Actor)

    # Pin the run to a specific network snapshot. When omitted the current
    # snapshot is used and recorded on the execution.
    network_snapshot_id: Optional[str] = None

    # Bypass intent interpretation entirely (useful for programmatic callers
    # and for running without the LLM).
    explicit_intent: Optional[Intent] = None
    explicit_scenarios: List[ScenarioIntentSpec] = Field(default_factory=list)
    external_signal: Optional[ExternalSignal] = None

    # Opt out of all model calls for this run. Deterministic results are
    # unaffected; only interpretation and narrative degrade to rule-based.
    disable_llm: bool = False

    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
