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
    _stated_country,
    build_network_from_dataframes,
    classify_sheet,
    infer_geography,
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


# ------------------------------------------------- units, modes and currency
#
# A workbook from outside India exercises every assumption the pipeline used to
# bake in: distance in miles, a real modal mix, dollars, and a warehouse cost
# table. On the supplied US dataset each of these silently produced a wrong
# number rather than an error — 51 corridors at 0 km, zero carbon, all-ROAD
# telemetry, handling cost of zero, and rupee symbols against USD figures.

@pytest.fixture
def us_shaped_tables():
    """A miniature US workbook: miles, mixed modes, USD, warehouse costs."""
    facilities = pd.DataFrame([
        ("F001", "Los Angeles Plant", "PLANT", "Los Angeles", "California",
         33.9425, -118.4081, 55000, 4250000, "ACTIVE"),
        ("F004", "Dallas DC", "DC", "Dallas", "Texas",
         32.7767, -96.7970, 32000, 1620000, "ACTIVE"),
    ], columns=["Facility_ID", "Facility_Name", "Facility_Type", "City", "State",
                "Latitude", "Longitude", "Capacity_Units", "Fixed_Cost", "Status"])

    markets = pd.DataFrame([
        ("M001", "New York Metro", "New York", "New York", 40.7128, -74.0060, "Northeast", 2),
    ], columns=["Market_ID", "Market_Name", "City", "State", "Latitude",
                "Longitude", "Region", "Service_SLA_Days"])

    lanes = pd.DataFrame([
        ("L001", "F001", "F004", "PLANT", "DC", 1247.5, 2.4, 13238, True, "INTERMODAL"),
        ("L002", "F004", "M001", "DC", "MARKET", 100.0, 0.9, 10253, True, "TL"),
        ("L003", "F001", "M001", "PLANT", "MARKET", 2500.0, 5.0, 5000, False, "RAIL"),
    ], columns=["Lane_ID", "Origin_ID", "Destination_ID", "Origin_Type",
                "Destination_Type", "Distance_Miles", "Transit_Time_Days",
                "Capacity_Units", "Active", "Transport_Mode"])

    products = pd.DataFrame([
        ("P001", "Sparkling Water", "CPG - Beverage", 3.63, 8.99),
    ], columns=["Product_ID", "Product_Name", "Product_Category",
                "Unit_Weight_Kg", "Unit_Cost"])

    demand_history = pd.DataFrame([
        ("2026-06", "M001", "P001", 2100),
        ("2026-07", "M001", "P001", 2200),
        ("2026-08", "M001", "P001", 2300),
    ], columns=["Period", "Market_ID", "Product_ID", "Demand_Units"])

    rates = pd.DataFrame([
        ("L001", "P001", 1.62, "USD"),
        ("L002", "P001", 1.50, "USD"),
        # Priced against a lane no sheet defines — the orphan-reference case.
        ("L099", "P001", 2.10, "USD"),
    ], columns=["Lane_ID", "Product_ID", "Rate_Per_Unit", "Currency"])

    # F004's RENT is restated at a later date AND duplicated, both of which the
    # real workbook does.
    warehouse = pd.DataFrame([
        ("F001", "RENT", 89348, 1.62, "2023-09-01"),
        ("F001", "LABOR", 283578, 5.16, "2023-09-01"),
        ("F004", "RENT", 51927, 1.85, "2023-09-01"),
        ("F004", "RENT", 78400, 2.80, "2025-07-01"),
        ("F004", "RENT", 78400, 2.80, "2025-07-01"),
        ("F004", "LABOR", 153451, 5.48, "2023-09-01"),
    ], columns=["Facility_ID", "Cost_Type", "Monthly_Cost_USD",
                "Cost_Per_Unit_Handled", "Effective_Date"])

    return {
        "Facilities": facilities, "Markets": markets, "Lanes": lanes,
        "Products": products, "Demand_History": demand_history,
        "Transportation_Rates": rates, "Warehouse_Costs": warehouse,
    }


