"""
NetGravity — the explanation card
===================================
One shape for every explanation on every screen:

    headline   the conclusion, in plain business English
    meaning    why it matters, one short sentence
    warning    the one thing that must not be missed (may be empty)
    next_step  one action (may be empty)
    figures    up to three supporting numbers — SUPPLIED BY CODE
    details    the technical account, collapsed by default

WHY IT IS THIS SHAPE. The screens were showing one conclusion as six things:
a headline, a paragraph, an insight, an evidence table, a recommendation and
a technical note. Repeating a finding does not reinforce it; it makes one
message read as six, and the reader stops looking for the one that matters.

THE DIVISION OF LABOUR. The model writes `headline`, `meaning`, `warning` and
`next_step` and writes NO NUMBERS AT ALL. Code produces `figures`, already
formatted in the project's own currency.

That split is not stylistic. It fixes three faults at once:

  * currency drift — a comparison table in $ beside a recommendation in ₹,
    because the model wrote a figure and had no idea what the project uses;
  * grounding leakage — "[UNGROUNDED CLAIM REMOVED — authoritative ... ]"
    appearing on screen, which is internal validation text. A model that
    writes no figures gives grounding nothing to redact;
  * duplication — the same number stated in prose and again in a table.

Grounding still runs. It simply finds nothing to strip, which is the point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: What `numeric_grounding.redact()` leaves behind when a claim fails. These
#: are engineering notes for an audit trail and must never reach a reader.
_REDACTION_MARKERS = ("[UNGROUNDED CLAIM REMOVED", "[UNSUPPORTED FIGURE REMOVED")

#: First-person and machine voice. The screens read as a system talking about
#: itself — "I see", "My models" — rather than as a report about a network.
#: Ordered: the shaped rewrites run before the bare removals, so
#: "I see cost at X" becomes "Cost is X" rather than "Cost at X".
#:
#: EVERY REPLACEMENT IS LOWER CASE. A rule that wrote "Stated" copied the
#: capital off the pronoun it replaced, so "I have not run that scenario, so I
#: state no saving" came out as "... so Stated no saving" — a capital in the
#: middle of a sentence, on the line that leads a card. Which words begin a
#: sentence is not something a per-phrase rule can know; `plain_voice` decides
#: it once, afterwards, for all of them.
_VOICE_FIXES = (
    # "I see cost at 4.2 crore" -> "Cost is 4.2 crore". ONLY "at": the same
    # rewrite on "of" turned "I see 139,054 units of 3,956,705 units of demand
    # left unserved" into "139,054 units is 3,956,705 units of demand left
    # unserved", which is not English and was the card's warning line.
    (re.compile(r"\bI see (?:a |an |the )?(.+?) at ", re.I), r"\1 is "),
    # "I see a demand fill rate of 1.000" -> "The demand fill rate is 1.000".
    # The bare removal below leaves "A demand fill rate of 1.000." \u2014 a noun
    # phrase with no verb, which is acceptable in a headline and is not a
    # sentence.
    #
    # The subject is restricted to plain words: lazily matching anything up
    # to the first " of " turned "I see a structural break detected in 3 of
    # the 12 series" into "The structural break detected in 3 is the 12
    # series". A digit inside the subject means the " of " belongs to a
    # quantity rather than to the reading being named, and this rule then
    # correctly declines to fire.
    (re.compile(r"\bI see an? ([a-z][a-z ]{2,40}) of ", re.I), r"The \1 is "),
    (re.compile(r"\bI see (?:that )?", re.I), ""),
    # "I use this as the decision baseline" -> "This is the decision baseline".
    (re.compile(r"\bI use (?:this|it) as (?:a |an |the )?", re.I), "this is the "),
    (re.compile(r"\bI found that ", re.I), ""),
    # "I found the network INFEASIBLE" -> "This analysis found the network
    # INFEASIBLE". The rule above only strips "I found THAT ...", where what
    # follows is a whole clause; without the "that" the sentence loses its
    # verb ("The network INFEASIBLE under this configuration").
    (re.compile(r"\bI found\b(?! that\b)", re.I), "this analysis found"),
    # The negatives. Each of these is the engine saying what it did NOT do,
    # which is the half of a briefing that must survive rewriting intact —
    # dropping the pronoun would turn "I am not stating what it would cost"
    # into a fragment, and dropping the sentence would turn a stated limit
    # into a silent one.
    (re.compile(r"\bI am not stating\b", re.I), "this analysis does not state"),
    (re.compile(r"\bI have not run\b", re.I), "this analysis has not run"),
    (re.compile(r"\bI have not\b", re.I), "this analysis has not"),
    (re.compile(r"\bI have no\b", re.I), "there is no"),
    (re.compile(r"\bI cannot\b", re.I), "this analysis cannot"),
    (re.compile(r"\bI could not\b", re.I), "this analysis could not"),
    (re.compile(r"\bI do not\b", re.I), "this analysis does not"),
    (re.compile(r"\bI state no\b", re.I), "this analysis states no"),
    # "the scenario I would run next" -> "the scenario to run next".
    (re.compile(r"\bI would run next\b", re.I), "to run next"),
    # "I recommend reviewing X" -> "Consider reviewing X". Stripping the
    # phrase outright left a bare gerund ("Reviewing X with the people who
    # would have to carry it out") or a bare noun phrase ("A footprint
    # review: at least one open site costs more than it saves") - a label,
    # not an instruction. "Consider" is also the honest verb: the comparison
    # ranks, and a structural change is a human decision whatever the
    # economics say.
    (re.compile(r"\bI recommend (?:that )?", re.I), "consider "),
    # The one line whose whole point is that the engine is not deciding.
    (re.compile(r"\bnot one I make\b", re.I), "not one this analysis makes"),
    (re.compile(r"\bI compared\b", re.I), "compared"),
    (re.compile(r"\bI measured\b", re.I), "measured"),
    (re.compile(r"\bI (?:have )?produced\b", re.I), "produced"),
    (re.compile(r"\bI forecast\b", re.I), "forecast"),
    (re.compile(r"\bI chose\b", re.I), "chose"),
    (re.compile(r"\bI state\b", re.I), "stated"),
    (re.compile(r"\bI am\b", re.I), "this is"),
    (re.compile(r"\bMy models\b", re.I), "the models"),
    (re.compile(r"\bMy model\b", re.I), "the model"),
    # Urgency nothing computed. The engine ranks and measures; it does not
    # know that anything must happen today.
    (re.compile(r"\b(?:immediately|urgently|as soon as possible)\b,?\s*", re.I), ""),
)


def strip_redactions(text: str) -> str:
    """
    Drop any SENTENCE that lost a number to grounding.

    Not the marker alone: a sentence with its quantity cut out reads as a
    claim without evidence, which is worse than saying nothing. The whole
    sentence goes, and the count of dropped ones is reported in `details` so
    the loss is visible where it belongs rather than mid-paragraph.
    """
    if not text:
        return ""
    if not any(marker in text for marker in _REDACTION_MARKERS):
        return text
    kept = [part for part in re.split(r"(?<=[.!?])\s+", text)
            if not any(marker in part for marker in _REDACTION_MARKERS)]
    return " ".join(kept).strip()


def count_redactions(text: str) -> int:
    return sum(text.count(marker) for marker in _REDACTION_MARKERS) if text else 0


def plain_voice(text: str) -> str:
    """
    Remove the first person, and the machine talking about itself.

    A briefing is a report about a network, not a narration of what a program
    did. "I see business network cost at 4.2 crore" is the same fact as
    "Business network cost is 4.2 crore" with an extra actor in it.
    """
    if not text:
        return ""
    result = text
    for pattern, replacement in _VOICE_FIXES:
        result = pattern.sub(replacement, result)
    result = re.sub(r"\s{2,}", " ", result).strip()
    # Sentence starts, and only sentence starts. `[.!?]` followed by
    # whitespace is a terminator; a decimal point is followed by a digit and
    # is not one, so "4.2 crore" keeps its lower case.
    return re.sub(r"(^|[.!?]\s+)([a-z])",
                  lambda m: m.group(1) + m.group(2).upper(), result)


def trim(text: str, limit: int) -> str:
    """
    Cut to `limit` at a boundary a reader would recognise.

    A hard slice produced "...but produces unse" on the first live run — the
    model's own sentence, ending mid-word, on the card that leads the screen.
    A whole sentence is preferred; failing that a whole word, with an ellipsis
    so the reader can see something was dropped.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    # The last sentence that fits, if that keeps most of the budget.
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= int(limit * 0.55):
        return window[:cut + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window).rstrip(" ,;:-") + "\u2026"


