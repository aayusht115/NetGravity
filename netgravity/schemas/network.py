"""
NetGravity — Typed Data Schemas: Network Entities
==================================================
Version: 1.1.0
All supply-chain network entities with full Pydantic validation.

Every parameter carries:
  - value (the quantity itself)
  - unit  (physical unit string, documented in field description)
  - source (optional provenance)
  - confidence (optional: HIGH / MEDIUM / LOW)

This module defines the canonical data contract.
The optimizer, scenario engine, and metric engine all consume these types.

No UI, LLM, or dashboard logic is present here.

V1.1 Changes:
  - Added DEPOT, CUSTOMER to NodeRole
  - FacilityRecord: production_capacity_units_per_period, opening_cost,
    ramp_up_cost, is_forced_closed, is_existing/is_candidate properties
  - LaneRecord: distance_method, network_distance_km
  - OptimizationConfig: minimum_throughput_enabled, days_per_period,
    emission_methodology, emission_factor_table,
    inventory_max_iterations, inventory_convergence_tolerance
  - Model version bumped to 1.1.0
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class CostPeriod(str, Enum):
    """Optimization cost period convention."""
    MONTH   = "MONTH"
    YEAR    = "YEAR"
    DAY     = "DAY"
    QUARTER = "QUARTER"


class NodeRole(str, Enum):
    """Functional role of a node in the supply chain network."""
    SUPPLIER   = "SUPPLIER"     # raw-material / component supplier
    PLANT      = "PLANT"        # manufacturing plant
    WAREHOUSE  = "WAREHOUSE"    # regional warehouse
    DC         = "DC"           # distribution centre
    DEPOT      = "DEPOT"        # local depot (between DC and customer)
    MARKET     = "MARKET"       # demand aggregation node
    CUSTOMER   = "CUSTOMER"     # individual customer (alias for MARKET in flow model)
    DARKSTORE  = "DARKSTORE"    # dark store / micro-fulfilment (future)
    CROSS_DOCK = "CROSS_DOCK"   # cross-docking facility


class FacilityStatus(str, Enum):
    """Operational status of a facility in the baseline network."""
    EXISTING  = "EXISTING"    # currently operating
    CANDIDATE = "CANDIDATE"   # potential new location
    CLOSED    = "CLOSED"      # currently closed, could re-open


class TransportMode(str, Enum):
    """Available transportation modes."""
    ROAD        = "ROAD"
    RAIL        = "RAIL"
    AIR         = "AIR"
    SEA         = "SEA"
    INTERMODAL  = "INTERMODAL"


class ObjectiveMode(str, Enum):
    """Optimization objective configuration."""
    COST_MIN             = "COST_MIN"              # Mode A: minimize total cost
    COST_SERVICE         = "COST_SERVICE"          # Mode B: cost s.t. service constraint
    COST_CARBON          = "COST_CARBON"           # Mode C: cost s.t. carbon cap
    WEIGHTED_COST_CARBON = "WEIGHTED_COST_CARBON"  # Mode D: weighted cost + carbon


class SourcingPolicy(str, Enum):
    """Sourcing policy for market-product pairs."""
    MULTI  = "MULTI"   # any number of sources (default)
    SINGLE = "SINGLE"  # exactly one source per market-product
    DUAL   = "DUAL"    # exactly two sources (resilience)


class ServiceMetric(str, Enum):
    """
    Which service metric is used for constraints.

    V1 IMPLEMENTATION STATUS — read this before relying on a value.
    ───────────────────────────────────────────────────────────────
    Only TRANSIT_TIME is implemented as an actual optimization constraint.
    The remaining members are declared for forward compatibility and are NOT
    enforced by the MILP. Selecting one does not change the optimization; the
    solver falls back to transit-time SLA feasibility and records an explicit
    warning in `SolverMetadata.warnings` and `OptimizationResult.service_report`
    so the result never implies unsupported service optimization.

    See `V1_SUPPORTED_SERVICE_METRICS` and docs/v1_4_hardening.md (section 3).
    """
    TRANSIT_TIME  = "TRANSIT_TIME"   # IMPLEMENTED: hard SLA on lane/path lead time
    CSL           = "CSL"            # NOT IMPLEMENTED IN V1 (declared only)
    FILL_RATE     = "FILL_RATE"      # NOT IMPLEMENTED IN V1 (declared only)
    PENALTY       = "PENALTY"        # NOT IMPLEMENTED IN V1 (declared only)


# Service metrics actually enforced by the V1 MILP as optimization constraints.
# Anything outside this set is declared-but-inert and must be reported as such.
V1_SUPPORTED_SERVICE_METRICS: Set[str] = {ServiceMetric.TRANSIT_TIME.value}

# Objective modes actually enforced by the V1 MILP.
# COST_MIN            — implemented (default)
# WEIGHTED_COST_CARBON— implemented (adds carbon_weight × CO2 to the objective)
# COST_CARBON         — implemented via `carbon_cap_kg`, which applies in ANY mode
# COST_SERVICE        — NOT implemented: no service-fraction constraint exists in
#                       the V1 formulation. Selecting it does not add a constraint.
V1_SUPPORTED_OBJECTIVE_MODES: Set[str] = {
    "COST_MIN",
    "WEIGHTED_COST_CARBON",
    "COST_CARBON",
}


class ContractStatus(str, Enum):
    """
    Contractual state of a facility for the modelled planning period.

    Deliberately minimal: this is the smallest state set needed for realistic
    brownfield closure decisions, not a contract-management system. No contract
    rule is ever inferred — every state comes from input data or an explicit
    scenario override.

    NONE     No contractual commitment recorded. Default. The facility is
             subject to normal optimization constraints.
    ACTIVE   A contractual commitment is in force for the modelled planning
             period. Combined with `contract_allows_early_closure` this yields
             the two active states:
               - allows_early_closure = False → facility MUST remain open
                 (MILP constraint C5c pins y_i = 1)
               - allows_early_closure = True  → facility MAY close, and the
                 configured `closure_cost` is charged as the early-termination
                 penalty
    EXPIRED  A contract existed but has expired for the modelled planning
             period. Treated exactly like NONE for optimization purposes; the
             distinct value is retained so the input data stays self-describing.
    """
    NONE    = "NONE"
    ACTIVE  = "ACTIVE"
    EXPIRED = "EXPIRED"


class OptimizationMode(str, Enum):
    """
    What decision the optimizer is being asked to make.

    All five modes use the SAME MILP formulation. A mode never changes the
    mathematics; it only fixes decision variables and selects which economic
    terms apply, centralised in `optimization/modes.py`.

    ACTUAL_AS_IS_EVALUATION
        Evaluate the observed network without redesigning it. Existing
        facilities are pinned open, candidates are excluded, and only lanes
        flagged `is_active_baseline` are available. This is the comparison
        anchor for business performance and resilience.

        LIMITATION: NetGravity V1 has no observed-flow input field, so this
        mode evaluates the observed FOOTPRINT and LANE SET with a cost-minimal
        feasible allocation. It does not replay recorded shipment volumes.

    CURRENT_FOOTPRINT_OPTIMIZATION
        Optimize routing and allocation while the facility footprint stays
        fixed. Existing facilities remain open, candidates are excluded, and
        open/close decisions are locked — but every lane is available, so flow
        may re-route. No closure decision is possible, so no closure cost.

    GREENFIELD_OPTIMIZATION
        Optimize the footprint from candidate locations, releasing the existing
        footprint. Closure cost does NOT apply: a facility being absent from a
        greenfield design is not a decision to close it. This remains
        CANDIDATE-LOCATION optimization — NetGravity does not perform arbitrary
        continuous geographic siting.

    BROWNFIELD_SCENARIO_OPTIMIZATION  (default)
        Optimize a modified existing network. Facility flags are honoured
        exactly as supplied, so explicit scenario overrides drive the result.
        Closure economics and contractual constraints both apply. This is the
        default because it reproduces NetGravity's pre-existing behaviour
        exactly — selecting it transforms nothing.

    DISRUPTION_RESILIENCE_OPTIMIZATION
        Re-optimize after an explicit disruption override. Behaves like
        BROWNFIELD except that facilities marked `is_disruption_target` are
        exempt from closure cost and from contractual must-remain-open
        constraints: an involuntary outage is not a voluntary closure decision.
    """
    ACTUAL_AS_IS_EVALUATION            = "ACTUAL_AS_IS_EVALUATION"
    CURRENT_FOOTPRINT_OPTIMIZATION     = "CURRENT_FOOTPRINT_OPTIMIZATION"
    GREENFIELD_OPTIMIZATION            = "GREENFIELD_OPTIMIZATION"
    BROWNFIELD_SCENARIO_OPTIMIZATION   = "BROWNFIELD_SCENARIO_OPTIMIZATION"
    DISRUPTION_RESILIENCE_OPTIMIZATION = "DISRUPTION_RESILIENCE_OPTIMIZATION"


class SLAMode(str, Enum):
    """SLA lead time evaluation mode."""
    LAST_MILE  = "LAST_MILE"   # DC -> Market lead time <= market SLA (default)
    END_TO_END = "END_TO_END"  # Cumulative Plant -> DC -> Market lead time <= market SLA


class DistanceMethod(str, Enum):
    """Method used to calculate distance for a lane."""
    STRAIGHT_LINE = "STRAIGHT_LINE"  # Euclidean / geodesic between coordinates
    NETWORK       = "NETWORK"        # Actual road/rail network distance
    ESTIMATED     = "ESTIMATED"      # Estimated / manually entered
    HAVERSINE     = "HAVERSINE"      # Haversine great-circle approximation


class Confidence(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"


# ---------------------------------------------------------------------------
# Parameter wrapper (value + metadata)
# ---------------------------------------------------------------------------

class Param(BaseModel):
    """
    A typed parameter with physical unit, provenance and confidence.
    Enforces auditability: every input parameter knows where it came from.
    """
    value:      float
    unit:       str                   = "dimensionless"
    source:     Optional[str]         = None
    confidence: Optional[Confidence]  = None
    effective_date: Optional[str]     = None   # ISO 8601 date

    def __float__(self) -> float:
        return float(self.value)


# ---------------------------------------------------------------------------
# Facility
# ---------------------------------------------------------------------------

class FacilityRecord(BaseModel):
    """
    Complete specification of a network facility node.

    Covers: suppliers, plants, warehouses, DCs, markets.
    Markets have no fixed_cost, capacity, etc. (capacity=inf by convention).

    V1.1 ADDITIONS:
        production_capacity_units_per_period: Separate from throughput capacity.
            Applies to PLANT/SUPPLIER nodes as supply-side limit.
        opening_cost: One-time cost to open a CANDIDATE facility (enters objective).
        ramp_up_cost: Additional cost in first period of operation.
        is_forced_closed: If True, MILP forces y_i = 0 via explicit constraint.
    """
    id:           str = Field(..., description="Unique facility identifier")
    name:         str
    role:         NodeRole
    status:       FacilityStatus           = FacilityStatus.CANDIDATE

    # Geographic location (decimal degrees or schematic coords)
    latitude:     Optional[float]          = None
    longitude:    Optional[float]          = None

    # Facility economics
    # Unit: currency/year
    fixed_cost_per_year:  float   = 0.0
    # Unit: currency/unit throughput
    handling_cost_per_unit: float = 0.0
    # Unit: currency (one-time, applied when candidate facility y_i=1)
    opening_cost:         float   = 0.0
    # Unit: currency (one-time, applied to existing facility if closed)
    closure_cost:         float   = 0.0
    # Unit: currency (first-period ramp-up, informational only in V1.1)
    ramp_up_cost:         float   = 0.0
    # Unit: currency (one-time capital expenditure — informational, not in MILP objective by default)
    capex:                float   = 0.0

    # Throughput Capacity
    # Unit: units/period — primary capacity constraint (C2)
    # For DCs/Warehouses: represents outbound throughput limit
    capacity_units_per_period: float = 1e12   # effectively unlimited if not set

    # Production / Supply Capacity
    # Unit: units/period — separate supply-side limit for PLANT/SUPPLIER nodes (C10)
    # If set, enforces: Σ outbound <= production_capacity (unconditionally, no y_i multiplier)
    # If left at 1e12, treated as unlimited (plant always satisfies supply)
    production_capacity_units_per_period: float = 1e12

    # Minimum throughput if facility is open
    # Unit: units/period — C3 constraint (optional, see config.minimum_throughput_enabled)
    min_throughput_per_period: float = 0.0

    # How much stock this facility can hold at the end of a period.
    #
    # Unit: units. Only meaningful under a multi-period solve, where it caps the
    # inventory variable I_{i,k,t} that carries stock from one period into the
    # next (constraint C9).
    #
    # None means "not stated", NOT "infinite": the model then bounds carried
    # stock by total horizon demand, which is the largest quantity that could
    # ever usefully be held, so an unstated warehouse size cannot silently
    # become an unlimited one in the plan. Nothing here invents a number — a
    # facility with no stated storage simply is not the binding constraint.
    storage_capacity_units: Optional[float] = None

    # Products this facility can handle (empty = all products eligible)
    eligible_product_ids: List[str] = Field(default_factory=list)

    # Facility control flags
    is_closable:    bool = True    # can the MILP choose to close this?
    is_mandatory:   bool = False   # must remain open regardless (y_i = 1 forced)
    is_forced_closed: bool = False # force closed (y_i = 0 forced); used by scenario engine

    # --- Observed-baseline provenance (V1.4) ---
    # The facility's status in the OBSERVED baseline network, preserved when a
    # scenario override mutates `status`.
    #
    # Why this exists: ScenarioEngine's CLOSE action overwrites `status` with
    # CLOSED. Without this field the information that the facility was EXISTING
    # before the scenario is destroyed, and closure cost — which must be charged
    # exactly when an EXISTING facility transitions open → closed — could never
    # fire for the scenario-driven closures it is meant to price.
    #
    # None means "no override has occurred"; `effective_baseline_status` then
    # falls back to `status`. Set automatically by the scenario engine; callers
    # normally leave it alone.
    baseline_status: Optional[FacilityStatus] = None

    # --- Contractual state (V1.4) ---
    # Minimal contractual modelling for brownfield closure decisions.
    # Nothing here is ever inferred — see ContractStatus.
    contract_status: ContractStatus = ContractStatus.NONE
    # When the contract is ACTIVE: may the facility be closed early at all?
    #   False → MILP constraint (C5c) pins y_i = 1 (closure prohibited)
    #   True  → closure permitted; `closure_cost` is the early-termination penalty
    # Ignored when contract_status is NONE or EXPIRED.
    contract_allows_early_closure: bool = True

    # Marks a facility made unavailable by an involuntary DISRUPTION rather than
    # a business decision to close. Such a facility is exempt from closure cost
    # and from contractual must-remain-open constraints: an outage is not a
    # voluntary closure. Set by the resilience engine; not client input.
    is_disruption_target: bool = False

    # Replenishment lead time for inventory module
    # Unit: days
    replenishment_lead_time_days: float = 1.0

    # Metadata
    region:  Optional[str] = None
    country: Optional[str] = None
    tags:    List[str]     = Field(default_factory=list)

    @field_validator("capacity_units_per_period", "production_capacity_units_per_period")
    @classmethod
    def cap_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"capacity must be >= 0, got {v}")
        return v

    @field_validator("fixed_cost_per_year", "handling_cost_per_unit",
                     "opening_cost", "closure_cost", "ramp_up_cost", "capex")
    @classmethod
    def costs_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"Cost values must be >= 0, got {v}")
        return v

    @property
    def is_market(self) -> bool:
        return self.role in (NodeRole.MARKET, NodeRole.CUSTOMER)

    @property
    def is_facility(self) -> bool:
        return not self.is_market

    @property
    def is_existing(self) -> bool:
        """True if this is a currently operating (not candidate) facility."""
        return self.status == FacilityStatus.EXISTING

    @property
    def is_candidate(self) -> bool:
        """True if this is a candidate (not yet built) facility."""
        return self.status == FacilityStatus.CANDIDATE

    @property
    def is_plant_or_supplier(self) -> bool:
        return self.role in (NodeRole.PLANT, NodeRole.SUPPLIER)

    def get_fixed_cost_for_period(self, cost_period: CostPeriod | str = CostPeriod.MONTH) -> float:
        """
        Return facility fixed cost normalized to the specified cost_period.
        Source parameter `fixed_cost_per_year` is retained in USD/year.
        """
        period_str = cost_period.value if hasattr(cost_period, "value") else str(cost_period)
        if period_str == "MONTH":
            return self.fixed_cost_per_year / 12.0
        elif period_str == "YEAR":
            return self.fixed_cost_per_year
        elif period_str == "DAY":
            return self.fixed_cost_per_year / 365.0
        elif period_str == "QUARTER":
            return self.fixed_cost_per_year / 4.0
        return self.fixed_cost_per_year / 12.0

    @property
    def effective_baseline_status(self) -> FacilityStatus:
        """
        The facility's status in the OBSERVED baseline network.

        Falls back to `status` when no scenario override has recorded a
        baseline. This is the field closure economics must key on — `status`
        alone is unreliable because scenario overrides mutate it.
        """
        return self.baseline_status if self.baseline_status is not None else self.status

    @property
    def was_operating_in_baseline(self) -> bool:
        """True if this facility was an operating (EXISTING) facility in the observed baseline."""
        return self.effective_baseline_status == FacilityStatus.EXISTING

    @property
    def contract_prohibits_closure(self) -> bool:
        """
        True when an active contract forbids closing this facility outright.

        A disruption target is exempt: involuntary unavailability is not a
        contractual breach of a voluntary-closure clause.
        """
        return (
            self.contract_status == ContractStatus.ACTIVE
            and not self.contract_allows_early_closure
            and not self.is_disruption_target
        )

    def closure_cost_applies(self, is_open: bool) -> bool:
        """
        Whether the one-time closure cost should be charged for this facility.

        Charged only when ALL of the following hold:
          - the facility was operating (EXISTING) in the observed baseline;
          - it is closed in this solution (y_i = 0);
          - it is not an involuntary disruption target;
          - a non-zero closure_cost is configured.

        Deliberately NOT charged for: facilities that stay open, facilities
        already CLOSED in the baseline, and unselected CANDIDATE facilities.

        Note this is the FACILITY-level test only. Whether closure economics
        apply at all is a MODE-level decision — see optimization/modes.py.
        """
        return (
            not is_open
            and self.was_operating_in_baseline
            and not self.is_disruption_target
            and self.closure_cost > 0.0
        )

    @property
    def effective_supply_capacity(self) -> float:
        """
        Effective supply-side capacity for PLANT/SUPPLIER nodes.
        Uses production_capacity if explicitly set (< 1e11);
        otherwise falls back to capacity_units_per_period.
        """
        if self.is_plant_or_supplier and self.production_capacity_units_per_period < 1e11:
            return self.production_capacity_units_per_period
        return self.capacity_units_per_period


# ---------------------------------------------------------------------------
# Product
# ---------------------------------------------------------------------------

class ProductRecord(BaseModel):
    """
    Product / SKU specification.
    """
    id:            str
    name:          str
    unit:          str   = "units"       # base unit of measure
    weight_kg:     float = 1.0           # kg per base unit
    volume_m3:     float = 0.001         # m³ per base unit
    unit_value:    float = 0.0           # currency per unit (for inventory valuation)
    holding_rate:  float = 0.25          # annual holding cost as fraction of unit_value

    @field_validator("weight_kg", "volume_m3", "unit_value", "holding_rate")
    @classmethod
    def non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"Product parameter must be >= 0, got {v}")
        return v

    def get_holding_rate_for_period(self, cost_period: CostPeriod | str = CostPeriod.MONTH) -> float:
        """
        Return annual holding_rate normalized to cost_period.
        Source holding_rate is annual (e.g. 0.24 = 24%/yr).
        """
        cp = cost_period.value if hasattr(cost_period, "value") else str(cost_period)
        if cp == "MONTH":
            return self.holding_rate / 12.0
        elif cp == "QUARTER":
            return self.holding_rate / 4.0
        elif cp == "DAY":
            return self.holding_rate / 365.0
        return self.holding_rate


# ---------------------------------------------------------------------------
# Demand
# ---------------------------------------------------------------------------

class DemandRecord(BaseModel):
    """
    Demand specification at a market node for a product in a planning period.

    UNIT NOTES (critical for inventory module):
        quantity:  units/period  (e.g., units/month if planning period = 1 month)
        std_dev:   units/period  (same unit as quantity — NOT units/day)
                   The inventory module converts to daily std_dev internally
                   using config.days_per_period.
    """
    market_id:    str
    product_id:   str
    period:       int    = 1      # planning period index (1-based)

    # Unit: units/period (e.g., units/month)
    quantity:     float
    # Unit: units/period (same as quantity — standard deviation of periodic demand)
    std_dev:      float = 0.0

    # Service requirements
    sla_days:     Optional[float] = None   # max acceptable lead time (days)
    service_level: float = 0.95            # required CSL / fill rate

    # Priority (used in shortage allocation if demand cannot be fully met)
    priority:     int   = 1       # 1 = highest priority

    @field_validator("quantity")
    @classmethod
    def quantity_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"Demand quantity must be >= 0, got {v}")
        return v

    @field_validator("service_level")
    @classmethod
    def service_level_fraction(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"service_level must be in [0, 1], got {v}")
        return v


# ---------------------------------------------------------------------------
# Lane (Arc)
# ---------------------------------------------------------------------------

class LaneRecord(BaseModel):
    """
    A directed transportation lane from origin to destination.

    Defines all arc-level parameters: cost, distance, lead time, capacity,
    carbon factor, eligible products, and mode.

    V1.1 ADDITIONS:
        distance_method: How distance_km was measured/estimated.
        network_distance_km: Actual road/rail network distance when available.
            If set, carbon and cost calculations should prefer this over
            distance_km (which may be straight-line).
            NOTE: In V1.1, the optimizer uses distance_km for cost/carbon
            and network_distance_km is informational. Future versions may
            use network_distance_km as the primary cost driver.
    """
    origin_id:      str
    destination_id: str
    mode:           TransportMode = TransportMode.ROAD

    # Unit: currency/unit of product
    rate_per_unit:  float

    # Unit: km — primary distance used in cost/carbon calculations
    # May be straight-line, estimated, or network distance depending on distance_method
    distance_km:    float = 0.0

    # Unit: days — transit time
    lead_time_days: float = 1.0

    # Unit: units/period; 0 or None means uncapacitated
    lane_capacity:  Optional[float] = None

    # How was distance_km measured?
    distance_method: DistanceMethod = DistanceMethod.ESTIMATED

    # Unit: km — actual road/rail/sea network distance (when available)
    # If None, distance_km serves as the best available estimate
    network_distance_km: Optional[float] = None

    # Emission factor override (kg CO₂ / tonne·km)
    # If None, use the mode-level default from CarbonModule
    emission_factor_override: Optional[float] = None

    # Which products are allowed on this lane (empty = all)
    eligible_product_ids: List[str] = Field(default_factory=list)

    # Is this lane currently active in the baseline?
    is_active_baseline: bool = True

    # Tariff component breakdown fields (F-12 business relocation logic)
    rate_per_km:                 Optional[float] = None  # currency / unit / km
    fixed_leg_cost:              Optional[float] = None  # currency / unit fixed leg handling charge
    speed_km_per_day:            Optional[float] = None  # transit speed in km / day
    terminal_time_days:          Optional[float] = None  # fixed terminal / loading / unloading time in days
    tariff_requires_user_input: bool            = False # set True if flat rate cannot be defensibly recomputed

    @field_validator("rate_per_unit")
    @classmethod
    def rate_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"rate_per_unit must be >= 0, got {v}")
        return v

    @field_validator("distance_km", "lead_time_days")
    @classmethod
    def non_negative_geo(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"distance_km / lead_time_days must be >= 0, got {v}")
        return v

    @property
    def effective_distance_km(self) -> float:
        """
        The best available distance for cost/carbon calculations.
        Prefers network_distance_km when available; falls back to distance_km.
        """
        if self.network_distance_km is not None and self.network_distance_km > 0:
            return self.network_distance_km
        return self.distance_km


# ---------------------------------------------------------------------------
# Optimization Configuration
# ---------------------------------------------------------------------------

class OptimizationConfig(BaseModel):
    """
    All solver and model configuration parameters.

    Separating configuration from data ensures that the same dataset can be
    solved under different objective modes, solver settings, and constraints
    without modifying the network data.

    V1.1 ADDITIONS:
        minimum_throughput_enabled: Global switch for C3 constraint.
        days_per_period:            Days in one planning period (for inventory unit conversion).
        emission_methodology:       Name/version of emission factor methodology.
        emission_factor_table:      Override emission factors (mode -> kg CO2/tonne-km).
        inventory_max_iterations:   Max iterations for iterative inventory solve.
        inventory_convergence_tolerance: Stop iterating when objective change < this fraction.
    """
    # --- Optimization mode (V1.4) ---
    # What decision the optimizer is being asked to make. All modes share one
    # MILP formulation; the mode only fixes variables and selects which economic
    # terms apply (see optimization/modes.py).
    #
    # Defaults to BROWNFIELD_SCENARIO_OPTIMIZATION because that mode honours
    # facility flags exactly as supplied and therefore reproduces NetGravity's
    # pre-V1.4 behaviour with no transformation — existing configurations keep
    # working unchanged.
    optimization_mode:    OptimizationMode = OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION

    # --- Objective ---
    objective_mode:       ObjectiveMode   = ObjectiveMode.COST_MIN
    carbon_price:         float           = 0.0    # currency / kg CO₂ (Mode D / C)
    carbon_cap_kg:        Optional[float] = None   # total CO₂ cap (Mode C)
    carbon_weight:        float           = 0.0    # weight on CO₂ in Mode D
    shortage_penalty:     float           = 1e6    # currency / unit unmet demand
    allow_shortage:       bool            = False

    # When the strict model proves infeasible, re-solve once with
    # allow_shortage=True and return THAT plan, marked as a relaxation.
    #
    # Off by default: a caller that asks for a fully-served plan and gets an
    # infeasible answer has learned something true, and silently substituting a
    # different model would hide it. It is switched on for networks assembled
    # from a client upload, where "no feasible plan exists" is the beginning of
    # the analysis rather than the end — the planner still needs to know which
    # facilities to open, what the served volume costs, and exactly how much
    # demand is stranded. The relaxed result carries
    # metadata["solve_relaxation"] and a non-zero unserved_demand, so nothing
    # downstream can mistake it for a fully-served plan.
    relax_to_shortage_when_infeasible: bool = False

    # --- Service ---
    service_metric:       ServiceMetric   = ServiceMetric.TRANSIT_TIME
    #: What a demand table stating more than one period is solved as. See
    #: `netgravity/optimization/periods.py`.
    #:
    #:   FULL_HORIZON         every period modelled, with stock carried between
    #:                        them (default)
    #:   REPRESENTATIVE_MEAN  collapsed to the mean of the periods
    #:   PEAK                 collapsed to the largest period
    #:   SUM                  collapsed to every period added together
    #:
    #: The default models the horizon because that is the question the data
    #: asks. A collapse answers a narrower one — PEAK is a genuinely useful
    #: "can the footprint carry the worst month", and is far cheaper to solve —
    #: but no averaging rule can tell a planner whether a network that carries
    #: the mean of twelve months carries the peak of one.
    #:
    #: A single-period network ignores this entirely and is solved exactly as
    #: it always was.
    multi_period_policy:  str             = "FULL_HORIZON"
    enforce_sla:          bool            = True   # filter lanes by SLA
    sla_mode:             SLAMode         = SLAMode.LAST_MILE

    # --- Sourcing ---
    sourcing_policy:      SourcingPolicy  = SourcingPolicy.MULTI

    # --- Facility constraints ---
    max_facilities:       Optional[int]   = None
    budget_capex:         Optional[float] = None

    # --- Closure economics (V1.4) ---
    # Charge FacilityRecord.closure_cost when an EXISTING facility transitions
    # open → closed. Only active in modes where closing is an actual decision
    # (brownfield / disruption) — see optimization/modes.py. Set False to
    # suppress closure economics entirely.
    # Safe by default: closure_cost defaults to 0.0, so enabling this changes
    # nothing unless closure costs are actually supplied.
    enable_closure_cost:  bool            = True

    # Enforce contractual must-remain-open commitments (constraint C5c).
    # Inert unless a facility carries contract_status=ACTIVE with
    # contract_allows_early_closure=False.
    enforce_contracts:    bool            = True

    # --- Minimum throughput ---
    # Set False to disable C3 globally (e.g. when client data lacks min-throughput)
    minimum_throughput_enabled: bool = True

    # --- Inventory ---
    enable_inventory:     bool            = True
    include_cycle_stock:  bool            = True
    inventory_z_score:    float           = 1.645  # 95% CSL

    # Days per planning period — used to convert periodic σ to daily σ for SS formula
    # Default: 30 days/month (monthly planning period)
    # Must match the time unit of DemandRecord.quantity and DemandRecord.std_dev
    days_per_period:      int             = 30

    # Iterative inventory solve settings
    # Iterations: 1 = no iteration (post-solve attribution only)
    #             >=2 = iterative solve until convergence
    inventory_max_iterations:         int   = 5
    # Stop iterating when |obj_k+1 - obj_k| / obj_k < this fraction
    # Also stops if open-facility set is identical between iterations
    inventory_convergence_tolerance:  float = 0.001   # 0.1%
    # Blend factor for inventory-coefficient updates between iterations
    # new_coeff = alpha * newly_computed + (1 - alpha) * previous_coeff
    inventory_damping_factor:         float = 0.5

    # --- Carbon ---
    enable_carbon_cost:   bool            = False

    # Emission factor methodology identifier — recorded in results for auditability
    # Example: "GLEC_v2.0", "GLEC_v3.0", "EPA_2023", "custom"
    emission_methodology: str             = "GLEC_v2.0"

    # Override emission factor table (mode -> kg CO2/tonne-km)
    # If None, uses defaults from config/defaults.py (GLEC v2.0)
    # If set, overrides mode-level defaults (lane-level overrides still take precedence)
    emission_factor_table: Optional[Dict[str, float]] = None

    # --- Solver ---
    solver_name:          str             = "HiGHS"   # HiGHS / CBC / Gurobi / CPLEX
    time_limit_seconds:   int             = 300
    mip_gap:              float           = 0.001     # 0.1% optimality gap
    # Absolute optimality tolerance, in the model's currency. None leaves the
    # relative gap in sole charge.
    #
    # It exists because a RELATIVE gap is meaningless once the objective is
    # dominated by the shortage penalty. When `allow_shortage` is on, the
    # objective is `business_cost + shortage_penalty x unserved`, and at the
    # default penalty of 1e6/unit that second term reaches ~8.7bn on a network
    # whose real cost is ~1.8e7. A 0.1% relative gap is then a tolerance of
    # ~8.7 MILLION rupees of genuine spend: the solver stops as soon as it has
    # the right unserved quantity and stops caring about the money. Two solves
    # of models differing only by a RELAXED constraint came back at
    # 9,561,047 and 14,512,146 — both reported OPTIMAL, a 52% spread — which is
    # exactly the "the metrics seem random" symptom seen on the scenario page.
    #
    # Set alongside `allow_shortage` so optimality is judged on money.
    mip_gap_abs:          Optional[float] = None
    threads:              int             = 0         # 0 = auto
    verbose:              bool            = False

    # --- Cost Period ---
    cost_period:          CostPeriod      = CostPeriod.MONTH

    # --- Versioning ---
    model_version:        str             = "1.2.0"

    @field_validator("mip_gap")
    @classmethod
    def gap_fraction(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"mip_gap must be in [0, 1], got {v}")
        return v

    @field_validator("days_per_period")
    @classmethod
    def positive_days(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"days_per_period must be > 0, got {v}")
        return v

    @field_validator("inventory_max_iterations")
    @classmethod
    def positive_iterations(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"inventory_max_iterations must be >= 1, got {v}")
        return v

    @field_validator("inventory_damping_factor")
    @classmethod
    def valid_damping_factor(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError(f"inventory_damping_factor must be in (0.0, 1.0], got {v}")
        return v


# ---------------------------------------------------------------------------
# Canonical Network (assembled)
# ---------------------------------------------------------------------------

class CanonicalNetwork(BaseModel):
    """
    The assembled, validated network ready for optimization.

    This is the single authoritative representation of the supply chain
    network passed to the optimizer. It is immutable after construction.
    """
    facilities:  List[FacilityRecord]
    products:    List[ProductRecord]
    demands:     List[DemandRecord]
    lanes:       List[LaneRecord]
    config:      OptimizationConfig = Field(default_factory=OptimizationConfig)

    # Metadata
    network_id:   str             = "network_default"
    data_version: Optional[str]   = None   # set by builder from input hash
    description:  str             = ""

    #: What each modelled planning period is, in the source's own words —
    #: `{"1": "2023-09", "2": "2023-10", ...}`.
    #:
    #: The engine indexes periods by integer everywhere (`DemandRecord.period`,
    #: `FlowDecision.period`, constraint names), which is what makes the model
    #: build. But "period 7" is not a thing a planner can act on, and an upload
    #: that states months knows perfectly well which month each index is. This
    #: is the only place that correspondence is recorded, so a screen, an
    #: assumption line or an evidence reference can say "2024-03" instead of
    #: silently renumbering the client's own calendar.
    #:
    #: Empty when the source stated no period labels, which is the honest
    #: answer for a single-period upload — not a fabricated "Period 1".
    period_labels: Dict[str, str] = Field(default_factory=dict)

    #: The money unit every cost in this network is stated in — an ISO 4217
    #: code such as "USD" or "INR", taken from the upload.
    #:
    #: The optimiser is unit-agnostic: it minimises a sum of numbers and never
    #: needed to know. Everything that *reports* those numbers does. Before this
    #: field existed the answer was hardcoded to INR in three separate places —
    #: the metric registry's `unit=`, the evidence formatter's `₹`, and the
    #: browser's `formatCurrency` — so a network priced in dollars reported a
    #: ₹23,226,260 baseline on every screen and in every export.
    #:
    #: None when the upload states no currency anywhere. That is a real
    #: possibility and it must stay distinguishable from "rupees": a caller
    #: renders a bare amount rather than stamping it with a unit the data does
    #: not support.
    currency: Optional[str] = None

    #: Where this network is, from the coordinates it carries — a region label
    #: plus the bounding box a map should fit to. Empty when the upload has no
    #: usable coordinates. Read, never assumed: every project used to be
    #: created as "India" whatever its data said.
    geography: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_network_consistency(self) -> "CanonicalNetwork":
        facility_ids: Set[str] = {f.id for f in self.facilities}
        product_ids:  Set[str] = {p.id for p in self.products}

        # Demand nodes must exist
        for d in self.demands:
            if d.market_id not in facility_ids:
                raise ValueError(
                    f"DemandRecord references unknown market_id '{d.market_id}'"
                )
            if d.product_id not in product_ids:
                raise ValueError(
                    f"DemandRecord references unknown product_id '{d.product_id}'"
                )

        # Lane endpoints must exist
        for ln in self.lanes:
            if ln.origin_id not in facility_ids:
                raise ValueError(
                    f"LaneRecord origin_id '{ln.origin_id}' not in facilities"
                )
            if ln.destination_id not in facility_ids:
                raise ValueError(
                    f"LaneRecord destination_id '{ln.destination_id}' not in facilities"
                )

        return self

    def get_facility(self, facility_id: str) -> FacilityRecord:
        for f in self.facilities:
            if f.id == facility_id:
                return f
        raise KeyError(f"Facility '{facility_id}' not found")

    def get_product(self, product_id: str) -> ProductRecord:
        for p in self.products:
            if p.id == product_id:
                return p
        raise KeyError(f"Product '{product_id}' not found")

    def get_markets(self) -> List[FacilityRecord]:
        return [f for f in self.facilities
                if f.role in (NodeRole.MARKET, NodeRole.CUSTOMER)]

    def get_facilities_by_role(self, role: NodeRole) -> List[FacilityRecord]:
        return [f for f in self.facilities if f.role == role]

    def get_optimizable_facilities(self) -> List[FacilityRecord]:
        """Facilities that are candidates for open/close in the MILP."""
        return [f for f in self.facilities if not f.is_market]

    def get_plants_and_suppliers(self) -> List[FacilityRecord]:
        """Plant and supplier nodes (supply-side)."""
        return [f for f in self.facilities
                if f.role in (NodeRole.PLANT, NodeRole.SUPPLIER)]

    def compute_data_version(self) -> str:
        """Compute a deterministic hash of input data for reproducibility."""
        payload = json.dumps(
            {
                "facilities": [f.model_dump() for f in self.facilities],
                "products":   [p.model_dump() for p in self.products],
                "demands":    [d.model_dump() for d in self.demands],
                "lanes":      [ln.model_dump() for ln in self.lanes],
                # Part of the input data, not presentation: the same numbers
                # denominated in a different currency are different facts, and
                # a cached analysis keyed only on the numbers would be served
                # back under the wrong unit.
                "currency":   self.currency,
                # Where the network is, for exactly the same reason.
                #
                # `SnapshotManager.register` derives the snapshot id from this
                # hash and RETURNS THE EXISTING SNAPSHOT when the id already
                # exists — correct for identical content, and the reason a
                # Canadian workbook kept reporting "United States" long after
                # the inference that produced that label had been fixed. The
                # rows were byte-identical, so the re-upload was handed the
                # snapshot registered by the earlier one, geography and all.
                #
                # A network's region is a fact about the network, so it belongs
                # in the fingerprint beside its currency. Two uploads that
                # agree on every row but disagree about where they are are not
                # the same network.
                "geography":  self.geography,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
