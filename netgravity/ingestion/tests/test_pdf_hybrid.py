"""
PDF extraction tests.

ONE ROUTE: pypdf extracts the text, the text goes to the model.

  - clean text        -> extracted normally
  - poor-quality text -> still sent, but ring-fenced: LOW confidence, R-027,
                         never cached, never learned as a document shape
  - no text at all    -> rejected as unreadable (R-027). No second attempt.

Sending the PDF file itself to the model was REMOVED (2026-08-21): the
configured provider rejects document parts outright, so it could only ever
spend an API call to rediscover the same 400. No live API calls here either
— the client is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.adapters import contracts as adapter
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMResponse
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.memory import DocumentMemory
from netgravity.ingestion.schemas.contract import ExtractionConfidence
from netgravity.ingestion.storage.local import LocalStorage

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "contracts"
GOOD_PDF = CONTRACT_DIR / "pdf_samples" / "transcorp_rate_card.pdf"

_EXTRACTION = {
    "vendor_name": "TransCorp Logistics", "contract_id": "TC-1",
    "base_rate": 10.0, "rate_unit": "INR/kg",
    "surcharges": [{"surcharge_type": "FUEL", "rate": 2.0,
                    "applies_to_location_ids": [], "applies_to_pin_codes": []}],
}


class _FakeClient:
    """Records which route was taken: text extraction or document read."""

    def __init__(self, *, stub_mode=False, pdf_fails=False):
        self.stub_mode = stub_mode
        self._pdf_fails = pdf_fails
        self.text_calls = 0
        self.pdf_calls = 0
        self.pdf_bytes_seen = None

    def extract_json(self, **kwargs):
        self.text_calls += 1
        if self.stub_mode:
            # Mirror the real client: no key means canned data, labelled as
            # such. A fake that returns live-looking output in stub mode
            # would silently skip every guard that keys off provenance.
            return LLMResponse(data=dict(_EXTRACTION), stubbed=True,
                               model="stub", notes="stubbed (no API key)")
        return LLMResponse(data=dict(_EXTRACTION), stubbed=False,
                           model="fake:model", notes="live extraction")

    def extract_json_from_pdf(self, *, task, prompt, pdf_bytes, filename,
                              stub_key, stub_context=None, max_tokens=2500):
        self.pdf_calls += 1
        self.pdf_bytes_seen = pdf_bytes
        if self._pdf_fails:
            return LLMResponse(data={}, stubbed=True, model="stub", failed=True,
                               notes=f"{task}: {LLM_FAILURE_MARKER} (provider "
                                     f"does not support document input)")
        return LLMResponse(data=dict(_EXTRACTION), stubbed=False,
                           model="fake:model", notes="live document read")


@pytest.fixture
def live_config():
    config = IngestionConfig()
    config.llm_api_key = "test-key"        # not stub mode
    return config


@pytest.fixture
def patch_client(monkeypatch):
    def _install(client):
        monkeypatch.setattr(adapter, "get_client", lambda config: client)
        return client
    return _install


def _blank_pdf(path):
    """A scan: pages exist, no text layer."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def _garbled_pdf(path):
    """A text layer that exists but is corrupt — the QUIET failure."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 10)
    y = 800
    for _ in range(12):
        c.drawString(40, y, "x" * 90)
        y -= 14
    c.save()
    return path


# --- the cheap path is tried first -----------------------------------------

def test_a_clean_pdf_takes_the_text_path(live_config, patch_client):
    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(GOOD_PDF, live_config, None, None,
                                       use_cache=False)
    assert rule is not None
    assert client.text_calls == 1
    assert client.pdf_calls == 0, "the document route no longer exists"
    assert not any(i.code == "R-027" for i in result.issues)


def test_read_contract_reports_clean_text_as_trustworthy():
    text, warning, quality_failed = adapter.read_contract(GOOD_PDF)
    assert warning is None
    assert quality_failed is False
    assert "TRANSCORP" in text


# --- poor-quality text is used, but ring-fenced ----------------------------

def test_garbled_text_is_still_sent_to_the_model(tmp_path, live_config,
                                                 patch_client):
    """
    Imperfect text is all we have, and a corrupt text layer is often only
    partly corrupt. Throwing it away guarantees nothing; sending it might
    recover something.
    """
    path = _garbled_pdf(tmp_path / "garbled.pdf")
    _, _, quality_failed = adapter.read_contract(path)
    assert quality_failed is True

    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)

    assert client.text_calls == 1
    assert client.pdf_calls == 0, "the document route no longer exists"
    assert rule is not None
    assert result.rows_accepted == 1
    assert result.rows_rejected == 0


def test_a_degraded_read_is_forced_to_low_confidence(tmp_path, live_config,
                                                     patch_client):
    """
    The model scores its certainty from the text it was handed. It has no way
    to know that text had already been judged untrustworthy, so the pipeline
    applies that judgement itself — on the rule AND on every surcharge.
    """
    path = _garbled_pdf(tmp_path / "garbled.pdf")
    patch_client(_FakeClient())

    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)

    assert rule.extraction_confidence == ExtractionConfidence.LOW
    assert all(s.confidence == ExtractionConfidence.LOW
               for s in rule.surcharges), \
        "a surcharge read from unreliable text is not MEDIUM confidence"
    assert any(i.code == "R-027" for i in result.issues)


def test_a_clean_read_keeps_the_confidence_the_model_reported(live_config,
                                                              patch_client):
    """The LOW override must apply ONLY to degraded reads."""
    patch_client(_FakeClient())
    rule, result = adapter.ingest_file(GOOD_PDF, live_config, None, None,
                                       use_cache=False)
    assert rule.extraction_confidence != ExtractionConfidence.LOW
    assert not any(i.code == "R-027" for i in result.issues)


def test_a_degraded_read_never_enters_document_memory(tmp_path, live_config,
                                                      patch_client):
    """
    Learning the wording shape of text we KNOW is corrupt would poison every
    future match against that template.
    """
    storage = LocalStorage(tmp_path / "data")
    path = _garbled_pdf(tmp_path / "garbled.pdf")
    patch_client(_FakeClient())

    rule, _ = adapter.ingest_file(path, live_config, None, storage,
                                  use_cache=False)
    assert rule is not None
    assert DocumentMemory(storage)._all() == []


def test_a_degraded_read_is_never_cached_for_reuse(tmp_path, live_config,
                                                   patch_client):
    """
    Caching it would let one low-confidence read be silently served later as
    though it were a sound extraction.
    """
    from netgravity.ingestion.ai.cache import load_cached_contract

    storage = LocalStorage(tmp_path / "data")
    path = _garbled_pdf(tmp_path / "garbled.pdf")
    patch_client(_FakeClient())

    adapter.ingest_file(path, live_config, None, storage, use_cache=True)

    text, _, _ = adapter.read_contract(path)
    assert load_cached_contract(text, storage) is None


# --- no text at all ---------------------------------------------------------

def test_a_scan_is_rejected_and_names_ocr_as_the_missing_capability(
        tmp_path, live_config, patch_client):
    """
    OCR is PARKED, not built. The rejection message is often the only thing a
    user sees, so it has to say WHY — otherwise a deliberate gap reads as a
    bug.
    """
    path = _blank_pdf(tmp_path / "scan.pdf")
    client = patch_client(_FakeClient())

    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)

    assert rule is None
    assert result.rows_rejected == 1
    reasons = [i.message for i in result.issues if i.code == "R-027"]
    assert reasons, "a rejected unreadable file must carry an R-027 reason"
    assert "OCR" in reasons[0]


def test_an_unreadable_file_costs_no_api_call(tmp_path, live_config,
                                              patch_client):
    """
    THE POINT OF THE SIMPLIFICATION. A file we cannot turn into text used to
    burn an API call proving the provider would not read it either. It must
    now cost nothing at all.
    """
    path = _blank_pdf(tmp_path / "scan.pdf")
    client = patch_client(_FakeClient())

    adapter.ingest_file(path, live_config, None, None, use_cache=False)

    assert client.text_calls == 0
    assert client.pdf_calls == 0


def test_an_unsupported_file_type_costs_no_api_call(tmp_path, live_config,
                                                    patch_client):
    path = tmp_path / "rate_card.docx"
    path.write_text("not a pdf")
    client = patch_client(_FakeClient())

    rule, _ = adapter.ingest_file(path, live_config, None, None,
                                  use_cache=False)

    assert rule is None
    assert client.text_calls == 0
    assert client.pdf_calls == 0


def test_without_a_key_a_scan_is_still_rejected(tmp_path, patch_client):
    config = IngestionConfig()
    config.llm_api_key = None
    client = patch_client(_FakeClient(stub_mode=True))
    path = _blank_pdf(tmp_path / "scan.pdf")

    rule, result = adapter.ingest_file(path, config, None, None,
                                       use_cache=False)
    assert rule is None
    assert result.rows_rejected == 1
    assert client.text_calls == 0


# --- document shape memory --------------------------------------------------

def test_a_successful_extraction_records_the_document_shape(tmp_path, live_config,
                                                            patch_client):
    storage = LocalStorage(tmp_path)
    patch_client(_FakeClient())
    adapter.ingest_file(CONTRACT_DIR / "transcorp_rate_card.txt", live_config,
                        None, storage, use_cache=False)

    patterns = DocumentMemory(storage)._all()
    assert len(patterns) == 1
    assert patterns[0].observed_labels.get("vendor") == "TransCorp Logistics"


def test_a_renewal_is_recognised_as_a_known_template(tmp_path, live_config,
                                                     patch_client):
    """
    What the exact-text cache cannot do: the bytes changed, so the cache
    misses, but the template is clearly the same one.
    """
    storage = LocalStorage(tmp_path)
    patch_client(_FakeClient())

    original = CONTRACT_DIR / "transcorp_rate_card.txt"
    adapter.ingest_file(original, live_config, None, storage, use_cache=False)

    renewal = tmp_path / "transcorp_2027.txt"
    renewal.write_text(original.read_text(encoding="utf-8")
                       .replace("10.00", "11.50").replace("TC-2026-0472",
                                                          "TC-2027-0913"),
                       encoding="utf-8")

    _, result = adapter.ingest_file(renewal, live_config, None, storage,
                                    use_cache=False)
    assert any("shape recognised" in n for n in result.ai_notes)


def test_stub_extractions_never_pollute_document_memory(tmp_path, patch_client):
    config = IngestionConfig()
    config.llm_api_key = None
    storage = LocalStorage(tmp_path)
    patch_client(_FakeClient(stub_mode=True))

    adapter.ingest_file(CONTRACT_DIR / "transcorp_rate_card.txt", config, None,
                        storage, use_cache=False)
    assert DocumentMemory(storage)._all() == []


def test_memory_failure_never_breaks_a_good_extraction(tmp_path, live_config,
                                                       patch_client):
    """Memory is an optimisation, not a dependency."""
    class _BrokenStorage(LocalStorage):
        def list(self, zone, prefix=""):
            raise RuntimeError("storage is down")

        def save_text(self, zone, key, body):
            raise RuntimeError("storage is down")

    patch_client(_FakeClient())
    rule, result = adapter.ingest_file(CONTRACT_DIR / "transcorp_rate_card.txt",
                                       live_config, None,
                                       _BrokenStorage(tmp_path), use_cache=False)
    assert rule is not None
    assert result.rows_accepted == 1


# --- page counting ----------------------------------------------------------

def test_page_count_scales_the_emptiness_check():
    assert adapter.page_count(GOOD_PDF) >= 1
    assert adapter.page_count(CONTRACT_DIR / "transcorp_rate_card.txt") == 1


def test_page_count_of_a_broken_pdf_falls_back_to_one(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 truncated garbage")
    assert adapter.page_count(path) == 1
