"""
Contract extraction cache tests.

Re-reading an unchanged contract on every run is pure wasted spend. These
tests pin the three behaviours that make caching safe rather than merely
cheap: unchanged text is reused, edited text is re-read, and stub output is
never allowed into the cache.
"""

from __future__ import annotations

from pathlib import Path

from netgravity.ingestion.ai.cache import (
    contract_cache_key,
    load_cached_contract,
    save_contract,
)
from netgravity.ingestion.adapters import contracts as contracts_adapter
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.schemas.contract import ContractRule
from netgravity.ingestion.storage.local import LocalStorage

TEXT = "TransCorp rate card. Base rate Rs.10/kg. NSL surcharge Rs.5/kg."


def _storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(tmp_path)


def _live_rule() -> ContractRule:
    return ContractRule(
        contract_id="C1", vendor_name="TransCorp", base_rate=10.0,
        extracted_by="anthropic:claude-sonnet-4-5",
    )


def _stub_rule() -> ContractRule:
    return ContractRule(
        contract_id="C1", vendor_name="TransCorp", base_rate=10.0,
        extracted_by="stub",
    )


# --- keying -----------------------------------------------------------------

def test_same_text_gives_same_key_different_text_does_not():
    assert contract_cache_key(TEXT) == contract_cache_key(TEXT)
    assert contract_cache_key(TEXT) != contract_cache_key(TEXT + " Amended.")


def test_round_trip_returns_the_rule(tmp_path):
    storage = _storage(tmp_path)
    assert save_contract(_live_rule(), TEXT, storage) is not None
    got = load_cached_contract(TEXT, storage)
    assert got is not None
    assert got.vendor_name == "TransCorp"
    assert got.extracted_by == "anthropic:claude-sonnet-4-5"


def test_edited_contract_misses_the_cache(tmp_path):
    """
    Keyed by content, not filename — so an amended rate card re-extracts
    instead of silently serving the superseded terms.
    """
    storage = _storage(tmp_path)
    save_contract(_live_rule(), TEXT, storage)
    assert load_cached_contract(TEXT + " Amended: NSL now Rs.7/kg.", storage) is None


# --- the stub-poisoning guard ----------------------------------------------

def test_stub_results_are_never_written(tmp_path):
    """
    If stub output were cached, adding a real API key later would still return
    canned demo data on every hit — live-looking and fake.
    """
    storage = _storage(tmp_path)
    assert save_contract(_stub_rule(), TEXT, storage) is None
    assert load_cached_contract(TEXT, storage) is None


def test_a_stub_entry_found_in_the_cache_is_refused(tmp_path):
    """Defence in depth: even a hand-edited cache file cannot serve stub data."""
    storage = _storage(tmp_path)
    save_contract(_live_rule(), TEXT, storage)
    poisoned = _stub_rule()
    storage.save_text(
        "standardized", contract_cache_key(TEXT),
        f'{{"rule": {poisoned.model_dump_json()}}}',
    )
    assert load_cached_contract(TEXT, storage) is None


# --- degradation ------------------------------------------------------------

def test_no_storage_means_no_cache_not_a_crash():
    assert load_cached_contract(TEXT, None) is None
    assert save_contract(_live_rule(), TEXT, None) is None


def test_corrupt_cache_entry_is_treated_as_a_miss(tmp_path):
    storage = _storage(tmp_path)
    storage.save_text("standardized", contract_cache_key(TEXT), "{not json")
    assert load_cached_contract(TEXT, storage) is None


# --- adapter integration ----------------------------------------------------

def test_adapter_reuses_cache_and_still_reports_hidden_costs(tmp_path):
    """
    A cached run must surface the SAME hidden-surcharge warning as a fresh
    one. If it did not, the headline business finding would vanish on the
    second run and reappear only when the cache was cleared.
    """
    storage = _storage(tmp_path)
    cfg = IngestionConfig()          # no API key -> stub mode
    contract = tmp_path / "transcorp_rate_card.txt"
    contract.write_text(TEXT, encoding="utf-8")

    # First pass runs in stub mode, so nothing should be cached.
    rule_a, result_a = contracts_adapter.ingest_file(contract, cfg, None, storage)
    assert result_a.ai_stubbed is True
    assert load_cached_contract(TEXT, storage) is None

    # Seed the cache with a live-looking extraction that carries a surcharge,
    # then confirm the adapter serves it AND re-raises the R-014 warning.
    seeded = rule_a.model_copy(update={"extracted_by": "anthropic:test-model"})
    assert seeded.has_hidden_cost, "fixture must carry a location-scoped surcharge"
    save_contract(seeded, TEXT, storage)

    rule_b, result_b = contracts_adapter.ingest_file(contract, cfg, None, storage)
    assert rule_b is not None
    assert result_b.ai_used is False
    assert result_b.ai_stubbed is False
    assert any("reused cached extraction" in n for n in result_b.ai_notes)
    assert any(i.code == "R-014" for i in result_b.issues)


def test_use_cache_false_forces_a_fresh_read(tmp_path):
    storage = _storage(tmp_path)
    cfg = IngestionConfig()
    contract = tmp_path / "transcorp_rate_card.txt"
    contract.write_text(TEXT, encoding="utf-8")

    save_contract(
        _live_rule().model_copy(update={"vendor_name": "STALE"}), TEXT, storage
    )
    rule, result = contracts_adapter.ingest_file(
        contract, cfg, None, storage, use_cache=False
    )
    assert rule.vendor_name != "STALE"
    assert result.ai_used is True
