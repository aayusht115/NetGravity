"""
NetGravity — Transportation Cost Engine
========================================
Abstract interface for computing transport cost on a lane.

V1: Linear cost  →  rate_per_unit × flow
Future V2: Fixed trip cost + variable cost + fuel surcharge + toll + carrier markup

The optimizer calls CostEngine.compute() for every arc.
Changing the cost formula never requires changing the MILP builder.

Assumption A-003: Transport cost is linear in flow volume (documented).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from netgravity.schemas.network import LaneRecord, ProductRecord


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

class CostEngine(ABC):
    """
    Abstract base class for transport cost computation.
    All cost engines must implement compute().
    """

    @abstractmethod
    def compute(
        self,
        lane:     LaneRecord,
        product:  ProductRecord,
        flow:     float,
    ) -> float:
        """
        Compute total transport cost for a given flow on a lane.

        Args:
            lane:    The LaneRecord defining origin, destination, mode, rate
            product: The ProductRecord (weight, volume used in future models)
            flow:    Units shipped (decision variable value or planning quantity)

        Returns:
            Total cost (currency)
        """
        ...

    @abstractmethod
    def unit_cost(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
    ) -> float:
        """
        Unit transport cost (currency / unit of product).
        Used as the coefficient c_{ijvk} in the MILP objective.
        """
        ...


# ---------------------------------------------------------------------------
# V1 — Linear rate engine (default)
# ---------------------------------------------------------------------------

class LinearCostEngine(CostEngine):
    """
    V1 Linear Transport Cost Engine.

    Formula:
        TransportCost = rate_per_unit × flow

    Where rate_per_unit is defined in LaneRecord.

    Assumption A-003: Linear in flow volume.
    """

    def compute(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
        flow:    float,
    ) -> float:
        return lane.rate_per_unit * flow

    def unit_cost(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
    ) -> float:
        return lane.rate_per_unit


# ---------------------------------------------------------------------------
# V2 scaffold — Fixed + Variable cost engine (future)
# ---------------------------------------------------------------------------

class FixedVariableCostEngine(CostEngine):
    """
    Future V2: Fixed trip cost + variable cost per unit.

    Formula:
        TransportCost = fixed_cost_per_trip × ceil(flow / vehicle_capacity)
                      + variable_cost_per_unit × flow
                      + fuel_surcharge_pct × (variable_cost_per_unit × flow)

    NOTE: ceil() introduces nonlinearity. In MILP, this requires a
    trip-count binary variable. Use LinearCostEngine unless explicit
    full-truck-load modeling is needed.
    """

    def __init__(
        self,
        vehicle_capacity: float = 1.0,
        fuel_surcharge_pct: float = 0.0,
    ):
        self.vehicle_capacity   = vehicle_capacity
        self.fuel_surcharge_pct = fuel_surcharge_pct

    def compute(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
        flow:    float,
    ) -> float:
        import math
        # Variable portion
        variable = lane.rate_per_unit * flow
        surcharge = variable * self.fuel_surcharge_pct
        return variable + surcharge

    def unit_cost(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
    ) -> float:
        # Linear coefficient for MILP (ignoring fixed trip cost at LP relaxation)
        return lane.rate_per_unit * (1 + self.fuel_surcharge_pct)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_cost_engine(engine_type: str = "linear", **kwargs) -> CostEngine:
    """
    Factory function: return the appropriate CostEngine.

    Args:
        engine_type: "linear" | "fixed_variable"
        **kwargs:    Passed to engine constructor

    Returns:
        CostEngine instance
    """
    if engine_type == "linear":
        return LinearCostEngine()
    elif engine_type == "fixed_variable":
        return FixedVariableCostEngine(**kwargs)
    else:
        raise ValueError(f"Unknown cost engine type: '{engine_type}'")
