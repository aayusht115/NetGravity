"""
Orchestrator — Authoritative KPI/Metric contract.

Phase 9.1 closes one specific gap the forensic audit found: every deterministic
engine already produces a typed, well-tested result — `NetworkKPIs`,
`FacilityResilienceRegistry`, `RiskAssessment`, `ForecastResult`, `ScenarioResult`
— but nothing exposes them through ONE consistent envelope carrying status,
provenance and threshold context together. A caller wanting "the fill rate, and
whether I should trust it" had to know which of five different typed models to
read and which of three different zero-denominator conventions that particular
model uses.

`KPIResult` is that envelope. It wraps a value that already exists somewhere
authoritative; it never computes one. Every engine keeps sole ownership of its
own arithmetic — see `netgravity/orchestrator/metrics/registry.py` for the
mapping from metric to owning engine.

WHY NOT `AgentResult`
----------------------
`AgentResult` (schemas/agent_result.py) already has a status envelope, and reuse
was considered first. It was rejected because the two questions are genuinely
different. `AgentResult.status` asks "did the CAPABILITY execute successfully?"
`KPIResult.status` asks "does this NUMBER exist and can it be trusted?" — and
those can disagree in both directions:

  * `resilience.assess` can SUCCEED as a capability while `rei` for one
    particular facility is `NOT_COMPUTED` (infeasible disruption) — capability
    success, metric non-availability.
  * `risk.compute_rf` can report `NOT_COMPUTABLE` for a facility's RF while the
    capability itself ran perfectly correctly — that IS the correct, intended
    output, not a capability failure.

Collapsing these into one status vocabulary would force a caller to guess which
question was being answered. They stay separate, and `KPIResult.source_capability`
plus `KPIResult.formula_id` are how the two connect: this metric came from this
capability's run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The value type a KPIResult wraps. Left unbound deliberately: a metric may be
#: a float (cost, REI), an int (facility count), or a small structured object
#: (a per-quantile forecast point). The envelope constrains STATUS, not VALUE
#: shape.
T = TypeVar("T")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KPIStatus(str, Enum):
    """
    Whether a metric VALUE exists and may be trusted — not whether the
    capability that would produce it ran.

    VALID                  Computed from complete, in-range inputs by its
                            declared formula. May be cited as fact.

    INSUFFICIENT_EVIDENCE   The required inputs were never supplied — no
                            capability ran that would produce them, or an
                            optional upstream capability didn't (e.g. no
                            external signal was ever offered, so `likelihood`
                            was never present to combine with REI). Distinct
                            from NOT_COMPUTABLE: here, nothing was attempted.

    NOT_COMPUTABLE          The relevant capability ran, and explicitly
                            reported that this particular value could not be
                            derived (REI `NOT_COMPUTED` because the disrupted
                            solve hit a time limit; RF `NOT_COMPUTABLE` because
                            REI came from a stale snapshot). The engine's own
                            reason is carried in `metadata['reason']`.

    INFEASIBLE              The underlying solve was proven infeasible.
                            Distinct from NOT_COMPUTABLE because infeasibility
                            is itself a finding — the network genuinely cannot
                            satisfy the modelled constraints — not an absence
                            of information.

    INVALID_INPUT           An input was present but out of its declared
                            domain (a probability outside [0, 1], a negative
                            REI outside tolerance). The engine refused to
                            silently clamp or discard it.

    Never mapped from these statuses: a plain Python `None` read as "zero", or
    a caller substituting a default. Every non-VALID status carries `value =
    None` — enforced below — specifically so that pattern is unavailable.
    """
    VALID                 = "VALID"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_COMPUTABLE        = "NOT_COMPUTABLE"
    INFEASIBLE            = "INFEASIBLE"
    INVALID_INPUT         = "INVALID_INPUT"


#: Statuses that permit a non-None `value`. Kept as a set (not just "VALID")
#: because a future status might legitimately carry a value alongside a
#: caveat; today only VALID does.
_VALUED_STATUSES = frozenset({KPIStatus.VALID})


class MetricScope(str, Enum):
    """What the metric describes — a whole network, one entity, or one lane."""
    NETWORK  = "NETWORK"
    FACILITY = "FACILITY"
    LANE     = "LANE"
    MARKET   = "MARKET"
    PRODUCT  = "PRODUCT"
    SCENARIO_COMPARISON = "SCENARIO_COMPARISON"


class ThresholdBasis(str, Enum):
    """
    Where a threshold's specific numeric value came from.

    Recorded because "why is it 0.8 and not 0.7" must have an answer that does
    not require reading the git history. Distinguishing these four also
    prevents an unconfigured, disabled threshold (default `None` in
    `RiskClassificationRules`) from being reported as though it fired.
    """
    MATHEMATICAL       = "MATHEMATICAL"        # derives from the formula itself
    BUSINESS_POLICY    = "BUSINESS_POLICY"     # an explicit configured decision
    ENGINEERING        = "ENGINEERING"         # a safeguard (retry limits, etc.)
    STATISTICAL        = "STATISTICAL"         # a named statistical convention
    UNCONFIGURED       = "UNCONFIGURED"        # exists in code, disabled by default


class ThresholdSpec(BaseModel):
    """
    One governing threshold, traceable to its source constant.

    Deliberately metadata-only: this describes a threshold that ALREADY exists
    in code (see `netgravity/orchestrator/metrics/thresholds.py` for the
    verified catalogue). Nothing here changes a value; changing thresholds is
    explicitly out of scope for this phase.
    """
    threshold_id: str
    metric_id: str
    operator: str                 # ">=" | ">" | "<=" | "<" | "=="
    value: Optional[float]        # None when UNCONFIGURED (disabled by default)
    unit: str = ""
    severity: str = ""            # e.g. "HUMAN_ONLY", "APPROVAL_REQUIRED", "CRITICAL"
    basis: ThresholdBasis
    action_if_triggered: str = ""
    source_file: str = ""
    configurable: bool = True

    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def is_active(self) -> bool:
        """False for a disabled (None-valued) opt-in threshold."""
        return self.value is not None

    def evaluate(self, value: Optional[float]) -> bool:
        """
        Whether `value` trips this threshold.

        Returns False (never raises, never guesses) when the threshold is
        unconfigured or the value is unavailable — an absent measurement
        cannot trigger a rule about it.
        """
        if not self.is_active or value is None:
            return False
        ops = {
            ">=": value >= self.value, ">": value > self.value,
            "<=": value <= self.value, "<": value < self.value,
            "==": value == self.value,
        }
        return ops.get(self.operator, False)


class KPIResult(BaseModel, Generic[T]):
    """
    One metric value, with enough context to answer "where did this come from,
    and can I trust it?" without inspecting an LLM response.

    Frozen except in spirit — `model_config` doesn't set `frozen=True` because
    Pydantic generics with `frozen=True` reject `model_copy(update=...)` in some
    versions; treat instances as immutable by convention, matching every other
    result contract in this codebase.
    """
    metric_id: str                       # e.g. "demand_fill_rate", "rei", "risk_factor"
    display_name: str = ""
    value: Optional[T] = None
    unit: str = ""
    scope: MetricScope = MetricScope.NETWORK
    entity_id: Optional[str] = None      # facility/lane/market id, when scope requires one

    formula_id: str = ""                 # e.g. "REI_NORMALIZATION", "RF_COMPOUND"
    source_capability: str = ""          # capability id that produced the input, e.g. "resilience.assess"
    authoritative_owner: str = ""        # module/class that actually computed it

    status: KPIStatus = KPIStatus.VALID
    threshold: Optional[ThresholdSpec] = None
    triggered: Optional[bool] = None     # whether `threshold` fired for this value

    # Traceable inputs: name -> the value actually used, so a reader can verify
    # the formula by hand without re-running the engine.
    input_evidence: Dict[str, Any] = Field(default_factory=dict)

    snapshot_id: Optional[str] = None
    scenario_id: Optional[str] = None
    execution_id: str = ""
    calculated_at: str = Field(default_factory=_utc_now)

    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def value_matches_status(self) -> "KPIResult[T]":
        """
        The single invariant this whole contract exists to enforce: a status
        that means "no trustworthy number" may not carry one, and VALID may not
        be silently empty.
        """
        if self.status not in _VALUED_STATUSES and self.value is not None:
            raise ValueError(
                f"KPIResult for '{self.metric_id}' has status {self.status.value} "
                f"but carries a value ({self.value!r}). A status that means the "
                f"metric could not be trusted must not present a number — a "
                f"caller would read it as a measurement. Put the rejected figure "
                f"in `metadata` if it is needed for diagnosis."
            )
        if self.status in _VALUED_STATUSES and self.value is None:
            raise ValueError(
                f"KPIResult for '{self.metric_id}' is VALID but carries no "
                f"value. Use INSUFFICIENT_EVIDENCE, NOT_COMPUTABLE, INFEASIBLE "
                f"or INVALID_INPUT instead — never a value-less VALID."
            )
        return self

    @property
    def is_valid(self) -> bool:
        return self.status == KPIStatus.VALID

    @property
    def is_trustworthy(self) -> bool:
        """Alias read the way a caller phrases the question."""
        return self.is_valid

    def require(self) -> T:
        """Return the value or raise — the KPI-layer equivalent of `AgentResult.require()`."""
        if self.value is None:
            reason = self.metadata.get("reason", self.status.value)
            raise ValueError(
                f"Metric '{self.metric_id}' has no usable value "
                f"({self.status.value}): {reason}"
            )
        return self.value

    @classmethod
    def insufficient_evidence(
        cls, metric_id: str, *, reason: str, scope: MetricScope = MetricScope.NETWORK,
        entity_id: Optional[str] = None, source_capability: str = "",
        execution_id: str = "", **kw: Any,
    ) -> "KPIResult":
        return cls(
            metric_id=metric_id, value=None, scope=scope, entity_id=entity_id,
            source_capability=source_capability, execution_id=execution_id,
            status=KPIStatus.INSUFFICIENT_EVIDENCE,
            metadata={"reason": reason}, **kw,
        )

    @classmethod
    def not_computable(
        cls, metric_id: str, *, reason: str, scope: MetricScope = MetricScope.NETWORK,
        entity_id: Optional[str] = None, source_capability: str = "",
        execution_id: str = "", **kw: Any,
    ) -> "KPIResult":
        return cls(
            metric_id=metric_id, value=None, scope=scope, entity_id=entity_id,
            source_capability=source_capability, execution_id=execution_id,
            status=KPIStatus.NOT_COMPUTABLE,
            metadata={"reason": reason}, **kw,
        )


class ScenarioMetricDelta(BaseModel):
    """
    One metric's change between two scopes (BASELINE / CURRENT / SCENARIO).

    Mirrors `orchestrator.twin.schemas.twin.MetricDelta` deliberately — same
    field names, same `NOT_COMPARABLE` semantics for a metric missing on either
    side — so a reader who already knows that contract needs nothing new here.
    This one exists because the twin's delta list is confined to `TwinKPIs`
    fields; risk/resilience figures (`rei`, `risk_factor`) live in a separate
    `RiskContext` the twin never diffs. See
    `netgravity/orchestrator/metrics/scenario.py`.
    """
    metric_id: str
    baseline_value: Optional[float] = None
    comparison_value: Optional[float] = None
    abs_delta: Optional[float] = None
    pct_delta: Optional[float] = None
    #: "INCREASED" | "DECREASED" | "UNCHANGED" | "NOT_COMPARABLE"
    direction: str = "NOT_COMPARABLE"
    reason: str = ""

    model_config = ConfigDict(extra="forbid")

    @property
    def is_comparable(self) -> bool:
        return self.direction != "NOT_COMPARABLE"


class TriggeredThreshold(BaseModel):
    """One threshold that fired, against the value that fired it."""
    threshold: ThresholdSpec
    metric_id: str
    value: float
    entity_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class UnavailableMetric(BaseModel):
    """One metric the evidence package expected but could not populate."""
    metric_id: str
    status: KPIStatus
    reason: str
    scope: MetricScope = MetricScope.NETWORK
    entity_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class EvidenceProvenance(BaseModel):
    """Which run, which data version, this whole package describes."""
    execution_id: str = ""
    snapshot_id: Optional[str] = None
    scenario_id: Optional[str] = None
    generated_at: str = Field(default_factory=_utc_now)
    #: capability_id -> AgentStatus.value, copied from
    #: `ExecutionContext.capability_provenance()` so a reader of the package
    #: alone can see what actually ran without a second lookup.
    capability_statuses: Dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class AuthoritativeEvidencePackage(BaseModel):
    """
    Every authoritative number for one execution, assembled in one place.

    This is a VIEW, built on demand from `ExecutionContext` by
    `netgravity/orchestrator/metrics/registry.py::KPIRegistry.evidence_package`.
    It stores no state of its own between calls and computes nothing beyond the
    legitimate derived deltas documented on `KPIRegistry` — every value here
    already existed in a typed, authoritative engine result.

    Consumption boundary (Phase 9.1 establishes the interface; Phase 9.2+ wires
    it in): the Reasoning Agent may READ this package. Nothing in this schema,
    and nothing in the registry that builds it, accepts a write from the
    Reasoning Agent, the LLM gateway, or the frontend — there is no setter, no
    merge-from-narrative path, and no field an external caller can populate
    that would be mistaken for one of these. See
    `netgravity/tests/test_kpi_authoritative_layer.py::TestAuthorityBoundary`.
    """
    network_kpis: Dict[str, KPIResult] = Field(default_factory=dict)
    facility_kpis: Dict[str, Dict[str, KPIResult]] = Field(default_factory=dict)
    lane_kpis: Dict[str, Dict[str, KPIResult]] = Field(default_factory=dict)
    forecast_metrics: Dict[str, KPIResult] = Field(default_factory=dict)
    resilience_metrics: Dict[str, KPIResult] = Field(default_factory=dict)
    risk_metrics: Dict[str, KPIResult] = Field(default_factory=dict)
    sustainability_metrics: Dict[str, KPIResult] = Field(default_factory=dict)
    scenario_comparison: List[ScenarioMetricDelta] = Field(default_factory=list)

    triggered_thresholds: List[TriggeredThreshold] = Field(default_factory=list)
    unavailable_evidence: List[UnavailableMetric] = Field(default_factory=list)
    provenance: EvidenceProvenance = Field(default_factory=EvidenceProvenance)

    model_config = ConfigDict(extra="forbid")

    def all_results(self) -> List[KPIResult]:
        """Every KPIResult in the package, flattened, for bulk inspection."""
        out: List[KPIResult] = list(self.network_kpis.values())
        for group in self.facility_kpis.values():
            out.extend(group.values())
        for group in self.lane_kpis.values():
            out.extend(group.values())
        out.extend(self.forecast_metrics.values())
        out.extend(self.resilience_metrics.values())
        out.extend(self.risk_metrics.values())
        out.extend(self.sustainability_metrics.values())
        return out

    def to_evidence_payload(self) -> Dict[str, Any]:
        """
        Flatten to the plain-dict shape
        `netgravity.orchestrator.reasoning.evidence.build_evidence_pack` already
        consumes, so this package can supply that function's input without
        either module knowing about the other's types.

        Forward-compatibility only: nothing in this phase calls this from the
        live reasoning path. A test proves the round trip works.
        """
        payload: Dict[str, Any] = {}
        for metric_id, result in self.network_kpis.items():
            if result.is_valid:
                payload[metric_id] = result.value
        for group_name, group in (
            ("resilience", self.resilience_metrics),
            ("risk", self.risk_metrics),
            ("sustainability", self.sustainability_metrics),
        ):
            block = {mid: r.value for mid, r in group.items() if r.is_valid}
            if block:
                payload[group_name] = block
        if self.facility_kpis:
            payload["facilities"] = [
                {"facility_id": fid, **{mid: r.value for mid, r in metrics.items() if r.is_valid}}
                for fid, metrics in self.facility_kpis.items()
            ]
        return payload


__all__ = [
    "AuthoritativeEvidencePackage",
    "EvidenceProvenance",
    "KPIResult",
    "KPIStatus",
    "MetricScope",
    "ScenarioMetricDelta",
    "ThresholdBasis",
    "ThresholdSpec",
    "TriggeredThreshold",
    "UnavailableMetric",
]
