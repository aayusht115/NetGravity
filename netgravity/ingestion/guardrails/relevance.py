"""
NetGravity — Signal Relevance Scoring
======================================
The gate every external signal must pass before it can influence the
forecast (Layer 3) or a scenario (Layer 4).

AUDITABILITY IS THE POINT
-------------------------
Signals that fail are NOT discarded. They are stored with passed=False and a
written reason, so a planner can see what the system chose to ignore and why.
A silent filter would be indistinguishable from a broken one.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional, Set

from netgravity.ingestion.guardrails.buckets import GuardrailPolicy, load_policy
from netgravity.ingestion.schemas.signal import (
    ExternalSignal,
    GuardrailVerdict,
    ScenarioUse,
    SignalBucket,
)


def evaluate(signal: ExternalSignal, *,
             known_entity_ids: Optional[Set[str]] = None,
             policy: Optional[GuardrailPolicy] = None,
             today: Optional[date] = None) -> GuardrailVerdict:
    """Score one signal and decide whether it may reach the optimizer."""
    policy = policy or load_policy()
    known_entity_ids = known_entity_ids or set()

    # 1. Classify if the bucket was not already set
    bucket = signal.bucket
    if bucket == SignalBucket.UNKNOWN:
        haystack = " ".join([
            signal.title, signal.rationale, signal.magnitude,
            signal.geography, signal.source_title,
        ])
        bucket = policy.classify(haystack)

    bp = policy.for_bucket(bucket)

    # 2. Hard exclusion — competitor news, by team decision
    if bp.excluded_by_default:
        return GuardrailVerdict(
            passed=False, bucket=bucket, relevance_score=bp.base_relevance,
            threshold=bp.threshold,
            reason=f"{bucket.value} signals are excluded by default "
                   f"(team review: low signal-to-noise). Logged for visibility only.",
        )

    # 3. Score
    score = bp.base_relevance
    reasons = [f"base {bp.base_relevance:.2f} for {bucket.value}"]

    matched = sorted(set(signal.affected_entities) & known_entity_ids)
    if matched:
        score += policy.entity_match_bonus
        reasons.append(f"+{policy.entity_match_bonus:.2f} names our nodes "
                       f"({', '.join(matched)})")

    conf_bonus = policy.confidence_bonus.get(signal.confidence.value, 0.0)
    if conf_bonus:
        score += conf_bonus
        reasons.append(f"{conf_bonus:+.2f} source confidence {signal.confidence.value}")

    # 4. Time-boxing — a stale disruption must not remain an active assumption
    if bp.expiry_days and signal.effective_date:
        expired, age = _is_expired(signal.effective_date, bp.expiry_days, today)
        if expired:
            return GuardrailVerdict(
                passed=False, bucket=bucket, relevance_score=score,
                threshold=bp.threshold, matched_entities=matched,
                reason=f"{bucket.value} signal expired "
                       f"({age} days old, window {bp.expiry_days} days).",
            )

    # 5. Materiality — small moves are noted, not surfaced; large ones earn credit
    if bp.materiality_pct is not None:
        magnitude = _extract_pct(signal.magnitude)
        if magnitude is not None:
            if abs(magnitude) < bp.materiality_pct:
                return GuardrailVerdict(
                    passed=False, bucket=bucket, relevance_score=score,
                    threshold=bp.threshold, matched_entities=matched,
                    reason=f"movement of {magnitude:.1f}% is below the "
                           f"{bp.materiality_pct:.1f}% materiality threshold for "
                           f"{bucket.value}. Logged, not surfaced.",
                )
            # Network-wide signals (fuel, duty, policy) name no individual node,
            # so they never earn entity_match_bonus. Clearing the materiality bar
            # is the evidence of relevance in their case.
            score += policy.materiality_bonus
            reasons.append(f"+{policy.materiality_bonus:.2f} magnitude "
                           f"{abs(magnitude):.1f}% clears the "
                           f"{bp.materiality_pct:.1f}% materiality bar")

    score = max(0.0, min(1.0, score))
    passed = score >= bp.threshold

    verdict_word = "passes" if passed else "does not reach"
    return GuardrailVerdict(
        passed=passed, bucket=bucket, relevance_score=score,
        threshold=bp.threshold, matched_entities=matched,
        reason=f"score {score:.2f} {verdict_word} threshold {bp.threshold:.2f} "
               f"({'; '.join(reasons)}).",
    )


def apply(signals: Iterable[ExternalSignal], *,
          known_entity_ids: Optional[Set[str]] = None,
          policy: Optional[GuardrailPolicy] = None) -> list[ExternalSignal]:
    """
    Attach a verdict to every signal and set its permitted downstream use.

    Returns ALL signals — passed and filtered alike. Filtering happens at the
    point of consumption by checking `signal.passed_guardrail`, never by
    dropping records here.
    """
    policy = policy or load_policy()
    out = []
    for s in signals:
        verdict = evaluate(s, known_entity_ids=known_entity_ids, policy=policy)
        s.verdict = verdict
        s.bucket = verdict.bucket
        s.scenario_use = (policy.for_bucket(verdict.bucket).scenario_use
                          if verdict.passed else ScenarioUse.LOGGED_ONLY)
        out.append(s)
    return out


def _extract_pct(text: str) -> Optional[float]:
    """Pull a percentage magnitude out of free text like '+8% fuel cost'."""
    import re
    m = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", text or "")
    return float(m.group(1)) if m else None


def _is_expired(effective_date: str, window_days: int,
                today: Optional[date]) -> tuple[bool, int]:
    today = today or date.today()
    try:
        eff = datetime.fromisoformat(effective_date[:10]).date()
    except (ValueError, TypeError):
        return False, 0
    age = (today - eff).days
    return age > window_days, age
