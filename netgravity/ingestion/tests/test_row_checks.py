"""
Row-level validation tests.

The point of these is that the pipeline CATCHES bad data, not merely that it
passes on good data. Each test feeds a deliberately broken row and asserts the
specific issue code is raised.
"""

from __future__ import annotations

from netgravity.ingestion.adapters import structured
from netgravity.ingestion.schemas.ingest_result import Severity
from netgravity.ingestion.validation import row_checks as rc


def codes(result) -> set:
    return {i.code for i in result.issues}


def errors(result) -> list:
    return [i for i in result.issues if i.severity == Severity.ERROR]


# --- R-001 required fields --------------------------------------------------

def test_missing_required_field_is_rejected():
    rows = [{"facility_id": "", "facility_name": "Nameless", "role": "DC"}]
    _, result = structured.parse_facilities(rows)
    assert "R-001" in codes(result)
    assert result.rows_accepted == 0
    assert result.rows_rejected == 1


# --- R-002 / R-003 numeric --------------------------------------------------

def test_non_numeric_capacity_is_rejected():
    rows = [{"facility_id": "DC_X", "facility_name": "X", "role": "DC",
             "capacity_units_per_period": "lots"}]
    _, result = structured.parse_facilities(rows)
    assert "R-002" in codes(result)


def test_negative_cost_is_rejected():
    rows = [{"facility_id": "DC_X", "facility_name": "X", "role": "DC",
             "fixed_cost_per_year": "-5000"}]
    _, result = structured.parse_facilities(rows)
    assert "R-003" in codes(result)


def test_currency_symbols_and_separators_are_tolerated():
    """Human-maintained spreadsheets contain '₹1,20,000' — we should cope."""
    rows = [{"facility_id": "DC_X", "facility_name": "X", "role": "DC",
             "fixed_cost_per_year": "1,20,000"}]
    records, result = structured.parse_facilities(rows)
    assert not errors(result)
    assert records[0].fixed_cost_per_year == 120000.0


# --- R-004 coordinates ------------------------------------------------------

def test_impossible_latitude_is_rejected():
    issues = rc.check_coordinates(120.0, 77.0, "f.csv", 2)
    assert any(i.code == "R-004" and i.severity == Severity.ERROR for i in issues)


def test_coordinates_outside_india_warn_but_do_not_reject():
    """The UK test fixture's coordinates should be flagged, not silently accepted."""
    issues = rc.check_coordinates(52.5, -1.9, "f.csv", 2)
    assert issues
    assert all(i.severity == Severity.WARNING for i in issues)


# --- R-005 enums ------------------------------------------------------------

def test_unknown_role_is_rejected():
    rows = [{"facility_id": "X", "facility_name": "X", "role": "SPACESHIP"}]
    _, result = structured.parse_facilities(rows)
    assert "R-005" in codes(result)


# --- R-006 referential integrity -------------------------------------------

def test_lane_to_unknown_destination_is_rejected():
    rows = [{"origin_id": "DC_TEST", "destination_id": "MKT_NOWHERE",
             "mode": "ROAD", "rate_per_unit": "10", "distance_km": "100",
             "lead_time_days": "1"}]
    _, result = structured.parse_lanes(rows, {"DC_TEST", "MKT_A"})
    assert "R-006" in codes(result)
    assert result.rows_accepted == 0


def test_demand_for_unknown_market_is_rejected():
    rows = [{"market_id": "MKT_GHOST", "product_id": "P001", "quantity": "100"}]
    _, result = structured.parse_demand(rows, {"MKT_A"}, {"P001"})
    assert "R-006" in codes(result)


# --- R-007 duplicates -------------------------------------------------------

def test_duplicate_facility_id_is_reported():
    rows = [
        {"facility_id": "DC_X", "facility_name": "First", "role": "DC"},
        {"facility_id": "DC_X", "facility_name": "Second", "role": "DC"},
    ]
    _, result = structured.parse_facilities(rows)
    assert "R-007" in codes(result)


# --- R-008 physical plausibility -------------------------------------------

def test_zero_lead_time_on_long_lane_warns():
    """
    A 1,400 km lane with zero transit time would make every SLA constraint
    pass automatically — the most damaging kind of silent bad data.
    """
    issues = rc.check_lane_plausibility(1400.0, 0.0, "lanes.csv", 5)
    assert any(i.code == "R-008" for i in issues)
    assert all(i.severity == Severity.WARNING for i in issues)


def test_short_lane_with_zero_lead_time_is_fine():
    assert rc.check_lane_plausibility(20.0, 0.0, "lanes.csv", 5) == []


def test_self_referencing_lane_is_rejected():
    rows = [{"origin_id": "DC_TEST", "destination_id": "DC_TEST",
             "mode": "ROAD", "rate_per_unit": "5"}]
    _, result = structured.parse_lanes(rows, {"DC_TEST"})
    assert result.rows_accepted == 0


# --- R-009 unit / magnitude sanity -----------------------------------------

def test_service_level_given_as_percentage_is_rejected():
    """95 instead of 0.95 is a classic and dangerous unit error."""
    rows = [{"market_id": "MKT_A", "product_id": "P001",
             "quantity": "100", "service_level": "95"}]
    _, result = structured.parse_demand(rows, {"MKT_A"}, {"P001"})
    assert "R-009" in codes(result)


# --- R-011 capacity vs throughput ------------------------------------------

def test_throughput_above_capacity_warns():
    issues = rc.check_capacity_vs_throughput(1000.0, 1400.0, "DC_X", "f.csv", 2)
    assert any(i.code == "R-011" for i in issues)


def test_throughput_below_capacity_is_silent():
    assert rc.check_capacity_vs_throughput(1000.0, 900.0, "DC_X", "f.csv", 2) == []
