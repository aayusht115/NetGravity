"""
Ingestion memory tests.

The design claim being defended: nothing is hardcoded as "always
sender-specific" or "always universal". Scope is resolved from how much
independent evidence exists, and disagreement between senders is surfaced
rather than silently resolved by picking a winner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.memory import (
    GENERALISE_AFTER_SOURCES,
    SCOPE_CONFLICT,
    SCOPE_EXACT,
    SCOPE_GENERALISED,
    SCOPE_NONE,
    SCOPE_SUGGESTED,
    DocumentMemory,
    FieldMemory,
    signature,
    similarity,
)
from netgravity.ingestion.memory.document_memory import (
    MIN_SIGNATURE_TOKENS,
    SIMILARITY_THRESHOLD,
)
from netgravity.ingestion.storage.local import LocalStorage

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "contracts"


@pytest.fixture
def memory(tmp_path):
    return FieldMemory(LocalStorage(tmp_path))


@pytest.fixture
def docs(tmp_path):
    return DocumentMemory(LocalStorage(tmp_path))


def _confirm(memory, source_id, target="quantity", column="Qty",
             content_type="SHIPMENT_LOG"):
    return memory.record(source_column=column, target_field=target,
                         content_type=content_type, source_id=source_id)


def _ask(memory, source_id, column="Qty", content_type="SHIPMENT_LOG"):
    return memory.resolve(source_column=column, content_type=content_type,
                          source_id=source_id)


# --- the evidence ladder ----------------------------------------------------

def test_unknown_column_is_reported_as_unknown(memory):
    assert _ask(memory, "vendor_a").scope == SCOPE_NONE


def test_same_sender_gets_an_exact_match_with_no_review(memory):
    _confirm(memory, "vendor_a")
    resolution = _ask(memory, "vendor_a")
    assert resolution.scope == SCOPE_EXACT
    assert resolution.target_field == "quantity"
    assert resolution.needs_review is False
    assert resolution.confidence == 1.0


def test_one_other_sender_is_only_a_suggestion(memory):
    """One data point is not a pattern — proposed, but still reviewed."""
    _confirm(memory, "vendor_a")
    resolution = _ask(memory, "vendor_b")
    assert resolution.scope == SCOPE_SUGGESTED
    assert resolution.target_field == "quantity"
    assert resolution.needs_review is True


def test_enough_agreeing_senders_lets_it_generalise(memory):
    """
    This is the payoff the whole design exists for: vendor_c has never sent
    this column, but two unrelated senders already agreed on its meaning, so
    vendor_c is not asked.
    """
    _confirm(memory, "vendor_a")
    _confirm(memory, "vendor_b")
    resolution = _ask(memory, "vendor_c")
    assert resolution.scope == SCOPE_GENERALISED
    assert resolution.needs_review is False
    assert "vendor_a" in resolution.rationale and "vendor_b" in resolution.rationale


def test_generalisation_needs_distinct_senders_not_repeat_confirmations(memory):
    """
    A sender agreeing with itself is not corroboration. Re-confirming the
    same source must not push the count over the generalisation line.
    """
    for _ in range(GENERALISE_AFTER_SOURCES + 3):
        _confirm(memory, "vendor_a")
    assert _ask(memory, "vendor_z").scope == SCOPE_SUGGESTED


# --- the ambiguity case -----------------------------------------------------

def test_disagreement_between_senders_is_surfaced_not_resolved(memory):
    """
    The most valuable output: rather than picking the majority answer and
    moving on, the conflict and its evidence are handed up so the review
    layer can ask a specific question.
    """
    _confirm(memory, "vendor_a", target="quantity")
    _confirm(memory, "vendor_b", target="quantity")
    _confirm(memory, "vendor_d", target="returns_quantity")

    resolution = _ask(memory, "vendor_e")
    assert resolution.scope == SCOPE_CONFLICT
    assert resolution.is_conflict
    assert resolution.needs_review is True

    fields = {a.target_field for a in resolution.alternatives}
    assert fields == {"quantity", "returns_quantity"}

    strongest = resolution.alternatives[0]
    assert strongest.target_field == "quantity"
    assert strongest.support == 2
    assert "vendor_a" in resolution.rationale and "vendor_d" in resolution.rationale


def test_a_sender_with_its_own_confirmation_is_unaffected_by_others_disputing(memory):
    """vendor_a said what IT means; another sender's different usage does not
    reopen a settled question for vendor_a."""
    _confirm(memory, "vendor_a", target="quantity")
    _confirm(memory, "vendor_d", target="returns_quantity")
    resolution = _ask(memory, "vendor_a")
    assert resolution.scope == SCOPE_EXACT
    assert resolution.target_field == "quantity"


# --- content type is always part of the key ---------------------------------

def test_nothing_generalises_across_content_types(memory):
    """
    The 'Qty means two different things' trap. Confirmations on a shipment
    log must say nothing about the same column on a product sheet.
    """
    _confirm(memory, "vendor_a", content_type="SHIPMENT_LOG")
    _confirm(memory, "vendor_b", content_type="SHIPMENT_LOG")
    assert _ask(memory, "vendor_c", content_type="SHIPMENT_LOG").scope == SCOPE_GENERALISED
    assert _ask(memory, "vendor_c", content_type="PRODUCT").scope == SCOPE_NONE


# --- keys and hygiene -------------------------------------------------------

def test_column_lookup_ignores_case_and_separators(memory):
    _confirm(memory, "vendor_a", column="Location Code", target="market_id")
    for spelling in ("location_code", "LOCATION-CODE", "locationcode", "Location  Code"):
        resolution = _ask(memory, "vendor_a", column=spelling)
        assert resolution.scope == SCOPE_EXACT, f"failed on {spelling!r}"


def test_reconfirming_replaces_rather_than_appends(memory):
    """
    A sender correcting an earlier mistake must not leave the stale answer on
    file — it would keep voting in the generalisation count forever.
    """
    _confirm(memory, "vendor_a", target="quantity")
    _confirm(memory, "vendor_a", target="order_count")

    observations = memory.observations_for("SHIPMENT_LOG", "Qty")
    assert len(observations) == 1
    assert observations[0].target_field == "order_count"
    assert _ask(memory, "vendor_a").target_field == "order_count"


def test_corrupt_memory_file_degrades_to_unknown_instead_of_breaking(tmp_path):
    """Losing memory costs a re-review; raising would stop the whole run."""
    storage = LocalStorage(tmp_path)
    memory = FieldMemory(storage)
    _confirm(memory, "vendor_a")
    storage.save_text("standardized", "field_memory/SHIPMENT_LOG/qty.json",
                      "{ this is not json")
    assert _ask(memory, "vendor_a").scope == SCOPE_NONE


def test_stats_report_what_has_been_learned(memory):
    assert memory.stats()["observations"] == 0
    _confirm(memory, "vendor_a")
    _confirm(memory, "vendor_b")
    _confirm(memory, "vendor_a", column="Rate", target="rate_per_unit")
    stats = memory.stats()
    assert stats["columns"] == 2
    assert stats["observations"] == 3


# --- document pattern memory ------------------------------------------------

def _contract(name):
    return (CONTRACT_DIR / name).read_text(encoding="utf-8")


def test_a_renewal_with_changed_numbers_still_matches(docs):
    """
    The gap the exact-text cache cannot close: same contract, new rates. The
    signature drops digits precisely so changed figures do not make a renewal
    look like a stranger.
    """
    original = _contract("transcorp_rate_card.txt")
    renewal = (original.replace("10.00", "11.50").replace("2.00", "2.75")
                       .replace("TC-2026-0472", "TC-2027-0913"))

    docs.record(original, document_name="transcorp_2026.pdf",
                labels={"vendor": "TransCorp Logistics"})
    match = docs.find(renewal)

    assert match.matched
    assert match.score >= SIMILARITY_THRESHOLD
    assert "TransCorp" in match.rationale


def test_an_unrelated_vendors_contract_does_not_match(docs):
    """Shared legal boilerplate must not be enough to claim a match."""
    docs.record(_contract("transcorp_rate_card.txt"), document_name="tc.pdf")
    match = docs.find(_contract("speedfreight_rate_card.txt"))
    assert not match.matched
    assert match.score < SIMILARITY_THRESHOLD


def test_seeing_a_template_again_extends_one_pattern_not_many(docs):
    original = _contract("transcorp_rate_card.txt")
    docs.record(original, document_name="2026.pdf")
    docs.record(original.replace("10.00", "12.00"), document_name="2027.pdf")

    patterns = docs._all()
    assert len(patterns) == 1
    assert patterns[0].times_seen == 2
    assert set(patterns[0].seen_documents) == {"2026.pdf", "2027.pdf"}


def test_labels_are_observed_and_carried_forward_not_imposed(docs):
    """Scope is shape, not vendor — the label is a discovered hint only."""
    docs.record(_contract("transcorp_rate_card.txt"), document_name="a.pdf",
                labels={"vendor": "TransCorp", "doc_type": "rate_card"})
    pattern = docs._all()[0]
    assert pattern.observed_labels == {"vendor": "TransCorp", "doc_type": "rate_card"}


def test_a_short_document_is_never_matched_by_shape(docs):
    """Too few distinct words for a Jaccard score to carry any meaning."""
    docs.record(_contract("transcorp_rate_card.txt"), document_name="a.pdf")
    assert docs.find("Rate: Rs 10 per kg. Thanks.").matched is False
    assert docs.record("too short", document_name="b.pdf") is None


def test_signature_ignores_digits_ordering_and_repetition():
    assert signature("Rate 10 rate RATE") == ["rate"]
    assert signature("alpha beta") == signature("beta alpha beta")
    assert similarity([], ["a"]) == 0.0
    assert similarity(["a", "b"], ["a", "b"]) == 1.0


def test_signature_length_guard_matches_the_documented_constant():
    text = " ".join(f"word{i}" for i in range(MIN_SIGNATURE_TOKENS - 5))
    assert len(signature(text)) < MIN_SIGNATURE_TOKENS
