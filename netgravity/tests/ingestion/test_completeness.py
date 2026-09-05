"""
Tests for netgravity.ingestion.completeness.

Two claims under test:
  1. Required-field gaps are reported per named entity (Supply Location /
     Candidate DC / Demand Zone / Lane), using the exact display_label
     strings a planner would recognise — never the internal canonical_key.
  2. Optional-field gaps are reported dataset-wide, never blocking, and a
     field satisfied by contracts (rather than a tabular column) is
     correctly suppressed when has_contracts=True.
"""

from __future__ import annotations

from netgravity.ingestion.completeness import (
    ENTITY_DC,
    ENTITY_DEMAND_ZONE,
    ENTITY_LANE,
    ENTITY_SUPPLY,
    check_completeness,
)
from netgravity.ingestion.schemas.content import ContentType
from netgravity.ingestion.tabular import TabularResult


def _complete_outcome() -> TabularResult:
    outcome = TabularResult()
    outcome.network_rows[ContentType.FACILITY] = [
        {"facility_id": "SUP1", "facility_name": "Mumbai Plant", "role": "PLANT",
         "capacity_units_per_period": 5000},
        {"facility_id": "DC1", "facility_name": "Pune DC", "role": "DC",
         "capacity_units_per_period": 8000, "fixed_cost_per_year": 1200000},
    ]
    outcome.network_rows[ContentType.MARKET] = [
        {"market_id": "MKT1", "market_name": "Pune Zone"},
    ]
    outcome.network_rows[ContentType.DEMAND] = [
        {"market_id": "MKT1", "quantity": 300},
    ]
    outcome.network_rows[ContentType.LANE] = [
        {"origin_id": "SUP1", "destination_id": "DC1", "rate_per_unit": 12},
        {"origin_id": "DC1", "destination_id": "MKT1", "rate_per_unit": 5},
    ]
    return outcome


def test_complete_dataset_has_no_missing_required():
    report = check_completeness(_complete_outcome())
    assert report.missing_required == []
    assert not report.is_blocking


def test_complete_dataset_reports_all_optional_fields_missing():
    report = check_completeness(_complete_outcome())
    labels = {m.display_label for m in report.missing_optional}
    assert "Carbon Emission Factor (kg CO₂/unit)" in labels
    assert "Service Level Target (%)" in labels
    assert "Contract Rate Card / Surcharge Details" in labels


def test_has_contracts_suppresses_contract_rate_card_gap():
    report = check_completeness(_complete_outcome(), has_contracts=True)
    labels = {m.display_label for m in report.missing_optional}
    assert "Contract Rate Card / Surcharge Details" not in labels


def test_missing_dc_fixed_cost_is_reported_against_the_named_dc():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.FACILITY][1]["fixed_cost_per_year"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required
              if m.canonical_key == "fixed_cost_per_year"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_DC
    assert matches[0].entity_name == "Pune DC"
    assert matches[0].display_label == "DC Annual Fixed Cost (₹ lakh/year)"


def test_missing_supply_capacity_bucketed_as_supply_location():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.FACILITY][0]["capacity_units_per_period"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required
              if m.canonical_key == "capacity_units_per_period"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_SUPPLY
    assert matches[0].entity_name == "Mumbai Plant"


def test_missing_demand_zone_name_is_reported():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.MARKET][0]["market_name"]

    report = check_completeness(outcome)
    matches = [m for m in report.missing_required if m.display_label == "Demand Zone Name"]
    assert len(matches) == 1
    assert matches[0].entity_type == ENTITY_DEMAND_ZONE


def test_lane_legs_are_classified_source_to_dc_vs_dc_to_demand():
    outcome = _complete_outcome()
    del outcome.network_rows[ContentType.LANE][0]["rate_per_unit"]
    del outcome.network_rows[ContentType.LANE][1]["rate_per_unit"]

    report = check_completeness(outcome)
    labels = {m.display_label for m in report.missing_required if m.entity_type == ENTITY_LANE}
    assert "Transport Cost: Source → DC (₹/unit)" in labels
    assert "Transport Cost: DC → Demand Zone (₹/unit)" in labels


def test_unresolvable_lane_endpoints_are_skipped_not_misclassified():
    outcome = _complete_outcome()
    outcome.network_rows[ContentType.LANE].append(
        {"origin_id": "UNKNOWN1", "destination_id": "UNKNOWN2"})

    report = check_completeness(outcome)
    lane_gaps = [m for m in report.missing_required if m.entity_type == ENTITY_LANE]
    # Only the two resolvable lanes' legs — the unresolvable third lane
    # contributes nothing, since guessing its classification would be worse
    # than skipping it.
    assert len(lane_gaps) == 0
