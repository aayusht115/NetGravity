"""
Field mapping engine tests.

No live API calls — the client is faked. What is under test is how the three
opinions (memory, model, alias dictionary) are combined, and specifically
that a confidently-wrong single opinion cannot silently apply itself.
"""

from __future__ import annotations

import pytest

from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMResponse
from netgravity.ingestion.ai.field_mapper import (
    REVIEW_BELOW,
    _SHIPMENT_LOOKUP,
    build_mapping,
    canonical_fields_for,
    dictionary_opinion,
)
from netgravity.ingestion.memory import FieldMemory
from netgravity.ingestion.schemas.content import ContentClassification, ContentType
from netgravity.ingestion.schemas.field_mapping import (
    BY_AI,
    BY_AI_AND_DICTIONARY,
    BY_MEMORY_EXACT,
    BY_MEMORY_GENERALISED,
    BY_NONE,
)
from netgravity.ingestion.sources.base import RecordOrigin, RecordSet
from netgravity.ingestion.storage.local import LocalStorage


class _FakeClient:
    def __init__(self, payload=None, *, stub_mode=False, failed=False):
        self.stub_mode = stub_mode
        self._payload = payload or {}
        self._failed = failed
        self.prompts = []
        self.calls = 0

    def extract_json(self, *, task, prompt, stub_key, stub_context=None,
                     max_tokens=2000):
        self.calls += 1
        self.prompts.append(prompt)
        return LLMResponse(data=dict(self._payload), stubbed=self._failed,
                           model="fake:model", notes="live extraction",
                           failed=self._failed)


@pytest.fixture
def memory(tmp_path):
    return FieldMemory(LocalStorage(tmp_path))


def _rs(columns, rows=None, source_id="vendor_a"):
    rows = rows or [{c: f"v{i}" for c in columns} for i in range(4)]
    return RecordSet(key="k", columns=columns, rows=rows,
                     origin=RecordOrigin(source_id=source_id,
                                         container="f.xlsx", sheet="S1"))


def _classified(content_type, confidence=0.95):
    return ContentClassification(content_type=content_type, confidence=confidence,
                                 needs_review=False, proposed_by="fake:model")


def _ai(*pairs):
    return {"mappings": [
        {"source_column": c, "target_field": t, "confidence": conf,
         "reasoning": "because"} for c, t, conf in pairs
    ], "unmapped_columns": []}


def _find(mapping, column):
    return next(d for d in mapping.decisions if d.source_column == column)


# --- memory settles things --------------------------------------------------

