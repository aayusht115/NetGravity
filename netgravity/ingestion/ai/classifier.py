"""
NetGravity — Content Classification
====================================
Decides WHAT a record set contains, from the data itself.

WHY FROM THE DATA, NOT THE FILENAME
-----------------------------------
Ingestion previously inferred meaning from the path: facilities.csv was
facilities, anything under distributors/ was shipment data. Both assumptions
break on real client files — a workbook called Master_Data_FINAL.xlsx with
five tabs carries five different things, and none of them announce
themselves.

WHY THE ROW DATA AND NOT JUST THE HEADERS
-----------------------------------------
Headers alone are context-blind, which is the same weakness that makes the
static alias table unreliable on its own. A sheet with columns Weight,
Quantity, Date could be a product master or a despatch register; what tells
them apart is that one has a stable row per SKU and the other has a row per
movement with repeating destinations. That pattern only exists in the rows,
so several real rows are always sent, never one.

TWO OPINIONS, AS EVERYWHERE ELSE IN THIS LAYER
----------------------------------------------
    AI       reads columns AND sample rows, so it has context.
    RULES    scores alias-table overlap per content type. Context-blind, but
             perfectly repeatable, free, and available with no API key.

Agreement raises confidence. Disagreement flags for review. With no key
configured the rule scorer runs alone and says so — the same honest-degrade
contract stub mode already follows, never a silent downgrade.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMClient
from netgravity.ingestion.field_aliases import (
    DEMAND_LOOKUP,
    FACILITY_LOOKUP,
    HISTORY_LOOKUP,
    LANE_LOOKUP,
    MARKET_LOOKUP,
    PRODUCT_LOOKUP,
    normalise_name,
)
from netgravity.ingestion.schemas.content import ContentClassification, ContentType
from netgravity.ingestion.sources.base import RecordSet

#: Below this, a classification is proposed but a human confirms it first.
REVIEW_BELOW = 0.85

#: Below this the answer is not usable at all — the record set is held as
#: UNKNOWN for a human to label rather than guessed at. Mirrors the guardrail
#: UNKNOWN bucket: an unclassifiable input is withheld, not assumed.
UNKNOWN_BELOW = 0.50

#: Alias tables that map one-to-one onto a content type.
_RULE_TABLES: Dict[ContentType, Dict[str, str]] = {
    ContentType.FACILITY: FACILITY_LOOKUP,
    ContentType.MARKET: MARKET_LOOKUP,
    ContentType.DEMAND: DEMAND_LOOKUP,
    ContentType.LANE: LANE_LOOKUP,
    ContentType.PRODUCT: PRODUCT_LOOKUP,
    ContentType.HISTORICAL_VOLUME: HISTORY_LOOKUP,
}

#: SHIPMENT_LOG has no alias table — it is exactly the case nobody wrote a
#: schema for, since every distributor names these columns differently. It is
#: recognised instead by the markers a transaction record carries and a master
#: record does not: a movement date, a document/vehicle reference, a party.
_SHIPMENT_MARKERS = {
    "despatchdt", "despatchdate", "dispatchdate", "shipdate", "shippeddate",
    "invoicedate", "billdate", "transactiondate", "movementdate",
    "vehicleno", "vehiclenumber", "truckno", "trucknumber", "lrno", "lrnumber",
    "invoiceno", "invoicenumber", "docno", "documentno", "awb", "consignmentno",
    "grn", "ewaybill", "challan", "challanno",
}

PROMPT_TEMPLATE = """You are identifying what kind of supply-chain data a \
spreadsheet contains, so it can be routed correctly.

Return ONLY a JSON object with this exact shape:

{{
  "content_type": "<one of: {allowed}>",
  "confidence": 0.0,
  "reasoning": "<one sentence naming the evidence you used>"
}}

WHAT EACH TYPE MEANS
  FACILITY           master list of warehouses / DCs / plants: one row per site,
                     with capacity or cost attributes
  MARKET             master list of demand zones / delivery destinations
  PRODUCT            SKU master: one row per product, with weight/value attributes
  DEMAND             demand quantities per market and/or product
  LANE               transport links between two locations, with rates or distance
  SHIPMENT_LOG       TRANSACTIONAL despatch/shipment records: one row per movement,
                     typically with a date, a destination that repeats across rows,
                     and a document or vehicle reference
  HISTORICAL_VOLUME  volume measured over time periods, one row per node per period
  UNKNOWN            genuinely cannot tell, or the sheet mixes several kinds

RULES
- Judge from the ROW DATA, not just the column names. The distinction that
  matters most: a MASTER list has one row per distinct entity, a
  SHIPMENT_LOG/HISTORICAL_VOLUME has many rows repeating the same entities
  over time.
- Set confidence honestly. If the sheet blends several kinds of data, or the
  sample is too thin to judge, return UNKNOWN with low confidence. A wrong
  confident answer is far worse than an admitted uncertainty.

SHEET NAME: {sheet_name}
COLUMNS: {columns}

