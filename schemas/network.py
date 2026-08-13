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
from typing import Dict, List, Optional, Set, Tuple

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
    """Which service metric is used for constraints."""
    TRANSIT_TIME  = "TRANSIT_TIME"   # hard SLA on lead time
    CSL           = "CSL"            # cycle service level (% of cycles without stockout)
    FILL_RATE     = "FILL_RATE"      # fraction of demand fulfilled
    PENALTY       = "PENALTY"        # no hard constraint; add shortage penalty


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

    # Products this facility can handle (empty = all products eligible)
    eligible_product_ids: List[str] = Field(default_factory=list)

    # Facility control flags
    is_closable:    bool = True    # can the MILP choose to close this?
    is_mandatory:   bool = False   # must remain open regardless (y_i = 1 forced)
    is_forced_closed: bool = False # force closed (y_i = 0 forced); used by scenario engine

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
    # --- Objective ---
    objective_mode:       ObjectiveMode   = ObjectiveMode.COST_MIN
    carbon_price:         float           = 0.0    # currency / kg CO₂ (Mode D / C)
    carbon_cap_kg:        Optional[float] = None   # total CO₂ cap (Mode C)
    carbon_weight:        float           = 0.0    # weight on CO₂ in Mode D
    shortage_penalty:     float           = 1e6    # currency / unit unmet demand
    allow_shortage:       bool            = False

    # --- Service ---
    service_metric:       ServiceMetric   = ServiceMetric.TRANSIT_TIME
    enforce_sla:          bool            = True   # filter lanes by SLA
    sla_mode:             SLAMode         = SLAMode.LAST_MILE

    # --- Sourcing ---
    sourcing_policy:      SourcingPolicy  = SourcingPolicy.MULTI

    # --- Facility constraints ---
    max_facilities:       Optional[int]   = None
    budget_capex:         Optional[float] = None

    # --- Minimum throughput ---
    # Set False to disable C3 globally (e.g. when client data lacks min-throughput)
    minimum_throughput_enabled: bool = True

    # --- Inventory ---
    enable_inventory:     bool            = True
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
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
