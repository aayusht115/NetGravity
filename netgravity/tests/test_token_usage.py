"""
Token usage ledger tests.

The ledger is accounting, and accounting has exactly two ways to be worse
than useless: reporting a confident wrong number, or breaking the work it
was only supposed to observe. Most of these tests pin those two properties.
"""

from __future__ import annotations

import pytest

from netgravity.telemetry import (
    TokenUsage,
    UsageLedger,
    estimate_cost_usd,
    record_call,
)


@pytest.fixture
def book():
    """An isolated ledger — never the process-wide one, which other tests share."""
    return UsageLedger()


# --- counting ---------------------------------------------------------------

def test_live_calls_accumulate_tokens(book):
    record_call(task="classify", model="openai:gpt-5-mini",
                usage={"prompt_tokens": 100, "completion_tokens": 50,
                       "total_tokens": 150}, into=book)
    record_call(task="classify", model="openai:gpt-5-mini",
                usage={"prompt_tokens": 200, "completion_tokens": 40,
                       "total_tokens": 240}, into=book)
    assert book.total_tokens == 390
    assert len(book.live_records) == 2


def test_stub_calls_are_counted_but_never_priced(book):
    """A stub costs nothing. Letting it reach the pricing table would invent spend."""
    record_call(task="classify", model="stub", stubbed=True,
                usage={"prompt_tokens": 100, "completion_tokens": 50}, into=book)
    assert len(book.records) == 1
    assert book.live_records == []
    assert book.total_tokens == 0
    assert book.total_cost_usd is None
    assert book.records[0].cost_usd is None


def test_failed_calls_are_tracked_separately(book):
    """
    A failed live call may still have been billed. Folding it in with stubs
    would let a run of failures read as a cheap run.
    """
    record_call(task="contract", model="openai:gpt-5-mini", failed=True,
                usage={"prompt_tokens": 900, "completion_tokens": 0,
                       "total_tokens": 900}, into=book)
    assert book.as_dict()["calls_failed"] == 1
    assert book.total_tokens == 900, "a failed call still consumed input tokens"
    assert "may still have been billed" in book.summary()


# --- pricing ----------------------------------------------------------------

def test_a_known_model_is_priced_from_the_table():
    # gpt-5-mini: $0.25 per 1M in, $2.00 per 1M out.
    cost = estimate_cost_usd("gpt-5-mini", 1_000_000, 1_000_000)
    assert cost == pytest.approx(2.25)


def test_a_provider_prefixed_model_name_still_matches():
    """Real model strings arrive decorated: 'openai:gpt-5-mini'."""
    assert estimate_cost_usd("openai:gpt-5-mini", 1_000_000, 0) == \
        pytest.approx(0.25)


def test_an_unknown_model_returns_none_rather_than_guessing(book):
    """
    None renders as 'cost unknown'. Zero would render as free, which is a
    confident wrong number — the exact failure this codebase avoids.
    """
    assert estimate_cost_usd("some-model-nobody-configured", 1000, 1000) is None

    record_call(task="classify", model="mystery-model",
                usage={"prompt_tokens": 10, "completion_tokens": 10}, into=book)
    assert book.total_cost_usd is None
    assert "unknown" in book.summary()


def test_a_partial_total_says_that_it_is_partial(book):
    """Some calls priced, some not — the total must not pose as complete."""
    record_call(task="a", model="gpt-5-mini",
                usage={"prompt_tokens": 1_000_000, "completion_tokens": 0},
                into=book)
    record_call(task="b", model="unpriced-model",
                usage={"prompt_tokens": 5000, "completion_tokens": 5000},
                into=book)
    assert book.total_cost_usd == pytest.approx(0.25)
    assert book.unpriced_call_count == 1
    assert "PARTIAL" in book.summary()


def test_prices_can_be_overridden_by_environment(monkeypatch):
    """Provider prices change; a code edit must not be the only way to react."""
    monkeypatch.setenv("NETGRAVITY_TOKEN_PRICES", "housemodel:1.0:10.0")
    assert estimate_cost_usd("housemodel", 1_000_000, 1_000_000) == \
        pytest.approx(11.0)


def test_a_malformed_price_override_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("NETGRAVITY_TOKEN_PRICES", "broken,also:bad:xyz")
    assert estimate_cost_usd("gpt-5-mini", 1_000_000, 0) == pytest.approx(0.25)


# --- robustness -------------------------------------------------------------

def test_recording_never_raises_on_a_malformed_payload(book):
    """Accounting must not be able to break the work it measures."""
    record_call(task="t", model="m", usage={"prompt_tokens": "not-a-number"},
                into=book)
    assert len(book.records) == 1
    assert book.records[0].usage.prompt_tokens is None


def test_a_missing_total_is_derived_from_the_parts():
    """Providers differ: some report only the parts, some only the total."""
    usage = TokenUsage.from_mapping({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.total_tokens == 15


def test_an_absent_usage_payload_is_not_an_error(book):
    record_call(task="t", model="m", usage=None, into=book)
    assert book.records[0].usage.total_tokens is None
    assert book.total_tokens == 0


# --- reporting --------------------------------------------------------------

def test_by_task_shows_where_the_spend_went(book):
    record_call(task="contract extraction", model="gpt-5-mini",
                usage={"prompt_tokens": 900_000, "completion_tokens": 0},
                into=book)
    record_call(task="column mapping", model="gpt-5-mini",
                usage={"prompt_tokens": 1000, "completion_tokens": 0},
                into=book)
    grouped = book.by_task()
    assert grouped["contract extraction"]["tokens"] == 900_000
    assert grouped["column mapping"]["calls"] == 1
    assert "contract extraction" in book.summary()


def test_an_empty_ledger_says_so_plainly(book):
    assert "no model calls" in book.summary()


def test_reset_clears_the_ledger(book):
    record_call(task="t", model="gpt-5-mini",
                usage={"prompt_tokens": 1, "completion_tokens": 1}, into=book)
    book.reset()
    assert book.records == []
    assert book.total_tokens == 0