SAMPLE ROWS:
{sample_rows}
"""


def score_by_rules(columns: List[str]) -> Tuple[ContentType, float, Dict[str, float]]:
    """
    Deterministic opinion: which content type's alias table best covers these
    columns, as a fraction of all columns.

    Context-blind by design — this is the second opinion, not the decision.
    """
    if not columns:
        return ContentType.UNKNOWN, 0.0, {}

    normalised = [normalise_name(c) for c in columns]
    scores: Dict[str, float] = {}

    for content_type, table in _RULE_TABLES.items():
        hits = sum(1 for n in normalised if n in table)
        scores[content_type.value] = round(hits / len(normalised), 3)

    shipment_hits = sum(1 for n in normalised if n in _SHIPMENT_MARKERS)
    scores[ContentType.SHIPMENT_LOG.value] = round(
        min(1.0, shipment_hits / max(1, len(normalised)) * 3.0), 3
    )

    best_name = max(scores, key=lambda k: scores[k])
    best_score = scores[best_name]
    if best_score <= 0.0:
        return ContentType.UNKNOWN, 0.0, scores
    return ContentType.parse(best_name), best_score, scores


def _format_rows(record_set: RecordSet, limit: int = 5) -> str:
    rows = record_set.sample_rows(limit)
    if not rows:
        return "(no rows)"
    lines = []
    for i, row in enumerate(rows, start=1):
        cells = ", ".join(
            f"{k}={str(v)[:40]!r}" for k, v in list(row.items())[:14]
        )
        lines.append(f"  {i}. {cells}")
    return "\n".join(lines)


def classify(client: Optional[LLMClient], record_set: RecordSet,
             *, sample_limit: int = 5) -> ContentClassification:
    """Classify one record set. Never raises — an unclassifiable set is UNKNOWN."""
    rule_type, rule_score, rule_scores = score_by_rules(record_set.columns)

    if record_set.is_empty:
        return ContentClassification(
            content_type=ContentType.UNKNOWN,
            confidence=0.0,
            reasoning="record set has no columns or no rows to judge",
            proposed_by="rules",
            rule_type=rule_type, rule_score=rule_score, rule_scores=rule_scores,
            needs_review=True,
            review_reasons=["nothing to classify"],
        )

    # --- no key: rules alone, said out loud -------------------------------
    if client is None or client.stub_mode:
        confident = rule_score >= 0.5 and rule_type != ContentType.UNKNOWN
        return ContentClassification(
            content_type=rule_type if confident else ContentType.UNKNOWN,
            confidence=rule_score if confident else 0.0,
            reasoning=(f"no AI key configured — classified by alias-table overlap "
                       f"alone ({rule_score:.0%} of columns matched "
                       f"{rule_type.value})"),
            proposed_by="rules (no AI key)",
            rule_type=rule_type, rule_score=rule_score, rule_scores=rule_scores,
            needs_review=True,
            review_reasons=["classified without AI — rule overlap only"],
        )

    # --- live ---------------------------------------------------------------
    prompt = PROMPT_TEMPLATE.format(
        allowed=", ".join(ct.value for ct in ContentType),
        sheet_name=record_set.origin.label,
        columns=", ".join(record_set.columns),
        sample_rows=_format_rows(record_set, sample_limit),
    )
    response = client.extract_json(
        task=f"content classification ({record_set.origin.label})",
        prompt=prompt,
        stub_key="content_classification",
        stub_context={"filename": record_set.origin.container},
        max_tokens=400,
    )

    if response.failed:
        # The live call broke. Fall back to rules, but never let the failure
        # look like a successful classification.
        return ContentClassification(
            content_type=rule_type if rule_score >= 0.5 else ContentType.UNKNOWN,
            confidence=0.0,
            reasoning="AI classification failed; fell back to alias overlap",
            proposed_by="rules (AI call failed)",
            rule_type=rule_type, rule_score=rule_score, rule_scores=rule_scores,
            needs_review=True,
            review_reasons=[LLM_FAILURE_MARKER],
            ai_note=response.notes,
        )

    data: Dict[str, Any] = response.data or {}
    ai_type = ContentType.parse(data.get("content_type"))
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(data.get("reasoning") or "").strip()

    review_reasons: List[str] = []
    agrees = rule_type != ContentType.UNKNOWN and rule_type == ai_type

    if agrees:
        # Two independent methods landed together — worth real confidence,
        # but never enough to exceed what the model itself claimed by more
        # than a small margin.
        confidence = min(1.0, max(confidence, 0.90))
    elif rule_type != ContentType.UNKNOWN:
        review_reasons.append(
            f"the alias table scored this as {rule_type.value} "
            f"({rule_score:.0%} of columns) but the model read it as "
            f"{ai_type.value} — two independent methods disagreed"
        )

    final_type = ai_type
    if confidence < UNKNOWN_BELOW:
        final_type = ContentType.UNKNOWN
        review_reasons.append(
            f"confidence {confidence:.0%} is below the {UNKNOWN_BELOW:.0%} floor — "
            f"held for a human to label rather than guessed"
        )
    elif confidence < REVIEW_BELOW:
        review_reasons.append(
            f"confidence {confidence:.0%} is below the {REVIEW_BELOW:.0%} bar"
        )

    if final_type == ContentType.UNKNOWN and not review_reasons:
        review_reasons.append("the model could not identify this content")

    return ContentClassification(
        content_type=final_type,
        confidence=confidence,
        reasoning=reasoning or "no reasoning returned",
        proposed_by=response.provenance,
        rule_type=rule_type, rule_score=rule_score, rule_scores=rule_scores,
        needs_review=bool(review_reasons),
        review_reasons=review_reasons,
        ai_note=response.notes,
    )
