"""
Client field-name alias tests.

The workbook NetGravity_Input_Data_Fields.xlsx is the contract we hand a
client. If data arriving in exactly the format we asked for fails to load,
the ingestion layer has failed at its one job. These tests guard that.
"""

from __future__ import annotations

import pytest

from netgravity.ingestion.adapters import structured
from netgravity.ingestion.field_aliases import (
    FACILITY_LOOKUP,
    LANE_LOOKUP,
    MARKET_LOOKUP,
    PRODUCT_LOOKUP,
    rename_row,
)
from netgravity.schemas.network import NodeRole


# --- the workbook's names must resolve --------------------------------------

def test_workbook_facility_names_resolve():
    row = {
        "Facility_ID": "DC_X", "Facility_Name": "X DC", "Type": "DC",
        "Capacity_Units": "5000", "Fixed_Annual_Cost": "100000",
        "Variable_Handling_Cost_Per_Unit": "4.2",
        "Mandatory_Open_Flag": "TRUE",
        "Observed_Throughput_Units": "4000",
        "Replenishment_Lead_Time": "3",
    }
    out = rename_row(row, FACILITY_LOOKUP)
    assert out["facility_id"] == "DC_X"
    assert out["role"] == "DC"
    assert out["capacity_units_per_period"] == "5000"
    assert out["fixed_cost_per_year"] == "100000"
    assert out["handling_cost_per_unit"] == "4.2"
    assert out["is_mandatory"] == "TRUE"
    assert out["observed_throughput"] == "4000"
    assert out["replenishment_lead_time_days"] == "3"


def test_workbook_zone_names_resolve():
    out = rename_row({"Zone_ID": "MKT_A", "Zone_Name": "A",
                      "Daily_Demand_Units": "4200",
                      "SLA_Requirement": "2"}, MARKET_LOOKUP)
    assert out["market_id"] == "MKT_A"
    assert out["market_name"] == "A"
    assert out["quantity"] == "4200"
    assert out["sla_days"] == "2"


def test_workbook_lane_names_resolve():
    out = rename_row({"Origin_ID": "A", "Destination_ID": "B",
                      "Unit_Cost": "12", "Distance_KM": "310",
                      "Current_Lane_Flag": "TRUE",
                      "Observed_Current_Volume": "8200"}, LANE_LOOKUP)
    assert out["rate_per_unit"] == "12"
    assert out["distance_km"] == "310"
    assert out["is_active_baseline"] == "TRUE"
    assert out["observed_current_volume"] == "8200"


def test_workbook_product_names_resolve():
    out = rename_row({"Product_ID": "P1", "Weight": "2.5",
                      "Unit_Value": "6500", "Holding_Rate": "0.25"},
                     PRODUCT_LOOKUP)
    assert out["product_id"] == "P1"
    assert out["weight_kg"] == "2.5"


# --- matching is forgiving --------------------------------------------------

def test_matching_ignores_case_and_separators():
    for spelling in ("Facility_ID", "facility id", "FACILITY-ID", "facilityid"):
        out = rename_row({spelling: "DC_X"}, FACILITY_LOOKUP)
        assert out.get("facility_id") == "DC_X", f"failed on {spelling!r}"


def test_internal_names_still_work():
    """Existing files must keep loading — this is additive, not a migration."""
    out = rename_row({"facility_id": "DC_X", "role": "DC",
                      "capacity_units_per_period": "5000"}, FACILITY_LOOKUP)
    assert out["facility_id"] == "DC_X"
    assert out["role"] == "DC"


def test_unknown_columns_are_preserved_not_dropped():
    out = rename_row({"Facility_ID": "X", "Some_Client_Column": "keep me"},
                     FACILITY_LOOKUP)
    assert out["Some_Client_Column"] == "keep me"


# --- end to end through the parsers ----------------------------------------

def test_parser_accepts_workbook_named_facility_rows():
    rows = [{"Facility_ID": "DC_X", "Facility_Name": "X DC", "Type": "DC",
             "Status": "EXISTING", "Latitude": "28.61", "Longitude": "77.21",
             "Capacity_Units": "10000", "Fixed_Annual_Cost": "1440000",
             "Variable_Handling_Cost_Per_Unit": "4.2"}]
    records, result = structured.parse_facilities(rows)
    assert result.rows_accepted == 1
    assert records[0].id == "DC_X"
    assert records[0].role == NodeRole.DC
    assert records[0].capacity_units_per_period == 10000
    assert records[0].handling_cost_per_unit == 4.2


