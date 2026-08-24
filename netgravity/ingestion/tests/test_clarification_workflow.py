from __future__ import annotations

from netgravity.ingestion import review
from netgravity.ingestion.ai.clarification import (
    MAX_DISPLAY_WORDS,
    MAX_QUESTION_WORDS,
    MAX_REASON_WORDS,
    analyse,
)
from netgravity.ingestion.ai.client import LLMResponse
from netgravity.ingestion.ai.field_mapper import build_mapping
from netgravity.ingestion.memory.field_catalog import FieldCatalog
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.schemas.content import ContentClassification, ContentType
from netgravity.ingestion.schemas.field_mapping import FieldDisposition
from netgravity.ingestion.service import IngestionService
from netgravity.ingestion.sources.base import RecordOrigin, RecordSet


def _record_set(column="Dock Door Count"):
    return RecordSet(
        key="sites.xlsx#Facilities",
        columns=["Facility_ID", column],
        rows=[{"Facility_ID": "DC_1", column: 8},
              {"Facility_ID": "DC_2", column: 12}],
        origin=RecordOrigin(source_id="client_a", container="sites.xlsx",
                            sheet="Facilities"),
    )


def _classification():
    return ContentClassification(
        content_type=ContentType.FACILITY,
        confidence=1.0,
        needs_review=False,
    )


def test_unknown_column_is_profiled_preserved_and_nonblocking(tmp_storage):
    mapping = build_mapping(
        None, _record_set(), _classification(),
        memory=FieldMemory(tmp_storage),
        catalog=FieldCatalog(tmp_storage, "client_a"),
    )
    decision = next(d for d in mapping.decisions
                    if d.source_column == "Dock Door Count")

    assert decision.disposition == FieldDisposition.UNRESOLVED
    assert decision.profile.data_type == "numeric"
    assert decision.profile.minimum == 8
    assert decision.profile.maximum == 12
    assert decision.profile.adjacent_columns == ["Facility_ID"]
    assert "Dock Door Count" not in mapping.rename_map

    request = review.build_request([mapping], run_id="run_1")
    item = next(i for i in request.items if i.kind == review.KIND_UNFAMILIAR)
    assert item.blocking is False
    assert request.has_blocking is True  # Facility_ID is optimiser-bound.
    assert item.context["raw_preserved"] is True


def test_supplementary_choice_is_remembered_per_client(tmp_storage):
    catalog_a = FieldCatalog(tmp_storage, "client_a")
    mapping = build_mapping(
        None, _record_set(), _classification(),
        memory=FieldMemory(tmp_storage, "client_a"), catalog=catalog_a,
    )
    request = review.build_request([mapping])
    item = next(i for i in request.items if i.kind == review.KIND_UNFAMILIAR)
    outcome = review.apply(
        request,
        [{"item_id": item.item_id,
          "value": review.KEEP_SUPPLEMENTARY,
          "definition": "Number of usable dock doors",
          "decided_by": "consultant"}],
        [mapping], FieldMemory(tmp_storage, "client_a"), catalog_a,
    )
    assert outcome.rejected == []

    again = build_mapping(
        None, _record_set(), _classification(),
        memory=FieldMemory(tmp_storage, "client_a"), catalog=catalog_a,
    )
    dock = next(d for d in again.decisions if d.source_column == "Dock Door Count")
    assert dock.disposition == FieldDisposition.SUPPLEMENTARY
    assert not dock.is_unfamiliar

    other_client = build_mapping(
        None, _record_set(), _classification(),
        memory=FieldMemory(tmp_storage, "client_b"),
        catalog=FieldCatalog(tmp_storage, "client_b"),
    )
    other_dock = next(d for d in other_client.decisions
                      if d.source_column == "Dock Door Count")
    assert other_dock.is_unfamiliar


class _VerboseClient:
    stub_mode = False

    def extract_json(self, **_kwargs):
        return LLMResponse(
            data={
                "recommendation": "made_up_milp_variable",
                "reason": " ".join(["verbose"] * 80),
                "question": " ".join(["question"] * 40),
                "missing_information": ["unit", "period", "definition"],
            },
            stubbed=False,
            model="fake",
        )


def test_ai_clarification_is_whitelisted_and_word_bounded(tmp_storage):
    mapping = build_mapping(
        None, _record_set(), _classification(),
        memory=FieldMemory(tmp_storage),
        catalog=FieldCatalog(tmp_storage),
    )
    item = next(i for i in review.build_request([mapping]).items
                if i.kind == review.KIND_UNFAMILIAR)
    suggestion = analyse(_VerboseClient(), item, "What could this mean?")

    assert suggestion.recommendation == review.KEEP_UNRESOLVED
    assert suggestion.valid is False
    assert len(suggestion.reason.split()) <= MAX_REASON_WORDS
    assert len(suggestion.question.split()) <= MAX_QUESTION_WORDS
    assert len(suggestion.display.split()) <= MAX_DISPLAY_WORDS


