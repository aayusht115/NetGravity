"""
Time-period consistency tests.

The engine works in MONTHS. Every per-period quantity — demand, capacity,
throughput, lane capacity — must arrive on that basis. A mismatch does not
crash: it produces a confident, wrong answer, which is why these are pinned.
"""

from __future__ import annotations

import pytest

from netgravity.ingestion.adapters import structured
from netgravity.ingestion.adapters.distributor import normalise_periods_to_month
from netgravity.ingestion.builder import build_network
from netgravity.ingestion.field_aliases import infer_period_from_name
from netgravity.ingestion.schemas.ingest_result import Severity
from netgravity.ingestion.schemas.mapping import ColumnMapping, DistributorMapping
from netgravity.schemas.network import ProductRecord


# --- period inference -------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Daily_Demand_Units", "DAY"),
    ("Weekly_Volume", "WEEK"),
    ("Monthly_Capacity", "MONTH"),
    ("Fixed_Annual_Cost", "YEAR"),
    ("Capacity_Units", None),          # workbook states no period — this is the trap
    ("quantity", None),
    ("Lead_Time_Days", None),          # 'days' is a duration, not a rate period
])
def test_period_is_read_from_the_column_name(name, expected):
    assert infer_period_from_name(name) == expected


# --- capacity follows demand ------------------------------------------------

def test_daily_capacity_column_is_converted_like_demand():
    rows = [{"Facility_ID": "DC_A", "Facility_Name": "A", "Type": "DC",
             "Daily_Capacity_Units": "2000"}]
    records, result = structured.parse_facilities(rows)
    assert records[0].capacity_units_per_period == 2000 * 30
    assert any(i.code == "R-020" for i in result.issues)


def test_unlimited_capacity_sentinel_is_never_rescaled():
    """1e12 means 'no limit'. Multiplying it by 30 would be meaningless."""
    rows = [{"Facility_ID": "DC_A", "Facility_Name": "A", "Type": "DC",
             "Daily_Capacity_Units": ""}]
    records, _ = structured.parse_facilities(rows)
    assert records[0].capacity_units_per_period == pytest.approx(1e12)


def test_min_throughput_follows_the_same_period():
    rows = [{"Facility_ID": "DC_A", "Facility_Name": "A", "Type": "DC",
             "Daily_Min_Throughput_Per_Period": "100"}]
    records, _ = structured.parse_facilities(rows)
    assert records[0].min_throughput_per_period == 100 * 30


# --- the safety net (R-021) -------------------------------------------------

def _build(capacity_col, capacity, demand_col, demand):
    fac, _ = structured.parse_facilities([{
        "Facility_ID": "DC_A", "Facility_Name": "A", "Type": "DC",
        capacity_col: capacity}])
    mkt, _ = structured.parse_markets([{"Zone_ID": "MKT_A", "Zone_Name": "A"}])
    dem, _ = structured.parse_demand_from_markets(
        [{"Zone_ID": "MKT_A", demand_col: demand}], {"MKT_A"}, {"P001"})
    return build_network(fac + mkt, [ProductRecord(id="P001", name="P")], dem, [])


def test_monthly_demand_against_daily_capacity_is_caught_as_an_error():
    """
    The exact trap: the workbook names the demand period but not the capacity
    period. Solving as-is reports a false INFEASIBLE on a healthy network.
    """
    _, issues = _build("Capacity_Units", "2000", "Daily_Demand_Units", "1500")
    errors = [i for i in issues if i.code == "R-021"]
    assert errors and errors[0].severity == Severity.ERROR
    assert "30x" in errors[0].message or "30" in errors[0].message


def test_consistent_periods_raise_nothing():
    _, issues = _build("Monthly_Capacity_Units", "60000",
                       "Daily_Demand_Units", "1500")
    assert not [i for i in issues if i.code == "R-021"]


def test_a_genuine_shortfall_is_a_warning_not_a_period_error():
    """A real 2x shortfall must not be misreported as a unit bug."""
    _, issues = _build("Monthly_Capacity_Units", "22500",
                       "Daily_Demand_Units", "1500")
    found = [i for i in issues if i.code == "R-021"]
    assert found and found[0].severity == Severity.WARNING