def test_a_column_this_sender_confirmed_needs_no_review(memory):
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_a")
    mapping = build_mapping(_FakeClient(_ai(("Qty", "order_count", 0.99))),
                            _rs(["Qty"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    decision = _find(mapping, "Qty")
    assert decision.decided_by == BY_MEMORY_EXACT
    assert decision.target_field == "quantity"     # memory beats a fresh AI guess
    assert decision.needs_review is False


def test_a_generalised_mapping_settles_a_new_sender(memory):
    for sender in ("vendor_a", "vendor_b"):
        memory.record(source_column="Qty", target_field="quantity",
                      content_type="SHIPMENT_LOG", source_id=sender)
    mapping = build_mapping(_FakeClient(_ai(("Qty", "quantity", 0.9))),
                            _rs(["Qty"], source_id="vendor_c"),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    assert _find(mapping, "Qty").decided_by == BY_MEMORY_GENERALISED
    assert _find(mapping, "Qty").needs_review is False


def test_columns_already_settled_are_not_sent_to_the_model(memory):
    """The cost saving: a fully-remembered sheet costs no model call at all."""
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_a")
    client = _FakeClient(_ai(("Qty", "quantity", 0.9)))
    build_mapping(client, _rs(["Qty"]), _classified(ContentType.SHIPMENT_LOG),
                  memory=memory)
    assert client.calls == 0


# --- the cross-check --------------------------------------------------------

def test_model_and_dictionary_agreeing_clears_review(memory):
    """Corroboration IS the second opinion — no question needed."""
    mapping = build_mapping(_FakeClient(_ai(("Rate", "rate_per_unit", 0.93))),
                            _rs(["Rate"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    decision = _find(mapping, "Rate")
    assert decision.dictionary_target == "rate_per_unit"
    assert decision.methods_agree is True
    assert decision.decided_by == BY_AI_AND_DICTIONARY
    assert decision.needs_review is False


def test_disagreement_is_flagged_and_names_both_opinions(memory):
    """
    The confidently-wrong case. The alias table says Rate is a freight rate;
    the model claims it is a holding rate. Neither applies unchecked.
    """
    mapping = build_mapping(_FakeClient(_ai(("Rate", "holding_rate", 0.96))),
                            _rs(["Rate"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    decision = _find(mapping, "Rate")
    assert decision.methods_conflict is True
    assert decision.needs_review is True
    assert decision.confidence <= 0.60          # a claimed 96% does not survive
    reason = " ".join(decision.review_reasons)
    assert "holding_rate" in reason and "rate_per_unit" in reason
    assert "disagreed" in reason


def test_a_silent_dictionary_is_not_treated_as_dissent(memory):
    """Having no entry is silence, not disagreement."""
    mapping = build_mapping(_FakeClient(_ai(("Despatch Dt", "period", 0.96))),
                            _rs(["Despatch Dt"]),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    decision = _find(mapping, "Despatch Dt")
    assert decision.dictionary_target is None
    assert decision.methods_conflict is False


def test_first_sighting_without_corroboration_is_confirmed_once(memory):
    """
    One opinion is one opinion, however confident it sounds. Memory then
    carries the confirmation forward, so the cost is a single question.
    """
    mapping = build_mapping(_FakeClient(_ai(("Qty", "quantity", 0.99))),
                            _rs(["Qty"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    decision = _find(mapping, "Qty")
    assert decision.decided_by == BY_AI
    assert decision.needs_review is True
    assert any("single opinion" in r for r in decision.review_reasons)


def test_the_second_run_is_silent_once_confirmations_exist(memory):
    """End to end: confirm the pending columns, then nothing is asked again."""
    columns = ["Location Code", "Qty", "Rate", "Despatch Dt"]
    ai = _ai(("Location Code", "market_id", 0.97), ("Qty", "quantity", 0.95),
             ("Rate", "rate_per_unit", 0.93), ("Despatch Dt", "period", 0.96))
    classification = _classified(ContentType.SHIPMENT_LOG)

    first = build_mapping(_FakeClient(ai), _rs(columns), classification,
                          memory=memory)
    assert first.pending, "a first run should have something to confirm"

    for decision in first.pending:
        memory.record(source_column=decision.source_column,
                      target_field=decision.target_field,
                      content_type=ContentType.SHIPMENT_LOG.value,
                      source_id="vendor_a")

    second = build_mapping(_FakeClient(ai), _rs(columns), classification,
                           memory=memory)
    assert second.pending == []
    assert set(second.rename_map) == set(columns)


# --- the stricter bar for optimiser-bound data ------------------------------

def test_optimiser_bound_data_is_confirmed_even_when_both_methods_agree(memory):
    """
    A wrong facility mapping produces a wrong recommendation that looks
    authoritative. Agreement is not enough here; a human confirms once.
    """
    mapping = build_mapping(
        _FakeClient(_ai(("Facility_ID", "facility_id", 0.99))),
        _rs(["Facility_ID"]), _classified(ContentType.FACILITY), memory=memory)
    decision = _find(mapping, "Facility_ID")
    assert decision.methods_agree is True
    assert decision.needs_review is True
    assert any("feeds the optimiser" in r for r in decision.review_reasons)


def test_optimiser_bound_data_is_silent_once_remembered(memory):
    memory.record(source_column="Facility_ID", target_field="facility_id",
                  content_type="FACILITY", source_id="vendor_a")
    mapping = build_mapping(_FakeClient(_ai(("Facility_ID", "facility_id", 0.99))),
                            _rs(["Facility_ID"]), _classified(ContentType.FACILITY),
                            memory=memory)
    assert _find(mapping, "Facility_ID").needs_review is False


# --- pending mappings are never silently applied ---------------------------

def test_a_pending_column_is_excluded_from_the_rename_map(memory):
    mapping = build_mapping(_FakeClient(_ai(("Rate", "holding_rate", 0.96))),
                            _rs(["Rate"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    assert "Rate" not in mapping.rename_map
    assert mapping.needs_review is True


def test_an_unmapped_column_is_not_review_noise(memory):
    """Data we do not need is not a problem to escalate."""
    payload = _ai(("Vehicle No", None, 0.9))
    payload["unmapped_columns"] = ["Vehicle No"]
    mapping = build_mapping(_FakeClient(payload), _rs(["Vehicle No"]),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    decision = _find(mapping, "Vehicle No")
    assert decision.decided_by == BY_NONE
    assert decision.needs_review is False
    assert "Vehicle No" in mapping.unmapped_columns


# --- the vocabulary offered -------------------------------------------------

def test_canonical_vocabulary_is_scoped_to_the_content_type():
    """Offering every field invites cross-entity mistakes."""
    facility = canonical_fields_for(ContentType.FACILITY)
    lane = canonical_fields_for(ContentType.LANE)
    assert "facility_id" in facility
    assert "rate_per_unit" not in facility
    assert "rate_per_unit" in lane


def test_shipment_logs_get_a_union_dictionary_rather_than_silence():
    """
    Distributor files are the ones that need the cross-check most, and they
    are exactly the type with no alias table of their own.
    """
    assert _SHIPMENT_LOOKUP, "shipment lookup must not be empty"
    assert dictionary_opinion("Rate", ContentType.SHIPMENT_LOG) == "rate_per_unit"
    assert dictionary_opinion("Product_ID", ContentType.SHIPMENT_LOG) == "product_id"


def test_an_ambiguous_alias_is_dropped_from_the_union_not_guessed():
    """
    An ambiguous second opinion is worse than none — the whole value of the
    check is that a disagreement means something.
    """
    from netgravity.ingestion.ai.field_mapper import _ALIAS_TABLES
    seen = {}
    for _, lookup in _ALIAS_TABLES.values():
        for normalised, canonical in lookup.items():
            seen.setdefault(normalised, set()).add(canonical)
    ambiguous = {n for n, targets in seen.items() if len(targets) > 1}
    for normalised in ambiguous:
        assert normalised not in _SHIPMENT_LOOKUP


# --- context handed to the model --------------------------------------------

def test_the_prompt_carries_sample_values_known_ids_and_precedent(memory):
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_b")
    rows = [{"Dest": "MKT_DELHI", "Qty": 10}, {"Dest": "MKT_JAIPUR", "Qty": 20}]
    client = _FakeClient(_ai(("Dest", "market_id", 0.9), ("Qty", "quantity", 0.9)))

    build_mapping(client, _rs(["Dest", "Qty"], rows=rows),
                  _classified(ContentType.SHIPMENT_LOG), memory=memory,
                  known_ids=["MKT_DELHI", "MKT_JAIPUR"])

    prompt = client.prompts[0]
    assert "MKT_DELHI" in prompt
    assert "match known network identifiers" in prompt
    assert "PREVIOUSLY CONFIRMED" in prompt and "vendor_b" in prompt
    assert "SHIPMENT_LOG" in prompt


def test_precedent_is_offered_as_strong_but_not_binding(memory):
    """It must not override values that clearly contradict it."""
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_b")
    client = _FakeClient(_ai(("Qty", "quantity", 0.9)))
    build_mapping(client, _rs(["Qty"]), _classified(ContentType.SHIPMENT_LOG),
                  memory=memory)
    assert "unless the values clearly" in client.prompts[0]


# --- degrading honestly -----------------------------------------------------

def test_without_a_key_it_falls_back_to_memory_and_dictionary_only(memory):
    mapping = build_mapping(_FakeClient(stub_mode=True), _rs(["Rate", "Qty"]),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    assert "no AI key" in mapping.proposed_by
    assert _find(mapping, "Rate").target_field == "rate_per_unit"
    assert _find(mapping, "Qty").target_field is None      # dictionary is silent


def test_a_failed_call_is_labelled_and_nothing_is_auto_applied(memory):
    mapping = build_mapping(_FakeClient(_ai(("Rate", "rate_per_unit", 0.99)),
                                        failed=True),
                            _rs(["Rate"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    assert LLM_FAILURE_MARKER in mapping.proposed_by
    assert _find(mapping, "Rate").ai_target is None


def test_garbage_model_values_do_not_crash_or_auto_apply(memory):
    payload = {"mappings": [{"source_column": "Rate", "target_field": "rate_per_unit",
                             "confidence": "very sure",
                             "conversion_factor": "lots"}]}
    mapping = build_mapping(_FakeClient(payload), _rs(["Rate"]),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    decision = _find(mapping, "Rate")
    assert 0.0 <= decision.ai_confidence <= 1.0
    assert decision.conversion_factor == 1.0


def test_a_column_the_model_forgot_to_answer_still_gets_a_decision(memory):
    """Every column must be accounted for, including ones the model skipped."""
    mapping = build_mapping(_FakeClient(_ai(("Rate", "rate_per_unit", 0.95))),
                            _rs(["Rate", "Mystery"]),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    assert {d.source_column for d in mapping.decisions} == {"Rate", "Mystery"}
    assert _find(mapping, "Mystery").decided_by == BY_NONE


# --- reviewer-facing detail -------------------------------------------------

def test_every_opinion_is_kept_for_the_reviewer(memory):
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_b")
    mapping = build_mapping(_FakeClient(_ai(("Qty", "order_count", 0.8))),
                            _rs(["Qty"], source_id="vendor_a"),
                            _classified(ContentType.SHIPMENT_LOG), memory=memory)
    decision = _find(mapping, "Qty")
    assert decision.ai_target == "order_count"
    assert decision.memory_scope == "suggested"
    assert {o.target_field for o in decision.options} >= {"quantity", "order_count"}
    assert decision.sample_values, "reviewer needs to see real values"


def test_decisions_serialise_for_transport(memory):
    mapping = build_mapping(_FakeClient(_ai(("Rate", "rate_per_unit", 0.95))),
                            _rs(["Rate"]), _classified(ContentType.SHIPMENT_LOG),
                            memory=memory)
    payload = mapping.as_dict()
    assert payload["content_type"] == "SHIPMENT_LOG"
    assert payload["destination"] == "staging"
    assert payload["decisions"][0]["source_column"] == "Rate"