#: The identifiers this system addresses its own records by. None of them
#: means anything to a reader, and every one of them has appeared in prose:
#: the recommendation on a scenario card read "Proceed with a detailed
#: implementation review of Key probe SCN_084df97a" — the scenario's name,
#: which is right, followed by its primary key, which is noise a senior reader
#: has to decide whether to worry about.
#:
#: They reach the prose honestly. The reasoning payload carries them because
#: the grounding and provenance layers need them, the model is shown the
#: payload, and a model shown an identifier beside a name will sometimes cite
#: both. Forbidding it in the prompt helps and cannot be relied on; this is
#: the guarantee.
_INTERNAL_ID = re.compile(
    r"""
    \s*
    \(?                                     # sometimes parenthesised
    \b(?:
        SCN|INS|ACT|PRJ|EXE                 # scenario, insight, action, …
      | snap|tws|scn|req|exec|prop|wf       # snapshot, twin state, request, …
    )_[A-Za-z0-9_]{4,}
    \)?
    """,
    re.VERBOSE,
)


def strip_internal_ids(text: str) -> str:
    """
    Remove record identifiers from a sentence, leaving the sentence.

    Deliberately not a redaction marker. A reader losing "SCN_084df97a" loses
    nothing, and "[ID REMOVED]" would be a second unreadable token in place of
    the first — unlike an ungrounded FIGURE, whose absence changes what the
    sentence claims and must therefore be visible.
    """
    out = _INTERNAL_ID.sub("", text or "")
    # The id often trails a name — "Key probe SCN_084df97a." — so removing it
    # can leave a space before the full stop.
    return re.sub(r"\s+([.,;:!?])", r"\1", out).strip()


