"""
Content classification tests.

No live API calls: the LLM client is replaced by a fake that returns whatever
payload a test needs. What is being tested here is the classifier's JUDGEMENT
- how it combines the model's answer with the deterministic rule score, and
what it does when they disagree or when confidence is thin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.ai.classifier import (
    REVIEW_BELOW,
    UNKNOWN_BELOW,
    classify,
    score_by_rules,
)
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMResponse
from netgravity.ingestion.schemas.content import ContentType
from netgravity.ingestion.sources import discover
from netgravity.ingestion.sources.base import RecordOrigin, RecordSet

MOCK_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india"


class _FakeClient:
    """Minimal stand-in. classify() only needs stub_mode and extract_json."""

    def __init__(self, payload=None, *, stub_mode=False, failed=False, notes=""):
        self.stub_mode = stub_mode
        self._payload = payload or {}
        self._failed = failed
        self._notes = notes
        self.prompts = []

    def extract_json(self, *, task, prompt, stub_key, stub_context=None,
                     max_tokens=2000):
        self.prompts.append(prompt)
        return LLMResponse(
            data=dict(self._payload),
            stubbed=self._failed,
            model="fake:model",
            notes=self._notes,
            failed=self._failed,
        )


def _rs(columns, rows=None, sheet="Sheet1"):
    rows = rows if rows is not None else [{c: "x" for c in columns}]
    return RecordSet(key="k", columns=columns, rows=rows,
                     origin=RecordOrigin(container="file.xlsx", sheet=sheet))


def _ai(content_type, confidence, reasoning="because"):
    return {"content_type": content_type, "confidence": confidence,
            "reasoning": reasoning}


# --- the deterministic scorer ----------------------------------------------

def test_rule_scoring_identifies_every_real_mock_file():
    """
    The no-key fallback has to actually work, not just exist. These are the
    real sample files, scored with zero AI involvement.
    """
    expected = {
        "facilities.csv": ContentType.FACILITY,
        "markets.csv": ContentType.MARKET,
        "products.csv": ContentType.PRODUCT,
        "demand.csv": ContentType.DEMAND,
        "lanes.csv": ContentType.LANE,
        "historical_volume.csv": ContentType.HISTORICAL_VOLUME,
    }
    seen = {}
    for source in discover(MOCK_DIR):
        for record_set in source.record_sets():
            content_type, score, _ = score_by_rules(record_set.columns)
            seen[record_set.origin.container] = (content_type, score)

    for filename, want in expected.items():
        got, score = seen[filename]
        assert got == want, f"{filename}: got {got}, expected {want}"
        assert score >= 0.5


def test_a_messy_distributor_sheet_is_recognised_as_a_shipment_log():
    """
    The case nobody wrote a schema for. No alias table covers it — it is
    recognised by the markers a transaction carries and a master list does
    not: a movement date and a document/vehicle reference.
    """
    content_type, score, _ = score_by_rules(
        ["Location Code", "Qty", "Wt (kgs)", "Rate", "Despatch Dt",
         "Vehicle No", "Remarks"])
    assert content_type == ContentType.SHIPMENT_LOG
    assert score > 0.0


def test_scoring_reports_every_candidate_not_just_the_winner():
    """Ambiguity should be visible, not hidden behind a single answer."""
    _, _, scores = score_by_rules(["Facility_ID", "Capacity_Units"])
    assert set(scores) >= {ct.value for ct in _RULE_COVERED}
    assert scores[ContentType.FACILITY.value] > 0


_RULE_COVERED = {ContentType.FACILITY, ContentType.MARKET, ContentType.DEMAND,
                 ContentType.LANE, ContentType.PRODUCT,
                 ContentType.HISTORICAL_VOLUME}


def test_unrecognisable_columns_score_nothing():
    content_type, score, _ = score_by_rules(["zzz", "qqq", "wibble"])
    assert content_type == ContentType.UNKNOWN
    assert score == 0.0


def test_no_columns_is_not_a_crash():
    assert score_by_rules([]) == (ContentType.UNKNOWN, 0.0, {})


# --- no key: honest degrade -------------------------------------------------

def test_without_a_key_it_uses_rules_and_says_so(): 
    """Item 10 of the plan: degrade to rules, never silently."""
    result = classify(_FakeClient(stub_mode=True),
                      _rs(["Facility_ID", "Capacity_Units", "Fixed_Annual_Cost"]))
    assert result.content_type == ContentType.FACILITY
    assert "no AI key" in result.proposed_by
    assert result.needs_review is True
    assert any("without AI" in r for r in result.review_reasons)


def test_without_a_key_and_no_rule_match_stays_unknown():
    result = classify(_FakeClient(stub_mode=True), _rs(["zzz", "qqq"]))
    assert result.content_type == ContentType.UNKNOWN
    assert result.destination == "hold"


# --- the two opinions -------------------------------------------------------

def test_agreement_between_ai_and_rules_clears_review():
    client = _FakeClient(_ai("FACILITY", 0.88))
    result = classify(client, _rs(["Facility_ID", "Capacity_Units"]))
    assert result.content_type == ContentType.FACILITY
    assert result.rules_agree is True
    assert result.confidence >= 0.90
    assert result.needs_review is False


def test_disagreement_is_flagged_with_both_opinions_named():
    """
    The whole point of running two methods. The rule table sees facility
    columns; the model claims it is a shipment log. Neither is silently
    discarded.
    """
    client = _FakeClient(_ai("SHIPMENT_LOG", 0.95))
    result = classify(client, _rs(["Facility_ID", "Capacity_Units",
                                   "Fixed_Annual_Cost"]))
    assert result.content_type == ContentType.SHIPMENT_LOG
    assert result.rule_type == ContentType.FACILITY
    assert result.rules_agree is False
    assert result.needs_review is True
    reason = " ".join(result.review_reasons)
    assert "FACILITY" in reason and "SHIPMENT_LOG" in reason
    assert "disagreed" in reason


def test_agreement_cannot_be_manufactured_from_a_weak_model_answer():
    """Corroboration lifts confidence to a floor, it does not invent certainty
    beyond what either method actually supports."""
    client = _FakeClient(_ai("FACILITY", 0.55))
    result = classify(client, _rs(["Facility_ID", "Capacity_Units"]))
    assert result.confidence == pytest.approx(0.90)
    assert result.confidence < 1.0


# --- honesty about uncertainty ---------------------------------------------

def test_low_confidence_is_held_as_unknown_not_guessed():
    client = _FakeClient(_ai("LANE", UNKNOWN_BELOW - 0.1))
    result = classify(client, _rs(["mystery_a", "mystery_b"]))
    assert result.content_type == ContentType.UNKNOWN
    assert result.destination == "hold"
    assert any("below" in r and "floor" in r for r in result.review_reasons)


def test_middling_confidence_is_kept_but_sent_for_review():
    client = _FakeClient(_ai("LANE", (UNKNOWN_BELOW + REVIEW_BELOW) / 2))
    result = classify(client, _rs(["mystery_a", "mystery_b"]))
    assert result.content_type == ContentType.LANE
    assert result.needs_review is True


def test_a_model_returning_unknown_is_respected():
    client = _FakeClient(_ai("UNKNOWN", 0.9, "sheet mixes several kinds of data"))
    result = classify(client, _rs(["mystery_a", "mystery_b"]))
    assert result.content_type == ContentType.UNKNOWN
    assert result.needs_review is True


def test_garbage_confidence_values_do_not_crash_or_pass():
    for bad in ("not a number", None, -5, 99):
        client = _FakeClient({"content_type": "LANE", "confidence": bad})
        result = classify(client, _rs(["a", "b"]))
        assert 0.0 <= result.confidence <= 1.0


def test_an_unrecognised_type_name_becomes_unknown_not_an_error():
    client = _FakeClient(_ai("SPACESHIP_MANIFEST", 0.99))
    result = classify(client, _rs(["a", "b"]))
    assert result.content_type == ContentType.UNKNOWN


# --- failure must not look like success ------------------------------------

def test_a_failed_ai_call_falls_back_but_is_never_reported_as_confident():
    client = _FakeClient(_ai("FACILITY", 0.99), failed=True,
                         notes=f"{LLM_FAILURE_MARKER} (timeout)")
    result = classify(client, _rs(["Facility_ID", "Capacity_Units"]))
    assert result.confidence == 0.0
    assert result.needs_review is True
    assert "failed" in result.proposed_by
    assert any(LLM_FAILURE_MARKER in r for r in result.review_reasons)


def test_an_empty_record_set_is_unknown_not_an_exception():
    result = classify(_FakeClient(_ai("FACILITY", 0.99)),
                      RecordSet(key="k", columns=[], rows=[]))
    assert result.content_type == ContentType.UNKNOWN
    assert result.needs_review is True


# --- the prompt actually carries the context we promised -------------------

def test_the_prompt_includes_several_real_rows_not_just_headers():
    """
    Row PATTERN is what separates a product master from a despatch register
    when both have Weight and Quantity columns. One row cannot show it.
    """
    rows = [{"Dest": f"MKT_{i}", "Qty": i * 10} for i in range(8)]
    client = _FakeClient(_ai("SHIPMENT_LOG", 0.95))
    classify(client, _rs(["Dest", "Qty"], rows=rows), sample_limit=5)

    prompt = client.prompts[0]
    assert "MKT_0" in prompt and "MKT_4" in prompt
    assert "MKT_7" not in prompt          # limit respected
    assert "SAMPLE ROWS" in prompt


def test_the_prompt_names_the_sheet_so_a_hint_is_available():
    client = _FakeClient(_ai("LANE", 0.9))
    classify(client, _rs(["a"], sheet="Lane Rates 2026"))
    assert "Lane Rates 2026" in client.prompts[0]


# --- routing ----------------------------------------------------------------

@pytest.mark.parametrize("content_type,destination", [
    (ContentType.FACILITY, "network"),
    (ContentType.LANE, "network"),
    (ContentType.SHIPMENT_LOG, "staging"),
    (ContentType.HISTORICAL_VOLUME, "staging"),
    (ContentType.UNKNOWN, "hold"),
])
def test_destination_follows_content_not_folder(content_type, destination):
    assert content_type.destination == destination
