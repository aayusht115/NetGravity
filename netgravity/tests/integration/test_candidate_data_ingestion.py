"""
Client-supplied candidate sites, from the upload to the model.

A client can hand over sites they are considering — "we have options in Nagpur
and Coimbatore, here is what each would cost to build". Everything needed to
use that already existed except the parts that carry it: `FacilityRecord` had
`opening_cost` and `milp.py` charged it, but nothing ever read a column into it,
and `ProductRecord` had no category at all although the extractor parsed one.

These tests run the real extraction column table and the real assembler.
"""

import pandas as pd
import pytest

from app.backend.api.network_extractor import (
    _COLUMN_ROLES,
    build_network_from_dataframes,
    upload_schema,
)
from app.backend.services.network_assembler import assemble_network_from_structure
from netgravity.schemas.network import FacilityStatus


FACILITIES = pd.DataFrame([
    # An operating DC, and a site the client has only proposed.
    {"facility_id": "DC_MUM", "facility_name": "Mumbai DC", "facility_type": "DC",
     "status": "Existing", "latitude": 19.07, "longitude": 72.87,
     "capacity_units_per_period": 9000, "handling_cost_per_unit": 2.1,
     "fixed_cost_per_year": 4_000_000, "opening_cost": 0},
    {"facility_id": "DC_NAG", "facility_name": "Nagpur DC", "facility_type": "DC",
     "status": "Candidate", "latitude": 21.15, "longitude": 79.09,
     "capacity_units_per_period": 6000, "handling_cost_per_unit": 1.8,
     "fixed_cost_per_year": 3_000_000, "opening_cost": 180_000_000},
    {"facility_id": "PLT_PUN", "facility_name": "Pune Plant", "facility_type": "PLANT",
     "status": "Existing", "latitude": 18.52, "longitude": 73.86,
     "capacity_units_per_period": 20000, "handling_cost_per_unit": 0.0,
     "fixed_cost_per_year": 0, "opening_cost": 0},
])

MARKETS = pd.DataFrame([
    {"market_id": "MKT_MUM", "market_name": "Mumbai", "latitude": 19.0, "longitude": 72.9,
     "demand_units": 5000, "sla_days": 2, "region": "West"},
    {"market_id": "MKT_NAG", "market_name": "Nagpur", "latitude": 21.1, "longitude": 79.1,
     "demand_units": 3000, "sla_days": 3, "region": "Central"},
])

PRODUCTS = pd.DataFrame([
    {"product_id": "SKU_A", "product_name": "Ambient pack", "product_category": "Ambient"},
    {"product_id": "SKU_B", "product_name": "Chilled pack", "product_category": "Chilled"},
])

LANES = pd.DataFrame([
    {"origin_id": "PLT_PUN", "destination_id": "DC_MUM", "distance_km": 150,
     "rate_per_unit": 2.0, "transit_days": 1, "mode": "ROAD"},
    {"origin_id": "PLT_PUN", "destination_id": "DC_NAG", "distance_km": 700,
     "rate_per_unit": 5.0, "transit_days": 2, "mode": "ROAD"},
    {"origin_id": "DC_MUM", "destination_id": "MKT_MUM", "distance_km": 20,
     "rate_per_unit": 1.0, "transit_days": 1, "mode": "ROAD"},
    {"origin_id": "DC_NAG", "destination_id": "MKT_NAG", "distance_km": 15,
     "rate_per_unit": 1.0, "transit_days": 1, "mode": "ROAD"},
])


@pytest.fixture(scope="module")
def assembled():
    structure = build_network_from_dataframes({
        "facilities": FACILITIES, "markets": MARKETS,
        "products": PRODUCTS, "lanes": LANES,
    })
    network, assumptions, _issues = assemble_network_from_structure(structure)
    return network, assumptions


class TestTheColumnIsAdvertisedBeforeItIsAskedFor:
    """
    The template a client downloads is generated from the same table the parser
    reads, so a column the parser understands cannot be missing from the sheet
    they are asked to fill in.
    """

    def test_opening_cost_is_a_declared_facility_column(self):
        labels = [label for label, _ in _COLUMN_ROLES["facilities"]]
        assert "Opening cost" in labels

    def test_it_reaches_the_downloadable_template(self):
        facilities = next(s for s in upload_schema() if s["role"] == "facilities")
        column = next(c for c in facilities["columns"] if c["label"] == "Opening cost")
        assert column["header"] == "opening_cost"
        assert "capex" in column["accepted"]


class TestSuppliedCandidatesReachTheModel:

    def test_status_survives_to_the_canonical_network(self, assembled):
        network, _ = assembled
        by_id = {f.id: f for f in network.facilities}
        assert by_id["DC_MUM"].status == FacilityStatus.EXISTING
        assert by_id["DC_NAG"].status == FacilityStatus.CANDIDATE

    def test_the_cost_to_open_is_carried_not_dropped(self, assembled):
        """
        The one that silently mattered: `opening_cost` defaulting to 0.0 tells
        the optimiser a ₹18 crore build is free, and a free site that saves
        anything at all is always worth opening.
        """
        network, _ = assembled
        by_id = {f.id: f for f in network.facilities}
        assert by_id["DC_NAG"].opening_cost == 180_000_000
        assert by_id["DC_NAG"].is_candidate

    def test_an_existing_site_is_not_charged_to_stay_open(self, assembled):
        network, _ = assembled
        assert {f.id: f.opening_cost for f in network.facilities}["DC_MUM"] == 0.0

    def test_region_is_carried_for_every_node(self, assembled):
        network, _ = assembled
        by_id = {f.id: f for f in network.facilities}
        assert by_id["MKT_MUM"].region == "West"
        assert by_id["MKT_NAG"].region == "Central"

    def test_product_category_is_carried(self, assembled):
        network, _ = assembled
        by_id = {p.id: p for p in network.products}
        assert by_id["SKU_A"].category == "Ambient"
        assert by_id["SKU_B"].category == "Chilled"


class TestAnUnpricedCandidateIsCalledOut:
    """Silence about a missing opening cost is the dangerous case, so it is
    reported as an assumption the user can see rather than left at zero."""

    def test_missing_opening_cost_is_reported_as_an_assumption(self):
        facilities = FACILITIES.drop(columns=["opening_cost"])
        structure = build_network_from_dataframes({
            "facilities": facilities, "markets": MARKETS,
            "products": PRODUCTS, "lanes": LANES,
        })
        _network, assumptions, _issues = assemble_network_from_structure(structure)
        assert any("DC_NAG" in a and "opening cost" in a for a in assumptions), assumptions
