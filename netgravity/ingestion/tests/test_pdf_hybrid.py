"""
Hybrid PDF extraction tests.

The rule: try the free pypdf extraction first, judge whether its output can
be TRUSTED, and only hand the document to the model when it cannot. No live
API calls — the client is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.adapters import contracts as adapter
from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMResponse
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.memory import DocumentMemory
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

def test_a_clean_pdf_never_escalates(live_config, patch_client):
    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(GOOD_PDF, live_config, None, None,
                                       use_cache=False)
    assert rule is not None
    assert client.text_calls == 1
    assert client.pdf_calls == 0, "a readable PDF must not cost a document read"
    assert not any(i.code == "R-023" for i in result.issues)


def test_read_contract_reports_clean_text_as_trustworthy():
    text, warning, needs_model = adapter.read_contract(GOOD_PDF)
    assert warning is None
    assert needs_model is False
    assert "TRANSCORP" in text


# --- escalation -------------------------------------------------------------

def test_a_scan_with_no_text_layer_escalates(tmp_path, live_config, patch_client):
    """
    Previously an automatic rejection: the file's contents never reached the
    network at all.
    """
    path = _blank_pdf(tmp_path / "scan.pdf")
    _, _, needs_model = adapter.read_contract(path)
    assert needs_model is True

    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)
    assert client.pdf_calls == 1
    assert rule is not None
    assert rule.vendor_name == "TransCorp Logistics"
    assert result.rows_accepted == 1
    assert any(i.code == "R-023" for i in result.issues)


def test_garbled_text_escalates_rather_than_being_trusted(tmp_path, live_config,
                                                          patch_client):
    """
    The dangerous case. The text layer returns something that looks like
    text; trusting it would produce confident figures never in the document.
    """
    path = _garbled_pdf(tmp_path / "garbled.pdf")
    text, warning, needs_model = adapter.read_contract(path)
    assert text, "this PDF does have a text layer"
    assert needs_model is True
    assert warning and "quality checks" in warning

    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)
    assert client.pdf_calls == 1
    assert client.text_calls == 0, "garbled text must not also be sent as text"
    assert rule is not None


def test_the_actual_document_bytes_are_sent(tmp_path, live_config, patch_client):
    path = _blank_pdf(tmp_path / "scan.pdf")
    client = patch_client(_FakeClient())
    adapter.ingest_file(path, live_config, None, None, use_cache=False)
    assert client.pdf_bytes_seen == path.read_bytes()


# --- failure must not become invention --------------------------------------

def test_a_failed_document_read_rejects_rather_than_inventing(tmp_path,
                                                              live_config,
                                                              patch_client):
    """
    A provider that cannot take a document is a real, reportable outcome.
    Passing stub figures off as a reading of an unreadable scan would be the
    worst possible result.
    """
    path = _blank_pdf(tmp_path / "scan.pdf")
    patch_client(_FakeClient(pdf_fails=True))
    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)
    assert rule is None
    assert result.rows_rejected == 1
    assert result.ai_failed is True
    assert any(LLM_FAILURE_MARKER in n for n in result.ai_notes)


def test_without_a_key_a_scan_is_rejected_not_escalated(tmp_path, patch_client):
    """No key means no escalation is possible — say so, do not pretend."""
    config = IngestionConfig()
    config.llm_api_key = None
    client = patch_client(_FakeClient(stub_mode=True))
    path = _blank_pdf(tmp_path / "scan.pdf")

    rule, result = adapter.ingest_file(path, config, None, None, use_cache=False)
    assert client.pdf_calls == 0
    assert rule is None
    assert result.rows_rejected == 1


def test_an_unsupported_file_type_is_not_escalated(tmp_path, live_config,
                                                   patch_client):
    path = tmp_path / "rate_card.docx"
    path.write_text("not a pdf")
    client = patch_client(_FakeClient())
    rule, result = adapter.ingest_file(path, live_config, None, None,
                                       use_cache=False)
    assert client.pdf_calls == 0
    assert rule is None


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
                                                       patch_client, monkeypatch):
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