def test_service_resumes_after_stubbed_review_and_finalizes(sample_dir, tmp_config):
    service = IngestionService(tmp_config)
    session = service.start(sample_dir, client_id="case_comp")
    assert session.status == "AWAITING_REVIEW"
    assert session.review["has_blocking"] is True
    assert session.draft["safe_for_optimization"] is False

    decisions = [
        {
            "item_id": item["item_id"],
            "value": item["proposed_value"],
            "decided_by": "unit-test-consultant",
        }
        for item in session.review["items"]
        if item["blocking"] and item["proposed_value"]
    ]
    answered = service.answer(
        session.run_id, decisions, expected_revision=session.revision)
    refreshed = service.get(session.run_id)

    assert answered["outcome"]["rejected"] == []
    assert refreshed.review["has_blocking"] is False
    assert refreshed.status == "PROVISIONAL_READY"

    final = service.finalize(
        refreshed.run_id, expected_revision=refreshed.revision)
    assert final.status == "READY"
    assert final.snapshot_path


def test_asking_ai_does_not_apply_or_advance_the_run(sample_dir, tmp_config):
    service = IngestionService(tmp_config)
    session = service.start(sample_dir, client_id="case_comp")
    item = next(item for item in session.review["items"] if item["blocking"])

    suggestion = service.analyse_item(
        session.run_id, item["item_id"], "What does this column mean?")
    unchanged = service.get(session.run_id)

    assert suggestion["requires_confirmation"] is True
    assert suggestion["suggestion"]["stubbed"] is True
    assert len(suggestion["suggestion"]["display"].split()) <= MAX_DISPLAY_WORDS
    assert unchanged.revision == session.revision
    assert unchanged.review["has_blocking"] is True


def test_content_type_override_remaps_against_the_chosen_schema(tmp_path,
                                                                tmp_config,
                                                                tmp_storage):
    path = tmp_path / "ambiguous.csv"
    path.write_text(
        "Facility_ID,Facility_Name,Type\nDC_1,Delhi,DC\n", encoding="utf-8")
    from netgravity.ingestion.tabular import ingest_tabular

    result = ingest_tabular(
        path, tmp_config, tmp_storage,
        content_type_overrides={"ambiguous.csv": "FACILITY"},
    )
    mapping = result.mappings[0]
    assert mapping.content_type == ContentType.FACILITY
    assert mapping.classification.proposed_by == "human:content-type-override"
    assert all(decision.target_field in {
        "facility_id", "facility_name", "role"
    } for decision in mapping.decisions)


def test_auto_observations_do_not_generalize_to_another_sender(tmp_storage):
    memory = FieldMemory(tmp_storage, "client_a")
    for sender in ("vendor_a", "vendor_b"):
        memory.record(
            source_column="Qty", target_field="quantity",
            content_type="SHIPMENT_LOG", source_id=sender,
            confirmed_by="auto",
        )
    resolution = memory.resolve(
        source_column="Qty", content_type="SHIPMENT_LOG", source_id="vendor_c")
    assert resolution.is_known is False


def test_confirmed_unit_and_period_survive_the_next_run(tmp_storage):
    record_set = RecordSet(
        key="sites.csv",
        columns=["Capacity_Units"],
        rows=[{"Capacity_Units": 500}],
        origin=RecordOrigin(source_id="client_a", container="sites.csv"),
    )
    memory = FieldMemory(tmp_storage, "client_a")
    mapping = build_mapping(None, record_set, _classification(), memory=memory)
    item = review.build_request([mapping]).items[0]
    outcome = review.apply(
        review.build_request([mapping]),
        [{"item_id": item.item_id,
          "value": "capacity_units_per_period",
          "unit": "units",
          "period": "month",
          "definition": "Usable monthly throughput"}],
        [mapping], memory,
    )
    assert not outcome.rejected

    again = build_mapping(None, record_set, _classification(), memory=memory)
    decision = again.decisions[0]
    assert decision.source_unit == "units"
    assert decision.confirmed_period == "MONTH"
    assert decision.user_definition == "Usable monthly throughput"


def test_unapproved_unit_is_rejected_for_a_canonical_field(tmp_storage):
    record_set = RecordSet(
        key="sites.csv",
        columns=["Capacity_Units"],
        rows=[{"Capacity_Units": 500}],
        origin=RecordOrigin(source_id="client_a", container="sites.csv"),
    )
    mapping = build_mapping(
        None, record_set, _classification(), memory=FieldMemory(tmp_storage))
    request = review.build_request([mapping])
    outcome = review.apply(
        request,
        [{"item_id": request.items[0].item_id,
          "value": "capacity_units_per_period",
          "unit": "mystery-crates"}],
        [mapping], FieldMemory(tmp_storage),
    )
    assert outcome.rejected
    assert mapping.decisions[0].needs_review is True