def clean(text: str, limit: int = 400) -> str:
    """Everything a reader-facing string goes through, in one call."""
    return trim(
        strip_internal_ids(plain_voice(strip_redactions(text or ""))), limit)


#: How a figure is rendered. Money is handed over as an AMOUNT rather than a
#: string, because the project's currency is decided in exactly one place —
#: `data.js::formatCurrency`, from the currency the upload stated — and a
#: second formatter here would be a second place for it to be decided. That
#: is how a table in $ came to sit beside a recommendation in ₹.
FORMAT_TEXT = "text"
FORMAT_CURRENCY = "currency"


@dataclass
class Figure:
    """One supporting number. Supplied by code, never written by a model."""

    label: str
    #: Rendered already — a percentage, a count, a word like "High".
    value: str = ""
    #: Money, unformatted. The screen applies the project's currency.
    amount: Optional[float] = None
    format: str = FORMAT_TEXT
    #: Optional one-word qualifier — "Good", "High", "Not available".
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "value": self.value,
                "amount": self.amount, "format": self.format,
                "note": self.note}

    @classmethod
    def money(cls, label: str, amount: Optional[float], note: str = "") -> "Figure":
        return cls(label=label, amount=amount, format=FORMAT_CURRENCY, note=note)


@dataclass
class ExplanationCard:
    """One conclusion, said once."""

    headline: str = ""
    meaning: str = ""
    warning: str = ""
    next_step: str = ""
    #: At most three. A fourth is a table, and a table is not a card.
    figures: List[Figure] = field(default_factory=list)
    #: The technical account, behind "How was this calculated?".
    details: List[str] = field(default_factory=list)
    #: "llm" | "template", plus whether it came from the store.
    source: str = "template"
    cached: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "headline": self.headline,
            "meaning": self.meaning,
            "warning": self.warning,
            "next_step": self.next_step,
            "figures": [f.as_dict() for f in self.figures[:3]],
            "details": list(self.details),
            "source": self.source,
            "cached": self.cached,
        }

    @property
    def is_empty(self) -> bool:
        return not (self.headline or self.meaning or self.figures)


