"""
Contract extraction tests.

The business claim these defend: a vendor quoting a LOWER headline rate can be
the MORE expensive vendor once a conditional surcharge applies. If that
arithmetic ever breaks, the headline story breaks with it.
"""

from __future__ import annotations

from pathlib import Path

from netgravity.ingestion.adapters import contracts as adapter
from netgravity.ingestion.schemas.contract import (
    ContractRule,
    SurchargeRule,
    SurchargeType,
)

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "contracts"


def _transcorp() -> ContractRule:
    return ContractRule(
        contract_id="TC-TEST", vendor_name="TransCorp",
        base_rate=10.0, rate_unit="INR/kg",
        surcharges=[
            SurchargeRule(surcharge_type=SurchargeType.FUEL, rate=2.0,
                          applies_to_location_ids=[]),                    # blanket
            SurchargeRule(surcharge_type=SurchargeType.NSL, rate=5.0,
                          applies_to_location_ids=["MKT_GUWAHATI"]),      # conditional
        ],
    )


def _speedfreight() -> ContractRule:
    return ContractRule(contract_id="SF-TEST", vendor_name="SpeedFreight",
                        base_rate=12.0, rate_unit="INR/kg", surcharges=[])


def test_blanket_surcharge_applies_everywhere():
    assert _transcorp().effective_rate_for("MKT_DELHI") == 12.0   # 10 + 2 fuel


def test_conditional_surcharge_applies_only_where_named():
    tc = _transcorp()
    assert tc.effective_rate_for("MKT_GUWAHATI") == 17.0          # 10 + 2 + 5
    assert tc.effective_rate_for("MKT_DELHI") == 12.0             # no NSL here


def test_cheaper_headline_is_more_expensive_at_an_nsl_destination():
    """The whole point: Rs.10/kg beats Rs.12/kg — until it doesn't."""
    tc, sf = _transcorp(), _speedfreight()

    assert tc.base_rate < sf.base_rate                  # TransCorp looks cheaper

    ranked = adapter.compare_vendors([tc, sf], "MKT_GUWAHATI")
    assert ranked[0]["vendor"] == "SpeedFreight"        # ...but is not, here
    assert ranked[0]["effective_rate"] == 12.0
    assert ranked[1]["effective_rate"] == 17.0
    assert ranked[1]["premium"] == 7.0


def test_ranking_flips_back_at_a_serviceable_destination():
    ranked = adapter.compare_vendors([_transcorp(), _speedfreight()], "MKT_DELHI")
    assert ranked[0]["effective_rate"] == ranked[1]["effective_rate"] == 12.0


def test_has_hidden_cost_detects_only_conditional_surcharges():
    assert _transcorp().has_hidden_cost is True
    assert _speedfreight().has_hidden_cost is False

    blanket_only = ContractRule(
        contract_id="X", vendor_name="X", base_rate=10.0,
        surcharges=[SurchargeRule(surcharge_type=SurchargeType.FUEL, rate=2.0)],
    )
    assert blanket_only.has_hidden_cost is False, \
        "a surcharge applying to everything is not hidden — it is just the rate"


# --- adapter-level, running in stub mode (no API key) ----------------------

def test_extraction_runs_without_an_api_key(tmp_config):
    if not CONTRACT_DIR.exists():
        import pytest
        pytest.skip("sample contracts not present")

    rules, results = adapter.ingest_directory(CONTRACT_DIR, tmp_config)
    assert rules, "expected at least one contract to be extracted"
    assert all(r.ai_stubbed for r in results), "no key set — must be stubbed"


def test_hidden_cost_is_raised_as_a_warning(tmp_config):
    if not CONTRACT_DIR.exists():
        import pytest
        pytest.skip("sample contracts not present")

    _, results = adapter.ingest_directory(CONTRACT_DIR, tmp_config)
    codes = {i.code for r in results for i in r.issues}
    assert "R-014" in codes, "a conditional surcharge must surface as a warning"


def test_unreadable_file_type_degrades_gracefully(tmp_path, tmp_config):
    """An unsupported file must not take the whole run down."""
    (tmp_path / "notes.rtf").write_text("not a contract", encoding="utf-8")
    rules, results = adapter.ingest_directory(tmp_path, tmp_config)
    assert rules == []
    assert results == []
