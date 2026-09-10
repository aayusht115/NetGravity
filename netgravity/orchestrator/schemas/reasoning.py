"""Typed contracts for executive, evidence-backed reasoning."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


REASONING_SCHEMA_VERSION = "2.0"


class ReasoningScope(str, Enum):
    """The level at which an insight is being requested."""

    NETWORK = "NETWORK"
    FACILITY = "FACILITY"
    LANE = "LANE"
    SCENARIO = "SCENARIO"
    COMPARISON = "COMPARISON"
    RESILIENCE = "RESILIENCE"
    INGESTION = "INGESTION"
    #: A projection of demand, not a statement about the network as it stands.
    #: Its absence is why the Forecast screen showed the NETWORK briefing —
    #: correct about the network, and captioning the wrong picture.
    FORECAST = "FORECAST"


class EvidenceCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"


class EvidenceMetric(BaseModel):
    """One deterministic value addressable by a stable evidence reference."""

    ref: str
    label: str
    value: Any = None
    display_value: str = ""
    unit: str = ""
    source: str
    scope: ReasoningScope = ReasoningScope.NETWORK
    entity_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid", frozen=True)


class MissingInformation(BaseModel):
    """A concise question whose answer would materially improve the insight."""

    question_ref: str
    question: str = Field(max_length=180)
    impact: str = Field(default="", max_length=240)
    blocking: bool = False

    model_config = ConfigDict(extra="forbid")


class InsightSeverity(str, Enum):
    """
    What KIND of finding an insight is.

    Stated by the engine that made the finding, because the engine is the only
    thing that knows. The dashboard was deciding this by keyword-matching the
    prose — looking for the strings "high impact", "opportunity", "positive" —
    which meant the colour, the icon and the priority of every card on the Home
    feed depended on incidental wording, and any insight phrased differently
    fell through to a neutral "Status" whatever it had found.
    """
    #: Something is wrong, at its limit, or unserved. Needs attention.
    RISK = "RISK"
    #: Nothing is wrong, but there is money or service to be had by changing
    #: something. Worth testing, not urgent.
    OPPORTUNITY = "OPPORTUNITY"
    #: A fact worth stating that asks for no decision.
    INFORMATION = "INFORMATION"


class KPIInsight(BaseModel):
    """Narrative-first KPI interpretation for an executive UI card."""

    theme: str = Field(max_length=60)
    headline: str = Field(max_length=140)
    narrative: str = Field(max_length=700)
    #: What to DO about this finding — one plain-English step, no figures.
    #:
    #: A finding without an action is a fact a reader has to translate into a
    #: decision on their own, and the Overview's insight tiles are read by
    #: people who want the decision. It is deliberately prose-only: the model
    #: writes no numbers anywhere (see `reasoning/card.py`), so this line
    #: cannot drift from the authoritative figures beside it, and numeric
    #: grounding has nothing here to redact.
    #:
    #: Optional, and empty is a legitimate value: the deterministic template
    #: path does not write one, and the presentation layer supplies a
    #: theme-appropriate default rather than leaving the tile mute.
    recommended_action: str = Field(default="", max_length=200)
    #: WHICH KIND of intervention answers this finding — one of
    #: ``strategic_actions.ACTION_KEYS``, or empty.
    #:
    #: The presentation layer derives a real, site-named recommendation from
    #: the solved rows (see `app/backend/api/insights.py`), and it chooses the
    #: rung of the ladder from the finding's theme and severity. That is enough
    #: for almost every theme and not enough for two of them: a "Footprint"
    #: opportunity is either "a candidate site is going unused" — answered by
    #: opening one — or "an open site costs more than the routing it saves" —
    #: answered by closing one. Same theme, same severity, opposite decisions.
    #:
    #: Set it only where the theme does not already imply the answer. Empty is
    #: the normal value, and the LLM path never sets it: a model choosing the
    #: intervention is the thing the deterministic ladder exists to prevent.
    action_hint: str = Field(default="", max_length=32)
    severity: InsightSeverity = InsightSeverity.INFORMATION
    metric_refs: List[str] = Field(default_factory=list, max_length=6)
    comparison_refs: List[str] = Field(default_factory=list, max_length=4)
    driver_refs: List[str] = Field(default_factory=list, max_length=6)

    model_config = ConfigDict(extra="forbid")


class ExecutiveBriefing(BaseModel):
    """UI-ready briefing; prose is primary and evidence remains inspectable."""

    schema_version: str = REASONING_SCHEMA_VERSION
    scope: ReasoningScope = ReasoningScope.NETWORK
    entity_id: Optional[str] = None
    opening: str = Field(default="", max_length=500)
    context: str = Field(default="", max_length=700)
    #: Raised from 4 to 6 when the deterministic template stopped emitting only
    #: a cost card. Six themes can genuinely apply to one network at once —
    #: service, capacity, cost structure, footprint, resilience, carbon — and a
    #: cap of four silently dropped the last two, which on a stressed network
    #: were the ones a planner most needed. It is still a cap: a briefing is a
    #: briefing, not a report.
    kpi_insights: List[KPIInsight] = Field(default_factory=list, max_length=6)
    key_drivers: List[str] = Field(default_factory=list, max_length=4)
    recommendation: str = Field(default="", max_length=350)
    limitation: str = Field(default="", max_length=350)
    missing_information: List[MissingInformation] = Field(default_factory=list, max_length=2)
    suggested_questions: List[str] = Field(default_factory=list, max_length=3)
    evidence_completeness: EvidenceCompleteness = EvidenceCompleteness.COMPLETE

    model_config = ConfigDict(extra="forbid")

    def visible_text(self) -> str:
        """All prose that can reach a UI, in display order."""
        values = [self.opening, self.context]
        for insight in self.kpi_insights:
            values.extend((insight.headline, insight.narrative,
                           insight.recommended_action))
        values.extend(self.key_drivers)
        values.extend((self.recommendation, self.limitation))
        for item in self.missing_information:
            values.extend((item.question, item.impact))
        values.extend(self.suggested_questions)
        return " ".join(value for value in values if value)


class ReasoningDraft(ExecutiveBriefing):
    """Structured model output before deterministic validation and grounding."""

    confidence: str = "LOW"
    evidence_refs: List[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _normalise_confidence(self) -> "ReasoningDraft":
        self.confidence = self.confidence.strip().upper()
        if self.confidence not in {"LOW", "MEDIUM", "HIGH"}:
            self.confidence = "LOW"
        return self


class ReasoningEvidencePack(BaseModel):
    """Bounded, immutable evidence supplied to the reasoning runtime."""

    scope: ReasoningScope = ReasoningScope.NETWORK
    entity_id: Optional[str] = None
    user_question: str = Field(default="", max_length=1000)
    metrics: Dict[str, EvidenceMetric] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)
    unavailable: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)


class InsightRequest(BaseModel):
    """UI request for a network, node, lane, scenario, or comparison insight."""

    state_id: str
    scope: ReasoningScope = ReasoningScope.NETWORK
    entity_id: Optional[str] = None
    comparison_state_id: Optional[str] = None
    question: str = Field(default="", max_length=1000)
    disable_llm: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _scope_requires_entity(self) -> "InsightRequest":
        if self.scope in {ReasoningScope.FACILITY, ReasoningScope.LANE} and not self.entity_id:
            raise ValueError("entity_id is required for FACILITY and LANE insights")
        if self.scope is ReasoningScope.COMPARISON and not self.comparison_state_id:
            raise ValueError("comparison_state_id is required for COMPARISON insights")
        return self
