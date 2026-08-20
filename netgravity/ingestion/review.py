"""
NetGravity — Human Review
==========================
Turns "this needs a human" into a specific, answerable question, and turns
the answer back into permanent memory.

WHAT WAS WRONG WITH THE OLD FLOW
--------------------------------
There was one action: `--confirm-mapping <distributor_id>`, which approved
an entire file's mapping in one keystroke. Two problems with that:

  1. ALL OR NOTHING. A reviewer who agreed with six columns and doubted the
     seventh had no way to say so. The realistic response to partial doubt is
     to approve everything and hope, which defeats the point of asking.

  2. NO QUESTION, JUST A FLAG. The reviewer saw "Qty -> quantity, 87%,
     needs review" and had to work out for themselves what the doubt even
     was. The system knew more than it was saying: it knew the alias table
     disagreed, or that two senders had used this column differently. None
     of that reached the person being asked.

WHAT THIS PRODUCES INSTEAD
--------------------------
One ReviewItem per open question, each carrying:

    question   generated from the actual evidence, e.g. "This column has
               meant units shipped for vendor_a and units returned for
               vendor_b — which is it here?" rather than a bare flag.
    options    the real candidates, most-supported first, each labelled with
               where it came from and how much backing it has.
    context    sample values from the file, the sheet it came from, and the
               reason this was escalated — everything needed to answer
               without opening the source file.

BUILT FOR A SCREEN, NOT BUILT AS ONE
------------------------------------
`build_request()` and `apply()` are plain data in, plain data out — dicts
that serialise cleanly over HTTP. No UI is built here. A future ingestion
console calls build_request() to render, and apply() to submit. The CLI uses
the identical pair, so both paths share one implementation and cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from netgravity.ingestion.memory.field_memory import (
    SCOPE_CONFLICT,
    SCOPE_SUGGESTED,
    FieldMemory,
)
from netgravity.ingestion.schemas.content import ContentType
from netgravity.ingestion.schemas.field_mapping import ColumnDecision, SheetMapping

KIND_COLUMN = "column_mapping"
KIND_CONTENT_TYPE = "content_type"

#: Sentinel a reviewer sends back to say "this column maps to nothing".
NOT_NEEDED = "__not_needed__"


@dataclass
class ReviewOption:
    """One selectable answer."""

    value: str
    label: str
    rationale: str = ""
    suggested_by: str = ""
    support: int = 0
    recommended: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "label": self.label,
            "rationale": self.rationale,
            "suggested_by": self.suggested_by,
            "support": self.support,
            "recommended": self.recommended,
        }


@dataclass
class ReviewItem:
    """One open question, with everything needed to answer it."""

    item_id: str
    kind: str
    question: str
    record_key: str
    source_id: str
    origin_label: str = ""
    content_type: str = ContentType.UNKNOWN.value
    source_column: Optional[str] = None
    proposed_value: Optional[str] = None
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    options: List[ReviewOption] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "question": self.question,
            "record_key": self.record_key,
            "source_id": self.source_id,
            "origin_label": self.origin_label,
            "content_type": self.content_type,
            "source_column": self.source_column,
            "proposed_value": self.proposed_value,
            "confidence": round(self.confidence, 3),
            "reasons": list(self.reasons),
            "options": [o.as_dict() for o in self.options],
            "context": dict(self.context),
        }


@dataclass
class ReviewRequest:
    """Everything awaiting a human for one ingestion run."""

    run_id: str = ""
    items: List[ReviewItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.items

    @property
    def summary(self) -> str:
        if self.is_empty:
            return "nothing needs review"
        columns = sum(1 for i in self.items if i.kind == KIND_COLUMN)
        sheets = sum(1 for i in self.items if i.kind == KIND_CONTENT_TYPE)
        parts = []
        if sheets:
            parts.append(f"{sheets} file(s) whose type could not be confirmed")
        if columns:
            parts.append(f"{columns} column(s) awaiting confirmation")
        return " and ".join(parts)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "summary": self.summary,
            "item_count": len(self.items),
            "items": [i.as_dict() for i in self.items],
        }


@dataclass
class ReviewDecision:
    """One answer coming back."""

    item_id: str
    value: str
    note: str = ""
    decided_by: str = "human"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ReviewDecision":
        return cls(
            item_id=str(raw.get("item_id") or ""),
            value=str(raw.get("value") or ""),
            note=str(raw.get("note") or ""),
            decided_by=str(raw.get("decided_by") or "human"),
        )


@dataclass
class ReviewOutcome:
    """What applying a batch of answers actually changed."""

    applied: List[str] = field(default_factory=list)
    remembered: List[str] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "applied": list(self.applied),
            "remembered": list(self.remembered),
            "rejected": list(self.rejected),
        }


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

def _column_question(decision: ColumnDecision, content_type: ContentType) -> str:
    """
    Ask about the actual doubt, not the generic fact of doubt.

    Ordered by how much the system already knows: a remembered disagreement
    is the most specific thing we can ask about, so it wins over a
    method-level disagreement, which in turn beats a bare first sighting.
    """
    column = decision.source_column

    if decision.memory_scope == SCOPE_CONFLICT:
        # Name the senders, not just the candidate fields — "vendor_a called
        # it X, vendor_d called it Y" is answerable; "it has meant two things"
        # is not.
        parts = [
            f"'{o.target_field}' ({o.rationale})" if o.rationale
            else f"'{o.target_field}'"
            for o in decision.options if o.suggested_by == "memory"
        ]
        joined = " and ".join(parts[:3]) if parts else "different things"
        return (f'"{column}" has meant {joined} before. '
                f"Which does it mean in this file?")

    if decision.methods_conflict:
        return (f'"{column}": the model read this as \'{decision.ai_target}\' '
                f"from the file's context, but the alias table says "
                f"'{decision.dictionary_target}'. Which is correct?")

    if decision.memory_scope == SCOPE_SUGGESTED:
        return (f'"{column}" was confirmed as \'{decision.target_field}\' once '
                f"before, by a different sender. Does it mean the same here?")

    if content_type.feeds_optimizer:
        return (f'"{column}" looks like \'{decision.target_field}\'. This file '
                f"feeds the optimiser directly, so please confirm before it is "
                f"used — it will be remembered afterwards.")

    return (f'"{column}" looks like \'{decision.target_field}\', but this is '
            f"the first time it has been seen and the alias table does not "
            f"recognise it. Is that right?")


def _content_type_question(mapping: SheetMapping) -> str:
    classification = mapping.classification
    if classification.content_type == ContentType.UNKNOWN:
        return (f"What kind of data is '{mapping.origin_label}'? It could not "
                f"be identified confidently enough to route automatically.")
    if not classification.rules_agree and \
            classification.rule_type != ContentType.UNKNOWN:
        return (f"'{mapping.origin_label}' was read as "
                f"{classification.content_type.value} by the model, but the "
                f"column names look more like "
                f"{classification.rule_type.value}. Which is it?")
    return (f"'{mapping.origin_label}' looks like "
            f"{classification.content_type.value} "
            f"({classification.confidence:.0%} confidence). Please confirm.")


def _column_options(decision: ColumnDecision) -> List[ReviewOption]:
    """
    Candidates, most-supported first, de-duplicated across sources.

    A field suggested by two methods appears ONCE, carrying both rationales —
    showing the same answer twice would misrepresent it as two choices.
    """
    merged: Dict[str, ReviewOption] = {}
    for option in decision.options:
        if not option.target_field:
            continue
        existing = merged.get(option.target_field)
        if existing is None:
            merged[option.target_field] = ReviewOption(
                value=option.target_field,
                label=option.target_field,
                rationale=option.rationale,
                suggested_by=option.suggested_by,
                support=option.support,
            )
        else:
            existing.suggested_by = f"{existing.suggested_by}+{option.suggested_by}"
            if option.rationale:
                existing.rationale = f"{existing.rationale}; {option.rationale}".strip("; ")
            existing.support = max(existing.support, option.support)

    options = sorted(merged.values(),
                     key=lambda o: (o.support, o.suggested_by == "memory"),
                     reverse=True)
    for option in options:
        option.recommended = option.value == decision.target_field

    options.append(ReviewOption(
        value=NOT_NEEDED,
        label="not needed — do not import this column",
        rationale="choose this when the column has no canonical meaning",
        suggested_by="reviewer",
    ))
    return options


def item_id_for(record_key: str, column: Optional[str] = None) -> str:
    """Stable id so an answer can be matched back to its question."""
    return f"{record_key}::{column}" if column else f"{record_key}::__content_type__"


# ---------------------------------------------------------------------------
# Request / response
# ---------------------------------------------------------------------------

def build_request(mappings: Sequence[SheetMapping],
                  run_id: str = "") -> ReviewRequest:
    """Collect everything awaiting a human across a whole run."""
    request = ReviewRequest(run_id=run_id)

    for mapping in mappings:
        if mapping.classification.needs_review:
            request.items.append(ReviewItem(
                item_id=item_id_for(mapping.record_key),
                kind=KIND_CONTENT_TYPE,
                question=_content_type_question(mapping),
                record_key=mapping.record_key,
                source_id=mapping.source_id,
                origin_label=mapping.origin_label,
                content_type=mapping.content_type.value,
                proposed_value=mapping.content_type.value,
                confidence=mapping.classification.confidence,
                reasons=list(mapping.classification.review_reasons),
                options=[
                    ReviewOption(
                        value=ct.value, label=ct.value,
                        rationale=f"routes to the {ct.destination} zone",
                        suggested_by=("model" if ct == mapping.content_type
                                      else ("alias table"
                                            if ct == mapping.classification.rule_type
                                            else "")),
                        recommended=ct == mapping.content_type,
                    )
                    for ct in ContentType
                ],
                context={
                    "columns": [d.source_column for d in mapping.decisions],
                    "model_said": mapping.classification.content_type.value,
                    "alias_table_said": mapping.classification.rule_type.value,
                    "alias_table_scores": mapping.classification.rule_scores,
                    "reasoning": mapping.classification.reasoning,
                    "destination_if_accepted": mapping.destination,
                },
            ))

        for decision in mapping.pending:
            request.items.append(ReviewItem(
                item_id=item_id_for(mapping.record_key, decision.source_column),
                kind=KIND_COLUMN,
                question=_column_question(decision, mapping.content_type),
                record_key=mapping.record_key,
                source_id=mapping.source_id,
                origin_label=mapping.origin_label,
                content_type=mapping.content_type.value,
                source_column=decision.source_column,
                proposed_value=decision.target_field,
                confidence=decision.confidence,
                reasons=list(decision.review_reasons),
                options=_column_options(decision),
                context={
                    "sample_values": list(decision.sample_values),
                    "model_said": decision.ai_target,
                    "model_confidence": round(decision.ai_confidence, 3),
                    "model_reasoning": decision.ai_reasoning,
                    "alias_table_said": decision.dictionary_target,
                    "memory_scope": decision.memory_scope,
                    "memory_rationale": decision.memory_rationale,
                    "feeds_optimizer": mapping.content_type.feeds_optimizer,
                },
            ))

    return request


def apply(request: ReviewRequest, decisions: Sequence[Any],
          mappings: Sequence[SheetMapping],
          memory: Optional[FieldMemory] = None) -> ReviewOutcome:
    """
    Apply answers: settle the decisions and write them into memory.

    Accepts ReviewDecision objects or plain dicts, so an HTTP handler can
    pass a parsed JSON body straight through.
    """
    outcome = ReviewOutcome()
    by_id = {item.item_id: item for item in request.items}
    by_key = {m.record_key: m for m in mappings}

    for raw in decisions:
        answer = raw if isinstance(raw, ReviewDecision) else ReviewDecision.from_dict(raw)
        item = by_id.get(answer.item_id)
        if item is None:
            outcome.rejected.append({
                "item_id": answer.item_id,
                "reason": "no open review item with this id",
            })
            continue
        if not answer.value:
            outcome.rejected.append({
                "item_id": answer.item_id, "reason": "no value supplied"})
            continue

        mapping = by_key.get(item.record_key)
        if mapping is None:
            outcome.rejected.append({
                "item_id": answer.item_id,
                "reason": f"no mapping found for '{item.record_key}'"})
            continue

        valid = {o.value for o in item.options}
        if answer.value not in valid:
            outcome.rejected.append({
                "item_id": answer.item_id,
                "reason": (f"'{answer.value}' is not one of the offered options "
                           f"for this item")})
            continue

        if item.kind == KIND_CONTENT_TYPE:
            chosen = ContentType.parse(answer.value)
            mapping.classification.content_type = chosen
            mapping.classification.needs_review = False
            mapping.classification.review_reasons = []
            mapping.classification.confidence = 1.0
            mapping.classification.proposed_by = answer.decided_by
            outcome.applied.append(answer.item_id)
            continue

        decision = next((d for d in mapping.decisions
                         if d.source_column == item.source_column), None)
        if decision is None:
            outcome.rejected.append({
                "item_id": answer.item_id,
                "reason": f"column '{item.source_column}' is no longer present"})
            continue

        if answer.value == NOT_NEEDED:
            decision.target_field = None
            decision.needs_review = False
            decision.review_reasons = []
            if decision.source_column not in mapping.unmapped_columns:
                mapping.unmapped_columns.append(decision.source_column)
            outcome.applied.append(answer.item_id)
            continue

        decision.target_field = answer.value
        decision.confidence = 1.0
        decision.decided_by = f"confirmed:{answer.decided_by}"
        decision.needs_review = False
        decision.review_reasons = []
        outcome.applied.append(answer.item_id)

        if memory is not None:
            memory.record(
                source_column=decision.source_column,
                target_field=answer.value,
                content_type=mapping.content_type.value,
                source_id=mapping.source_id,
                confirmed_by=answer.decided_by,
                note=answer.note,
            )
            outcome.remembered.append(
                f"{mapping.content_type.value}:{decision.source_column}"
                f" -> {answer.value}")

    return outcome
