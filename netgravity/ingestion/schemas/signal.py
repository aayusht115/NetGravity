"""
NetGravity — External Signal Schemas
=====================================
Dated, sourced external information (news, macro, policy, weather).

GUARDRAIL PRINCIPLE
-------------------
No external signal reaches the forecast or the optimizer without passing a
materiality check first. Filtered signals are STILL STORED, flagged
`passed_guardrail=False`, so the filter itself is auditable — a reader can
see what was excluded and why, rather than trusting a silent black box.

Signals are never deterministic solver facts. They enrich assumptions and
provide root-cause context, and must always be rendered as such.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class SignalBucket(str, Enum):
    """
    Taxonomy agreed in team review: carrier/supplier/customer news is high
    value; competitor news was judged low signal-to-noise and is excluded
    by default.
    """
    CARRIER = "CARRIER"            # carriers / logistics providers
    SUPPLIER = "SUPPLIER"          # suppliers / vendors
    CUSTOMER = "CUSTOMER"          # customer expansion / contraction
    MACRO = "MACRO"                # macro, commodity, fuel, policy, duty
    WEATHER = "WEATHER"            # weather / force majeure (time-boxed)
    COMPETITOR = "COMPETITOR"      # excluded by default
    UNKNOWN = "UNKNOWN"


class SignalDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    NEUTRAL = "NEUTRAL"


class SignalConfidence(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ScenarioUse(str, Enum):
    FORECAST_ENRICHMENT = "FORECAST_ENRICHMENT"
    SEPARATE_WHATIF = "SEPARATE_WHATIF"
    ROOT_CAUSE_CONTEXT = "ROOT_CAUSE_CONTEXT"
    LOGGED_ONLY = "LOGGED_ONLY"


class GuardrailVerdict(BaseModel):
    """Why a signal was allowed through, or wasn't."""
    passed: bool
    bucket: SignalBucket
    relevance_score: float = 0.0        # 0.0 - 1.0
    threshold: float = 0.0
    reason: str = ""
    matched_entities: List[str] = Field(default_factory=list)


class ExternalSignal(BaseModel):
    """A dated, sourced external signal."""
    signal_id: str
    title: str

    source_title: str = ""
    source_url: Optional[str] = None

    published_date: str                    # ISO 8601
    effective_date: Optional[str] = None

    bucket: SignalBucket = SignalBucket.UNKNOWN
    direction: SignalDirection = SignalDirection.NEUTRAL
    magnitude: str = ""                    # human-readable, e.g. "+8% fuel cost"

    affected_entities: List[str] = Field(default_factory=list)   # facility/lane/market IDs
    geography: str = ""

    confidence: SignalConfidence = SignalConfidence.MEDIUM
    rationale: str = ""

    scenario_use: ScenarioUse = ScenarioUse.LOGGED_ONLY

    # Guardrail outcome — set by guardrails/relevance.py
    verdict: Optional[GuardrailVerdict] = None

    structured_by: str = "stub"            # "stub" | "<provider>:<model>"

    @property
    def passed_guardrail(self) -> bool:
        return bool(self.verdict and self.verdict.passed)
