"""Short, schema-checked AI suggestions for one review item.

The model never authors UI prose directly. It returns four small fields; this
module validates them and constructs a bounded display message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from netgravity.ingestion.ai.client import LLMClient
from netgravity.ingestion.review import KEEP_UNRESOLVED, ReviewItem

MAX_RECOMMENDATION_WORDS = 8
MAX_REASON_WORDS = 18
MAX_QUESTION_WORDS = 12
MAX_DISPLAY_WORDS = 35


PROMPT = """You are helping a logistics consultant classify one uploaded column.

COLUMN REVIEW ITEM:
{item}

USER QUESTION OR CONTEXT:
{user_text}

Return one JSON object with exactly these fields:
{{
  "recommendation": "one allowed value",
  "reason": "at most 18 words using only supplied evidence",
  "question": "at most 12 words asking for the one missing fact",
  "missing_information": ["unit", "period"]
}}

Rules:
- recommendation must be one of: {allowed}
- never invent a new optimiser field, unit, value, or client fact
- keep the answer crisp; no paragraphs and no hidden reasoning
- if evidence is insufficient, recommend {unresolved}
"""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def _clip(value: Any, limit: int) -> str:
    words = _clean(value).split()
    return " ".join(words[:limit])


@dataclass
class ClarificationSuggestion:
    recommendation: str
    reason: str
    question: str
    missing_information: List[str] = field(default_factory=list)
    display: str = ""
    stubbed: bool = False
    valid: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "reason": self.reason,
            "question": self.question,
            "missing_information": list(self.missing_information),
            "display": self.display,
            "stubbed": self.stubbed,
            "valid": self.valid,
            "limits": {
                "recommendation_words": MAX_RECOMMENDATION_WORDS,
                "reason_words": MAX_REASON_WORDS,
                "question_words": MAX_QUESTION_WORDS,
                "display_words": MAX_DISPLAY_WORDS,
            },
        }


def analyse(client: LLMClient, item: ReviewItem,
            user_text: str = "") -> ClarificationSuggestion:
    """Return a bounded suggestion; malformed model output becomes unresolved."""
    option_values = [option.value for option in item.options]
    canonical = list(item.context.get("allowed_canonical_fields") or [])
    allowed = list(dict.fromkeys(option_values + canonical))
    if item.proposed_value:
        allowed.insert(0, item.proposed_value)
    allowed = list(dict.fromkeys(allowed))
    if KEEP_UNRESOLVED not in allowed:
        allowed.append(KEEP_UNRESOLVED)

    response = client.extract_json(
        task=f"field clarification ({item.source_column or item.origin_label})",
        prompt=PROMPT.format(
            item=item.as_dict(),
            user_text=_clip(user_text, 80) or "No additional context supplied.",
            allowed=", ".join(allowed),
            unresolved=KEEP_UNRESOLVED,
        ),
        stub_key="field_clarification",
        stub_context={
            "proposed_value": item.proposed_value,
            "column": item.source_column,
            "allowed": allowed,
        },
        max_tokens=300,
    )

    raw = response.data or {}
    recommendation = _clean(raw.get("recommendation"))
    valid = (recommendation in allowed
             and len(recommendation.split()) <= MAX_RECOMMENDATION_WORDS)
    if not valid:
        recommendation = KEEP_UNRESOLVED

    reason = _clip(raw.get("reason"), MAX_REASON_WORDS)
    question = _clip(raw.get("question"), MAX_QUESTION_WORDS)
    if not reason:
        reason = "Available evidence is insufficient."
    if not question:
        question = "Can you confirm its meaning?"

    missing = [
        _clip(value, 3) for value in (raw.get("missing_information") or [])
        if _clip(value, 3)
    ][:4]
    display = _clip(f"Likely: {recommendation}. {reason} {question}", MAX_DISPLAY_WORDS)
    return ClarificationSuggestion(
        recommendation=recommendation,
        reason=reason,
        question=question,
        missing_information=missing,
        display=display,
        stubbed=response.stubbed,
        valid=valid,
    )