class TestDistanceStatedInMiles:
    def test_miles_are_converted_rather_than_ignored(self, us_shaped_tables):
        """
        `Distance_Miles` matched no alias, so every lane came through with no
        distance at all: 0 km corridors, 0 kg carbon, and a distance-weighted
        average of zero on a network spanning a continent.
        """
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {l["laneId"]: l for l in st["lanes"]}
        # Rounded to metres by the extractor, which is finer than any freight
        # rate or emission factor this engine applies.
        assert by_id["L001"]["distance"] == pytest.approx(1247.5 * 1.609344, abs=1e-3)
        assert by_id["L002"]["distance"] == pytest.approx(100.0 * 1.609344, abs=1e-3)

    def test_the_conversion_is_declared_not_silent(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        assert any("miles" in n and "1.609344" in n for n in st["notes"])
        assert all(l["distanceSource"] == "miles" for l in st["lanes"])

    def test_a_kilometre_column_still_wins(self, us_shaped_tables):
        """A workbook carrying both is read in its canonical unit."""
        lanes = us_shaped_tables["Lanes"].copy()
        lanes["Distance_Km"] = [10.0, 20.0, 30.0]
        us_shaped_tables["Lanes"] = lanes
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {l["laneId"]: l for l in st["lanes"]}
        assert by_id["L001"]["distance"] == 10.0
        assert by_id["L001"]["distanceSource"] == "km"

    def test_distance_survives_into_the_canonical_network(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        network, assumptions, _ = assemble_network_from_structure(st)
        assert all(l.distance_km > 0 for l in network.lanes)
        assert any("miles" in a for a in assumptions)


class TestTransportModeIsTheClientsNotOurs:
    def test_the_uploaded_mode_is_read(self, us_shaped_tables):
        """A literal "ROAD" was written onto every lane the extractor built."""
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {l["laneId"]: l for l in st["lanes"]}
        assert by_id["L001"]["mode"] == "INTERMODAL"
        # TL is a road service, and normalises onto the mode the engine models.
        assert by_id["L002"]["mode"] == "ROAD"

    def test_mode_reaches_the_lane_record(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        network, assumptions, _ = assemble_network_from_structure(st)
        modes = {l.mode.value for l in network.lanes}
        assert "INTERMODAL" in modes, (
            "mode decides the emission factor; defaulting it to ROAD misprices "
            "carbon on every intermodal corridor"
        )
        assert any("Transport mode taken from the upload" in a for a in assumptions)

    def test_a_missing_mode_column_is_stated_not_assumed(self, normalised_tables):
        st = build_network_from_dataframes(normalised_tables)
        assert all(l["mode"] is None for l in st["lanes"])
        assert any("no transport-mode column" in n.lower() for n in st["notes"])

    def test_an_unmodelled_mode_is_named_rather_than_relabelled(self, us_shaped_tables):
        lanes = us_shaped_tables["Lanes"].copy()
        lanes.loc[lanes["Lane_ID"] == "L001", "Transport_Mode"] = "PIPELINE"
        us_shaped_tables["Lanes"] = lanes
        st = build_network_from_dataframes(us_shaped_tables)
        assert {l["laneId"]: l["mode"] for l in st["lanes"]}["L001"] == "PIPELINE"
        _, assumptions, _ = assemble_network_from_structure(st)
        assert any("PIPELINE" in a for a in assumptions), (
            "a mode this engine cannot cost must be named, not silently "
            "substituted with ROAD"
        )


class TestCurrencyIsReadFromTheUpload:
    def test_the_rates_table_states_the_currency(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        assert st["currency"] == "USD"

    def test_it_reaches_the_canonical_network(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        network, _, _ = assemble_network_from_structure(st)
        assert network.currency == "USD"

    def test_assumption_text_uses_the_clients_currency(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        _, assumptions, _ = assemble_network_from_structure(st)
        fixed = [a for a in assumptions if "fixed cost read as" in a]
        assert fixed
        assert all("$" in a for a in fixed)
        assert not any("₹" in a for a in assumptions)

    def test_an_upload_naming_no_currency_gets_none_not_rupees(self, us_shaped_tables):
        rates = us_shaped_tables["Transportation_Rates"].drop(columns=["Currency"])
        us_shaped_tables["Transportation_Rates"] = rates
        # Drop the USD-suffixed header too, so nothing states a currency.
        wh = us_shaped_tables["Warehouse_Costs"].rename(
            columns={"Monthly_Cost_USD": "Monthly_Cost"})
        us_shaped_tables["Warehouse_Costs"] = wh
        st = build_network_from_dataframes(us_shaped_tables)
        assert st["currency"] is None, (
            "no currency must stay distinguishable from rupees; an amount with "
            "an unknown unit is not an amount in INR"
        )
        assert any("no column in this upload states a currency" in n.lower()
                   for n in st["notes"])

    def test_a_column_header_can_name_the_currency(self, us_shaped_tables):
        """`Monthly_Cost_USD` says what it is; nothing else in the file does."""
        rates = us_shaped_tables["Transportation_Rates"].drop(columns=["Currency"])
        us_shaped_tables["Transportation_Rates"] = rates
        st = build_network_from_dataframes(us_shaped_tables)
        assert st["currency"] == "USD"
        assert st["currencyBasis"] == "named in a money column header"


class TestWarehouseCostsBecomeHandlingCost:
    def test_handling_cost_is_built_from_the_cost_lines(self, us_shaped_tables):
        """
        The sheet was classified "unknown" and dropped, so `handling_cost` was
        zero network-wide and the optimiser moved units between sites as if
        handling them were free.
        """
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {n["id"]: n for n in st["plants"] + st["dcs"]}
        assert by_id["F001"]["handlingCost"] == pytest.approx(1.62 + 5.16)

    def test_a_restated_cost_line_supersedes_the_earlier_one(self, us_shaped_tables):
        """F004 rent is restated at a later effective date. It is a rent
        review, not a second rent."""
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {n["id"]: n for n in st["plants"] + st["dcs"]}
        assert by_id["F004"]["handlingCost"] == pytest.approx(2.80 + 5.48)

    def test_duplicate_rows_are_one_fact_not_several(self, us_shaped_tables):
        """F004 later rent row appears twice; summing both doubles it."""
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {n["id"]: n for n in st["plants"] + st["dcs"]}
        assert by_id["F004"]["handlingCost"] < 2.80 + 2.80 + 5.48

    def test_it_reaches_the_facility_record(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        network, _, _ = assemble_network_from_structure(st)
        handling = {f.id: f.handling_cost_per_unit for f in network.facilities}
        assert handling["F001"] == pytest.approx(6.78)
        assert handling["F004"] == pytest.approx(8.28)

    def test_a_facilities_sheet_figure_is_not_overwritten(self, us_shaped_tables):
        """The client own consolidated figure wins over a derived sum."""
        facilities = us_shaped_tables["Facilities"].copy()
        facilities["Handling_Cost_Per_Unit"] = [4.0, 4.0]
        us_shaped_tables["Facilities"] = facilities
        st = build_network_from_dataframes(us_shaped_tables)
        by_id = {n["id"]: n for n in st["plants"] + st["dcs"]}
        assert by_id["F001"]["handlingCost"] == 4.0


class TestCrossSheetReferentialIntegrity:
    def test_an_orphan_reference_is_reported(self, us_shaped_tables):
        """
        Row-level validity cannot see this: the file is 100% complete and the
        L099 rates are still dropped by the join, silently.
        """
        st = build_network_from_dataframes(us_shaped_tables)
        orphans = [p for p in st["integrity"] if p["type"] == "Orphan reference"]
        assert orphans
        assert orphans[0]["missingIds"] == ["L099"]
        assert orphans[0]["count"] == 1

    def test_it_is_also_surfaced_as_a_note(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        assert any("L099" in n for n in st["notes"])

    def test_an_inactive_lane_is_defined_not_dangling(self, us_shaped_tables):
        """
        L003 is switched off, so the model does not carry it — but the sheet
        DEFINES it. Reporting its rates as broken references would send a user
        hunting for a row that is exactly where it belongs.
        """
        st = build_network_from_dataframes(us_shaped_tables)
        assert "L003" not in {l["laneId"] for l in st["lanes"]}
        missing = {i for p in st["integrity"] for i in p.get("missingIds", [])}
        assert "L003" not in missing

    def test_a_clean_workbook_reports_nothing(self, normalised_tables):
        st = build_network_from_dataframes(normalised_tables)
        assert st["integrity"] == []


class TestGeographyIsInferredFromTheUpload:
    """
    SUPERSEDED NAME: this was `TestGeographyIsInferredFromCoordinates`, and
    coordinates were the only evidence read. They cannot answer this question
    in North America. The box that holds Alaska and Hawaii holds the whole of
    Canada, so a Canadian network reported "United States" with a confidence
    of 0.857 — and tightening the box does not help, because Toronto (43.65N)
    is south of Seattle, Minneapolis and half of Maine. No horizontal line
    divides the two countries.

    The upload states the answer: a facilities sheet carries a State/Province
    column, and "Ontario" is not ambiguous. That is read first; the box is the
    fallback for everywhere the coordinates are decisive on their own.
    """

    def test_us_coordinates_are_not_india(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        assert st["geography"]["region"] == "United States"
        assert st["geography"]["confidence"] == 1.0

    def test_india_coordinates_are_india(self, normalised_tables):
        st = build_network_from_dataframes(normalised_tables)
        assert st["geography"]["region"] == "India"

    def test_the_basis_is_stated_so_a_label_is_not_a_bare_assertion(self, us_shaped_tables):
        """
        SUPERSEDED ASSERTION: this required the word "coordinate", which was
        true when coordinates were the only evidence. What it protects — that
        the label names what it was derived from, rather than asserting itself
        — is unchanged, and now has two forms to cover.
        """
        st = build_network_from_dataframes(us_shaped_tables)
        basis = st["geography"]["basis"]
        assert "of" in basis and basis.rstrip().endswith("."), basis
        assert ("state or province" in basis) or ("coordinate" in basis), basis

    # ---- the pair the boxes cannot separate --------------------------
    def test_a_canadian_network_is_canada_not_the_united_states(self):
        """
        Measured before this existed, on Dump/NetGravity_Canada_Test_Data.xlsx:
        region "United States", confidence 0.857, basis "6 of 7 node
        coordinate(s) fall inside United States." Every screen that names the
        geography said so, including the sidebar's Region field.
        """
        nodes = [
            {"lat": 43.65, "lng": -79.38, "state": "Ontario"},
            {"lat": 45.50, "lng": -73.57, "state": "Quebec"},
            {"lat": 49.28, "lng": -123.12, "state": "British Columbia"},
            {"lat": 51.05, "lng": -114.07, "state": "Alberta"},
        ]
        geo = infer_geography(nodes)
        assert geo["region"] == "Canada", geo
        assert "Canada" in geo["basis"], geo

    def test_postal_abbreviations_count_as_naming_the_province(self):
        geo = infer_geography([{"lat": 43.65, "lng": -79.38, "state": "ON"},
                               {"lat": 45.50, "lng": -73.57, "state": "QC"}])
        assert geo["region"] == "Canada", geo

    def test_a_us_network_is_not_dragged_into_canada_by_its_northern_sites(self):
        """
        The mirror of the case above, and the reason a "Canada" rectangle was
        not the fix: every one of these sits inside any box wide enough to
        hold Canada.
        """
        geo = infer_geography([
            {"lat": 47.61, "lng": -122.33, "state": "Washington"},
            {"lat": 44.98, "lng": -93.27, "state": "Minnesota"},
            {"lat": 42.36, "lng": -71.06, "state": "Massachusetts"},
        ])
        assert geo["region"] == "United States", geo

    def test_a_network_spanning_the_border_claims_neither_country(self):
        """Half in each is not "mostly" either; it falls back to the box."""
        nodes = [{"lat": 43.65, "lng": -79.38, "state": "Ontario"},
                 {"lat": 45.50, "lng": -73.57, "state": "Quebec"},
                 {"lat": 40.71, "lng": -74.00, "state": "New York"},
                 {"lat": 41.88, "lng": -87.63, "state": "Illinois"}]
        stated, agree, of_stated = _stated_country(nodes)
        assert stated is None, (stated, agree, of_stated)
        assert "coordinate" in infer_geography(nodes)["basis"]

    def test_a_workbook_that_names_no_province_still_gets_an_answer(self):
        geo = infer_geography([{"lat": 28.61, "lng": 77.20},
                               {"lat": 19.07, "lng": 72.87}])
        assert geo["region"] == "India"
        assert "coordinate" in geo["basis"]

    def test_one_mislabelled_row_does_not_relabel_a_network(self):
        nodes = [{"lat": 43.65, "lng": -79.38, "state": "Ontario"},
                 {"lat": 45.50, "lng": -73.57, "state": "Quebec"},
                 {"lat": 49.28, "lng": -123.12, "state": "British Columbia"},
                 {"lat": 51.05, "lng": -114.07, "state": "Alberta"},
                 {"lat": 53.55, "lng": -113.49, "state": "Texas"}]
        assert infer_geography(nodes)["region"] == "Canada"

    def test_a_reupload_after_the_fix_is_not_served_the_old_answer(self):
        """
        `SnapshotManager.register` derives the snapshot id from the network's
        data version and returns the EXISTING snapshot when that id is already
        registered. Geography was not in the fingerprint, so re-uploading the
        same Canadian workbook was handed the snapshot the earlier upload had
        registered — region and all. The label could not be corrected by
        re-uploading, which is the only remedy a user has.
        """
        from netgravity.tests.fixtures.case16_synthetic import build_case16_network
        net = build_case16_network()
        net.geography = {"region": "Canada"}
        canada = net.compute_data_version()
        net.geography = {"region": "United States"}
        assert net.compute_data_version() != canada

    def test_bounds_are_the_extent_a_map_must_fit(self, us_shaped_tables):
        b = build_network_from_dataframes(us_shaped_tables)["geography"]["bounds"]
        assert b["lngMin"] < -100 and b["lngMax"] > -80

    def test_geography_reaches_the_canonical_network(self, us_shaped_tables):
        st = build_network_from_dataframes(us_shaped_tables)
        network, _, _ = assemble_network_from_structure(st)
        assert network.geography.get("region") == "United States"

    def test_no_coordinates_yields_no_region_rather_than_a_guess(self):
        assert infer_geography([])["region"] is None
        assert infer_geography([{"lat": None, "lng": None}])["region"] is None


class TestTheUploadTemplateDescribesThisParser:
    """
    "Download template" on the upload screen builds a workbook from
    `GET /api/ingestions/preview/schema`, which is generated from the
    extractor's own `_COLUMN_ROLES`. These tests hold the two together: a
    template that offered a column the parser does not read, or omitted one it
    does, would send the user away to fill in the wrong spreadsheet.

    The arrangement this replaces is the one that produced the mapping
    screen's nine hand-typed dropdown options, none of which matched what the
    server sent — so every row rendered as the first one.
    """

    def test_every_sheet_role_the_extractor_knows_is_offered(self):
        from app.backend.api.network_extractor import _COLUMN_ROLES, upload_schema
        roles = {s["role"] for s in upload_schema()}
        assert roles == set(_COLUMN_ROLES)

    def test_every_column_is_one_the_parser_recognises(self):
        from app.backend.api.network_extractor import (
            classify_column_name, upload_schema,
        )
        for sheet in upload_schema():
            for column in sheet["columns"]:
                field, status, _confidence = classify_column_name(
                    column["header"], sheet["role"])
                assert status == "auto", (sheet["sheet"], column["header"], status)
                assert field == column["label"], (column["header"], field)

    def test_a_template_sheet_classifies_as_the_role_it_was_built_for(self):
        """
        Headers alone, no rows: `classify_sheet` reads the column signature, so
        a template filled in by the user lands on the branch it was built for.
        """
        from app.backend.api.network_extractor import classify_sheet, upload_schema
        # Roles that share every id column with a master table are identified by
        # columns the template does carry; the ones below are the unambiguous
        # set and are the sheets a user actually fills in.
        checked = {"facilities", "markets", "lanes", "products",
                   "demand_history", "warehouse_costs"}
        for sheet in upload_schema():
            if sheet["role"] not in checked:
                continue
            headers = [c["header"] for c in sheet["columns"]]
            frame = pd.DataFrame(columns=headers)
            assert classify_sheet(frame) == sheet["role"], sheet["sheet"]

    def test_the_preferred_header_is_the_first_accepted_alias(self):
        from app.backend.api.network_extractor import upload_schema
        for sheet in upload_schema():
            for column in sheet["columns"]:
                assert column["header"] == column["accepted"][0]
                assert len(column["accepted"]) == len(set(column["accepted"]))

    def test_sheet_names_are_ones_excel_will_accept(self):
        from app.backend.api.network_extractor import upload_schema
        seen = set()
        for sheet in upload_schema():
            name = sheet["sheet"]
            assert 0 < len(name) <= 31, name
            assert not set(name) & set('[]:*?/\\'), name
            assert name.lower() not in seen
            seen.add(name.lower())

    def test_the_endpoint_serves_it_and_requires_a_session(self):
        from app.backend.app import app
        with app.test_client() as client:
            anonymous = client.get("/api/ingestions/preview/schema")
            assert anonymous.status_code in (401, 403)
