"""
Normalised, multi-sheet uploads.
=================================

A real client workbook is *normalised*: demand lives in a monthly history
table, freight rates in a table keyed by lane and product, capacity in a
monthly series, and the markets/lanes sheets carry no demand or rate columns at
all. The extractor originally understood only the denormalised shape and filled
every gap with an invented constant — capacity 10,000 units, freight ₹10/unit,
lead time 1 day, handling ₹4/unit — so a workbook it did not recognise was
solved against uniform fictional economics and reported as the user's own
network.

These tests pin the behaviour that replaced it. The fixture mirrors the
structure of `Dump/NetGravity_Test_Data_Clean.xlsx` but is built in-process, so
the suite does not depend on a file outside the repository.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backend.api.network_extractor import (
    build_network_from_dataframes,
    classify_sheet,
)
from app.backend.services.errors import ValidationError
from app.backend.services.network_assembler import (
    _HORIZON_PERIODS,
    _MAX_MODELLED_DEMAND_ROWS,
    assemble_network_from_structure,
    choose_horizon,
    diagnose_servability,
)


@pytest.fixture
def normalised_tables():
    """A miniature of the client workbook: 8 sheets, fully normalised."""
    facilities = pd.DataFrame([
        ("F001", "Mumbai Plant", "PLANT", "Mumbai", "Maharashtra", 19.076, 72.8777, 42150, 3852000, "ACTIVE"),
        ("F002", "Chennai Plant", "PLANT", "Chennai", "Tamil Nadu", 13.0827, 80.2707, 38480, 3624500, "ACTIVE"),
        ("F004", "Delhi DC", "DC", "Delhi", "Delhi", 28.7041, 77.1025, 28430, 1452300, "ACTIVE"),
        ("F005", "Bangalore DC", "DC", "Bangalore", "Karnataka", 12.9716, 77.5946, 26180, 1381600, "ACTIVE"),
    ], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City", "State",
                "Latitude", "Longitude", "Capacity_Units", "Fixed_Cost", "Status"])

    markets = pd.DataFrame([
        ("M001", "Delhi Market", "Delhi", "Delhi", 28.7041, 77.1025, "North", 2),
        ("M003", "Bangalore Market", "Bangalore", "Karnataka", 12.9716, 77.5946, "South", 2),
    ], columns=["Market_ID", "Market_Name", "City", "State", "Latitude",
                "Longitude", "Region", "Service_SLA_Days"])

    lanes = pd.DataFrame([
        ("L001", "F001", "F004", "PLANT", "DC", 1434.4, 2.4, 11598, True),
        ("L002", "F002", "F005", "PLANT", "DC", 342.2, 0.8, 13689, True),
        ("L003", "F004", "M001", "DC", "MARKET", 20.0, 0.5, 9000, True),
        ("L004", "F005", "M003", "DC", "MARKET", 15.0, 0.5, 9000, True),
    ], columns=["Lane_ID", "Origin_ID", "Destination_ID", "Origin_Type",
                "Destination_Type", "Distance_Km", "Transit_Time_Days",
                "Capacity_Units", "Active"])

    products = pd.DataFrame([
        ("P001", "Instant Noodles", "FMCG - Food", 0.42, 78.5),
        ("P002", "Detergent Powder", "FMCG - Home Care", 1.05, 118.0),
    ], columns=["Product_ID", "Product_Name", "Product_Category",
                "Unit_Weight_Kg", "Unit_Cost"])

    history_rows = []
    for period_index, period in enumerate(["2026-06", "2026-07", "2026-08"]):
        for market, base in (("M001", 3000), ("M003", 2000)):
            for product, bump in (("P001", 0), ("P002", 500)):
                history_rows.append(
                    (period, market, product, base + bump + period_index * 10)
                )
    demand_history = pd.DataFrame(
        history_rows, columns=["Period", "Market_ID", "Product_ID", "Demand_Units"])

    capacity = pd.DataFrame([
        ("F001", "2026-08", 42561, 29301),
        ("F004", "2026-08", 28430, 19000),
    ], columns=["Facility_ID", "Period", "Available_Capacity_Units",
                "Used_Capacity_Units"])

    rates = pd.DataFrame([
        ("L001", "P001", 28.85, "INR"), ("L001", "P002", 38.72, "INR"),
        ("L002", "P001", 24.41, "INR"), ("L002", "P002", 32.48, "INR"),
        ("L003", "P001", 4.00, "INR"), ("L003", "P002", 6.00, "INR"),
        ("L004", "P001", 3.50, "INR"), ("L004", "P002", 5.50, "INR"),
    ], columns=["Lane_ID", "Product_ID", "Rate_Per_Unit", "Currency"])

    signals = pd.DataFrame([
        ("SIG001", "2026-07-14", "CUSTOMER_EXPANSION", "M001", "Retailer expansion", "HIGH", None),
    ], columns=["Signal_ID", "Signal_Date", "Signal_Type", "Market_ID",
                "Description", "Relevance", "Event_Probability"])

    return {
        "Facilities": facilities, "Markets": markets, "Lanes": lanes,
        "Products": products, "Demand_History": demand_history,
        "Capacity": capacity, "Transportation_Rates": rates,
        "External_Signals": signals,
    }


# ---------------------------------------------------------------- extraction

def test_each_sheet_is_classified_as_exactly_one_thing(normalised_tables):
    """
    The Capacity sheet shares `Facility_ID` with the Facilities sheet, and the
    Demand_History sheet shares `Market_ID` with Markets. Both used to match
    the facilities/markets branches as well as their own, which registered
    every facility twice — three plants also appeared as DCs.
    """
    roles = {name: classify_sheet(df) for name, df in normalised_tables.items()}
    assert roles == {
        "Facilities": "facilities",
        "Markets": "markets",
        "Lanes": "lanes",
        "Products": "products",
        "Demand_History": "demand_history",
        "Capacity": "capacity_history",
        "Transportation_Rates": "lane_rates",
        "External_Signals": "signals",
    }


def test_facilities_are_not_duplicated_across_roles(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    assert len(structure["plants"]) == 2
    assert len(structure["dcs"]) == 2
    plant_ids = {p["id"] for p in structure["plants"]}
    dc_ids = {d["id"] for d in structure["dcs"]}
    assert not (plant_ids & dc_ids)


def test_capacity_is_read_not_invented(normalised_tables):
    """`Capacity_Units` was not in the alias list, so every facility got 10,000."""
    structure = build_network_from_dataframes(normalised_tables)
    by_id = {f["id"]: f for f in structure["plants"] + structure["dcs"]}
    assert by_id["F001"]["capacity"] == 42150
    assert by_id["F004"]["capacity"] == 28430


def test_missing_capacity_is_absent_not_defaulted():
    tables = {"Facilities": pd.DataFrame([
        ("F001", "Nameless DC", "DC", "Delhi"),
    ], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City"])}
    structure = build_network_from_dataframes(tables)
    assert structure["dcs"][0]["capacity"] is None
    assert any("no capacity" in n for n in structure["notes"])


def test_coordinates_come_from_the_file(normalised_tables):
    """Markets were positioned by hashing their id, ignoring real lat/long."""
    structure = build_network_from_dataframes(normalised_tables)
    delhi = next(m for m in structure["markets"] if m["id"] == "M001")
    assert delhi["coordsExact"] is True
    # Fanning may nudge a co-located node for display; the source is preserved.
    assert delhi.get("latSource", delhi["lat"]) == pytest.approx(28.7041, abs=0.01)


def test_market_names_are_read(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    names = {m["id"]: m["name"] for m in structure["markets"]}
    assert names["M001"] == "Delhi Market"


def test_freight_rates_join_from_the_rates_table(normalised_tables):
    """Lane rate used to default to a flat ₹10/unit for every lane."""
    structure = build_network_from_dataframes(normalised_tables)
    by_lane = {l["laneId"]: l for l in structure["lanes"]}
    assert by_lane["L001"]["ratesByProduct"] == {"P001": 28.85, "P002": 38.72}
    assert by_lane["L001"]["cost"] != 10.0


def test_transit_times_are_read_not_defaulted(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    leads = {l["laneId"]: l["leadTime"] for l in structure["lanes"]}
    assert leads["L001"] == pytest.approx(2.4)
    assert leads["L003"] == pytest.approx(0.5)


def test_demand_comes_from_the_latest_period_of_history(normalised_tables):
    """The markets sheet carries no demand column at all."""
    structure = build_network_from_dataframes(normalised_tables)
    demand = {m["id"]: m["demand"] for m in structure["markets"]}
    # 2026-08 is the latest period: M001 = 3020 (P001) + 3520 (P002).
    assert demand["M001"] == pytest.approx(6540)
    assert demand["M003"] == pytest.approx(4540)


def test_history_and_signals_are_carried_through(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    assert len(structure["demandHistory"]) == 12
    assert len(structure["capacityHistory"]) == 2
    assert len(structure["signals"]) == 1
    assert structure["signals"][0]["marketId"] == "M001"


# ---------------------------------------------------------------- assembly

def test_assembly_keeps_the_product_dimension(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    network, assumptions, _ = assemble_network_from_structure(
        structure, network_id="test_net")
    assert {p.id for p in network.products} == {"P001", "P002"}
    # One demand record per market-product pair PER PERIOD, not one aggregate
    # per market. The pair count is what this test is about; the period count is
    # asserted separately in test_the_observed_horizon_is_modelled.
    pairs = {(d.market_id, d.product_id) for d in network.demands}
    assert pairs == {("M001", "P001"), ("M001", "P002"),
                     ("M003", "P001"), ("M003", "P002")}
    assert not any("single aggregate product" in a for a in assumptions)


def test_the_observed_horizon_is_modelled(normalised_tables):
    """
    Every period the upload states is carried into the model, not just the
    latest one.

    The fixture holds three months for two markets and two products. Collapsing
    that to the newest month — which is what happened until the horizon was
    wired through — discards two thirds of the client's own data and makes the
    multi-period MILP unreachable from any upload, so seasonality cannot be
    asked about at all.
    """
    structure = build_network_from_dataframes(normalised_tables)
    network, assumptions, _ = assemble_network_from_structure(
        structure, network_id="test_net")

    assert {d.period for d in network.demands} == {1, 2, 3}
    assert network.period_labels == {"1": "2026-06", "2": "2026-07", "3": "2026-08"}
    # 2 markets x 2 products x 3 periods.
    assert len(network.demands) == 12

    # The latest period's figures survive unchanged at the newest index, so the
    # horizon is an addition to what was reported before, not a restatement.
    latest = {(d.market_id, d.product_id): d.quantity
              for d in network.demands if d.period == 3}
    assert latest[("M001", "P001")] == pytest.approx(3020)
    assert latest[("M001", "P002")] == pytest.approx(3520)

    assert network.config.multi_period_policy == "FULL_HORIZON"
    assert any("modelled over 3 periods" in a for a in assumptions)


def test_a_horizon_does_not_manufacture_a_capacity_shortfall(normalised_tables):
    """
    Demand and capacity are both per period, so a horizon must not be added up
    and compared with one period's capacity.

    Doing so reports a shortfall equal in size to the length of the horizon —
    and for a twelve-month table that lands squarely on the 12x ratio the
    consistency check treats as a units error, so the upload is told its
    capacity column is on the wrong period when nothing is wrong with it.
    """
    structure = build_network_from_dataframes(normalised_tables)
    _, _, issues = assemble_network_from_structure(structure, network_id="test_net")
    assert not any("looks like" in i and "monthly capacity" in i for i in issues)
    assert not any("exceeds total capacity" in i for i in issues)


def test_per_product_rates_are_blended_by_demand_and_declared(normalised_tables):
    """
    The MILP keys arcs on (origin, destination, mode, product) and takes the
    rate from the lane, skipping duplicate keys — so emitting one lane per
    product would silently apply the first product's rate to all of them. The
    rates are collapsed to a demand-weighted average, and that is stated.
    """
    structure = build_network_from_dataframes(normalised_tables)
    network, assumptions, _ = assemble_network_from_structure(
        structure, network_id="test_net")

    lane = next(l for l in network.lanes
                if l.origin_id == "F001" and l.destination_id == "F004")
    assert 28.85 < lane.rate_per_unit < 38.72
    assert any("demand-weighted average" in a for a in assumptions)

    # One lane per origin-destination pair, never one per product.
    pairs = [(l.origin_id, l.destination_id) for l in network.lanes]
    assert len(pairs) == len(set(pairs))


def test_lane_capacity_is_carried_into_the_network(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    network, _, _ = assemble_network_from_structure(structure, network_id="test_net")
    lane = next(l for l in network.lanes
                if l.origin_id == "F004" and l.destination_id == "M001")
    assert lane.lane_capacity == 9000


def test_fixed_cost_annualisation_is_stated(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    network, assumptions, _ = assemble_network_from_structure(
        structure, network_id="test_net")
    dc = next(f for f in network.facilities if f.id == "F004")
    assert dc.fixed_cost_per_year == pytest.approx(1452300 * 12)
    assert any("annualised" in a and "F004" in a for a in assumptions)


def test_upload_without_demand_is_refused_not_defaulted():
    """A network with no demand must not be solved against invented quantities."""
    tables = {
        "Facilities": pd.DataFrame([
            ("F001", "A DC", "DC", "Delhi", 28.7, 77.1, 1000),
        ], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City",
                    "Latitude", "Longitude", "Capacity_Units"]),
        "Markets": pd.DataFrame([
            ("M001", "A Market", "Delhi", 28.7, 77.1),
        ], columns=["Market_ID", "Market_Name", "City", "Latitude", "Longitude"]),
    }
    structure = build_network_from_dataframes(tables)
    with pytest.raises(ValidationError, match="demand"):
        assemble_network_from_structure(structure, network_id="test_net")


# ---------------------------------------------------------------- diagnosis

def test_servability_names_the_market_and_the_binding_constraint(normalised_tables):
    """
    "INFEASIBLE" alone is a dead end. When a market's SLA-eligible lanes cannot
    carry its demand, the diagnosis must say which market, how short, and why.
    """
    tables = dict(normalised_tables)
    # Squeeze the only lane into M001 well below its demand.
    lanes = tables["Lanes"].copy()
    lanes.loc[lanes["Lane_ID"] == "L003", "Capacity_Units"] = 100
    tables["Lanes"] = lanes

    structure = build_network_from_dataframes(tables)
    network, _, issues = assemble_network_from_structure(structure, network_id="t")

    findings = diagnose_servability(network)
    m001 = next(f for f in findings if f["market_id"] == "M001")
    assert m001["capacity"] == 100
    assert m001["shortfall"] == pytest.approx(m001["demand"] - 100)
    assert "M001" in m001["reason"] and "short" in m001["reason"]
    # The same reason reaches the caller through the assembler's issue list.
    assert any("M001" in i for i in issues)


def test_servable_network_reports_no_finding(normalised_tables):
    structure = build_network_from_dataframes(normalised_tables)
    network, _, issues = assemble_network_from_structure(structure, network_id="t")
    assert diagnose_servability(network) == []
    assert not any("short" in i for i in issues)


def test_market_with_no_sla_eligible_lane_is_reported(normalised_tables):
    tables = dict(normalised_tables)
    lanes = tables["Lanes"].copy()
    # 9 days transit against a 2-day SLA: nothing can serve M001 in time.
    lanes.loc[lanes["Lane_ID"] == "L003", "Transit_Time_Days"] = 9.0
    tables["Lanes"] = lanes

    structure = build_network_from_dataframes(tables)
    network, _, _ = assemble_network_from_structure(structure, network_id="t")
    findings = diagnose_servability(network)
    m001 = next(f for f in findings if f["market_id"] == "M001")
    assert m001["eligible_lanes"] == 0
    assert "service level" in m001["reason"]


# ---------------------------------------------------------------- history

def test_demand_history_becomes_forecastable_series(normalised_tables):
    from app.backend.services.demand_history_store import build_series_from_structure

    structure = build_network_from_dataframes(normalised_tables)
    series, notes = build_series_from_structure(structure)
    assert len(series) == 4                       # 2 markets x 2 products
    assert all(len(s.history) == 3 for s in series)
    # Period index orders the series; the source label is preserved.
    first = sorted(series, key=lambda s: s.key)[0]
    assert [p.period for p in first.history] == [0, 1, 2]
    assert first.history[0].timestamp == "2026-06"
    assert notes


def test_single_observation_pair_is_dropped_with_a_reason():
    from app.backend.services.demand_history_store import build_series_from_structure

    structure = {"demandHistory": [
        {"period": "2026-08", "marketId": "M001", "productId": "P001", "quantity": 10.0},
    ]}
    series, notes = build_series_from_structure(structure)
    assert series == []
    assert any("only one observation" in n for n in notes)


# ---------------------------------------------------------------------------
# Phase 10.2 — data points that were parsed and then dropped, and the KPI
# blackout that followed a proved-infeasible network.
# ---------------------------------------------------------------------------


def test_plant_fixed_cost_is_read_not_only_dc_fixed_cost(normalised_tables):
    """`Fixed_Cost` was read for DCs only, silently zeroing every plant."""
    structure = build_network_from_dataframes(normalised_tables)
    plants = {p["id"]: p for p in structure["plants"]}
    assert plants["F001"]["fixedCost"] == 3852000
    assert plants["F002"]["fixedCost"] == 3624500

    network, _, _ = assemble_network_from_structure(structure, network_id="n")
    by_id = {f.id: f for f in network.facilities}
    assert by_id["F001"].fixed_cost_per_year == pytest.approx(3852000 * 12)


def test_product_weight_and_value_reach_the_product_record(normalised_tables):
    """Weight drives carbon (tonne-km); unit value drives holding cost."""
    structure = build_network_from_dataframes(normalised_tables)
    network, _, _ = assemble_network_from_structure(structure, network_id="n")
    by_id = {p.id: p for p in network.products}
    assert by_id["P001"].weight_kg == pytest.approx(0.42)
    assert by_id["P002"].weight_kg == pytest.approx(1.05)
    assert by_id["P001"].unit_value == pytest.approx(78.5)
    assert by_id["P002"].unit_value == pytest.approx(118.0)


def test_active_facilities_are_existing_not_candidates(normalised_tables):
    """A live site offered to the solver as a candidate invites a greenfield answer."""
    from netgravity.schemas.network import FacilityStatus, NodeRole

    structure = build_network_from_dataframes(normalised_tables)
    network, _, _ = assemble_network_from_structure(structure, network_id="n")
    for facility in network.facilities:
        if facility.role is NodeRole.MARKET:
            continue
        assert facility.status is FacilityStatus.EXISTING, facility.id


def test_demand_variability_comes_from_the_uploaded_history(normalised_tables):
    """`std_dev` defaults to 0.0, which means no safety stock at all."""
    structure = build_network_from_dataframes(normalised_tables)
    network, assumptions, _ = assemble_network_from_structure(structure, network_id="n")
    assert any(d.std_dev > 0 for d in network.demands)
    assert any("standard deviation" in a for a in assumptions)


def test_baseline_evaluates_the_network_as_uploaded(normalised_tables):
    """The baseline is the client's network, not a redesign of it."""
    from netgravity.schemas.network import OptimizationMode

    structure = build_network_from_dataframes(normalised_tables)
    network, _, _ = assemble_network_from_structure(structure, network_id="n")
    assert network.config.optimization_mode is OptimizationMode.ACTUAL_AS_IS_EVALUATION
    assert network.config.relax_to_shortage_when_infeasible is True


