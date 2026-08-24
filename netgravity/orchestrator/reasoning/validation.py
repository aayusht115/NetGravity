"""Deterministic structural validation for Reasoning Agent output."""

from __future__ import annotations

import re
from typing import List

from netgravity.orchestrator.schemas.reasoning import ReasoningDraft, ReasoningEvidencePack


_FIRST_PERSON = re.compile(r"\b(?:I|I'm|I've|I'd|I'll|my)\b", re.IGNORECASE)
_COLLECTIVE = re.compile(r"\b(?:we|we're|we've|our|ours)\b", re.IGNORECASE)


def validate_reasoning_draft(
    draft: ReasoningDraft,
    evidence: ReasoningEvidencePack,
) -> List[str]:
    """Return contract violations. Callers fail closed to the template path."""
    errors: List[str] = []
    if not draft.opening.strip():
        errors.append("opening is empty")
    elif not _FIRST_PERSON.search(draft.opening):
        errors.append("opening must use first-person singular")
    if draft.recommendation and not _FIRST_PERSON.search(draft.recommendation):
        errors.append("recommendation must use first-person singular")
    if _COLLECTIVE.search(draft.visible_text()):
        errors.append("briefing must not use collective voice (we/our)")

    limit = 3 if evidence.scope.value in {"FACILITY", "LANE"} else 4
    if len(draft.kpi_insights) > limit:
        errors.append(f"scope permits at most {limit} KPI insights")
    if len(draft.missing_information) > 2:
        errors.append("at most two missing-information questions are allowed")
    if draft.scope != evidence.scope:
        errors.append("output scope does not match request scope")
    if draft.entity_id != evidence.entity_id:
        errors.append("output entity does not match the selected entity")

    referenced = set(draft.evidence_refs)
    for insight in draft.kpi_insights:
        referenced.update(insight.metric_refs)
        referenced.update(insight.comparison_refs)
        referenced.update(insight.driver_refs)
    unknown = sorted(ref for ref in referenced if ref not in evidence.metrics)
    if unknown:
        errors.append(f"unknown evidence refs: {', '.join(unknown[:5])}")

    known_question_refs = set(evidence.unavailable) | set(evidence.metrics)
    bad_questions = [item.question_ref for item in draft.missing_information
                     if item.question_ref not in known_question_refs]
    if bad_questions:
        errors.append(f"unknown missing-information refs: {', '.join(bad_questions[:2])}")
    return errors
