"""
Orchestrator — external-signal routing.

    Extraction / Parsing Agent
            ↓
    structured MarketIntelligenceSignal objects
            ↓
        ORCHESTRATOR  ← this module decides where each signal may go
            ↓                    ↓
      Forecasting Agent      RF pathway
                          (only with a genuine event_probability)

**This is the decision layer, and it lives here on purpose.**

Extraction structures signals. It classifies them — bucket, direction,
guardrail verdict, a policy-derived `scenario_use` — and stops. It does not
decide that a signal will influence a forecast, because a classification made
while parsing a news item cannot know which workflow is running, which snapshot
is pinned, or which entities are in scope.

The Forecasting Agent does not decide either. It receives a list of signals on
its request and works out *how* they change the numbers; it never asks whether
it should have been given them. That is what keeps it from independently
consuming the signal source.

So the routing decision is made exactly once, here, and recorded. The split is:

    ORCHESTRATOR (this module)     may this signal influence a forecast?
                                   — guardrail passed, use permitted,
                                     confidence sufficient, entity in scope,
                                     and not a risk signal

    FORECASTING (enrichment.py)    given that it may, how does it move the
                                   numbers? — bucket mechanism, direction,
                                   magnitude, bounds

`scenario_use` is read here as EVIDENCE from extraction, not as an instruction.
Extraction saying `FORECAST_ENRICHMENT` means "this is the kind of signal that
can enrich a forecast"; the orchestrator decides whether it does.

── The RF pathway is separate and stays separate ──────────────────────────────
The same real-world event may legitimately reach both pathways, but never
through the same object and never through an inference. An RF-eligible signal
carries `event_probability`, a stated likelihood. A market-intelligence signal
carries `confidence` — HIGH, MEDIUM, LOW — which is a judgement about the
SOURCE, not a probability of the event.

Anything carrying a probability field is refused as a forecasting input here,
and nothing in this module reads `confidence`, `severity`, `magnitude` or
`direction` and produces a number from it. Confidence is used as a threshold on
whether to route at all, never as a quantity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


#: Attribute names that mark an object as a RISK signal rather than market
#: intelligence. An orchestrator `ExternalSignal` reaching the forecasting
#: pathway would carry a stated event likelihood into a demand estimate, so it
#: is refused at the boundary rather than deeper in.
_PROBABILITY_FIELDS = (
    "event_probability", "probability", "likelihood", "p_event", "prob",
)

#: Extraction's classification of what a signal may be used for. Only this one
#: is eligible to reach the forecaster.
_FORECAST_USE = "FORECAST_ENRICHMENT"

#: Confidence grades permitted to influence a forecast, in descending order.
#: A GATE, not a coefficient — nothing multiplies by these.
_DEFAULT_MIN_CONFIDENCE = ("HIGH", "MEDIUM")


class RoutingOutcome(str, Enum):
    """What the orchestrator decided about one signal."""
    ROUTED_TO_FORECASTING = "ROUTED_TO_FORECASTING"
    #: Structurally ineligible — it is a risk signal, not market intelligence.
    REFUSED_RISK_SIGNAL = "REFUSED_RISK_SIGNAL"
    #: Extraction classified it as something other than forecast enrichment.
    NOT_FORECAST_USE = "NOT_FORECAST_USE"
    #: Did not clear the ingestion relevance guardrail.
    GUARDRAIL_NOT_PASSED = "GUARDRAIL_NOT_PASSED"
    #: Confidence below the routing threshold.
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    #: Names no entity, or none that this network contains.
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


@dataclass(frozen=True)
class SignalRoutingRecord:
    """One signal, and what the orchestrator decided about it."""
    signal_id: str
    outcome: RoutingOutcome
    reason: str = ""
    bucket: str = ""
    matched_entities: Tuple[str, ...] = ()

    @property
    def routed(self) -> bool:
        return self.outcome is RoutingOutcome.ROUTED_TO_FORECASTING


@dataclass
class SignalRoutingDecision:
    """
    The orchestrator's complete routing decision for one forecast request.

    Every signal offered appears in `records`, routed or not. A signal that was
    supplied and silently ignored would leave a reader unable to tell "there was
    no signal" from "there was one and we dropped it".
    """
    #: Signals the orchestrator is passing to the Forecasting Agent.
    accepted: List[Any] = field(default_factory=list)
    records: List[SignalRoutingRecord] = field(default_factory=list)

    @property
    def rejected(self) -> List[SignalRoutingRecord]:
        return [r for r in self.records if not r.routed]

    @property
    def accepted_ids(self) -> List[str]:
        return [r.signal_id for r in self.records if r.routed]

    def outcome_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for record in self.records:
            counts[record.outcome.value] = counts.get(record.outcome.value, 0) + 1
        return counts

    def audit_rows(self) -> List[Dict[str, Any]]:
        """Flat rows for the execution trace."""
        return [
            {
                "signal_id": r.signal_id, "outcome": r.outcome.value,
                "bucket": r.bucket, "reason": r.reason,
                "matched_entities": list(r.matched_entities),
            }
            for r in self.records
        ]


def _enum_value(obj: Any, attr: str, default: str = "") -> str:
    """Read `obj.attr.value`, tolerating a plain string or a missing field."""
    raw = getattr(obj, attr, None)
    if raw is None:
        return default
    return str(getattr(raw, "value", raw))


class ExternalSignalRouter:
    """
    Decides which extracted signals may reach the Forecasting Agent.

    Holds no state between calls and performs no I/O. It reads structured
    fields off signals the Extraction Agent already produced — it never fetches
    a signal, parses text, or calls a model.
    """

    def __init__(
        self,
        *,
        min_confidence: Sequence[str] = _DEFAULT_MIN_CONFIDENCE,
        require_entity_scope: bool = True,
    ) -> None:
        """
        Args:
            min_confidence: Confidence grades permitted through. Used as a
                threshold on routing; never as a numeric weight.
            require_entity_scope: When True, a signal must name at least one
                entity the network contains. An unscoped signal would otherwise
                apply to every market at once, which is a broad silent effect
                worth refusing by default.
        """
        self.min_confidence = tuple(min_confidence)
        self.require_entity_scope = require_entity_scope

    # ------------------------------------------------------------------

    def route_for_forecast(
        self,
        signals: Iterable[Any],
        *,
        known_entity_ids: Optional[Set[str]] = None,
    ) -> SignalRoutingDecision:
        """
        Select the signals that may inform a forecast.

        Args:
            signals: Structured signals from the Extraction Agent.
            known_entity_ids: Entities the pinned network contains. A signal
                naming only entities outside it is out of scope, however
                relevant it is in general.

        Returns:
            The decision, carrying both the accepted signals and a record for
            every signal considered.
        """
        decision = SignalRoutingDecision()
        scope = known_entity_ids or set()

        for signal in signals:
            signal_id = str(getattr(signal, "signal_id", "<unknown>"))
            bucket = _enum_value(signal, "bucket", "UNKNOWN")

            # ---- structural refusal: this is a risk signal ----------------
            carried = next(
                (f for f in _PROBABILITY_FIELDS if hasattr(signal, f)), None,
            )
            if carried is not None:
                decision.records.append(SignalRoutingRecord(
                    signal_id=signal_id,
                    outcome=RoutingOutcome.REFUSED_RISK_SIGNAL,
                    bucket=bucket,
                    reason=(
                        f"carries '{carried}' and is therefore a risk signal. Event "
                        f"likelihood belongs to the RF pathway (P + REI - P*REI) and "
                        f"is never a forecasting input."
                    ),
                ))
                continue

            # ---- extraction's classification, read as evidence ------------
            scenario_use = _enum_value(signal, "scenario_use", "LOGGED_ONLY")
            if scenario_use != _FORECAST_USE:
                decision.records.append(SignalRoutingRecord(
                    signal_id=signal_id,
                    outcome=RoutingOutcome.NOT_FORECAST_USE, bucket=bucket,
                    reason=(
                        f"extraction classified this signal as {scenario_use}, not "
                        f"{_FORECAST_USE}"
                    ),
                ))
                continue

            if not bool(getattr(signal, "passed_guardrail", False)):
                decision.records.append(SignalRoutingRecord(
                    signal_id=signal_id,
                    outcome=RoutingOutcome.GUARDRAIL_NOT_PASSED, bucket=bucket,
                    reason="did not clear the ingestion relevance guardrail",
                ))
                continue

            confidence = _enum_value(signal, "confidence", "")
            if confidence not in self.min_confidence:
                decision.records.append(SignalRoutingRecord(
                    signal_id=signal_id,
                    outcome=RoutingOutcome.LOW_CONFIDENCE, bucket=bucket,
                    reason=(
                        f"confidence {confidence or 'UNSET'} is below the routing "
                        f"threshold {list(self.min_confidence)}"
                    ),
                ))
                continue

            # ---- entity scope ---------------------------------------------
            entities = tuple(getattr(signal, "affected_entities", ()) or ())
            matched = tuple(e for e in entities if e in scope) if scope else entities

            if self.require_entity_scope and not matched:
                decision.records.append(SignalRoutingRecord(
                    signal_id=signal_id,
                    outcome=RoutingOutcome.OUT_OF_SCOPE, bucket=bucket,
                    reason=(
                        "names no entity in the pinned network"
                        if entities else
                        "names no affected entities, so it cannot be scoped"
                    ),
                ))
                continue

            decision.accepted.append(signal)
            decision.records.append(SignalRoutingRecord(
                signal_id=signal_id,
                outcome=RoutingOutcome.ROUTED_TO_FORECASTING,
                bucket=bucket, matched_entities=matched,
                reason="eligible market intelligence within network scope",
            ))

        logger.info(
            "orchestrator.signals.routed accepted=%d considered=%d outcomes=%s",
            len(decision.accepted), len(decision.records), decision.outcome_counts(),
        )
        return decision


__all__ = [
    "ExternalSignalRouter",
    "SignalRoutingDecision",
    "SignalRoutingRecord",
    "RoutingOutcome",
]
