"""
NetGravity — Typed Data Schemas: Scenarios
==========================================
Scenario specification and parameter-override contracts.

A Scenario modifies a CanonicalNetwork by applying overrides to parameters,
facilities, demands, costs, and constraints.

The optimization engine is NEVER modified by scenarios.
Scenarios modify INPUTS and call the SAME optimizer.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Scenario type enumeration
# ---------------------------------------------------------------------------

class ScenarioType(str, Enum):
    """
    Classification of scenario intent.
    Used for reporting and scenario management; does not affect optimization.
    """
    CLOSE_FACILITY       = "CLOSE_FACILITY"
    OPEN_FACILITY        = "OPEN_FACILITY"
    CHANGE_CAPACITY      = "CHANGE_CAPACITY"
    CHANGE_DEMAND        = "CHANGE_DEMAND"
    CHANGE_TRANSPORT_COST = "CHANGE_TRANSPORT_COST"
    LANE_DISRUPTION      = "LANE_DISRUPTION"
    FACILITY_DISRUPTION  = "FACILITY_DISRUPTION"
    CARBON_FACTOR_CHANGE = "CARBON_FACTOR_CHANGE"
    SERVICE_TARGET_CHANGE = "SERVICE_TARGET_CHANGE"
    CONSOLIDATE          = "CONSOLIDATE"
    EXPAND               = "EXPAND"
    CUSTOM               = "CUSTOM"


# ---------------------------------------------------------------------------
# Parameter override
# ---------------------------------------------------------------------------

class ParameterOverride(BaseModel):
    """
    A single parameter override identified by path and value.

    path:  dot-separated path in the CanonicalNetwork schema
           e.g. "facilities.DC_PUNE.capacity_units_per_period"
               "demands.MARKET_DELHI.P001.quantity"
               "lanes.DC_PUNE.MARKET_DELHI.ROAD.rate_per_unit"
    operation: SET (replace) | MULTIPLY (scale) | ADD (shift)
    value: new value or multiplier or delta
    """
    path:      str
    operation: str  = "SET"    # SET | MULTIPLY | ADD
    value:     Any

    model_config = ConfigDict(extra="forbid")

    @field_validator("operation")
    @classmethod
    def valid_op(cls, v: str) -> str:
        if v not in ("SET", "MULTIPLY", "ADD"):
            raise ValueError(f"operation must be SET | MULTIPLY | ADD, got '{v}'")
        return v


from netgravity.schemas.network import FacilityRecord, LaneRecord


# ---------------------------------------------------------------------------
# Facility change
# ---------------------------------------------------------------------------

class FacilityChange(BaseModel):
    """
    Targeted change to a specific facility's properties.
    More ergonomic than ParameterOverride for common facility changes.
    """
    facility_id:    str
    action:         str    # CLOSE | OPEN | SET_CAPACITY | SET_FIXED_COST | FORCE_OPEN | MOVE | SET_LOCATION | ADD_FACILITY

    # For capacity changes
    capacity_override:   Optional[float] = None
    capacity_multiplier: Optional[float] = None    # multiply existing capacity

    # For cost changes
    fixed_cost_override: Optional[float] = None

    # For location moves (MOVE / SET_LOCATION)
    latitude:  Optional[float] = None
    longitude: Optional[float] = None

    # For adding new facility (ADD_FACILITY)
    new_facility: Optional[FacilityRecord] = None
    new_lanes:    List[LaneRecord]        = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @field_validator("action")
    @classmethod
    def valid_action(cls, v: str) -> str:
        allowed = {
            "CLOSE", "OPEN", "SET_CAPACITY", "SET_FIXED_COST", "FORCE_OPEN",
            "MOVE", "SET_LOCATION", "MOVE_FACILITY", "ADD_FACILITY",
        }
        if v not in allowed:
            raise ValueError(f"action must be one of {allowed}, got '{v}'")
        return v


# ---------------------------------------------------------------------------
# Demand change
# ---------------------------------------------------------------------------

class DemandChange(BaseModel):
    """
    Change to demand for one or all market-product combinations.
    """
    market_id:   Optional[str] = None   # None = all markets
    product_id:  Optional[str] = None   # None = all products
    period:      Optional[int] = None   # None = all periods

    quantity_multiplier: Optional[float] = None   # scale factor (e.g., 1.2 = +20%)
    quantity_override:   Optional[float] = None   # absolute override
    std_dev_multiplier:  Optional[float] = None   # scale demand variability

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Cost change
# ---------------------------------------------------------------------------

class CostChange(BaseModel):
    """
    Change to transportation cost on specific lanes or globally.
    """
    origin_id:      Optional[str] = None   # None = all origins
    destination_id: Optional[str] = None   # None = all destinations
    mode:           Optional[str] = None   # None = all modes

    rate_multiplier: Optional[float] = None   # scale factor
    rate_override:   Optional[float] = None   # absolute override

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Lane change
# ---------------------------------------------------------------------------

class LaneChange(BaseModel):
    """
    Add, remove, or modify a specific network lane.
    """
    origin_id:      str
    destination_id: str
    mode:           str = "ROAD"

    action:         str   # ADD | REMOVE | MODIFY

    rate_per_unit:   Optional[float] = None
    distance_km:     Optional[float] = None
    lead_time_days:  Optional[float] = None
    lane_capacity:   Optional[float] = None
    is_active:       Optional[bool]  = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("action")
    @classmethod
    def valid_lane_action(cls, v: str) -> str:
        allowed = {"ADD", "REMOVE", "MODIFY"}
        if v not in allowed:
            raise ValueError(f"action must be one of {allowed}, got '{v}'")
        return v


# ---------------------------------------------------------------------------
# Full Scenario
# ---------------------------------------------------------------------------

class Scenario(BaseModel):
    """
    A complete scenario specification.
    """
    scenario_id:     str
    scenario_name:   str
    scenario_type:   ScenarioType          = ScenarioType.CUSTOM
    description:     str                   = ""
    base_model_version: str                = "1.0.0"

    # Override lists (all applied in order)
    facility_changes:   List[FacilityChange]   = Field(default_factory=list)
    demand_changes:     List[DemandChange]     = Field(default_factory=list)
    cost_changes:       List[CostChange]       = Field(default_factory=list)
    lane_changes:       List[LaneChange]       = Field(default_factory=list)
    parameter_overrides: List[ParameterOverride] = Field(default_factory=list)

    # Config overrides (e.g., change objective mode, carbon price)
    config_overrides:   Dict[str, Any]          = Field(default_factory=dict)

    # Metadata
    created_by:  Optional[str]  = None
    tags:        List[str]      = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Scenario Library (convenience)
# ---------------------------------------------------------------------------

class ScenarioLibrary(BaseModel):
    """
    A named collection of scenarios to be compared.
    """
    library_id:      str
    baseline_id:     str              # scenario_id of the baseline (current state)
    scenarios:       List[Scenario]
    description:     str = ""

    model_config = ConfigDict(extra="forbid")

    def get_scenario(self, scenario_id: str) -> Optional[Scenario]:
        for s in self.scenarios:
            if s.scenario_id == scenario_id:
                return s
        return None
