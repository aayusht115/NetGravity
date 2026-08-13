"""
NetGravity — Typed Data Schemas: Results
=========================================
Version: 1.1.0
Structured outputs from the optimization engine.

Every result object is:
  - Fully typed
  - Traceable (carries model_version, scenario_id, solver metadata)
  - Dashboard-ready (structured for direct consumption by API / UI)
  - Independently serializable (JSON-compatible)

No optimization logic is present here — only output structure.

V1.1 Changes:
  - SolverMetadata: best_bound, optimality_label, get_optimality_label()
  - OptimizationResult: result_type, inventory_iterations
  - SensitivityPoint: facility_ids_open, configuration_stable
  - NetworkKPIs: weighted_avg_distance_km, inbound_avg_distance_km,
                 outbound_avg_distance_km, production_cost
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Solver Status
# ---------------------------------------------------------------------------

class SolverStatus(str, Enum):
    """Canonical solver status codes."""
    OPTIMAL         = "OPTIMAL"          # proven optimal solution
    FEASIBLE        = "FEASIBLE"         # feasible but gap > mip_gap
    INFEASIBLE      = "INFEASIBLE"       # no feasible solution exists
    UNBOUNDED       = "UNBOUNDED"        # objective is unbounded
    TIME_LIMIT      = "TIME_LIMIT"       # time limit reached with feasible sol
    NO_SOLUTION     = "NO_SOLUTION"      # time limit with no feasible solution
    ERROR           = "ERROR"            # solver error


# ---------------------------------------------------------------------------
# Solver Metadata
# ---------------------------------------------------------------------------

class SolverMetadata(BaseModel):
    """
    Diagnostic information from the solver run.

    V1.1 ADDITIONS:
        best_bound:      Lower bound on the optimal objective value (if available).
        optimality_label: Human-readable optimality statement.
            "Proven optimal" only when mip_gap = 0 and status = OPTIMAL.
            Otherwise "Best feasible (X% optimality gap)" or "Feasible solution".

    IMPORTANT: Do NOT label a result "optimal" unless:
        - status == OPTIMAL AND
        - mip_gap == 0.0 (or best_bound confirms it)
    """
    solver_name:     str
    solver_version:  Optional[str]  = None
    status:          SolverStatus
    objective_value: Optional[float] = None
    best_bound:      Optional[float] = None   # lower bound (V1.1)
    mip_gap:         Optional[float] = None   # fraction (0.001 = 0.1%)
    optimality_label: str            = ""     # human-readable optimality status (V1.1)
    runtime_seconds: Optional[float] = None
    n_variables:     Optional[int]   = None
    n_constraints:   Optional[int]   = None
    n_binary:        Optional[int]   = None
    iterations:      Optional[int]   = None
    warnings:        List[str]       = Field(default_factory=list)
    model_version:   str             = "1.1.0"
    scenario_id:     Optional[str]   = None
    timestamp:       Optional[str]   = None     # ISO 8601

    def get_optimality_label(self) -> str:
        """
        Return a precise, non-overclaiming optimality statement.

        Rules:
          - OPTIMAL + mip_gap = 0: "Proven optimal solution."
          - OPTIMAL + mip_gap > 0: "Best feasible solution within X% of optimal."
          - FEASIBLE:              "Feasible solution found (optimality not proven)."
          - TIME_LIMIT:            "Time limit reached. Best feasible solution returned."
          - INFEASIBLE:            "No feasible solution exists."
          - Others:                Reflect the status.
        """
        if self.status == SolverStatus.OPTIMAL:
            gap = self.mip_gap
            if gap is None or gap < 1e-9:
                return "Proven optimal solution."
            else:
                return f"Best feasible solution within {gap * 100:.2f}% of optimal."
        elif self.status == SolverStatus.FEASIBLE:
            if self.mip_gap is not None:
                return f"Feasible solution found. Optimality gap: {self.mip_gap * 100:.2f}%."
            return "Feasible solution found (optimality gap unknown)."
        elif self.status == SolverStatus.TIME_LIMIT:
            return "Time limit reached. Best feasible solution returned."
        elif self.status == SolverStatus.INFEASIBLE:
            return "No feasible solution exists with current constraints."
        elif self.status == SolverStatus.UNBOUNDED:
            return "Objective is unbounded — check model formulation."
        else:
            return f"Solver status: {self.status.value}."


# ---------------------------------------------------------------------------
# Facility Decision
# ---------------------------------------------------------------------------

class FacilityDecision(BaseModel):
    """
    MILP decision for a single facility.
    Directly traceable to y_i decision variable.
    """
    facility_id:          str
    facility_name:        str
    role:                 str
    is_open:              bool             # y_i value

    # Volume metrics
    throughput_units:     float = 0.0     # Σ outbound flows
    capacity_units:       float = 0.0     # CAP_i
    utilization_pct:      float = 0.0     # throughput / capacity * 100

    # Cost breakdown
    fixed_cost:           float = 0.0
    handling_cost:        float = 0.0
    inventory_cost:       float = 0.0     # attributed post-solve (iterative)
    opening_cost:         float = 0.0     # opening_cost × y_i for candidates
    total_facility_cost:  float = 0.0

    # Service
    n_markets_served:     int   = 0


# ---------------------------------------------------------------------------
# Flow Decision
# ---------------------------------------------------------------------------

class FlowDecision(BaseModel):
    """
    MILP decision for a single arc flow.
    Directly traceable to x_{ijvk} decision variable.
    """
    origin_id:       str
    destination_id:  str
    mode:            str
    product_id:      str
    period:          int   = 1

    flow_units:      float  # x_{ijvk} value
    distance_km:     float
    lead_time_days:  float
    rate_per_unit:   float

    transport_cost:  float  # rate_per_unit × flow_units
    carbon_kg:       float  # CO₂ for this flow

    # Arc utilization
    lane_capacity:   Optional[float] = None
    arc_utilization_pct: Optional[float] = None


# ---------------------------------------------------------------------------
# Network KPIs
# ---------------------------------------------------------------------------

class NetworkKPIs(BaseModel):
    """
    All network-level KPIs derived from optimization result.

    Every KPI has a mathematically defined formula.
    No heuristic scores.

    V1.1 ADDITIONS:
        weighted_avg_distance_km:  Σ(dist × flow) / Σ(flow) — demand-weighted
        inbound_avg_distance_km:   Average distance on plant/supplier → DC arcs
        outbound_avg_distance_km:  Average distance on DC → market arcs
        production_cost:           Total production cost (placeholder, 0 in V1.1)
    """
    # Cost breakdown
    total_cost:          float   # objective value (currency/period)
    facility_cost:       float   # Σ f_i × y_i (annualized or per-period)
    transport_cost:      float   # Σ c_{ijvk} × x_{ijvk}
    handling_cost:       float   # Σ h_i × throughput_i
    inventory_cost:      float   # Σ inventory_cost_i (post-solve attribution)
    production_cost:     float = 0.0   # V1.1: placeholder (not yet implemented)
    shortage_cost:       float   # Σ pen × u_{mk}

    # Volume
    total_demand:        float   # Σ D_{mk}
    total_served:        float   # Σ flow to markets
    unmet_demand:        float   # Σ u_{mk}
    demand_fill_rate:    float   # total_served / total_demand

    # Facilities
    n_facilities_open:   int
    n_facilities_closed: int

    # Distance
    # Simple flow-weighted average on last-mile arcs (legacy)
    avg_distance_km:     float
    # Explicitly demand-weighted: Σ(dist_a × flow_a) / Σ(flow_a) — last-mile only
    weighted_avg_distance_km: float = 0.0
    # Average distance on inbound arcs (plant/supplier → DC/warehouse)
    inbound_avg_distance_km:  float = 0.0
    # Average distance on outbound arcs (DC → market) — same as weighted_avg if single-echelon
    outbound_avg_distance_km: float = 0.0
    max_distance_km:     float
    min_distance_km:     float

    # Service
    pct_demand_in_sla:   float   # % demand met within SLA (0-100)

    # Utilization
    avg_utilization_pct: float
    max_utilization_pct: float
    min_utilization_pct: float
    overutilized_count:  int     # facilities > 90% utilization
    underutilized_count: int     # facilities < 30% utilization

    # Carbon
    total_carbon_kg:     float   # kg CO₂ / period
    carbon_per_unit:     float   # kg CO₂ / unit served


# ---------------------------------------------------------------------------
# Flow Analytics
# ---------------------------------------------------------------------------

class CorridorInfo(BaseModel):
    """A high-volume or high-cost network corridor."""
    origin_id:      str
    destination_id: str
    mode:           str
    total_flow:     float
    total_cost:     float
    distance_km:    float
    carbon_kg:      float


class FlowAnalytics(BaseModel):
    """
    Flow-pattern analytics derived from optimization result.
    Used for dashboard heatmaps, corridor highlights, and alerts.
    """
    top_corridors_by_volume: List[CorridorInfo] = Field(default_factory=list)
    top_corridors_by_cost:   List[CorridorInfo] = Field(default_factory=list)
    longest_distance_flows:  List[FlowDecision] = Field(default_factory=list)
    overutilized_facilities:  List[str]         = Field(default_factory=list)
    underutilized_facilities: List[str]         = Field(default_factory=list)
    cost_hotspots:           List[CorridorInfo] = Field(default_factory=list)
    high_carbon_corridors:   List[CorridorInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Go/No-Go Evidence
# ---------------------------------------------------------------------------

class GoNoGoEvidence(BaseModel):
    """
    Structured evidence for a go/no-go decision recommendation.

    The model returns EVIDENCE — not a decision.
    The decision layer (human or configurable rules) interprets the evidence.

    Rules should be defined as:
        GO if:  annual_savings >= threshold AND service >= target AND feasible
        NO-GO if: service < target OR infeasible OR negative economic value
    """
    scenario_id:          str
    baseline_id:          str

    # Financial evidence
    annual_savings:       Optional[float]   = None
    implementation_cost:  Optional[float]   = None
    payback_months:       Optional[float]   = None
    npv:                  Optional[float]   = None

    # Operational evidence
    service_delta_pct:    Optional[float]   = None  # + means improvement
    carbon_delta_kg:      Optional[float]   = None  # negative = reduction
    utilization_delta:    Optional[float]   = None

    # Feasibility
    is_feasible:          bool
    capacity_violations:  List[str]         = Field(default_factory=list)
    sla_violations:       List[str]         = Field(default_factory=list)

    # Implementation flags
    has_closure_cost:     bool = False
    closure_cost:         float = 0.0

    # Rule-based recommendation (configurable thresholds)
    go_no_go:             Optional[str]     = None   # "GO" | "NO-GO" | "MARGINAL"
    go_no_go_rationale:   Optional[str]     = None


# ---------------------------------------------------------------------------
# Scenario Comparison
# ---------------------------------------------------------------------------

class ScenarioDelta(BaseModel):
    """Absolute and percentage delta between two scenarios."""
    metric:      str
    baseline:    float
    scenario:    float
    abs_delta:   float
    pct_delta:   float   # positive = scenario is higher


class ScenarioComparison(BaseModel):
    """Side-by-side comparison of baseline vs one scenario."""
    baseline_id:    str
    scenario_id:    str
    scenario_name:  str

    kpi_deltas:     List[ScenarioDelta]       = Field(default_factory=list)
    facility_changes: List[Dict[str, Any]]    = Field(default_factory=list)
    flow_changes:   List[Dict[str, Any]]      = Field(default_factory=list)
    go_no_go:       Optional[GoNoGoEvidence]  = None


# ---------------------------------------------------------------------------
# Sensitivity Result
# ---------------------------------------------------------------------------

class SensitivityPoint(BaseModel):
    """
    A single point in a sensitivity sweep.

    V1.1 ADDITIONS:
        facility_ids_open:   Which facilities are open at this parameter value.
        configuration_stable: True if same facilities are open as at baseline.
    """
    parameter:       str
    parameter_value: float
    objective_value: float
    n_facilities:    int
    total_carbon_kg: float
    avg_distance_km: float
    demand_fill_rate: float

    # V1.1: track configuration changes
    facility_ids_open:     List[str] = Field(default_factory=list)
    configuration_stable:  bool      = True


class SensitivityResult(BaseModel):
    """
    Result of a sensitivity analysis sweep.

    Output is dashboard-ready for tornado charts, sensitivity curves,
    and two-way grids.
    """
    parameter:      str
    baseline_value: float
    points:         List[SensitivityPoint]

    # Derived sensitivity metrics
    obj_at_min:     float
    obj_at_max:     float
    obj_range:      float       # obj_at_max - obj_at_min
    sensitivity_pct: float      # obj_range / baseline_obj * 100

    baseline_obj:   float

    # V1.1: did any sensitivity point change the network configuration?
    any_configuration_change: bool = False


# ---------------------------------------------------------------------------
# Resilience Result
# ---------------------------------------------------------------------------

class ResilienceResult(BaseModel):
    """
    Result of a disruption scenario analysis.

    Derived from measurable MILP outputs — not a heuristic score.
    """
    scenario_id:        str
    disruption_type:    str

    # Pre-disruption
    pre_cost:           float
    pre_served:         float
    pre_carbon_kg:      float

    # Post-disruption
    post_cost:          float
    post_served:        float
    post_carbon_kg:     float
    post_status:        SolverStatus

    # Derived
    cost_delta:         float
    service_delta:      float   # fraction change in demand served
    unmet_demand:       float
    carbon_delta_kg:    float
    rerouted_volume:    float

    # Specific nodes/lanes affected
    affected_ids:       List[str] = Field(default_factory=list)


class AssignmentDecision(BaseModel):
    """
    Facility-to-Market assignment decision output (a_ij).
    Indicates whether facility_id is assigned to serve market_id,
    and the associated safety stock units & precomputed inventory cost.
    """
    facility_id:        str
    market_id:          str
    is_assigned:        bool
    safety_stock_units: float = 0.0
    inventory_cost:     float = 0.0


# ---------------------------------------------------------------------------
# Full Optimization Result
# ---------------------------------------------------------------------------

class OptimizationResult(BaseModel):
    """
    Complete, structured output from one optimization run.

    This is the canonical output object. Every downstream module
    (metrics, scenarios, sensitivity, dashboard) consumes this.

    V1.2 ADDITIONS:
        inventory_method: "DIRECT_MILP" (precomputed safety stock in objective).
        inventory_optimization_status: "INTEGRATED" (single-pass exact solve).
        assignment_decisions: Binary a[i,j] market assignment decisions.
    """
    # Run identification
    run_id:          str
    scenario_id:     Optional[str] = None
    network_id:      str
    data_version:    Optional[str] = None

    # Result type — always set this to avoid ambiguity
    # "BASELINE"  = current-state evaluation (no optimization)
    # "OPTIMIZED" = MILP result (mathematical optimum within constraints)
    # "SCENARIO"  = scenario variant of optimized result
    result_type:     str = "OPTIMIZED"

    # Solver output
    solver:          SolverMetadata

    # Decisions
    facility_decisions:   List[FacilityDecision]   = Field(default_factory=list)
    flow_decisions:       List[FlowDecision]       = Field(default_factory=list)
    assignment_decisions: List[AssignmentDecision] = Field(default_factory=list)

    # KPIs and analytics
    kpis:            Optional[NetworkKPIs]      = None
    flow_analytics:  Optional[FlowAnalytics]    = None

    # Raw objective components (for auditability)
    objective_components: Dict[str, float]      = Field(default_factory=dict)

    # Independently evaluated total cost (V1.1.2 / V1.2)
    # solver.objective_value remains the raw mathematical LP/MILP objective
    evaluated_total_cost: Optional[float]       = None

    # V1.2 Direct MILP Inventory Integration fields
    inventory_method:               str = "DIRECT_MILP"
    inventory_optimization_status:  str = "INTEGRATED"
    inventory_iteration_status:     str = "CONVERGED"

    # Inventory iteration tracking (V1.1 / V1.1.3 - DEPRECATED in V1.2, kept for back-compat)
    inventory_iterations:       int = 0
    # One of: NOT_APPLICABLE | INTEGRATED | CONVERGED | CYCLE_DETECTED | MAX_ITERATIONS_REACHED_NO_CONVERGENCE

    @property
    def inventory_converged(self) -> bool:
        """
        Backward-compatible property:
        True when inventory iteration status is CONVERGED or NOT_APPLICABLE (single-shot).

        Note for UI/Dashboard Developers:
        Prefer reading `inventory_iteration_status` directly ("NOT_APPLICABLE" | "CONVERGED" |
        "CYCLE_DETECTED" | "MAX_ITERATIONS_REACHED_NO_CONVERGENCE") for explicit status handling.
        """
        return self.inventory_iteration_status in ("CONVERGED", "NOT_APPLICABLE")

    @property
    def objective_reconciliation_gap(self) -> Optional[float]:
        """
        Difference between independently evaluated total cost and raw MILP solver objective.
        evaluated_total_cost - solver.objective_value
        """
        if self.evaluated_total_cost is not None and self.solver.objective_value is not None:
            return round(self.evaluated_total_cost - self.solver.objective_value, 4)
        return None

    @property
    def is_solved(self) -> bool:
        return self.solver.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE,
                                      SolverStatus.TIME_LIMIT)

    @property
    def is_optimal(self) -> bool:
        """
        True only when solver confirms proven optimality (gap = 0).
        DO NOT use this to claim 'optimal' in client-facing outputs
        when MIP gap > 0.
        """
        return (self.solver.status == SolverStatus.OPTIMAL and
                (self.solver.mip_gap is None or self.solver.mip_gap < 1e-9))

    @property
    def optimality_label(self) -> str:
        """Delegated to SolverMetadata for the precise optimality statement."""
        return self.solver.get_optimality_label()

    def get_open_facilities(self) -> List[FacilityDecision]:
        return [fd for fd in self.facility_decisions if fd.is_open]

    def get_closed_facilities(self) -> List[FacilityDecision]:
        return [fd for fd in self.facility_decisions if not fd.is_open]