def test_market_priority_is_not_invented(normalised_tables):
    """Priority was assigned by a hardcoded 2,500-unit threshold."""
    structure = build_network_from_dataframes(normalised_tables)
    assert all(m["priority"] is None for m in structure["markets"])


def test_capacity_history_yields_the_recorded_utilisation(normalised_tables):
    from app.backend.services.demand_history_store import CapacityHistoryStore

    structure = build_network_from_dataframes(normalised_tables)
    store = CapacityHistoryStore()
    store.put("net", structure["capacityHistory"])
    latest = store.latest_utilisation("net")
    assert latest, "capacity history produced no recorded utilisation"
    for facility_id, row in latest.items():
        assert row["period"] == "2026-08", facility_id
        if row["available"] and row["used"] is not None:
            assert row["utilisationPct"] == pytest.approx(
                row["used"] / row["available"] * 100.0, abs=0.01
            )


def test_missing_capacity_row_yields_no_percentage_not_zero():
    from app.backend.services.demand_history_store import CapacityHistoryStore

    store = CapacityHistoryStore()
    store.put("net", [{"facilityId": "F001", "period": "2026-08",
                       "available": None, "used": None}])
    assert store.latest_utilisation("net")["F001"]["utilisationPct"] is None


# ------------------------------------------------------- horizon selection

