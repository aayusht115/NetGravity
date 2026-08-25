"""Structured (no-AI) adapter tests — the path that must work without a key."""

from __future__ import annotations

from netgravity.ingestion.adapters import structured
from netgravity.schemas.network import FacilityStatus, NodeRole


def test_ingest_directory_reads_every_file(sample_dir):
    src = structured.ingest_directory(sample_dir)
    assert len(src.facilities) == 4          # 2 facilities + 2 markets
    assert len(src.products) == 1
    assert len(src.demands) == 2
    assert len(src.lanes) == 3


def test_markets_become_market_role_nodes(sample_dir):
    src = structured.ingest_directory(sample_dir)
    markets = [f for f in src.facilities if f.role == NodeRole.MARKET]
    assert {m.id for m in markets} == {"MKT_A", "MKT_B"}
    # Markets are demand sinks: never an open/close decision, no capacity ceiling
    assert all(not m.is_closable for m in markets)
    assert all(m.capacity_units_per_period >= 1e11 for m in markets)


def test_facility_fields_survive_the_round_trip(sample_dir):
    src = structured.ingest_directory(sample_dir)
    dc = next(f for f in src.facilities if f.id == "DC_TEST")
    assert dc.role == NodeRole.DC
    assert dc.status == FacilityStatus.EXISTING
    assert dc.capacity_units_per_period == 3000
    assert dc.fixed_cost_per_year == 600000
    assert dc.handling_cost_per_unit == 4.0
    assert dc.country == "India"


def test_mandatory_plant_is_not_closable(sample_dir):
    src = structured.ingest_directory(sample_dir)
    plant = next(f for f in src.facilities if f.id == "PLT_TEST")
    assert plant.is_mandatory is True
    assert plant.is_closable is False


def test_clean_fixture_produces_no_errors(sample_dir):
    src = structured.ingest_directory(sample_dir)
    for result in src.results:
        assert result.ok, f"{result.source_file}: {[i.render() for i in result.issues]}"


def test_missing_optional_file_is_not_an_error(tmp_path):
    """A source directory with only the essentials must still ingest."""
    (tmp_path / "products.csv").write_text(
        "product_id,product_name\nP001,Only Product\n", encoding="utf-8")
    src = structured.ingest_directory(tmp_path)
    assert len(src.products) == 1
    assert src.facilities == []
