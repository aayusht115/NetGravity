"""
NetGravity — Carbon Emission Module V1.1
=========================================
Computes CO₂ emissions from transportation flows.

Formula (Assumption A-005, A-009):
    CO₂_kg = distance_km × weight_kg × emission_factor_kg_co2_per_tonne_km / 1000

Where:
    distance_km     = lane distance
    weight_kg       = flow_units × product.weight_kg
    ef              = kg CO₂ / (tonne · km)
    / 1000          = convert kg to tonnes for ef denominator

The optimizer can:
  1. Ignore carbon (default)
  2. Minimize carbon (objective mode)
  3. Constrain carbon (carbon cap)
  4. Optimize weighted cost + carbon (carbon pricing)

V1.1 ADDITIONS:
  - methodology_version parameter: records which emission factor standard was used.
    Every CarbonResult now carries the methodology version for audit traceability.
  - emission_factor_table: injected from OptimizationConfig.emission_factor_table.
    Priority: lane-level override > config-level table > GLEC v2.0 defaults.
  - CarbonResult.methodology: records the methodology version string.

Default emission factors: GLEC Framework v2.0 (config/defaults.py)
Lane-level override: LaneRecord.emission_factor_override
Config-level override: OptimizationConfig.emission_factor_table

Source: Global Logistics Emissions Council (GLEC) Framework v2.0
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from netgravity.config.defaults import EMISSION_FACTORS_KG_CO2_PER_TONNE_KM
from netgravity.schemas.network import LaneRecord, ProductRecord, TransportMode


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class CarbonResult:
    """CO₂ result for a single flow on a lane."""
    origin_id:       str
    destination_id:  str
    mode:            str
    product_id:      str
    flow_units:      float
    distance_km:     float
    weight_per_unit: float          # kg per unit
    total_weight_kg: float          # flow_units × weight_per_unit
    emission_factor: float          # kg CO₂ / (tonne · km)
    co2_kg:          float          # total CO₂ for this flow
    methodology:     str = "GLEC_v2.0"   # V1.1: emission factor methodology (for audit)


# ---------------------------------------------------------------------------
# Carbon module
# ---------------------------------------------------------------------------

class CarbonModule:
    """
    Compute CO₂ emissions from freight flows.

    All assumptions are documented (see module docstring and assumptions/registry.py).
    """

    def __init__(
        self,
        emission_factors: Optional[Dict[str, float]] = None,
        methodology: str = "GLEC_v2.0",
    ):
        """
        Args:
            emission_factors: Override the default GLEC emission factors.
                              Keys: mode string (ROAD, RAIL, AIR, SEA, INTERMODAL)
                              Values: kg CO₂ per tonne·km
                              Priority: lane-level override > this table > GLEC defaults
            methodology: Emission factor methodology version string.
                         Recorded in every CarbonResult for audit traceability.
                         Example: 'GLEC_v2.0', 'GLEC_v3.0', 'EPA_2023', 'custom'
        """
        self._ef = dict(EMISSION_FACTORS_KG_CO2_PER_TONNE_KM)
        if emission_factors:
            self._ef.update(emission_factors)
        self._methodology = methodology

    def get_emission_factor(
        self,
        lane:    LaneRecord,
    ) -> float:
        """
        Retrieve emission factor for a lane.
        Lane-level override takes precedence over mode-level default.
        """
        if lane.emission_factor_override is not None:
            return lane.emission_factor_override
        mode_key = lane.mode.value if hasattr(lane.mode, "value") else str(lane.mode)
        return self._ef.get(mode_key, self._ef.get("ROAD", 0.062))

    def compute_unit_co2(
        self,
        lane:    LaneRecord,
        product: ProductRecord,
    ) -> float:
        """
        CO₂ emitted per unit of product transported on this lane.

        Returns: kg CO₂ / unit
        Formula: distance_km × weight_kg_per_unit × ef / 1000
        """
        ef = self.get_emission_factor(lane)
        return lane.distance_km * product.weight_kg * ef / 1000.0

    def compute(
        self,
        lane:       LaneRecord,
        product:    ProductRecord,
        flow_units: float,
    ) -> CarbonResult:
        """
        Compute total CO₂ for a given flow.

        Args:
            lane:       LaneRecord with distance and mode
            product:    ProductRecord with weight_kg
            flow_units: Units shipped

        Returns:
            CarbonResult with full audit trail
        """
        ef           = self.get_emission_factor(lane)
        total_weight = flow_units * product.weight_kg   # kg
        # Formula: kg_CO₂ = (distance_km) × (weight_tonnes) × (ef kg/tonne·km)
        # weight_tonnes = total_weight / 1000
        co2_kg = lane.distance_km * (total_weight / 1000.0) * ef

        return CarbonResult(
            origin_id       = lane.origin_id,
            destination_id  = lane.destination_id,
            mode            = lane.mode.value if hasattr(lane.mode, "value") else str(lane.mode),
            product_id      = product.id,
            flow_units      = flow_units,
            distance_km     = lane.distance_km,
            weight_per_unit = product.weight_kg,
            total_weight_kg = round(total_weight, 4),
            emission_factor = ef,
            co2_kg          = round(co2_kg, 6),
            methodology     = self._methodology,
        )

    def compute_total_co2(
        self,
        flows:    List[Dict],   # list of {lane, product, flow_units}
    ) -> float:
        """
        Sum CO₂ across all flows in a network solution.

        Args:
            flows: List of dicts with keys 'lane', 'product', 'flow_units'

        Returns:
            Total CO₂ in kg
        """
        total = 0.0
        for item in flows:
            result = self.compute(
                lane       = item["lane"],
                product    = item["product"],
                flow_units = item["flow_units"],
            )
            total += result.co2_kg
        return round(total, 4)

    def update_emission_factor(self, mode: str, ef_kg_co2_per_tonne_km: float) -> None:
        """Override emission factor for a transport mode (for scenarios)."""
        self._ef[mode] = ef_kg_co2_per_tonne_km
