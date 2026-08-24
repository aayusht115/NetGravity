"""
NetGravity — Market Intelligence Extraction Prompt & Parser
============================================================
Reads a news article, circular or notice and returns structured
`MarketIntelligenceSignal` records.

THE BUSINESS CASE
-----------------
Diesel goes up 8%. A port announces a congestion surcharge. A carrier
withdraws capacity on a corridor. Each of these moves a number the optimiser
already uses — a lane rate, a transit time, a lane capacity — and each
arrives as prose, in a PDF or a link, days before anyone thinks to update a
spreadsheet. This is the step that turns that prose into a dated, sourced,
machine-checkable record.

WHAT THE MODEL IS AND IS NOT ALLOWED TO PRODUCE
-----------------------------------------------
The model reads and structures. It does not decide relevance and it does not
decide magnitude in the optimiser's units. Two hard boundaries:

  1. NO PROBABILITY. `MarketIntelligenceSignal` has no probability field, and
     this prompt never asks for one. Converting "high confidence" into
     "P = 0.8" would manufacture the single number that drives
     `RF = P + REI - P*REI` and therefore governance, out of a qualitative
     judgement that was never a likelihood. If a source genuinely states an
     event probability, that is a hazard event for the orchestrator's own
     `ExternalSignal` path — a different pipeline, deliberately.

  2. NO EDIT TO A SOLVER INPUT. A signal never rewrites a rate. It is routed
     to staging, scored by the guardrail, and surfaced as context. What it
     changes, if anything, is changed by a person asking for a scenario.

Relevance is decided afterwards by `guardrails/relevance.py`, deterministically
and from a versioned policy file — never by the model that read the article.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from netgravity.ingestion.ai.client import LLMClient
from netgravity.ingestion.schemas.signal import (
    MarketIntelligenceSignal,
    SignalBucket,
    SignalConfidence,
    SignalDirection,
)

#: How much of a document is sent. A news item states its point early; the
#: tail of a long PDF is usually boilerplate, and every character costs
#: budget on a shared, cumulative allowance.
MAX_DOCUMENT_CHARS = 12000

PROMPT_TEMPLATE = """You are reading a news article, industry circular or \
official notice and extracting the supply-chain-relevant facts it states, so \
they can be reviewed against a distribution network.

Return ONLY a JSON object with this exact shape:

{{
  "signals": [
    {{
      "title": "one sentence stating what happened",
      "source_title": "publication or issuing body, if named",
      "source_url": "URL if the document contains one, else null",
      "published_date": "YYYY-MM-DD, ONLY if the document states it, else null",
      "effective_date": "YYYY-MM-DD the change takes effect, else null",
      "bucket": "CARRIER | SUPPLIER | CUSTOMER | MACRO | WEATHER | COMPETITOR | UNKNOWN",
      "direction": "UP | DOWN | NEUTRAL",
      "magnitude": "what the document states, in its own terms, e.g. '+8%' or 'INR 2/kg'",
      "affected_entities": ["identifiers from the known list below, if named"],
      "geography": "place or region named, else empty string",
      "confidence": "HIGH | MEDIUM | LOW",
      "rationale": "one sentence naming the sentence or figure you used",
      "states_probability": false
    }}
  ]
}}

WHAT COUNTS AS A SIGNAL
- A stated or announced CHANGE that would plausibly move freight cost,
  transit time, available capacity, or demand. Fuel and energy prices,
  freight or surcharge announcements, port and terminal notices, strikes and
  closures, duties and tariffs, carrier capacity changes, customer expansion
  or contraction.
- One entry per distinct change. A roundup article may yield several; an
  article about nothing relevant yields an empty list.

WHAT IS NOT A SIGNAL
- Opinion, forecast commentary, or analyst speculation with no stated change.
- Company results, appointments, awards, or product launches.
- Anything already true and unchanged — background, not news.

RULES
- Extract ONLY what the document states. Never estimate a figure it does not
  give. If the magnitude is described but not quantified, put the description
  in "magnitude" and set confidence to LOW.
- published_date must be null unless the DOCUMENT states a date. Do not use
  today's date, and do not infer one. A signal whose age is unknown is more
  dangerous than one that is missing.
- bucket describes WHO or WHAT the change concerns. Fuel, currency, duty and
  policy are MACRO. A specific carrier or port operator is CARRIER.
- Set "states_probability" to true ONLY if the document itself gives a numeric
  likelihood of a future event ("a 40% chance of..."). Do NOT extract the
  number. It is recorded separately by a different pipeline, and inventing or
  relocating it here would corrupt a governed risk calculation.
- affected_entities may ONLY contain identifiers from the known list. If the
  document names a place not on that list, leave the list empty and put the
  place in "geography". Never invent an identifier.

Known network identifiers (use these exact strings where the document refers \
to them): {known_entities}

