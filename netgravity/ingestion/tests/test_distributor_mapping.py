"""
Distributor column-mapping tests.

Two things matter here: low-confidence mappings must reach a human rather than
being trusted silently, and a confirmed mapping must be reused so the AI cost
is paid once per FORMAT, not once per file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.adapters import distributor
from netgravity.ingestion.schemas.mapping import ColumnMapping, DistributorMapping

DIST_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "distributors"


def _mapping(**kw) -> DistributorMapping:
    base = dict(
        distributor_id="test_dist",
        mappings=[
            ColumnMapping(source_column="Location Code", target_field="market_id",
                          confidence=0.93),
            ColumnMapping(source_column="Qty", target_field="quantity",
                          confidence=0.87),
        ],
    )
    base.update(kw)
    return DistributorMapping(**base)


def test_low_confidence_columns_are_flagged_for_review():
    m = _mapping()
    flagged = {c.source_column for c in m.needs_review}
    assert flagged == {"Qty"}, "0.87 is below the 0.90 review bar; 0.93 is above it"


def test_unit_conversion_is_applied_arithmetically():
    """The model proposes the factor; the multiplication is deterministic code."""
    m = DistributorMapping(
        distributor_id="d",
        mappings=[ColumnMapping(source_column="Wt (kgs)", target_field="quantity",
                                confidence=0.95, source_unit="kg",
                                target_unit="units", conversion_factor=0.4)],
    )
    rows, issues = distributor.apply_mapping([{"Wt (kgs)": "250"}], m, "f.xlsx")
    assert rows[0]["quantity"] == 100.0
    assert not issues


def test_unconvertible_value_warns_rather_than_crashing():
    m = DistributorMapping(
        distributor_id="d",
        mappings=[ColumnMapping(source_column="Wt", target_field="quantity",
                                confidence=0.95, conversion_factor=0.4)],
    )
    _, issues = distributor.apply_mapping([{"Wt": "heavy"}], m, "f.xlsx")
    assert any(i.code == "R-016" for i in issues)


def test_unmapped_columns_are_dropped_not_guessed():
    m = _mapping()
    rows, _ = distributor.apply_mapping(
        [{"Location Code": "MKT_A", "Qty": "10", "Vehicle No": "HR26AB1234"}],
        m, "f.xlsx",
    )
    assert "Vehicle No" not in rows[0]
    assert set(rows[0]) == {"market_id", "quantity"}


# --- caching ----------------------------------------------------------------

def test_mapping_cache_round_trip(tmp_storage):
    m = _mapping()
    distributor.save_mapping(m, tmp_storage)
    loaded = distributor.load_cached_mapping("test_dist", tmp_storage)
    assert loaded is not None
    assert loaded.distributor_id == "test_dist"
    assert len(loaded.mappings) == 2


def test_confirming_a_mapping_persists_the_flag(tmp_storage):
    distributor.save_mapping(_mapping(), tmp_storage)
    assert distributor.confirm_mapping("test_dist", tmp_storage) is True
    assert distributor.load_cached_mapping("test_dist", tmp_storage).confirmed_by_human


def test_confirming_an_unknown_distributor_returns_false(tmp_storage):
    assert distributor.confirm_mapping("never_seen", tmp_storage) is False


def test_confirmed_mapping_skips_the_model_call(tmp_config, tmp_storage):
    """The second file from a known distributor must cost no AI call."""
    if not DIST_DIR.exists():
        pytest.skip("sample distributor file not present")
    path = next(DIST_DIR.glob("*.xlsx"), None)
    if path is None:
        pytest.skip("no distributor spreadsheet present")

    # First pass: no cache, so the model (stub) proposes a mapping
    _, mapping, first = distributor.ingest_file(path, tmp_config, tmp_storage)
    assert first.ai_used is True
    assert mapping is not None

    distributor.confirm_mapping(mapping.distributor_id, tmp_storage)

    # Second pass: cached and confirmed, so no model call at all
    _, _, second = distributor.ingest_file(path, tmp_config, tmp_storage)
    assert second.ai_used is False
    assert any("reused cached mapping" in n for n in second.ai_notes)


def test_messy_spreadsheet_is_read_and_mapped(tmp_config, tmp_storage):
    if not DIST_DIR.exists():
        pytest.skip("sample distributor file not present")
    path = next(DIST_DIR.glob("*.xlsx"), None)
    if path is None:
        pytest.skip("no distributor spreadsheet present")

    rows, mapping, result = distributor.ingest_file(path, tmp_config, tmp_storage)
    assert result.rows_read == 40
    assert rows, "expected mapped rows out of the messy file"
    assert result.ai_stubbed is True, "no key set — must be stubbed"
    # The ambiguous 'Qty' and 'Rate' headers should reach a human
    assert any(i.code == "R-017" for i in result.issues)
