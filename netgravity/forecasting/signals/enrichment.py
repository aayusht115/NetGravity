"""
External signals as an exogenous forecasting input.

    Extraction  →  ORCHESTRATOR (routes)  →  ForecastRequest.signals
                                                    ↓
                                        this module: HOW they apply
                                                    ↓
                                                 forecast

**This module answers "how", never "whether".** Every signal it sees was already
selected by `ExternalSignalRouter` in the orchestrator, which decided that a
signal may influence a forecast at all — guardrail passed, use permitted,
confidence sufficient, entity within the pinned network. The Forecasting Agent
does not re-adjudicate that and does not fetch signals of its own; it works from
the list on its request.

What is decided here is methodology: which bucket has a demand mechanism, in
which direction, by how much, and within what bounds.

This is the FORECASTING pathway. The other pathway — the one that ends in a
governed decision — is:

    ExternalSignal.event_probability  +  REI  →  RF = P + REI − P·REI

The two share no vocabulary and nothing converts between them. That separation
is the whole reason this module consumes `MarketIntelligenceSignal`, which by
design has no probability field in any spelling, rather than inventing a third
signal type. Phase 4A renamed that class out of a collision with the RF-eligible
`ExternalSignal` precisely so this boundary could be stated in the type system.

**Confidence is a gate, never a number.** `SignalConfidence.LOW` suppresses a
signal entirely; HIGH and MEDIUM let it through unchanged. It is never
multiplied into anything. Turning `HIGH` into `0.8` is the same error as turning
`SEVERE` into `P = 0.7`, and the fact that the product would land in a forecast
rather than in RF does not make it a measurement.

── What was replaced, and why ─────────────────────────────────────────────────
The source repository's `signals/fuser.py` did two things this does not.

1. It asked an LLM for the numbers. The prompt requested a `demand_multiplier`
   between 0.5 and 2.0, and every forecast point was then multiplied by
   whatever came back. A model answering "1.7" made demand 70% higher, with no
   history, no fitting and no validation behind it. A language model may not
   compute a deterministic forecast value.

2. Its offline fallback matched regexes against raw text — `(diwali|festival)`
   → ×1.25 — and compounded matches multiplicatively. Free text is not a
   signal; the ingestion pipeline already resolves text into a structured,
   guardrailed, entity-scoped signal, and reaching behind that to re-read the
   prose discards the resolution.

What survives is the shape of the idea: a bounded multiplicative adjustment,
attributed to a named rule. The coefficients below are **declared assumptions,
not estimated effects** — no history with labelled events was available to fit
them against. `SignalAdjustment.is_assumption` says so on every record, and
`ForecastPoint.baseline_mean` preserves the model's own answer alongside the
adjusted one so the two are never conflated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from netgravity.forecasting.schemas import (
    ForecastPoint,
    SignalAdjustment,
    SignalEffect,
)

logger = logging.getLogger(__name__)


#: Field names that would mean the object is a risk signal, not a market one.
#: Checked defensively: an orchestrator `ExternalSignal` passed here by mistake
#: must be refused, never quietly used as a forecasting feature.
_PROBABILITY_FIELDS = (
    "event_probability", "probability", "likelihood", "p_event", "prob",
)


@dataclass(frozen=True)
class BucketRule:
    """
    One declared assumption about how a signal bucket moves demand.

    `mean_multiplier` of 1.0 means the bucket does not move the central
    estimate at all — only the spread. That is the default for anything whose
    mechanism on *demand quantity* is not clear.
    """
    rule_id: str
    effect: SignalEffect
    mean_multiplier: float
    std_multiplier: float
    basis: str


#: (bucket, direction) → assumption.
#:
#: Two principles decide what is in here.
#:
#: **Only demand-side buckets may move the mean.** CUSTOMER and MACRO describe
#: forces acting on how much gets bought. CARRIER and SUPPLIER act on how goods
#: move and what they cost — real effects, but on lead time and rate, neither of
#: which this target forecasts. Letting a carrier strike raise forecast *demand*
#: would be a mechanism nobody could defend, so those buckets are absent and the
#: gap is reported rather than papered over.
#:
#: **When the mechanism is uncertain, widen instead of shift.** A cyclone
#: warning justifies "the next few periods are less predictable" far more
#: readily than "demand will be 15% lower". WEATHER therefore leaves the central
#: estimate alone and widens the band — which is what actually reaches the MILP,
#: through `DemandRecord.std_dev` and safety stock.
_RULES: Dict[Tuple[str, str], BucketRule] = {
    ("CUSTOMER", "UP"): BucketRule(
        "CUSTOMER_EXPANSION", SignalEffect.INCREASE, 1.10, 1.15,
        "customer expansion raises demand; magnitude is a declared assumption",
    ),
    ("CUSTOMER", "DOWN"): BucketRule(
        "CUSTOMER_CONTRACTION", SignalEffect.DECREASE, 0.90, 1.15,
        "customer contraction lowers demand; magnitude is a declared assumption",
    ),
    ("MACRO", "UP"): BucketRule(
        "MACRO_EXPANSION", SignalEffect.INCREASE, 1.05, 1.20,
        "macro expansion lifts demand modestly; magnitude is a declared assumption",
    ),
    ("MACRO", "DOWN"): BucketRule(
        "MACRO_CONTRACTION", SignalEffect.DECREASE, 0.95, 1.20,
        "macro contraction softens demand; magnitude is a declared assumption",
    ),
    ("WEATHER", "UP"): BucketRule(
        "WEATHER_UNCERTAINTY", SignalEffect.WIDEN, 1.00, 1.30,
        "weather disruption widens uncertainty; no defensible mean effect on demand",
    ),
    ("WEATHER", "DOWN"): BucketRule(
        "WEATHER_UNCERTAINTY", SignalEffect.WIDEN, 1.00, 1.30,
        "weather disruption widens uncertainty; no defensible mean effect on demand",
    ),
    ("WEATHER", "NEUTRAL"): BucketRule(
        "WEATHER_UNCERTAINTY", SignalEffect.WIDEN, 1.00, 1.30,
        "weather disruption widens uncertainty; no defensible mean effect on demand",
    ),
}

#: Buckets deliberately absent from `_RULES`, with the reason. Reported as a
#: warning when such a signal is offered, so "nothing happened" is visible
#: rather than looking like the signal was considered and found immaterial.
_NO_DEMAND_MECHANISM: Dict[str, str] = {
    "CARRIER": "carrier events affect lane cost and lead time, not demand quantity",
    "SUPPLIER": "supplier events affect inbound supply and lead time, not demand quantity",
    "COMPETITOR": "competitor signals are excluded by the ingestion guardrail by default",
    "UNKNOWN": "an unclassified signal has no defensible mechanism",
}

#: Cumulative bounds across every signal applied to one series. Without these,
#: four CUSTOMER_EXPANSION signals would compound to ×1.46 and eight to ×2.14.
#: The clamp is the source repository's idea and worth keeping.
_MEAN_FLOOR, _MEAN_CEILING = 0.5, 2.0
_STD_FLOOR, _STD_CEILING = 1.0, 3.0


class SignalEnricher:
    """
    Applies external-signal assumptions to a forecast.

    Deterministic and offline. No model call, no network access, no text
    parsing — it reads structured fields off an already-guardrailed signal.
    """

    def __init__(self) -> None:
        """
        No policy configuration, deliberately.

        Confidence thresholds, guardrail requirements and scope rules are
        routing policy and live on `ExternalSignalRouter` in the orchestrator.
        A second copy here would be a second place to change them, and the two
        would drift.
        """

    # ------------------------------------------------------------------

    def applicable(
        self,
        signal: Any,
        market_id: str,
    ) -> Tuple[bool, str]:
        """
        Decide whether a ROUTED signal has a demand mechanism for this series.

        This is a METHODOLOGY question, and it is the only kind this class
        answers. Whether the signal was permitted to reach forecasting at all —
        guardrail, permitted use, confidence, network scope — was settled by
        `ExternalSignalRouter` in the orchestrator before the request was built.
        Re-deciding it here would mean the Forecasting Agent adjudicating its
        own inputs, which is exactly the routing boundary the architecture
        places upstream.

        What remains are two mechanical questions:

        * does the signal name THIS market (a signal about Delhi does not move
          Mumbai — that is what the signal says, not a policy);
        * does its bucket have a declared effect on demand at all.

        The probability check below is a type guard, not routing. The router
        refuses risk signals at the boundary; this catches one that reached a
        directly-constructed `ForecastRequest` without passing through it.

        Returns:
            (allowed, reason). `reason` explains a refusal and is surfaced as a
            warning, so a signal that arrived and did nothing is visible.
        """
        for field in _PROBABILITY_FIELDS:
            if hasattr(signal, field):
                return False, (
                    f"signal carries '{field}' and is therefore a RISK signal, not "
                    f"market intelligence. Event likelihood belongs to the RF "
                    f"pathway and must never become a forecasting feature."
                )

        entities = list(getattr(signal, "affected_entities", []) or [])
        if market_id not in entities:
            return False, f"signal does not name market '{market_id}'"

        bucket = getattr(getattr(signal, "bucket", None), "value", "UNKNOWN")
        if bucket in _NO_DEMAND_MECHANISM:
            return False, (
                f"bucket {bucket} has no declared demand mechanism: "
                f"{_NO_DEMAND_MECHANISM[bucket]}"
            )

        direction = getattr(getattr(signal, "direction", None), "value", "NEUTRAL")
        if (bucket, direction) not in _RULES:
            return False, f"no declared rule for bucket={bucket} direction={direction}"

        return True, ""

    # ------------------------------------------------------------------

    def adjustments_for(
        self,
        signals: Sequence[Any],
        market_id: str,
    ) -> Tuple[List[SignalAdjustment], List[str]]:
        """
        Resolve the signals that apply to one market.

        Returns:
            (adjustments, warnings). Every supplied signal produces either an
            adjustment or a warning — none is silently discarded.
        """
        adjustments: List[SignalAdjustment] = []
        warnings: List[str] = []

        for signal in signals:
            signal_id = str(getattr(signal, "signal_id", "<unknown>"))
            allowed, reason = self.applicable(signal, market_id)
            if not allowed:
                warnings.append(f"signal '{signal_id}' not applied to {market_id}: {reason}")
                continue

            bucket = signal.bucket.value
            direction = signal.direction.value
            rule = _RULES[(bucket, direction)]

            adjustments.append(SignalAdjustment(
                signal_id=signal_id,
                bucket=bucket,
                direction=direction,
                effect=rule.effect,
                mean_multiplier=rule.mean_multiplier,
                std_multiplier=rule.std_multiplier,
                rule_id=rule.rule_id,
                basis=rule.basis,
                signal_confidence=getattr(signal.confidence, "value", ""),
                is_assumption=True,
            ))

        return adjustments, warnings

    # ------------------------------------------------------------------

    @staticmethod
    def apply(
        points: Sequence[ForecastPoint],
        adjustments: Sequence[SignalAdjustment],
    ) -> List[ForecastPoint]:
        """
        Apply resolved adjustments to a forecast.

        `baseline_mean` and `baseline_std_dev` are stamped onto every adjusted
        point, so the engine's own answer survives next to the adjusted one and
        the signal's contribution is always recoverable by subtraction. The
        source implementation multiplied the two together and kept only the
        product, which made "what did the model think" unanswerable.

        The quantile band is rebuilt around the adjusted centre using the
        adjusted spread. That imposes symmetry, which is wrong for the
        intermittent engine's deliberately zero-inflated band — so the service
        records a warning when a signal is applied to an intermittent series
        rather than letting the distortion pass unremarked.
        """
        if not adjustments:
            return list(points)

        mean_mult = 1.0
        std_mult = 1.0
        for adj in adjustments:
            mean_mult *= adj.mean_multiplier
            std_mult *= adj.std_multiplier

        mean_mult = max(_MEAN_FLOOR, min(_MEAN_CEILING, mean_mult))
        std_mult = max(_STD_FLOOR, min(_STD_CEILING, std_mult))

        adjusted: List[ForecastPoint] = []
        for pt in points:
            new_mean = max(0.0, pt.mean * mean_mult)
            new_std = max(0.0, pt.std_dev * std_mult)
            adjusted.append(ForecastPoint(
                period=pt.period,
                mean=new_mean,
                std_dev=new_std,
                p10=max(0.0, new_mean - 1.282 * new_std),
                p50=new_mean,
                p90=new_mean + 1.282 * new_std,
                baseline_mean=pt.mean,
                baseline_std_dev=pt.std_dev,
            ))
        return adjusted


__all__ = ["SignalEnricher", "BucketRule"]