DOCUMENT TEXT:
---
{document_text}
---
"""

#: Recognised by the schema; anything else falls back to UNKNOWN/NEUTRAL.
_BUCKETS = {b.value for b in SignalBucket}
_DIRECTIONS = {d.value for d in SignalDirection}
_CONFIDENCES = {c.value for c in SignalConfidence}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract_signals(
    client: LLMClient,
    document_text: str,
    *,
    filename: str,
    known_entity_ids: Optional[Sequence[str]] = None,
    stub_key: str = "market_signal",
) -> Tuple[List[MarketIntelligenceSignal], List[str], str]:
    """
    Read one document and return the signals it states.

    Returns (signals, rejection_reasons, provenance_note).

    `rejection_reasons` names every candidate that was read but NOT turned
    into a signal, and why. They are returned rather than logged away because
    "the article said nothing usable" and "the article said something we threw
    out" are different outcomes and a reviewer must be able to tell them
    apart.
    """
    prompt = PROMPT_TEMPLATE.format(
        known_entities=", ".join(known_entity_ids or []) or "(none supplied)",
        document_text=document_text[:MAX_DOCUMENT_CHARS],
    )

    response = client.extract_json(
        task=f"market intelligence extraction ({filename})",
        prompt=prompt,
        stub_key=stub_key,
        stub_context={"filename": filename},
        max_tokens=2000,
    )

    raw_signals = response.data.get("signals")
    if not isinstance(raw_signals, list):
        # A single-object reply is a common model shape; accept it rather
        # than discarding a correct extraction over its wrapper.
        raw_signals = [response.data] if response.data.get("title") else []

    signals: List[MarketIntelligenceSignal] = []
    rejections: List[str] = []
    known = {str(e) for e in (known_entity_ids or [])}

    for index, raw in enumerate(raw_signals, start=1):
        if not isinstance(raw, dict):
            continue
        signal, reason = _to_signal(
            raw, index=index, filename=filename, known=known,
            extracted_by=response.provenance,
        )
        if signal is None:
            rejections.append(reason)
            continue
        signals.append(signal)

    return signals, rejections, response.notes


def _to_signal(raw: Dict[str, Any], *, index: int, filename: str,
               known: set, extracted_by: str,
               ) -> Tuple[Optional[MarketIntelligenceSignal], str]:
    """Validate one extracted record. Returns (signal, rejection_reason)."""
    title = str(raw.get("title") or "").strip()
    if not title:
        return None, (f"{filename}: a candidate signal had no title and was "
                      f"discarded — there is nothing for a reviewer to read.")

    published = _clean_date(raw.get("published_date"))
    if not published:
        # Deliberately fatal for this record. Every downstream use of a
        # signal is time-sensitive: the guardrail expires weather signals
        # after 30 days, and a stale price is worse than no price. Stamping
        # today's date would make a two-year-old article look like news.
        return None, (
            f"{filename}: '{title[:60]}' states no publication date, so it "
            f"was not ingested. Signals are time-sensitive and the ingest "
            f"date is not the publication date — add a date to the document "
            f"or enter this signal on a sheet with one."
        )

    entities = [str(e) for e in (raw.get("affected_entities") or [])]
    # The prompt forbids invented identifiers; this enforces it rather than
    # trusting it. An identifier we do not recognise cannot match anything
    # downstream anyway, and leaving it in would inflate the guardrail's
    # entity-match bonus on a name that does not exist.
    entities = [e for e in entities if not known or e in known]

    signal = MarketIntelligenceSignal(
        signal_id=_signal_id(filename, title, index),
        title=title,
        source_title=str(raw.get("source_title") or ""),
        source_url=_clean_url(raw.get("source_url")),
        published_date=published,
        effective_date=_clean_date(raw.get("effective_date")),
        bucket=_enum(SignalBucket, raw.get("bucket"), _BUCKETS,
                     SignalBucket.UNKNOWN),
        direction=_enum(SignalDirection, raw.get("direction"), _DIRECTIONS,
                        SignalDirection.NEUTRAL),
        magnitude=str(raw.get("magnitude") or ""),
        affected_entities=entities,
        geography=str(raw.get("geography") or ""),
        confidence=_enum(SignalConfidence, raw.get("confidence"), _CONFIDENCES,
                         SignalConfidence.MEDIUM),
        rationale=str(raw.get("rationale") or ""),
        structured_by=extracted_by,
    )
    return signal, ""


def states_probability(raw: Dict[str, Any]) -> bool:
    """Did the source state a numeric likelihood? Reported, never extracted."""
    return bool(raw.get("states_probability"))


def _signal_id(filename: str, title: str, index: int) -> str:
    """
    A stable identifier derived from the document and the headline.

    Deterministic on purpose: re-reading the same article produces the same
    id, so a duplicate upload is recognisable as a duplicate instead of
    accumulating as a second, apparently-corroborating signal.
    """
    digest = hashlib.sha1(f"{filename}|{title}".encode("utf-8")).hexdigest()[:8]
    return f"sig-{digest}-{index}"


def _clean_date(value: Any) -> Optional[str]:
    """Accept an ISO date, reject anything else. No parsing heroics."""
    text = str(value or "").strip()
    return text if _DATE_RE.match(text) else None


def _clean_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if text.lower() in {"", "null", "none", "n/a"}:
        return None
    return text if text.lower().startswith(("http://", "https://")) else None


def _enum(cls, value: Any, allowed: set, default):
    text = str(value or "").strip().upper()
    return cls(text) if text in allowed else default