class TestTheHorizonIsBounded:
    """
    How many periods get modelled must scale with the size of the upload, not
    with how much history it happens to contain.

    Solve cost grows linearly in the number of periods. On the sample network —
    fourteen market-product pairs — thirty-six periods is 3,032 variables and a
    tenth of a second. The same horizon over a client with four thousand pairs
    is a different proposition entirely, and a planning tool that stops working
    on a large upload has failed at the size where it matters most.
    """

    def months(self, n):
        return [f"2026-{i:02d}" if i <= 12 else f"2027-{i - 12:02d}"
                for i in range(1, n + 1)]

    def test_a_short_history_is_modelled_whole(self):
        modelled, notes = choose_horizon(self.months(4), rows_per_period=10)
        assert modelled == self.months(4)
        assert notes == []

    def test_a_long_history_keeps_the_most_recent_seasonal_cycle(self):
        observed = self.months(30)
        modelled, notes = choose_horizon(observed, rows_per_period=10)
        assert len(modelled) == _HORIZON_PERIODS
        # The most RECENT periods, ending at the present. A network is designed
        # forward from where it is, not from where it was three years ago.
        assert modelled == observed[-_HORIZON_PERIODS:]
        assert notes and "most recent 12" in notes[0]

    def test_a_wide_upload_shortens_the_horizon_rather_than_blowing_up(self):
        """
        The bound is on the demand table the horizon PRODUCES, so a network
        that is wide is limited by the same rule as one that is long.
        """
        wide = _MAX_MODELLED_DEMAND_ROWS // 4      # only 4 periods affordable
        modelled, notes = choose_horizon(self.months(24), rows_per_period=wide)
        assert len(modelled) == 4
        assert len(modelled) * wide <= _MAX_MODELLED_DEMAND_ROWS

    def test_shortening_the_horizon_is_never_silent(self):
        """
        A client whose answer covers fewer months than their file does has to
        be told. Absorbing that quietly is how a partial answer gets read as a
        complete one.
        """
        wide = _MAX_MODELLED_DEMAND_ROWS // 4
        _, notes = choose_horizon(self.months(24), rows_per_period=wide)
        assert notes
        assert "budget" in notes[0]
        assert "Seasonality outside that window is not modelled" in notes[0]

    def test_a_single_period_upload_is_left_exactly_as_it_is(self):
        assert choose_horizon(["2026-01"], rows_per_period=10) == (["2026-01"], [])
        assert choose_horizon([], rows_per_period=0) == ([], [])

    def test_the_budget_never_shortens_below_one_period(self):
        """An upload wider than the whole budget still has to produce a model."""
        modelled, _ = choose_horizon(
            self.months(12), rows_per_period=_MAX_MODELLED_DEMAND_ROWS * 5)
        assert len(modelled) == 1
