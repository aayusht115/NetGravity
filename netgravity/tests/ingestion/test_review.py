"""
Human review tests.

Two claims under test: that the question asked reflects the actual evidence
rather than a generic "low confidence" flag, and that an answer becomes
permanent memory so the same question is never asked twice.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from netgravity.ingestion import review
from netgravity.ingestion.ai.client import LLMResponse
from netgravity.ingestion.ai.field_mapper import build_mapping
from netgravity.ingestion.memory import FieldMemory
from netgravity.ingestion.review import NOT_NEEDED, ReviewDecision
from netgravity.ingestion.schemas.content import ContentClassification, ContentType
from netgravity.ingestion.sources.base import RecordOrigin, RecordSet
from netgravity.ingestion.storage.local import LocalStorage


class _FakeClient:
    stub_mode = False

    def __init__(self, payload):
        self._payload = payload

    def extract_json(self, **kwargs):
        return LLMResponse(data=dict(self._payload), stubbed=False,
                           model="fake:model", notes="live extraction")


@pytest.fixture
def memory(tmp_path):
    return FieldMemory(LocalStorage(tmp_path))


def _rs(columns, source_id="vendor_e", rows=None):
    rows = rows or [{c: 10 for c in columns}, {c: 20 for c in columns}]
    return RecordSet(key="ship.xlsx#S1", columns=columns, rows=rows,
                     origin=RecordOrigin(source_id=source_id,
                                         container="ship.xlsx", sheet="S1"))


def _cls(content_type=ContentType.SHIPMENT_LOG, needs_review=False,
         confidence=0.95, rule_type=None):
    return ContentClassification(
        content_type=content_type, confidence=confidence,
        needs_review=needs_review, rule_type=rule_type or content_type,
        proposed_by="fake:model")


def _ai(*pairs):
    return {"mappings": [{"source_column": c, "target_field": t,
                          "confidence": conf, "reasoning": "because"}
                         for c, t, conf in pairs]}


def _build(memory, columns, ai_payload, classification=None, source_id="vendor_e"):
    return build_mapping(_FakeClient(ai_payload), _rs(columns, source_id),
                         classification or _cls(), memory=memory)


# --- the question reflects the evidence ------------------------------------

def test_a_remembered_disagreement_becomes_a_specific_question(memory):
    """
    The case that motivated the whole design: rather than "Qty needs review",
    the reviewer is told who used it which way.
    """
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_a")
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_b")
    memory.record(source_column="Qty", target_field="returns_volume",
                  content_type="SHIPMENT_LOG", source_id="vendor_d")

    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.9)))
    item = review.build_request([mapping]).items[0]

    assert "quantity" in item.question and "returns_volume" in item.question
    assert "vendor_a" in item.question and "vendor_d" in item.question
    assert item.question.rstrip().endswith("?")


def test_a_method_disagreement_names_both_methods(memory):
    mapping = _build(memory, ["Rate"], _ai(("Rate", "holding_rate", 0.92)))
    item = review.build_request([mapping]).items[0]
    assert "holding_rate" in item.question
    assert "rate_per_unit" in item.question
    assert "alias table" in item.question


def test_a_first_sighting_says_so_plainly(memory):
    mapping = _build(memory, ["Despatch Dt"], _ai(("Despatch Dt", "period", 0.99)))
    item = review.build_request([mapping]).items[0]
    assert "first time" in item.question.lower()


def test_optimiser_bound_data_explains_why_it_is_being_asked(memory):
    mapping = build_mapping(
        _FakeClient(_ai(("Facility_ID", "facility_id", 0.99))),
        _rs(["Facility_ID"]), _cls(ContentType.FACILITY), memory=memory)
    item = review.build_request([mapping]).items[0]
    assert "optimiser" in item.question
    assert "remembered" in item.question


def test_an_unidentifiable_file_is_asked_about_as_a_whole(memory):
    classification = _cls(ContentType.UNKNOWN, needs_review=True, confidence=0.2)
    classification.review_reasons = ["below the confidence floor"]
    mapping = _build(memory, ["a", "b"], _ai(("a", None, 0.1)), classification)

    items = review.build_request([mapping]).items
    content_items = [i for i in items if i.kind == review.KIND_CONTENT_TYPE]
    assert len(content_items) == 1
    assert "What kind of data" in content_items[0].question
    assert content_items[0].context["destination_if_accepted"] == "hold"


def test_a_classification_disagreement_names_both_readings(memory):
    classification = _cls(ContentType.SHIPMENT_LOG, needs_review=True,
                          rule_type=ContentType.FACILITY)
    mapping = _build(memory, ["Facility_ID"], _ai(("Facility_ID", "facility_id", 0.9)),
                     classification)
    item = [i for i in review.build_request([mapping]).items
            if i.kind == review.KIND_CONTENT_TYPE][0]
    assert "SHIPMENT_LOG" in item.question and "FACILITY" in item.question


# --- the options offered ----------------------------------------------------

def test_options_are_ordered_by_support_and_mark_a_recommendation(memory):
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_a")
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_b")
    memory.record(source_column="Qty", target_field="returns_volume",
                  content_type="SHIPMENT_LOG", source_id="vendor_d")

    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.9)))
    options = review.build_request([mapping]).items[0].options

    assert options[0].value == "quantity"
    assert options[0].support == 2
    assert options[0].recommended is True
    assert any(o.value == "returns_volume" for o in options)


def test_the_same_field_from_two_methods_appears_once(memory):
    """
    Showing one answer twice would misrepresent it as two choices.

    Uses optimiser-bound content deliberately: it is the one case where the
    model and the alias table AGREE and a review item still exists, because
    network data is confirmed once regardless of agreement.
    """
    mapping = build_mapping(_FakeClient(_ai(("Facility_ID", "facility_id", 0.99))),
                            _rs(["Facility_ID"]), _cls(ContentType.FACILITY),
                            memory=memory)
    item = [i for i in review.build_request([mapping]).items
            if i.kind == review.KIND_COLUMN][0]
    matches = [o for o in item.options if o.value == "facility_id"]
    assert len(matches) == 1
    assert "ai" in matches[0].suggested_by
    assert "dictionary" in matches[0].suggested_by


def test_a_reviewer_can_always_say_the_column_is_not_needed(memory):
    mapping = _build(memory, ["Despatch Dt"], _ai(("Despatch Dt", "period", 0.99)))
    options = review.build_request([mapping]).items[0].options
    assert options[-1].value == NOT_NEEDED


def test_context_carries_what_is_needed_to_answer_without_the_file(memory):
    rows = [{"Qty": 120}, {"Qty": 80}]
    mapping = build_mapping(_FakeClient(_ai(("Qty", "quantity", 0.99))),
                            _rs(["Qty"], rows=rows), _cls(), memory=memory)
    context = review.build_request([mapping]).items[0].context
    assert context["sample_values"] == ["120", "80"]
    assert context["model_said"] == "quantity"
    assert context["feeds_optimizer"] is False


# --- applying answers -------------------------------------------------------

def test_an_answer_settles_the_column_and_is_remembered(memory):
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    item = request.items[0]

    outcome = review.apply(request, [ReviewDecision(item.item_id, "quantity")],
                           [mapping], memory)

    assert outcome.applied == [item.item_id]
    assert outcome.remembered
    assert mapping.pending == []
    assert mapping.rename_map == {"Qty": "quantity"}

    resolved = memory.resolve(source_column="Qty", content_type="SHIPMENT_LOG",
                              source_id="vendor_e")
    assert resolved.scope == "exact"
    assert resolved.target_field == "quantity"


def test_answering_once_silences_the_question_on_the_next_run(memory):
    """The payoff: one question, then never again."""
    payload = _ai(("Qty", "quantity", 0.99))
    first = _build(memory, ["Qty"], payload)
    request = review.build_request([first])
    review.apply(request, [ReviewDecision(request.items[0].item_id, "quantity")],
                 [first], memory)

    second = _build(memory, ["Qty"], payload)
    assert review.build_request([second]).is_empty


def test_a_reviewer_can_override_the_recommendation(memory):
    mapping = _build(memory, ["Rate"], _ai(("Rate", "holding_rate", 0.92)))
    request = review.build_request([mapping])
    review.apply(request, [ReviewDecision(request.items[0].item_id, "rate_per_unit")],
                 [mapping], memory)
    assert mapping.rename_map == {"Rate": "rate_per_unit"}


def test_marking_a_column_not_needed_unmaps_it(memory):
    mapping = _build(memory, ["Despatch Dt"], _ai(("Despatch Dt", "period", 0.99)))
    request = review.build_request([mapping])
    review.apply(request, [ReviewDecision(request.items[0].item_id, NOT_NEEDED)],
                 [mapping], memory)

    assert mapping.rename_map == {}
    assert "Despatch Dt" in mapping.unmapped_columns
    assert mapping.pending == []


def test_confirming_a_content_type_settles_routing(memory):
    classification = _cls(ContentType.UNKNOWN, needs_review=True, confidence=0.2)
    mapping = _build(memory, ["a"], _ai(("a", None, 0.1)), classification)
    request = review.build_request([mapping])
    item = [i for i in request.items if i.kind == review.KIND_CONTENT_TYPE][0]

    review.apply(request, [ReviewDecision(item.item_id, "FACILITY")],
                 [mapping], memory)

    assert mapping.content_type == ContentType.FACILITY
    assert mapping.destination == "network"
    assert mapping.classification.needs_review is False


# --- refusing bad input -----------------------------------------------------

def test_an_unknown_item_id_is_rejected_not_ignored(memory):
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    outcome = review.apply(request, [ReviewDecision("nope::nope", "quantity")],
                           [mapping], memory)
    assert outcome.applied == []
    assert outcome.rejected[0]["item_id"] == "nope::nope"


def test_a_value_that_was_never_offered_is_refused(memory):
    """
    Guards the HTTP path: a client must not be able to write an arbitrary
    field name straight into the canonical schema.
    """
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    outcome = review.apply(request,
                           [ReviewDecision(request.items[0].item_id, "rm -rf")],
                           [mapping], memory)
    assert outcome.applied == []
    assert "not one of the offered options" in outcome.rejected[0]["reason"]
    assert mapping.pending, "the column must remain unsettled"


def test_an_empty_answer_is_refused(memory):
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    outcome = review.apply(request, [ReviewDecision(request.items[0].item_id, "")],
                           [mapping], memory)
    assert outcome.rejected


# --- shaped for transport ---------------------------------------------------

def test_a_request_serialises_to_json_for_a_ui(memory):
    mapping = _build(memory, ["Qty", "Rate"],
                     _ai(("Qty", "quantity", 0.99), ("Rate", "holding_rate", 0.9)))
    payload = review.build_request([mapping], run_id="run_1").as_dict()

    encoded = json.dumps(payload)           # must not raise
    assert '"run_id": "run_1"' in encoded
    assert payload["item_count"] == len(payload["items"])
    for item in payload["items"]:
        assert item["question"]
        assert item["options"]


def test_answers_can_arrive_as_plain_dicts_from_an_http_body(memory):
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    body = [{"item_id": request.items[0].item_id, "value": "quantity",
             "note": "confirmed by ops", "decided_by": "aayush"}]

    outcome = review.apply(request, body, [mapping], memory)
    assert outcome.applied
    assert mapping.rename_map == {"Qty": "quantity"}


def test_nothing_pending_produces_an_empty_request(memory):
    memory.record(source_column="Qty", target_field="quantity",
                  content_type="SHIPMENT_LOG", source_id="vendor_e")
    mapping = _build(memory, ["Qty"], _ai(("Qty", "quantity", 0.99)))
    request = review.build_request([mapping])
    assert request.is_empty
    assert request.summary == "nothing needs review"
