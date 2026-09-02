"""
NetGravity — Frozen Deterministic Result Contracts
===================================================
Version: 1.4.0

The stable interface between the deterministic MILP core and everything built
on top of it (the Orchestrator Agent and Risk Factor calculator in the next
phase).

Why these exist
───────────────
`OptimizationResult` is the engine's native output and carries solver internals.
Downstream consumers must be able to reason about a run WITHOUT reading MILP
internals, and must not break when the formulation is refactored. These
contracts are that boundary: a flat, self-describing, JSON-serialisable summary
of one deterministic run.

Contract guarantees
───────────────────
1. Every result identifies the network/data snapshot it was produced from
   (`network_id` + `data_version`), so downstream reasoning can be tied to
   exact inputs.
2. Every result states its optimization mode and whether it is HYPOTHETICAL.
   An optimized or scenario state can never be mistaken for observed state.
3. Business cost is reported separately from the raw solver objective, with the
   shortage penalty broken out and excluded. Consumers never have to reverse a
   penalty out of an objective.
4. Scenario results carry their overrides explicitly and never overwrite
   observed baseline state.

Deliberately NOT in these contracts
───────────────────────────────────
Risk Factor (RF), disruption likelihood, and any probabilistic or
news-derived judgement. RF is computed later inside the Orchestrator Agent.
What IS provided is the deterministic evidence RF will need — cost impact,
service impact, feasibility, and full cost basis — so the Orchestrator can
calculate RF without reaching back into the MILP.

ForecastResult is intentionally absent: NetGravity has no forecasting module,
and inventing one was out of scope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from netgravity.schemas.results import (
    ServiceReport,
    SolverStatus,
)


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

class CostBreakdown(BaseModel):
    """
    Full cost decomposition for one run.

    Every economic category is kept distinct — the separation between operating
    cost, opening cost, closure cost, CapEx, shortage penalty and carbon is a
    deliberate contract guarantee, not an implementation detail.
    """
    facility_cost:   float = 0.0   # per-period operating/fixed cost of open facilities
    opening_cost:    float = 0.0   # one-time cost of opening CANDIDATE facilities
    closure_cost:    float = 0.0   # one-time cost of closing EXISTING facilities
    transport_cost:  float = 0.0
    handling_cost:   float = 0.0
    inventory_cost:  float = 0.0   # cycle + safety stock holding
    carbon_cost:     float = 0.0   # only when carbon is genuinely priced

    # Business network cost = sum of the components above that the configured
    # cost basis includes. This is the figure REI and all economic comparison
    # must use.
    business_network_cost: float = 0.0

    # --- Kept OUTSIDE business cost ---
    # Artificial penalty on unmet demand. Reported so it is visible and
    # auditable, never folded into business_network_cost.
    shortage_penalty_cost: float = 0.0

    # The raw mathematical objective, retained so the separation between what
    # the MILP minimises and what the business pays stays explicit.
    solver_objective: float = 0.0

    # Which components the configured cost basis actually included.
    included_components: List[str] = Field(default_factory=list)
    excluded_components: List[str] = Field(default_factory=list)

    # Independent reconciliation health for this run.
    reconciliation_gap:        float = 0.0
    reconciliation_is_closed:  bool  = True

    model_config = ConfigDict(extra="forbid")


class FacilitySummary(BaseModel):
    """Per-facility outcome, flat enough for an agent or dashboard to consume directly."""
    facility_id:      str
    facility_name:    str
    role:             str
    is_open:          bool
    throughput_units: float = 0.0
    capacity_units:   float = 0.0
    utilization_pct:  float = 0.0

    #: Utilisation in the single busiest period, and what moved in each period.
    #:
    #: `utilization_pct` above is throughput over capacity across the WHOLE
    #: horizon, which is an average — and an average is the one number that
    #: cannot answer the question a horizon was modelled to answer. A DC running
    #: at 43% for the year and 91% in March is at 43% by that measure and out of
    #: room in March. The MILP has computed both since the multi-period model
    #: was built; only the average was carried into this contract, so the peak
    #: could not be seen from any screen.
    #:
    #: `peak_utilization_pct` equals `utilization_pct` for a single-period
    #: solve, and `throughput_by_period` is empty for one — neither invents a
    #: seasonal profile the data does not describe.
    peak_utilization_pct: float = 0.0
    throughput_by_period: Dict[str, float] = Field(default_factory=dict)
    #: Utilisation period by period, on the same basis as the peak above. This
    #: is what lets a period selector show a solved reading for the month the
    #: user picked instead of the horizon average on every one of them.
    utilization_by_period: Dict[str, float] = Field(default_factory=dict)

    #: `throughput_units` divided by the number of periods modelled.
    #:
    #: `throughput_units` and `capacity_units` are both horizon totals, and they
    #: divide out to `utilization_pct` correctly. But a screen that pairs solved
    #: throughput with the capacity the UPLOAD states — which is per period, by
    #: the name of the column it came from — is comparing a twelve-month volume
    #: with one month of capacity, and would print a figure contradicting the
    #: utilisation shown beside it. This is the term that matches such a
    #: capacity, so the comparison stays on one basis without any consumer
    #: dividing by a period count it had to go and find.
    throughput_units_per_period: float = 0.0

    # Observed-baseline provenance, so open→closed transitions are visible.
    baseline_status:  Optional[str] = None
    # Contractual state that constrained (or did not constrain) this facility.
    contract_status:  str  = "NONE"
    closure_cost_charged: float = 0.0

    model_config = ConfigDict(extra="forbid")


class FlowSummary(BaseModel):
    """One origin → destination allocation, aggregated across modes and products."""
    origin_id:      str
    destination_id: str
    flow_units:     float
    transport_cost: float = 0.0
    distance_km:    float = 0.0
    carbon_kg:      float = 0.0

    #: `flow_units` divided by the number of periods modelled — the volume that
    #: is comparable with a lane's stated per-period capacity, and with the
    #: rate-per-unit economics a corridor is usually read against. Equal to
    #: `flow_units` on a single-period solve.
    flow_units_per_period: float = 0.0

    model_config = ConfigDict(extra="forbid")


class DemandSummary(BaseModel):
    """Demand coverage for one run."""
    total_demand:     float = 0.0
    served_demand:    float = 0.0
    unserved_demand:  float = 0.0
    demand_fill_rate: float = 0.0

    model_config = ConfigDict(extra="forbid")


class ModelMetadata(BaseModel):
    """
    Provenance of one deterministic run.

    Enough for the Orchestrator to reproduce, cache, or invalidate a result.
    """
    run_id:            str
    model_version:     str = "1.4.0"
    solver_name:       str = ""
    solver_status:     SolverStatus
    optimality_label:  str = ""
    mip_gap:           Optional[float] = None
    runtime_seconds:   Optional[float] = None
    n_variables:       Optional[int]   = None
    n_constraints:     Optional[int]   = None
    generated_at:      Optional[str]   = None
    warnings:          List[str]       = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# A. Network State Result
# ---------------------------------------------------------------------------

class NetworkStateResult(BaseModel):
    """
    The deterministic state of ONE network configuration.

    Produced for observed evaluations (ACTUAL_AS_IS_EVALUATION) and for
    optimization runs alike. `is_hypothetical` distinguishes them — it is False
    only for an as-is evaluation of the observed network.
    """
    # --- Snapshot identity (required for every result) ---
    network_id:   str
    data_version: Optional[str] = None

    # --- What decision this run represents ---
    optimization_mode: str
    mode_description:  str = ""
    # False ONLY for ACTUAL_AS_IS_EVALUATION of the observed network.
    is_hypothetical:   bool = True
    result_type:       str  = "OPTIMIZED"

    # --- Feasibility ---
    solver_status: SolverStatus
    is_feasible:   bool

    # --- Economics ---
    costs: CostBreakdown

    # --- Demand & service ---
    demand:  DemandSummary
    service: Optional[ServiceReport] = None

    # --- Network configuration ---
    open_facilities:   List[str] = Field(default_factory=list)
    closed_facilities: List[str] = Field(default_factory=list)
    facilities:        List[FacilitySummary] = Field(default_factory=list)
    flows:             List[FlowSummary] = Field(default_factory=list)

    # --- Planning horizon ---
    #: How many planning periods this result covers, and what the source calls
    #: each of them (`{"1": "2025-09", ...}`).
    #:
    #: Every cost, volume and carbon figure in this contract is a TOTAL over
    #: `periods_modelled` periods — the engine charges fixed and handling cost
    #: in each period a facility is open, and opening, closure and capex once.
    #: Reading a twelve-period total as one period's cost overstates it
    #: twelvefold, and nothing in the numbers themselves reveals which it is.
    #: So the period count travels with the figures rather than being left for a
    #: caller to infer, and `cost_per_period` gives the comparable per-period
    #: figure without any consumer having to divide and hope.
    periods_modelled: int = 1
    period_labels:    Dict[str, str] = Field(default_factory=dict)

    #: The money unit every cost figure in this result is stated in — the same
    #: class of fact as `periods_modelled`, and needed for the same reason.
    #:
    #: The solver is unit-agnostic; every layer that *reports* its output is
    #: not. This travels with the figures so no consumer has to assume, which
    #: is what the KPI registry, the evidence formatter and the browser each
    #: did independently — all three assuming INR, all three wrong for a
    #: network priced in dollars.
    #:
    #: None when the upload stated no currency. A caller renders a bare amount
    #: rather than stamping it with a unit the data does not support.
    currency: Optional[str] = None

    #: `business_network_cost` divided by `periods_modelled`.
    #:
    #: An AVERAGE period, not a typical one: fixed and handling costs recur
    #: every period, but opening, closure and capex are charged once across the
    #: horizon, so this spreads a one-off over the periods rather than assigning
    #: it to the period that incurred it. It is the right figure for comparing a
    #: horizon result against a monthly budget, and the wrong one for asking
    #: what a specific month cost.
    cost_per_period: float = 0.0

    # --- Utilisation indicators ---
    avg_utilization_pct: float = 0.0
    max_utilization_pct: float = 0.0
    overutilized_facilities:  List[str] = Field(default_factory=list)
    underutilized_facilities: List[str] = Field(default_factory=list)

    # --- Carbon ---
    total_carbon_kg: float = 0.0

    # --- Distance and intensity indicators (Phase 10.0, closing GAP-01) ---
    #
    # `compute_kpis()` has always produced these five figures, but the builder
    # in `metrics/contracts.py` never copied them across this bridge, so they
    # could not reach ExecutionContext, the KPI layer, or any caller — and the
    # information was unrecoverable downstream because the flattened transport
    # projection derives from THIS object, not from the original NetworkKPIs.
    #
    # They are Optional[float] rather than `= 0.0` on purpose: None means "this
    # solve did not report the figure", which must stay distinguishable from a
    # genuine measurement of zero. A zero default here would have reintroduced
    # exactly the fabricated-zero problem the KPI layer exists to prevent.
    weighted_avg_distance_km: Optional[float] = None
    inbound_avg_distance_km:  Optional[float] = None
    outbound_avg_distance_km: Optional[float] = None
    min_utilization_pct:      Optional[float] = None
    carbon_per_unit:          Optional[float] = None

    metadata: ModelMetadata

    # --- Solve relaxation ---
    #
    # Set only when the STRICT model proved infeasible and the engine returned
    # a relaxed plan instead: identical costs, capacities and service levels,
    # with unmet demand permitted and priced so the solver has to say which
    # demand it strands. None means the result came from the model as posed.
    #
    # It is a field of its own rather than an entry in `metadata`, which is a
    # typed `ModelMetadata` the Digital Twin reads attribute by attribute.
    solve_relaxation: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")

    @property
    def business_cost(self) -> float:
        """Convenience accessor for the figure all economic comparison uses."""
        return self.costs.business_network_cost


# ---------------------------------------------------------------------------
# B. Scenario Result
# ---------------------------------------------------------------------------

class ScenarioResult(BaseModel):
    """
    The deterministic outcome of ONE hypothetical scenario.

    A scenario result is ALWAYS hypothetical. It records the overrides that
    produced it and, when a baseline is supplied, the deltas against it. It
    never overwrites observed baseline state — `baseline` is a separate,
    read-only snapshot.
    """
    scenario_id:   str
    scenario_name: str = ""
    scenario_type: str = "CUSTOM"

    # Always True. Present explicitly so a consumer cannot treat a scenario as
    # observed reality by omission.
    is_hypothetical: bool = True

    # The hypothetical network state produced by this scenario.
    state: NetworkStateResult

    # Read-only snapshot identity of the baseline this scenario was derived
    # from. Carrying the identity rather than the full baseline keeps observed
    # state single-sourced.
    baseline_network_id:   Optional[str] = None
    baseline_data_version: Optional[str] = None
    baseline_business_cost: Optional[float] = None

    # --- Deltas vs baseline (None when no baseline was supplied) ---
    business_cost_delta:     Optional[float] = None
    business_cost_delta_pct: Optional[float] = None
    served_demand_delta:     Optional[float] = None
    carbon_delta_kg:         Optional[float] = None

    # --- What was changed ---
    # Explicit, human-readable summary of the overrides applied.
    scenario_overrides: List[str] = Field(default_factory=list)
    # Structured audit manifest from the scenario engine, when available.
    change_manifest: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    @property
    def business_cost(self) -> float:
        return self.state.costs.business_network_cost
