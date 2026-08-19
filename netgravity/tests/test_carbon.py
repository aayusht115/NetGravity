"""NetGravity — Carbon Module Tests"""

import pytest
from netgravity.carbon.module import CarbonModule
from netgravity.schemas.network import LaneRecord, ProductRecord, TransportMode


class TestCarbonFormula:
    """Test CO₂ = dist × (weight/1000) × ef"""

    def setup_method(self):
        self.carbon = CarbonModule()
        self.product = ProductRecord(id="P", name="Test", weight_kg=2.5, unit_value=0, holding_rate=0)
        self.lane    = LaneRecord(
            origin_id="A", destination_id="B", mode=TransportMode.ROAD,
            rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0
        )

    def test_co2_formula_road(self):
        result = self.carbon.compute(self.lane, self.product, flow_units=1000)
        # CO₂ = 100 km × (2500 kg / 1000) × 0.062 = 15.5 kg
        expected = 100 * (2500 / 1000) * 0.062
        assert abs(result.co2_kg - expected) < 0.01, f"Got {result.co2_kg}, expected {expected}"

    def test_zero_flow_zero_co2(self):
        result = self.carbon.compute(self.lane, self.product, flow_units=0)
        assert result.co2_kg == 0.0

    def test_air_has_higher_ef_than_road(self):
        air_lane = LaneRecord(
            origin_id="A", destination_id="B", mode=TransportMode.AIR,
            rate_per_unit=10.0, distance_km=100.0, lead_time_days=0.1
        )
        road_result = self.carbon.compute(self.lane,    self.product, 100)
        air_result  = self.carbon.compute(air_lane, self.product, 100)
        assert air_result.co2_kg > road_result.co2_kg, "Air should emit more CO₂ than road"

    def test_rail_has_lower_ef_than_road(self):
        rail_lane = LaneRecord(
            origin_id="A", destination_id="B", mode=TransportMode.RAIL,
            rate_per_unit=2.0, distance_km=100.0, lead_time_days=2.0
        )
        road_result = self.carbon.compute(self.lane,    self.product, 100)
        rail_result = self.carbon.compute(rail_lane, self.product, 100)
        assert rail_result.co2_kg < road_result.co2_kg, "Rail should emit less CO₂ than road"

    def test_lane_level_override(self):
        """Lane-level emission factor override takes precedence."""
        override_ef  = 0.200
        override_lane = LaneRecord(
            origin_id="A", destination_id="B", mode=TransportMode.ROAD,
            rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0,
            emission_factor_override=override_ef
        )
        result = self.carbon.compute(override_lane, self.product, flow_units=1000)
        expected = 100 * (2500 / 1000) * override_ef
        assert abs(result.co2_kg - expected) < 0.01

    def test_unit_co2_per_unit(self):
        uco2 = self.carbon.compute_unit_co2(self.lane, self.product)
        # 100 km × 2.5 kg/unit × 0.062 ef / 1000 = 0.0155 kg CO₂ / unit
        expected = 100 * 2.5 * 0.062 / 1000
        assert abs(uco2 - expected) < 1e-6

    def test_update_emission_factor(self):
        self.carbon.update_emission_factor("ROAD", 0.100)
        result = self.carbon.compute(self.lane, self.product, flow_units=1000)
        expected = 100 * (2500 / 1000) * 0.100
        assert abs(result.co2_kg - expected) < 0.01
