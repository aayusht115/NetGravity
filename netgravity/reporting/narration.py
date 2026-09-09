"""
Plain-English narration of a derivation, written by the gateway model.

WHY A MODEL IS INVOLVED AT ALL
------------------------------
Everywhere else in this codebase the rule is that a language model may phrase
a result and may never produce one. That rule is unchanged here. What the
model adds is the part the deterministic layer is worst at: joining a table of
figures into an account a reader who was not in the room can follow. A
derivation listing "Average utilisation 56.23%", "Busiest site 77.14%" and
"Utilisation threshold 90%" is complete, and still leaves the reader to work
out for themselves why those three lines add up to the conclusion. That gap is
what makes a correct derivation read as a black box.

THE ONE RULE THAT MAKES IT SAFE
-------------------------------
**A number in the explanation must be a number in the report.**

Not "a plausible number", not "a number the model derived correctly" — one of
the figures already on the page, which the engine computed and the grounding
pass already checked. Everything the model writes is verified against that set
before it reaches the file, and any sentence carrying a figure that is not in
it is dropped. So the worst case for a hallucinated quantity is a missing
sentence, never a fabricated one under a letterhead.

This is a deliberately stricter rule than `numeric_grounding.ground_narrative`,
which adjudicates a claim against whatever metric it names anywhere in the
reasoning payload. That is right for a briefing written from the payload. It is
too loose here, where the report has already narrowed to one finding: a
document restating a run must not reach past the run's own figures for a
number, however true that number is elsewhere.

WHAT HAPPENS WHEN THE GATEWAY IS NOT THERE
------------------------------------------
The document is written anyway, from the deterministic method note. The
gateway's budget is shared and small (100 requests/day across every consumer),
the token may not be configured at all, and a download is something a reader
just asked for. `narrate` therefore raises nothing and blocks nothing: it
returns what it has, and says which it is.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: How far a quoted figure may sit from the report's own, as a fraction.
#: A model asked to explain "150,627.70" may reasonably write "150,628" or
#: "about 150,600", and rejecting those would drop good sentences over
#: rounding. It may not write 151,000 — that is a different number.
_TOLERANCE = 0.005

#: Sentence-ish split. Deliberately crude: this decides what to DROP, so it
#: only has to be right about boundaries, and an over-long "sentence" costs a
#: little more removed text rather than letting a figure through.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")

#: Numbers in prose. Mirrors the pattern in `numeric_grounding` closely enough
#: to catch the same tokens, without importing a private one.
_NUMBER = re.compile(
    r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\.\d+|\d+)")

#: Numbers that say nothing quantitative about the network and must not cause
#: a sentence to be dropped: "the first of three steps", "two reasons".
#: Bounded hard — anything above this is a quantity, not a piece of grammar.
_STRUCTURAL_MAX = 12


@dataclass(frozen=True)
class Narration:
    """What the model wrote, and what happened to it."""

    paragraphs: Tuple[str, ...] = ()
    #: "llm" | "unavailable" | "rejected"
    source: str = "unavailable"
    #: One line for the document, telling the reader who wrote this section.
    note: str = ""
    #: Figures the model quoted that were not in the report. The sentences
    #: carrying them were dropped; they are reported so a reviewer can see
    #: that a drop happened rather than wondering why the prose is short.
    dropped: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.paragraphs)


def allowed_figures(report: Any) -> List[float]:
    """
    Every number the report already states, as floats.

    Read from the figure VALUES and from the prose the engine wrote, because
    both are already grounded: the display strings come from the evidence pack
    and the prose has been through `ground_narrative`.
    """
    texts: List[str] = []
    for attr in ("conclusion", "summary", "recommended_action", "method"):
        value = getattr(report, attr, "") or ""
        if value:
            texts.append(str(value))
    for step in getattr(report, "steps", []) or []:
        texts.append(str(getattr(step, "detail", "") or ""))
        for figure in getattr(step, "figures", []) or []:
            texts.append(str(getattr(figure, "value", "") or ""))
            texts.append(str(getattr(figure, "label", "") or ""))
    for line in (getattr(report, "assumptions", []) or []):
        texts.append(str(line))

    out: List[float] = []
    for text in texts:
        for token in _NUMBER.findall(text):
            parsed = _parse(token)
            if parsed is not None:
                out.append(parsed)
    return out


def _parse(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _close(a: float, b: float) -> bool:
    scale = max(abs(a), abs(b))
    if scale == 0:
        return True
    return abs(a - b) / scale <= _TOLERANCE


def _is_supported(value: float, allowed: Sequence[float]) -> bool:
    """True when the report states this number, or one it rounds to or from."""
    if abs(value) <= _STRUCTURAL_MAX and float(value).is_integer():
        # "in three steps", "both figures" — grammar, not a quantity.
        return True
    for fact in allowed:
        if fact == value or _close(fact, value):
            return True
        # A ratio quoted as its percentage, or the other way round: the same
        # equivalence `numeric_grounding._equivalent_values` already allows,
        # so a fill rate stored as 0.968 may be written 96.8%.
        if _close(fact * 100.0, value) or _close(fact / 100.0, value):
            return True
    return False


def verify(text: str, allowed: Sequence[float]) -> Tuple[List[str], List[str]]:
    """
    Keep the sentences whose every figure the report states; drop the rest.

    Returns `(kept_sentences, dropped_figures)`. The SENTENCE is the unit
    because a figure cannot be excised from prose without leaving a sentence
    that says something different from what its author meant — and a document
    is read as continuous text, not as a list of separable claims.
    """
    kept: List[str] = []
    dropped: List[str] = []
    for sentence in _SENTENCE_SPLIT.split(str(text or "").strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        bad = []
        for token in _NUMBER.findall(sentence):
            parsed = _parse(token)
            if parsed is not None and not _is_supported(parsed, allowed):
                bad.append(token)
        if bad:
            dropped.extend(bad)
            continue
        kept.append(sentence)
    return kept, dropped


def build_prompt(report: Any) -> str:
    """
    The whole instruction, in one string.

    The gateway takes exactly one field — no system role, no temperature — so
    everything the model needs is here. The figure list is given verbatim and
    named as the only permitted source of numbers, because that is the rule
    the output is checked against, and a model told the rule tends to keep it.
    """
    lines: List[str] = []
    for step in getattr(report, "steps", []) or []:
        for figure in getattr(step, "figures", []) or []:
            label = getattr(figure, "label", "") or ""
            value = getattr(figure, "value", "") or ""
            role = getattr(figure, "role", "") or ""
            if label and value:
                lines.append(f"  - {label}: {value} ({role})")
    figures = "\n".join(lines[:60]) or "  (no figures were recorded)"

    return (
        "You are writing one section of a supply-chain analysis document for "
        "senior leadership at a logistics client. The analysis has already "
        "been computed by a deterministic optimisation engine. Your only job "
        "is to explain, in plain business English, how the figures below lead "
        "to the conclusion.\n\n"
        f"SUBJECT: {getattr(report, 'subject', '')}\n"
        f"CONCLUSION REACHED: {getattr(report, 'conclusion', '')}\n"
        f"THE ENGINE'S OWN SUMMARY: {getattr(report, 'summary', '')}\n"
        f"HOW IT WAS COMPUTED: {getattr(report, 'method', '')}\n\n"
        "THE FIGURES THIS RESTS ON:\n"
        f"{figures}\n\n"
        "WRITE: two or three short paragraphs, 60 to 110 words each, that walk "
        "a reader from these figures to the conclusion. Say what each figure "
        "measures, what it was compared against, and why that comparison "
        "supports the conclusion. Where a figure is a rate or a ratio, say "
        "what it is a rate of.\n\n"
        "RULES, all mandatory:\n"
        "1. Do not state any number that is not in the list above. If you want "
        "to describe a difference or a total you have not been given, describe "
        "it in words instead of calculating it.\n"
        "2. Do not invent causes, forecasts, recommendations, or facts about "
        "the client's business. Explain only what is here.\n"
        "3. Do not mention being a language model, and do not address the "
        "reader as 'you'.\n"
        "4. Write continuous prose. No headings, no bullet points, no "
        "markdown, and no preamble such as 'Certainly' or 'Here is'.\n"
        "5. British spelling.\n\n"
        "Begin the first paragraph immediately."
    )


def narrate(report: Any, gateway: Any, *,
            purpose: str = "derivation_document") -> Narration:
    """
    Ask the gateway to explain the report, and verify what comes back.

    Never raises. A download is something a reader just asked for, and a
    best-effort call on a shared budget must not be able to turn that into an
    error page.
    """
    if gateway is None or not getattr(gateway, "available", False):
        return Narration(
            source="unavailable",
            note=("This section is written from the engine's own record of the "
                  "run. The text-generation service was not available to "
                  "expand it."))

    try:
        response = gateway.generate(build_prompt(report), purpose=purpose)
        # `output`, which is what `LLMResponse` calls the generated text. A
        # `text` attribute does not exist on it, and reading one returned the
        # empty string for every successful call — the budget was spent, 1,824
        # tokens came back, and this reported "the service returned nothing to
        # add". A silent miss, because the empty case is a legitimate outcome
        # that the caller is built to absorb.
        text = str(getattr(response, "output", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — best effort, by contract
        logger.warning("reporting.narration.failed purpose=%s error=%s",
                       purpose, exc)
        return Narration(
            source="unavailable",
            note=("This section is written from the engine's own record of the "
                  "run. The text-generation service could not be reached."))

    if not text:
        return Narration(
            source="unavailable",
            note="The text-generation service returned nothing to add.")

    kept, dropped = verify(text, allowed_figures(report))
    if not kept:
        logger.warning("reporting.narration.rejected purpose=%s dropped=%d",
                       purpose, len(dropped))
        return Narration(
            source="rejected", dropped=tuple(dropped),
            note=("An explanatory passage was generated for this section and "
                  "withheld: it quoted figures this analysis did not produce. "
                  "The working below is unaffected — it is the engine's."))

    # Back into paragraphs of roughly the shape the model wrote, so the section
    # reads as prose rather than as a list of retained sentences.
    paragraphs: List[str] = []
    buffer: List[str] = []
    for sentence in kept:
        buffer.append(sentence)
        if len(" ".join(buffer)) > 420:
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))

    note = ("Written by the NetGravity text service from the figures in this "
            "document. Every figure it quotes was checked against the working "
            "below before this was written out. The analysis itself is the "
            "optimisation engine's; no model took part in producing it.")
    if dropped:
        note += (f" {len(dropped)} figure(s) it quoted were not produced by "
                 f"this analysis, and the sentences carrying them were removed.")
    return Narration(paragraphs=tuple(paragraphs), source="llm", note=note,
                     dropped=tuple(dropped))
