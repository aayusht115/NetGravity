"""
NetGravity — Typed Data Schemas: Resilience Assessment Inputs
=============================================================
Version: 1.0.0

Input contracts for the deterministic Facility Resilience Assessment and the
cost-based Risk Exposure Index (REI).

This module defines ONLY inputs (what disruption to run, which cost components
constitute business cost, which deterministic risk rules apply). Outputs live in
`schemas/results.py` alongside the other result contracts.

No optimization logic, no UI logic, no LLM logic is present here.

Conceptual separation enforced by these schemas
───────────────────────────────────────────────
    Solver Objective        What the MILP mathematically minimises.
                            Includes artificial penalties.

    Business Network Cost   The economic network cost used for REI.
                            C_business = C_facility + C_opening + C_transport
                                       + C_handling + C_inventory + C_carbon*
                            (*carbon only when genuinely priced — see
                             ResilienceCostBasis.include_carbon_cost)

    Shortage Penalty        A mathematical penalty on unmet demand. NOT a
                            financial cost. Excluded from business cost by
                            default; retained as a resilience diagnostic.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from netgravity.schemas.network import NodeRole


# ---------------------------------------------------------------------------
# Disruption taxonomy
# ---------------------------------------------------------------------------

class DisruptionType(str, Enum):
    """
    Type of disruption applied during a resilience assessment.

    V1 supports FACILITY_FAILURE only. The existing ResilienceEngine covers
    LANE_FAILURE / CAPACITY_LOSS / DEMAND_SURGE as standalone disruption
    analyses; those are not yet part of the REI registry because REI requires a
    single, uniform disruption assumption across all compared entities.
    """
    FACILITY_FAILURE = "FACILITY_FAILURE"


class DisruptionPeriodBasis(str, Enum):
    """
    Temporal basis of the disruption experiment.

    MODELLED_PLANNING_PERIOD
        The facility is unavailable for the entire modelled planning period.
        This is the ONLY defensible basis while the MILP is single-period:
        DemandRecord.quantity, LaneRecord capacities and facility capacities are
        all expressed per planning period, and no decision variable is indexed
        by time.

    Time to Recovery (TTR = 1/2/4/8 weeks), as used in the HBR supply-chain
    resilience framework, requires a time-phased model. It is deliberately NOT
    offered here — see DisruptionConfig.time_to_recovery_days, which rejects any
    value rather than fabricating a temporal calculation the model cannot make.
    """
    MODELLED_PLANNING_PERIOD = "MODELLED_PLANNING_PERIOD"


# ---------------------------------------------------------------------------
# Business cost basis
# ---------------------------------------------------------------------------

class ResilienceCostBasis(BaseModel):
    """
    Declares exactly which cost components constitute Business Network Cost.

    Business Network Cost is the economic quantity used for Performance Impact
    and therefore for REI. It is deliberately NOT the raw solver objective.

    Carbon note
    ───────────
    `include_carbon_cost` is honoured only when carbon is genuinely part of the
    configured business objective, i.e. when `OptimizationConfig.enable_carbon_cost`
    is True (a real monetary carbon price applies). Under
    ObjectiveMode.WEIGHTED_COST_CARBON the `carbon_weight` term is a modelling
    preference weight — like the shortage penalty, it is a mathematical device
    rather than a financial cost — and is always excluded from business cost.

    Shortage note
    ─────────────
    `include_shortage_penalty` defaults to False. The shortage penalty
    (default 1e6 per unit) is an artificial device that forces demand coverage;
    treating it as revenue loss would invent a monetary value for lost demand.
    Set it True only when the configured penalty has been explicitly validated
    as a real business cost of lost demand.
    """
    include_facility_cost:  bool = True
    include_opening_cost:   bool = True
    include_transport_cost: bool = True
    include_handling_cost:  bool = True
    include_inventory_cost: bool = True

    # One-time cost of transitioning an EXISTING facility open → closed.
    # A genuine business cost, distinct from operating cost, opening cost and
    # CapEx. Note the facility DISRUPTED in a resilience run is exempt from it
    # (an outage is not a voluntary closure), so this covers only closures the
    # re-optimization itself chooses.
    include_closure_cost:   bool = True

    # Honoured only when OptimizationConfig.enable_carbon_cost is True.
    include_carbon_cost:    bool = True

    # Artificial penalty — NOT business cost unless explicitly validated as one.
    include_shortage_penalty: bool = False

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Deterministic risk classification rules
# ---------------------------------------------------------------------------

class RiskClassificationRules(BaseModel):
    """
    Explicit, deterministic rules for classifying facility risk.

    REI is fundamentally a RELATIVE RANKING metric. There is no documented
    business basis for bands such as "REI > 0.8 = Critical", so no REI threshold
    is applied here and none should be added without one. All optional
    thresholds below default to None (disabled), which means only the
    unambiguous rule fires:

        disruption infeasible  →  CRITICAL

    Everything else is reported as NOT_CLASSIFIED, leaving REI and rank as the
    ranking signal. Thresholds are exposed so an organisation can configure them
    once it has a documented basis.
    """
    # Infeasible disruption is unambiguously critical: the network cannot
    # absorb the disruption under the current constraints.
    infeasible_is_critical: bool = True

    # Optional, disabled by default. Fractions in [0, 1] for demand rates.
    unserved_demand_rate_critical: Optional[float] = None
    unserved_demand_rate_high:     Optional[float] = None

    # Optional, disabled by default. Percentage points (e.g. 15.0 = +15%).
    cost_impact_pct_high:          Optional[float] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("unserved_demand_rate_critical", "unserved_demand_rate_high")
    @classmethod
    def rate_fraction(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"unserved demand rate threshold must be in [0, 1], got {v}")
        return v

    @model_validator(mode="after")
    def critical_above_high(self) -> "RiskClassificationRules":
        crit = self.unserved_demand_rate_critical
        high = self.unserved_demand_rate_high
        if crit is not None and high is not None and crit < high:
            raise ValueError(
                f"unserved_demand_rate_critical ({crit}) must be >= "
                f"unserved_demand_rate_high ({high})"
            )
        return self


# ---------------------------------------------------------------------------
# Disruption configuration
# ---------------------------------------------------------------------------

class DisruptionConfig(BaseModel):
    """
    The complete, single set of assumptions under which a resilience assessment
    runs.

    FAIR COMPARISON REQUIREMENT
    ───────────────────────────
    REI is meaningful only when every facility is evaluated under identical
    assumptions. Passing ONE DisruptionConfig to `assess_network_resilience`
    guarantees that the disruption type, disruption period, cost basis, shortage
    policy and risk rules are the same for the baseline and for every facility
    in the batch. Never mix registries produced under different configurations
    into a single REI ranking.
    """
    disruption_type:   DisruptionType       = DisruptionType.FACILITY_FAILURE
    disruption_period: DisruptionPeriodBasis = DisruptionPeriodBasis.MODELLED_PLANNING_PERIOD

    # Reserved extension point for a future time-phased model. Any value is
    # rejected today rather than fabricating a temporal calculation.
    time_to_recovery_days: Optional[float] = None

    # Applied to BOTH the baseline and every disrupted solve so the comparison
    # stays fair.
    #
    # Defaults to False deliberately. With shortage enabled, a disruption the
    # network cannot absorb still solves — but the disrupted network then serves
    # LESS volume, so its business cost falls and the facility scores a NEGATIVE
    # Performance Impact. The most operationally exposed facility ranks last, and
    # the failure is silent. That failure mode worsens as the network grows: more
    # markets means more single-source coverage means more contaminated rows.
    #
    # With shortage disabled the cost comparison is like-for-like by construction
    # (every compared solution serves 100% of demand), and an unabsorbable
    # disruption reports INFEASIBLE → CRITICAL, which is loud and correct at any
    # network size. Unmet demand is still quantified — see
    # `service_diagnostic_on_infeasible`.
    #
    # Set True only when a shortage-tolerant cost comparison is genuinely wanted,
    # and read the negative-PI diagnostics carefully if you do.
    allow_shortage: bool = False

    # When a disruption is INFEASIBLE under the primary (like-for-like) basis,
    # re-solve that facility ONCE with shortage enabled purely to quantify the
    # service damage: unserved demand, service loss, rerouted volume, carbon.
    #
    # This is a DIAGNOSTIC pass only. It never produces performance_impact,
    # cost_impact_pct or rei — those stay None — so the artificial shortage
    # penalty can never reach the cost-based ranking. It costs at most one extra
    # MILP solve per infeasible facility, so total work stays bounded at
    # 1 + N + (number of infeasible facilities).
    service_diagnostic_on_infeasible: bool = True

    # Facility discovery (never hard-code facility identities).
    # None = every non-market role is eligible.
    eligible_roles: Optional[List[NodeRole]] = None
    # Restrict to facilities actually open in the baseline solution. A facility
    # the baseline does not use has no exposure by construction.
    only_baseline_open_facilities: bool = True
    # Explicit opt-out list (e.g. a facility already known to be closing).
    exclude_facility_ids: List[str] = Field(default_factory=list)

    cost_basis: ResilienceCostBasis     = Field(default_factory=ResilienceCostBasis)
    risk_rules: RiskClassificationRules = Field(default_factory=RiskClassificationRules)

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reject_fabricated_ttr(self) -> "DisruptionConfig":
        if self.time_to_recovery_days is not None:
            raise ValueError(
                "time_to_recovery_days is not supported: the NetGravity MILP is a "
                "single-period model, so a multi-period Time to Recovery cannot be "
                "modelled without fabricating a temporal calculation. Use "
                "disruption_period=MODELLED_PLANNING_PERIOD (facility unavailable for "
                "the modelled planning period). TTR support requires a time-phased "
                "formulation."
            )
        return self

    def describe(self) -> str:
        """Human-readable statement of the disruption assumption (for audit trails)."""
        return (
            f"{self.disruption_type.value} for the "
            f"{self.disruption_period.value.lower().replace('_', ' ')} "
            f"(allow_shortage={self.allow_shortage})"
        )