# --- std_dev uses its own column's period -----------------------------------

def test_monthly_std_dev_is_not_rescaled_by_a_daily_demand_column():
    """
    The workbook mixes periods on one sheet: Daily_Demand_Units alongside a
    Demand_Variability example of "200 units/month". Inheriting the quantity
    period would inflate safety stock ~5.5x.
    """
    rows = [{"Zone_ID": "MKT_A", "Daily_Demand_Units": "1500",
             "Demand_Variability": "200"}]
    records, _ = structured.parse_demand_from_markets(rows, {"MKT_A"}, {"P001"})
    assert records[0].quantity == 1500 * 30
    assert records[0].std_dev == 200


def test_an_explicitly_daily_std_dev_scales_by_sqrt():
    """Summing N independent daily draws scales the mean by N, the sd by sqrt(N)."""
    rows = [{"Zone_ID": "MKT_A", "Daily_Demand_Units": "1500",
             "Daily_Demand_Variability": "200"}]
    records, _ = structured.parse_demand_from_markets(rows, {"MKT_A"}, {"P001"})
    assert records[0].std_dev == pytest.approx(200 * (30 ** 0.5))


# --- lane capacity is per-period, not per-trip ------------------------------

def test_capacity_per_trip_without_frequency_leaves_the_lane_uncapacitated():
    """A wrong cap invents a constraint; no cap merely loses one."""
    lanes, result = structured.parse_lanes(
        [{"Origin_ID": "A", "Destination_ID": "B", "Unit_Cost": "12",
          "Capacity_Per_Trip": "500"}], {"A", "B"})
    assert lanes[0].lane_capacity is None
    assert any(i.code == "R-022" for i in result.issues)


def test_capacity_per_trip_times_frequency_gives_the_period_cap():
    lanes, _ = structured.parse_lanes(
        [{"Origin_ID": "A", "Destination_ID": "B", "Unit_Cost": "12",
          "Capacity_Per_Trip": "500", "Transit_Frequency": "30"}], {"A", "B"})
    assert lanes[0].lane_capacity == 15000


def test_an_internal_lane_capacity_column_is_taken_as_per_period():
    """Our own name already means per-period; it must not be multiplied."""
    lanes, result = structured.parse_lanes(
        [{"origin_id": "A", "destination_id": "B", "rate_per_unit": "12",
          "lane_capacity": "500"}], {"A", "B"})
    assert lanes[0].lane_capacity == 500
    assert not any(i.code == "R-022" for i in result.issues)


# --- distributor path -------------------------------------------------------

def test_distributor_daily_quantities_are_converted():
    """The structured path did this; the distributor path did not."""
    mapping = DistributorMapping(distributor_id="d1", mappings=[
        ColumnMapping(source_column="Daily Qty", target_field="quantity",
                      confidence=0.9)])
    rows, issues = normalise_periods_to_month(
        [{"quantity": 100}, {"quantity": 250}], mapping, "f.xlsx")
    assert [r["quantity"] for r in rows] == [3000, 7500]
    assert any(i.code == "R-020" for i in issues)


def test_distributor_column_with_no_period_is_left_alone():
    mapping = DistributorMapping(distributor_id="d2", mappings=[
        ColumnMapping(source_column="Qty", target_field="quantity",
                      confidence=0.9)])
    rows, issues = normalise_periods_to_month([{"quantity": 100}], mapping, "f.xlsx")
    assert rows[0]["quantity"] == 100
    assert not issues


def test_distributor_non_numeric_quantity_does_not_crash():
    mapping = DistributorMapping(distributor_id="d3", mappings=[
        ColumnMapping(source_column="Daily Qty", target_field="quantity",
                      confidence=0.9)])
    rows, _ = normalise_periods_to_month(
        [{"quantity": "n/a"}, {"quantity": None}, {"quantity": 10}], mapping, "f.xlsx")
    assert rows[0]["quantity"] == "n/a"
    assert rows[2]["quantity"] == 300
