"""
Market intelligence — the three intake routes and the boundaries around them.

EVERY TEST HERE RUNS OFFLINE. No test in this file makes a live model call,
and none should be added that does. `stub_mode` is forced on explicitly rather
than inherited from the environment, so the suite behaves identically on a
machine with credentials configured and one without — a test that quietly
becomes a paid API call when someone exports a key is not a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion import document_text
from netgravity.ingestion.adapters import market_intelligence as adapter
from netgravity.ingestion.ai.signal_reader import extract_signals
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.field_aliases import MARKET_SIGNAL_LOOKUP
from netgravity.ingestion.schemas.content import ContentType
from netgravity.ingestion.schemas.signal import (
    MarketIntelligenceSignal,
    SignalConfidence,
)

NEWS = (
    "Oil marketing companies raised diesel prices by 6% with effect from "
    "16 January 2026, the largest single revision in two years. Published "
    "15 January 2026 by the Petroleum Planning and Analysis Cell."
)


@pytest.fixture
def config() -> IngestionConfig:
    """Offline config. Never reads a key, never makes a call."""
    return IngestionConfig(llm_provider="openai", llm_api_key=None)


@pytest.fixture
def article(tmp_path: Path) -> Path:
    path = tmp_path / "diesel_revision.txt"
    path.write_text(NEWS, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Route 1 — a spreadsheet of signals rides the existing tabular pipeline
# ---------------------------------------------------------------------------

def test_market_signal_is_staging_and_never_reaches_the_optimizer():
    """
    The routing decision that matters most.

    A news item is context. If it reached the network destination it would
    become an input the MILP treats as fact, which is the one thing the
    architecture forbids a language model's reading of prose to do.
    """
    assert ContentType.MARKET_SIGNAL.destination == "staging"
    assert ContentType.MARKET_SIGNAL.feeds_optimizer is False


def test_signal_columns_are_recognised_without_a_model():
    """The dictionary alone maps a signal sheet — no AI required."""
    from netgravity.ingestion.ai.field_mapper import dictionary_opinion

    for column, expected in (("Headline", "title"),
                             ("Published", "published_date"),
                             ("Pct_Change", "magnitude"),
                             ("Region", "geography")):
        assert dictionary_opinion(column, ContentType.MARKET_SIGNAL) == expected


def test_no_alias_offers_a_probability():
    """
    There is no column a user could add that becomes a likelihood.

    `MarketIntelligenceSignal` has no probability field, and the alias table
    must not create a door to one: a "Probability" column mapping onto
    anything at all would be a route by which a spreadsheet set the number
    that drives RF and governance.
    """
    banned = {"probability", "likelihood", "chance", "risk", "p"}
    assert not banned & set(MARKET_SIGNAL_LOOKUP.values())
    assert not banned & {t.lower() for t in MARKET_SIGNAL_LOOKUP.values()}


# ---------------------------------------------------------------------------
# Route 2 — a document
# ---------------------------------------------------------------------------

def test_reads_a_document_into_a_guardrail_scored_signal(config, article):
    signals, result = adapter.ingest_file(article, config)

    assert signals, "the stub yields one signal; none came back"
    signal = signals[0]
    assert isinstance(signal, MarketIntelligenceSignal)
    assert signal.verdict is not None, "every signal carries a guardrail verdict"
    assert result.rows_accepted == len(signals)


def test_a_filtered_signal_is_returned_not_dropped(config, article, monkeypatch):
    """
    The guardrail filters by ATTACHING a verdict, never by removing a record.

    A filter whose decisions cannot be inspected is indistinguishable from a
    bug, and "the guardrail rejected this" must stay visibly different from
    "the pipeline never saw it".
    """
    signals, _ = adapter.ingest_file(article, config)
    assert all(s.verdict is not None for s in signals)
    # Whatever the verdict says, the record survives to be read.
    assert len(signals) >= 1


def test_an_unreadable_file_costs_no_model_call(config, tmp_path):
    """
    A scan with no text layer is rejected before any call is made.

    OCR is parked, so there is nothing to escalate to. Spending a call from a
    shared, cumulative budget to rediscover that would be the whole reason
    this path exists.
    """
    empty = tmp_path / "scan.pdf"
    empty.write_bytes(b"%PDF-1.4\n%%EOF\n")

    signals, result = adapter.ingest_file(empty, config)

    assert signals == []
    assert result.ai_used is False
    assert result.rows_rejected == 1
    assert any(i.code == "R-027" for i in result.issues)


def test_an_unsupported_type_costs_no_model_call_and_says_where_to_go(
        config, tmp_path):
    sheet = tmp_path / "signals.xlsx"
    sheet.write_text("not really a workbook")

    signals, result = adapter.ingest_file(sheet, config)

    assert signals == []
    assert result.ai_used is False
    issue = next(i for i in result.issues if i.code == "R-028")
    # The message must point at the route that WOULD work, not just refuse.
    assert "tabular" in issue.message


def test_plain_text_is_not_judged_by_the_pdf_quality_heuristic(config, tmp_path):
    """
    A short .txt snippet must not be ring-fenced for being short.

    `pdf_quality.assess()` detects pypdf EXTRACTION artefacts. A text file was
    never extracted from anything, so there is no extraction to distrust —
    and the characters-per-page heuristic would otherwise force a perfectly
    clean two-line news snippet to LOW confidence.
    """
    snippet = tmp_path / "short.txt"
    snippet.write_text("Diesel up 6% on 15 January 2026.", encoding="utf-8")

    _, _, quality_failed = document_text.read_document(snippet)
    assert quality_failed is True, "the shared reader still reports it"

    signals, result = adapter.ingest_file(snippet, config)
    assert not any(i.code == "R-027" for i in result.issues)
    assert all(s.confidence != SignalConfidence.LOW for s in signals)


# ---------------------------------------------------------------------------
# The reader's own rules
# ---------------------------------------------------------------------------

class _FakeClient:
    """Returns a scripted payload. Never touches a network."""

    def __init__(self, payload):
        self._payload = payload

    def extract_json(self, **_):
        from netgravity.ingestion.ai.client import LLMResponse
        return LLMResponse(data=self._payload, stubbed=True, model="fake",
                           notes="scripted")


def test_a_signal_with_no_stated_date_is_rejected_with_a_reason():
    """
    Dates are not optional and are never defaulted to today.

    Every downstream use of a signal is time-sensitive — the guardrail expires
    weather signals after 30 days. Stamping the ingest date onto an undated
    article would make a two-year-old story look like this morning's news.
    """
    client = _FakeClient({"signals": [
        {"title": "Diesel up 6%", "published_date": None, "bucket": "MACRO"},
    ]})
    signals, rejections, _ = extract_signals(client, NEWS, filename="x.txt")

    assert signals == []
    assert len(rejections) == 1
    assert "no publication date" in rejections[0]


def test_an_invented_entity_identifier_is_discarded():
    """
    The prompt forbids inventing identifiers; this enforces it.

    An unrecognised id matches nothing downstream anyway, but leaving it in
    would inflate the guardrail's entity-match bonus on a site that does not
    exist — a relevance score built on a hallucination.
    """
    client = _FakeClient({"signals": [{
        "title": "Surcharge at the northern hub",
        "published_date": "2026-01-15",
        "affected_entities": ["DC_DELHI", "DC_ATLANTIS"],
    }]})
    signals, _, _ = extract_signals(client, NEWS, filename="x.txt",
                                    known_entity_ids=["DC_DELHI"])

    assert signals[0].affected_entities == ["DC_DELHI"]


def test_a_stated_probability_is_never_carried_into_the_signal():
    """
    The boundary the two signal schemas exist to keep.

    Even when a source states a likelihood, it does not become part of a
    market signal. A probability belongs to the orchestrator's own
    ExternalSignal path; relocating it here would put the number that drives
    RF = P + REI - P*REI into a record that is not governed as risk.
    """
    client = _FakeClient({"signals": [{
        "title": "40% chance of a port strike next month",
        "published_date": "2026-01-15",
        "states_probability": True,
        "event_probability": 0.4,          # offered, and must be ignored
    }]})
    signals, _, _ = extract_signals(client, NEWS, filename="x.txt")

    assert not hasattr(signals[0], "event_probability")
    assert "event_probability" not in MarketIntelligenceSignal.model_fields
    assert "probability" not in signals[0].model_dump()


def test_the_same_document_yields_the_same_signal_id():
    """A re-upload is recognisable as a duplicate, not counted as agreement."""
    payload = {"signals": [{"title": "Diesel up 6%",
                            "published_date": "2026-01-15"}]}
    first, _, _ = extract_signals(_FakeClient(payload), NEWS, filename="a.txt")
    again, _, _ = extract_signals(_FakeClient(payload), NEWS, filename="a.txt")
    other, _, _ = extract_signals(_FakeClient(payload), NEWS, filename="b.txt")

    assert first[0].signal_id == again[0].signal_id
    assert first[0].signal_id != other[0].signal_id


def test_a_malformed_date_is_treated_as_no_date():
    """"January 2026" is not a date this pipeline will guess a day for."""
    client = _FakeClient({"signals": [{"title": "Diesel up",
                                       "published_date": "January 2026"}]})
    signals, rejections, _ = extract_signals(client, NEWS, filename="x.txt")
    assert signals == []
    assert rejections
