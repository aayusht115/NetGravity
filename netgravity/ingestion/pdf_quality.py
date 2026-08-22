"""
NetGravity — PDF Text Quality Checks
=====================================
Decides whether text pulled out of a PDF by pypdf is TRUSTWORTHY, or whether
the document should be escalated to the model to read directly.

WHY THIS EXISTS
---------------
pypdf extraction is free and instant, so it is always worth trying first.
But it fails in two ways, and only one of them is obvious:

    LOUD failure   — a scanned PDF has no text layer, extraction returns "".
                     Already handled: we refuse the file and say so.

    QUIET failure  — extraction returns text, but the text is wrong. Broken
                     font encodings produce symbol soup; some generators
                     emit table cells in visual rather than reading order.
                     The pipeline cannot tell this from a real contract, so
                     it hands garbage to the model, which dutifully
                     "extracts" numbers that were never in the document.

The quiet failure is the dangerous one, because it produces confident wrong
numbers rather than an error. These checks exist to catch it.

DESIGN RULE: LEAN TOWARDS ESCALATING
------------------------------------
Every threshold below is set so that an ambiguous case escalates to the
model rather than being accepted. Escalating a clean PDF costs a fraction of
a cent. Accepting a garbled one puts invented figures into a cost model.
Those are not symmetric, so the tie-break is not symmetric either.

TUNING
------
The four constants below are the entire policy. They are deliberately named
and documented here rather than inlined at the call site, and are described
in docs/ingestion_business_rules.md so the rationale survives independently
of this file. Changing extraction strictness should never require touching
adapter logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

# --- thresholds (the whole policy) -----------------------------------------

#: A real page of contract text runs to hundreds of characters. Well under
#: 100 means the text layer is present but essentially hollow — common with
#: image-based PDFs carrying only a header or a stamped page number.
MIN_CHARS_PER_PAGE = 100

#: Fraction of tokens that must look like real words or numbers. Genuine
#: prose scores very high (>0.9); mojibake and symbol soup score far below.
#: 0.60 leaves generous room for tables, codes and pin-code lists — the
#: TransCorp sample annexure is largely six-digit numbers and still passes.
MIN_WORD_RATIO = 0.60

#: Below this many tokens the ratio above is statistically meaningless — a
#: 5-token document can score 0.4 by chance. Short documents are judged on
#: emptiness alone, not on ratio.
MIN_TOKENS_FOR_RATIO = 20

#: No legitimate prose repeats a single LETTER OR DIGIT 30 times in a row.
#: This is the fingerprint of a broken text layer emitting filler.
#:
#: Restricted to alphanumerics deliberately. Punctuation runs are normal
#: document decoration — "=====" rules, "-----" separators, "....." leaders
#: in a table of contents. Both NetGravity sample contracts open with a
#: 64-character "=" rule, and an unrestricted version of this check flagged
#: them as corrupt. Repeated whitespace is excluded for the same reason:
#: PDF extraction pads table columns with long space runs routinely.
MAX_SINGLE_CHAR_RUN = 30

_TOKEN_RE = re.compile(r"\S+")
_RUN_RE = re.compile(r"([A-Za-z0-9])\1{%d,}" % (MAX_SINGLE_CHAR_RUN - 1))
_VOWELS = set("aeiou")
_NUMERIC_PUNCT = set(".,:/-%()")


@dataclass
class TextQuality:
    """Verdict plus the numbers behind it, so a report can explain itself."""

    usable: bool
    reasons: List[str] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.usable:
            return "extracted text looks usable"
        return "; ".join(self.reasons) or "extracted text failed quality checks"


def _looks_like_word(token: str) -> bool:
    """
    True for things that belong in a real document.

    Numbers count: a rate card is largely figures, and a pin-code annexure is
    almost entirely digits. Rejecting those would flag our own sample
    contracts as garbage.
    """
    if not token:
        return False

    stripped = token.strip("\"'()[]{}.,;:")
    if not stripped:
        return False

    # Numeric-ish: digits with currency/date/decimal punctuation.
    if any(c.isdigit() for c in stripped) and all(
        c.isdigit() or c in _NUMERIC_PUNCT for c in stripped
    ):
        return True

    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    # Mostly symbols with a letter sprinkled in is the mojibake signature.
    if len(letters) < len(stripped) * 0.6:
        return False
    if len(stripped) > 30:
        return False
    # Short tokens are abbreviations and units (kg, Rs, TC, per) — legitimate
    # and frequently vowel-free, so they are not held to the vowel test.
    if len(stripped) <= 3:
        return True
    return any(c.lower() in _VOWELS for c in letters)


def assess(text: str, page_count: int = 1) -> TextQuality:
    """
    Judge extracted PDF text. `page_count` scales the emptiness check.

    Returns usable=False when the text should be re-read by the model
    instead of trusted.
    """
    reasons: List[str] = []
    stripped = (text or "").strip()
    pages = max(1, page_count)

    tokens = _TOKEN_RE.findall(stripped)
    chars_per_page = len(stripped) / pages
    word_like = sum(1 for t in tokens if _looks_like_word(t))
    ratio = (word_like / len(tokens)) if tokens else 0.0

    metrics: Dict[str, float] = {
        "characters": float(len(stripped)),
        "pages": float(pages),
        "chars_per_page": round(chars_per_page, 1),
        "tokens": float(len(tokens)),
        "word_ratio": round(ratio, 3),
    }

    # 1. Near-empty. Distinct from the fully-empty case the adapter already
    #    catches: this is a text layer that exists but carries nothing.
    if chars_per_page < MIN_CHARS_PER_PAGE:
        reasons.append(
            f"only {chars_per_page:.0f} characters per page "
            f"(expected at least {MIN_CHARS_PER_PAGE}) — the text layer is "
            f"present but essentially empty, typical of a scan"
        )

    # 2. Low word ratio — the mojibake / broken-encoding signature.
    if len(tokens) >= MIN_TOKENS_FOR_RATIO and ratio < MIN_WORD_RATIO:
        reasons.append(
            f"only {ratio:.0%} of tokens look like words or numbers "
            f"(expected at least {MIN_WORD_RATIO:.0%}) — extraction likely "
            f"produced garbled output rather than readable text"
        )

    # 3. Degenerate repetition — a broken text layer emitting filler.
    match = _RUN_RE.search(stripped)
    if match:
        reasons.append(
            f"found a run of {len(match.group(0))} identical characters "
            f"({match.group(1)!r}) — no real document repeats a letter or "
            f"digit this way; the text layer is likely corrupt"
        )
        metrics["longest_char_run"] = float(len(match.group(0)))

    return TextQuality(usable=not reasons, reasons=reasons, metrics=metrics)