def test_facility_parser_reads_opening_closure_capex_min_throughput():
    """
    These four columns were aliased in field_aliases.py but never actually
    read by parse_facilities — FacilityRecord has held real fields for all
    four since v1.1. Confirms the gap is closed, not just documented.
    """
    rows = [{"Facility_ID": "DC_X", "Facility_Name": "X DC", "Type": "DC",
             "Opening_Cost": "500000", "Closure_Cost": "120000",
             "CapEx": "2000000", "Min_Throughput_Per_Period": "1000",
             "Country": "Bangladesh"}]
    records, result = structured.parse_facilities(rows)
    assert result.rows_accepted == 1
    f = records[0]
    assert f.opening_cost == 500000
    assert f.closure_cost == 120000
    assert f.capex == 2000000
    assert f.min_throughput_per_period == 1000
    # Country column is now honored instead of being hardcoded to "India".
    assert f.country == "Bangladesh"


def test_facility_country_defaults_to_india_when_column_absent():
    rows = [{"Facility_ID": "DC_X", "Facility_Name": "X DC", "Type": "DC"}]
    records, _ = structured.parse_facilities(rows)
    assert records[0].country == "India"


def test_lane_parser_reads_emission_factor_override():
    """Aliased (Emission_Factor_Override) but never read — LaneRecord has had
    this field since v1.1."""
    rows = [{"Origin_ID": "A", "Destination_ID": "B", "Unit_Cost": "12",
             "Emission_Factor_Override": "0.09"}]
    records, result = structured.parse_lanes(rows, {"A", "B"})
    assert result.rows_accepted == 1
    assert records[0].emission_factor_override == 0.09


def test_demand_can_be_read_off_the_zones_sheet():
    """
    The workbook keeps Daily_Demand_Units on the Demand Zones sheet rather
    than in a separate demand table. Clients should not have to split it.
    """
    rows = [{"Zone_ID": "MKT_A", "Zone_Name": "A", "Latitude": "28.7",
             "Longitude": "77.1", "Daily_Demand_Units": "4200",
             "Demand_Variability": "420", "SLA_Requirement": "2"}]
    records, result = structured.parse_demand_from_markets(
        rows, {"MKT_A"}, {"P001"})
    assert result.rows_accepted == 1
    assert records[0].market_id == "MKT_A"
    assert records[0].product_id == "P001"
    # Numeric value (and its DAY->MONTH conversion) is covered by
    # test_daily_demand_units_is_normalized_to_month() below.


def test_daily_demand_units_is_normalized_to_month():
    """
    DemandRecord.quantity is a MONTHLY figure (OptimizationConfig.cost_period
    default). 'Daily_Demand_Units' is DAY-native, so it must be scaled by
    DAYS_PER_MONTH (30) on the way in — a raw daily count landing unconverted
    understated monthly facility cost vs. monthly demand by ~30x and biased
    the optimizer toward closing facilities that should stay open.
    std_dev is NOT rescaled here: it is read from its own column name, and
    the workbook's Demand_Variability example is already monthly. See
    test_period_consistency.py for the full period rules.
    """
    rows = [{"Zone_ID": "MKT_A", "Zone_Name": "A", "Latitude": "28.7",
             "Longitude": "77.1", "Daily_Demand_Units": "4200",
             "Demand_Variability": "420", "SLA_Requirement": "2"}]
    records, result = structured.parse_demand_from_markets(
        rows, {"MKT_A"}, {"P001"})
    assert result.rows_accepted == 1
    assert records[0].quantity == 4200.0 * 30
    assert records[0].std_dev == 420.0
    assert any(i.code == "R-020" for i in result.issues)


def test_already_monthly_quantity_column_is_not_rescaled():
    """A column with no period in its name (e.g. plain 'quantity') is assumed
    already MONTH-native, matching the engine's convention — no conversion,
    no R-020 note."""
    rows = [{"market_id": "MKT_A", "product_id": "P001", "quantity": "4200",
             "std_dev": "420"}]
    records, result = structured.parse_demand(rows, {"MKT_A"}, {"P001"})
    assert records[0].quantity == 4200.0
    assert records[0].std_dev == 420.0
    assert not any(i.code == "R-020" for i in result.issues)


def test_zone_demand_needs_a_product_when_catalogue_has_several():
    """Splitting a zone total across products would be a guess — so refuse."""
    rows = [{"Zone_ID": "MKT_A", "Daily_Demand_Units": "4200"}]
    _, result = structured.parse_demand_from_markets(
        rows, {"MKT_A"}, {"P001", "P002"})
    assert result.rows_rejected == 1
    assert any(i.code == "R-019" for i in result.issues)


def test_zones_sheet_without_demand_columns_is_a_no_op():
    rows = [{"Zone_ID": "MKT_A", "Zone_Name": "A"}]
    records, result = structured.parse_demand_from_markets(
        rows, {"MKT_A"}, {"P001"})
    assert records == []
    assert result.rows_read == 0