def card_from_briefing(briefing: Any, *, figures: Optional[List[Figure]] = None,
                       details: Optional[List[str]] = None,
                       source: str = "template") -> ExplanationCard:
    """
    Reduce an `ExecutiveBriefing` to one card.

    The briefing carries an opening, a context, up to six themed insights,
    key drivers, a recommendation and a limitation — six places for one
    finding. This picks the ONE that leads and puts the rest where they
    belong: the strongest risk becomes the warning, the recommendation
    becomes the next step, and everything else goes into `details`.
    """
    if briefing is None:
        return ExplanationCard(source=source)

    insights = list(getattr(briefing, "kpi_insights", []) or [])
    opening = clean(getattr(briefing, "opening", "") or "", 200)

    lead = insights[0] if insights else None
    headline = clean(getattr(lead, "headline", "") or opening, 140)
    # A placeholder heading is worse than none: "What these results show" over
    # the conclusion pushes the conclusion into the body and leads the card
    # with a label. Where the lead insight has no headline of its own, the
    # first sentence of its narrative is the conclusion and becomes one.
    if not headline:
        headline = clean(first_sentence(getattr(lead, "narrative", "")
                                        or opening), 140)
    meaning = clean(getattr(lead, "narrative", "") or
                    getattr(briefing, "context", "") or "", 260)

    # DUPLICATION GUARD. The model has been observed returning the same
    # paragraph as both `opening` and `insights[0].narrative`, and the screen
    # rendered both. Identical text is one message, shown once.
    #
    # NOT when the headline is a TRUNCATION of the body. `_same_text` treats a
    # prefix as the same text, which is exactly what trimming produces — so a
    # single long sentence came out as a headline cut off at 140 characters
    # with nothing under it, and the rest of the sentence existed nowhere. A
    # truncated headline is a reason to show the body, not to drop it.
    truncated = headline.endswith("\u2026")
    if meaning and headline and not truncated and _same_text(meaning, headline):
        meaning = clean(getattr(briefing, "context", "") or "", 260)

    # The most important thing not to miss. A RISK-severity insight beats the
    # briefing's own limitation, which is usually about the data rather than
    # about the network.
    warning = ""
    for insight in insights[1:]:
        if str(getattr(getattr(insight, "severity", None), "value", "")) == "RISK":
            warning = clean(getattr(insight, "narrative", ""), 220)
            break
    if not warning:
        warning = clean(getattr(briefing, "limitation", "") or "", 220)

    extra = [clean(getattr(i, "narrative", ""), 300) for i in insights[1:]]
    extra += [clean(d, 200) for d in (getattr(briefing, "key_drivers", []) or [])]

    return ExplanationCard(
        headline=headline,
        meaning=meaning if (truncated or not _same_text(meaning, warning)) else "",
        warning=warning,
        next_step=clean(getattr(briefing, "recommendation", "") or "", 220),
        figures=list(figures or [])[:3],
        details=[d for d in (details or []) + extra if d],
        source=source,
    )


def first_sentence(text: str) -> str:
    """The leading sentence of a paragraph, or the whole of a short one."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip(), maxsplit=1)
    return parts[0] if parts else ""


def rest_after_first_sentence(text: str) -> str:
    """Everything after that leading sentence. Empty when there is only one."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip(), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _same_text(a: str, b: str) -> bool:
    """Whether two strings say the same thing, ignoring punctuation and case."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    left, right = norm(a), norm(b)
    if not left or not right:
        return False
    return left == right or left.startswith(right) or right.startswith(left)
