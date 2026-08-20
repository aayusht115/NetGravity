"""
PDF text-quality check tests.

The failure these defend against is the QUIET one: pypdf returns text, but
the text is garbled, and the pipeline cannot tell it from a real contract —
so the model dutifully "extracts" rates that were never in the document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.pdf_quality import (
    MAX_SINGLE_CHAR_RUN,
    MIN_CHARS_PER_PAGE,
    MIN_TOKENS_FOR_RATIO,
    MIN_WORD_RATIO,
    assess,
)

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "mock" / "india" / "contracts"


# --- real documents must not be flagged ------------------------------------

@pytest.mark.parametrize("name", ["transcorp_rate_card.txt",
                                  "speedfreight_rate_card.txt"])
def test_real_sample_contracts_pass(name):
    text = (CONTRACT_DIR / name).read_text(encoding="utf-8")
    quality = assess(text, page_count=2)
    assert quality.usable, quality.summary


def test_separator_rules_are_not_treated_as_corruption():
    """
    REGRESSION. The first version of the repetition check matched any
    repeated character, so the 64-character '=' rule that opens both sample
    contracts was read as a corrupt text layer — a false positive on our own
    known-good documents. Punctuation runs are ordinary decoration.
    """
    for rule in ("=" * 64, "-" * 70, "." * 40, "_" * 50, " " * 60):
        text = f"{rule}\nMASTER FREIGHT SERVICES AGREEMENT\n{rule}\n" + \
               "The base freight rate shall be Rs. 10.00 per kg. " * 12
        assert assess(text, page_count=1).usable, f"false positive on {rule[0]!r} rule"


def test_a_pin_code_annexure_of_mostly_digits_passes():
    """Annexure C in the TransCorp sample is almost entirely six-digit pin
    codes. Numbers are legitimate document content, not garbage."""
    text = ("ANNEXURE C - NON SERVICEABLE LOCATIONS\n"
            + "\n".join(f"78{i:04d}, 78{i+1:04d}, 78{i+2:04d}" for i in range(1000, 1040)))
    assert assess(text, page_count=1).usable


# --- the three failure signatures -------------------------------------------

def test_near_empty_text_layer_is_caught():
    """A scan carrying only a stamped header — text exists, but is hollow."""
    quality = assess("Page 1 of 12", page_count=12)
    assert not quality.usable
    assert any("characters per page" in r for r in quality.reasons)
    assert quality.metrics["chars_per_page"] < MIN_CHARS_PER_PAGE


def test_mojibake_is_caught_by_the_word_ratio():
    text = "ÿØ¾ ¤¢™ ‡‰Š ‹Œ Ž ‘’ •–— ˜™š ›œ žŸ ¡¢£ ¤¥¦ §¨© ª«¬ ®¯° ±²³ " * 12
    quality = assess(text, page_count=1)
    assert not quality.usable
    assert any("look like words" in r for r in quality.reasons)
    assert quality.metrics["word_ratio"] < MIN_WORD_RATIO


def test_degenerate_letter_run_is_caught():
    text = ("Legitimate contract wording appears here. " * 30
            + "y" * (MAX_SINGLE_CHAR_RUN + 15)
            + " and more legitimate wording follows. " * 20)
    quality = assess(text, page_count=1)
    assert not quality.usable
    assert any("identical characters" in r for r in quality.reasons)


# --- statistical honesty ----------------------------------------------------

def test_very_short_text_is_not_judged_on_ratio():
    """
    Below MIN_TOKENS_FOR_RATIO the ratio is meaningless — a handful of tokens
    can score badly by chance. Short text is judged on emptiness only, so the
    reported reason must never be the ratio one.
    """
    text = "Rs. 10/kg §± ÿ"
    quality = assess(text, page_count=1)
    assert quality.metrics["tokens"] < MIN_TOKENS_FOR_RATIO
    assert not any("look like words" in r for r in quality.reasons)


def test_empty_and_none_text_do_not_crash():
    for value in ("", "   ", None):
        quality = assess(value, page_count=1)
        assert not quality.usable
        assert quality.metrics["characters"] == 0.0


def test_page_count_of_zero_is_treated_as_one():
    """Guards a divide-by-zero on a malformed page count."""
    quality = assess("x" * 500, page_count=0)
    assert quality.metrics["pages"] == 1.0


# --- the verdict object -----------------------------------------------------

def test_metrics_are_reported_so_a_decision_can_be_explained():
    quality = assess("The base freight rate shall be Rs. 10.00 per kg. " * 20, 1)
    assert quality.usable
    assert quality.summary == "extracted text looks usable"
    for key in ("characters", "pages", "chars_per_page", "tokens", "word_ratio"):
        assert key in quality.metrics


def test_multiple_failures_are_all_reported_not_just_the_first():
    """A caller deciding what to do next benefits from the full picture."""
    quality = assess("z" * 40, page_count=5)
    assert len(quality.reasons) >= 2
