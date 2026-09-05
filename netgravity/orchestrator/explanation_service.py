"""
NetGravity — Explanation Service
==================================
One model request per completed analysis. Zero per view.

THE RULE THIS ENFORCES, agreed with Aayush:

    A new analysis can justify a new call; viewing the same analysis cannot.

So every explanation goes through `explain()`, which:

  1. fingerprints the RESULT (execution id, data version, the set of scenario
     ids — whatever identifies it);
  2. returns the saved explanation when one exists for that exact
     fingerprint, spending nothing;
  3. otherwise builds the evidence UPFRONT, makes one request, and saves it.

Step 3 passes `single_request=True`, which forbids the agent runtime whatever
the environment selects — that runtime reaches the model once per metric it
cites, which is an agent loop rather than a request.

Everything a screen needs for one analysis is produced together — the
explanation, the missing-data wording and the reasons for eligible suggested
tests — because they are one call's output, not three panes' worth of calls.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from netgravity.orchestrator.explanations import (
    ExplanationStore,
    SavedExplanation,
    fingerprint,
)
from netgravity.orchestrator.schemas.reasoning import ReasoningScope

logger = logging.getLogger(__name__)


def build_card(reasoning: Any, *, figures=None, details=None,
               source: Optional[str] = None) -> Dict[str, Any]:
    """
    The ONE card a screen renders: conclusion, meaning, warning, next step.

    Everything a reader sees passes through `card.clean()`, which drops any
    sentence grounding redacted and removes the first person. Figures are the
    caller's — code's — already formatted in the project's currency.
    """
    from netgravity.orchestrator.reasoning.card import (
        ExplanationCard,
        card_from_briefing,
        clean,
        count_redactions,
    )

    if reasoning is None:
        return ExplanationCard().as_dict()

    resolved_source = source or getattr(reasoning, "source", "template")
    briefing = getattr(reasoning, "briefing", None)
    notes = list(details or [])

    # Report the loss where it belongs — in the technical detail — rather
    # than mid-sentence, which is where it was appearing.
    dropped = count_redactions(getattr(reasoning, "summary", "") or "")
    if briefing is not None:
        dropped += count_redactions(briefing.visible_text())
    if dropped:
        notes.append(
            f"{dropped} figure(s) written here could not be matched to the "
            f"deterministic results and were removed before display.")

    if briefing is not None:
        card = card_from_briefing(briefing, figures=figures, details=notes,
                                  source=resolved_source)
    else:
        summary = clean(getattr(reasoning, "summary", "") or "", 260)
        risks = [clean(r, 220) for r in (getattr(reasoning, "risks", []) or [])]
        card = ExplanationCard(
            headline=summary[:140],
            meaning=summary[140:260].strip(),
            warning=risks[0] if risks else "",
            next_step=clean(getattr(reasoning, "recommendation", "") or "", 220),
            figures=list(figures or [])[:3],
            details=notes,
            source=resolved_source,
        )
    return card.as_dict()


def briefing_content(reasoning: Any) -> Dict[str, Any]:
    """
    A reasoning result in the shape the panes render.

    Selection only. Every field is already grounded — `numeric_grounding` has
    re-checked each numeric claim and stripped what it could not source — and
    is carried through as written.

    TWO SHAPES, because the two paths produce two. The template path builds a
    full `ExecutiveBriefing`; the single-request gateway path returns a
    `ReasoningResult` with `briefing=None` and its prose on the result itself.
    Both are handled here rather than by making the gateway path build a
    briefing, which would change code the chat layer also runs.
    """
    if reasoning is None:
        return {}
    briefing = getattr(reasoning, "briefing", None)
    if briefing is None:
        return _content_from_result(reasoning)
    return {
        "scope": briefing.scope.value,
        "entity_id": briefing.entity_id,
        "opening": briefing.opening,
        "context": briefing.context,
        "insights": [
            {
                "theme": item.theme,
                "headline": item.headline,
                "narrative": item.narrative,
                "severity": item.severity.value,
            }
            for item in briefing.kpi_insights
        ],
        "key_drivers": list(briefing.key_drivers),
        "recommendation": briefing.recommendation,
        "limitation": briefing.limitation,
        "evidence_completeness": briefing.evidence_completeness.value,
        "missing_information": [m.model_dump(mode="json")
                                for m in briefing.missing_information],
        "grounding": {
            "warnings": list(getattr(reasoning, "validation_warnings", [])),
        },
    }


def _content_from_result(reasoning: Any) -> Dict[str, Any]:
    """
    The gateway path's own fields, in the same shape.

    Nothing is invented: `summary`, `key_drivers`, `recommendation` and
    `risks` all exist on `ReasoningResult` and have been through the same
    grounding pass. The first risk becomes the limitation because that is
    what it is — the thing the reader has to carry.
    """
    summary = (getattr(reasoning, "summary", "") or "").strip()
    risks = list(getattr(reasoning, "risks", []) or [])
    if not summary and not risks:
        return {}
    return {
        "scope": "",
        "entity_id": None,
        "opening": summary,
        "context": "",
        # The gateway path returns one narrative rather than per-theme
        # insights, so there are none to list. An empty list is correct; a
        # fabricated theme to fill the pane would not be.
        "insights": [],
        "key_drivers": list(getattr(reasoning, "key_drivers", []) or []),
        "recommendation": getattr(reasoning, "recommendation", "") or "",
        "limitation": risks[0] if risks else "",
        "evidence_completeness": "COMPLETE",
        "missing_information": [],
        "grounding": {
            "warnings": list(getattr(reasoning, "validation_warnings", [])),
        },
    }


class ExplanationService:
    """Produces an explanation once per analysis, and serves it thereafter."""

    def __init__(self, reasoning_agent: Any, store: ExplanationStore):
        self.reasoning_agent = reasoning_agent
        self.store = store
        #: Model requests spent by THIS instance. Asserted in tests: the count
        #: must not move when the same analysis is viewed again.
        self.model_requests = 0

    def explain(
        self,
        *,
        subject_id: str,
        kind: str,
        scope: ReasoningScope,
        result_parts: List[Any],
        build_payload: Callable[[], Dict[str, Any]],
        entity_id: Optional[str] = None,
        unavailable_evidence: Optional[Dict[str, Any]] = None,
        provenance: Optional[Dict[str, str]] = None,
        allow_llm: bool = False,
        extras: Optional[Dict[str, Any]] = None,
        figures: Optional[List[Any]] = None,
        details: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        The explanation for one analysis.

        `result_parts` identifies the result — a cache HIT on it means this
        exact analysis has already been explained and nothing is spent.
        `build_payload` is deferred so the evidence is only assembled on a
        miss; on a hit it is never called.

        `extras` are additional already-computed blocks saved alongside the
        briefing (missing-data findings, eligible suggestions) so a screen
        gets them from the same record rather than asking again.

        Never raises: an explanation is advisory, and failing to produce one
        must not take down results that are perfectly good.
        """
        result_fingerprint = fingerprint(*result_parts)
        try:
            saved = self.store.get(subject_id, kind, result_fingerprint)
        except Exception as exc:  # noqa: BLE001 — a bad read is not fatal
            logger.warning("explanation.read_failed subject=%s kind=%s: %s",
                           subject_id, kind, exc)
            saved = None

        if saved is not None:
            logger.info("explanation.hit subject=%s kind=%s fingerprint=%s "
                        "source=%s", subject_id, kind, result_fingerprint,
                        saved.source)
            content = dict(saved.content)
            # Marked as served from the store, so a screen can say so and a
            # developer can tell "AI wrote this" from "AI wrote this once".
            card = dict(content.get("card") or {})
            card["cached"] = True
            content["card"] = card
            content["cached"] = True
            return content

        try:
            payload = build_payload()
        except Exception as exc:  # noqa: BLE001
            logger.warning("explanation.payload_failed subject=%s kind=%s: %s",
                           subject_id, kind, exc)
            return {}

        before = _runtime_calls(self.reasoning_agent)
        try:
            reasoning = self.reasoning_agent.reason(
                payload,
                unavailable_evidence=unavailable_evidence,
                provenance=provenance,
                allow_llm=allow_llm,
                scope=scope,
                entity_id=entity_id,
                # The promise. See the module docstring.
                single_request=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("explanation.reason_failed subject=%s kind=%s: %s",
                           subject_id, kind, exc)
            return {}

        spent = max(0, _runtime_calls(self.reasoning_agent) - before)
        self.model_requests += spent

        content = briefing_content(reasoning)
        if not content:
            return {}
        content.update(extras or {})
        content["source"] = getattr(reasoning, "source", "template")
        content["cached"] = False
        # The card the screens actually render. `figures` come from the
        # caller — code — already formatted in the project's currency, so a
        # model can never state a number in the wrong one.
        content["card"] = build_card(
            reasoning, figures=figures, details=details,
            source=content["source"])

        try:
            self.store.put(SavedExplanation(
                subject_id=subject_id, kind=kind,
                result_fingerprint=result_fingerprint,
                content=content,
                source=content["source"],
                model_requests=spent,
            ))
        except Exception as exc:  # noqa: BLE001 — the explanation still stands
            logger.warning("explanation.save_failed subject=%s kind=%s: %s",
                           subject_id, kind, exc)

        logger.info(
            "explanation.miss subject=%s kind=%s fingerprint=%s source=%s requests=%d",
            subject_id, kind, result_fingerprint, content["source"], spent,
        )
        return content


def _runtime_calls(agent: Any) -> int:
    """
    Model requests the agent has made so far.

    Read off the gateway when it counts, so "one request per analysis" is a
    measured number rather than an assurance. Zero when nothing counts — the
    template path spends none.
    """
    for holder, attr in (
        # An explicit counter wins. Nothing in production defines
        # `call_count`, so this can only be a deliberate one — the counting
        # gateway the one-request tests measure with.
        (getattr(agent, "gateway", None), "call_count"),
        # LLMGateway's own tally of requests made by this process. It is
        # incremented inside `generate()` (llm_gateway.py), so it moves for
        # the real thing and stays put for a subclass that overrides it.
        (getattr(agent, "gateway", None), "_total_requests"),
        # The agents runtime counts its own runs. Read for completeness; a
        # single_request call never reaches it.
        (getattr(agent, "runtime", None), "_calls"),
    ):
        calls = getattr(holder, attr, None)
        if isinstance(calls, int):
            return calls
    return 0
