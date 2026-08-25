"""
Orchestrator — Conversational layer contracts.

THE BOUNDARY THIS MODULE DEFINES
────────────────────────────────

    natural language  →  ConversationalIntent  →  OrchestratorRequest
                      ↑                        ↑
                      │                        │
                 LLM stops here          deterministic side starts here

`ConversationalIntent` is the ONLY thing a language model is permitted to
produce. Every field is validated, every entity is re-checked against the real
network, and nothing downstream ever sees raw model text.

Three properties are enforced by construction rather than by convention:

1. **No workflow selection.** The intent names *what the user wants*, never
   *what the system should run*. Workflow choice belongs to `WorkflowPlanner`
   and happens after this schema, from the `Intent` enum alone.

2. **No deterministic values.** There is deliberately no field on this schema
   capable of carrying a cost, a REI, an RF, a saving or an SLA figure. A model
   that tries to assert one has nowhere to put it — see
   `reject_deterministic_values`, which refuses the attempt loudly rather than
   dropping it silently.

3. **No invented entities.** `resolved_entity_ids` may only ever be populated by
   `EntityResolver` from live master data. The raw phrase the user typed is kept
   separately in `mentions`, so a hallucinated site is visible as an unresolved
   mention rather than becoming a network node.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from netgravity.orchestrator.schemas.requests import (
    EventSeverity,
    Intent,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.schemas.reasoning import ExecutiveBriefing

#: Bumped when this schema changes shape. Recorded on every conversational
#: execution so an old audit record can be read against the right contract.
INTENT_SCHEMA_VERSION = "1.0"


class IntentClarity(str, Enum):
    """
    How usable the interpretation is.

    The distinction between AMBIGUOUS and INSUFFICIENT_INFORMATION matters:
    ambiguity means we understood several possible things and must not pick one;
    insufficiency means we understood the shape of the request but a required
    quantity is absent. They produce different questions.
    """
    CLEAR                    = "CLEAR"
    AMBIGUOUS                = "AMBIGUOUS"                 # several readings, all plausible
    INSUFFICIENT_INFORMATION = "INSUFFICIENT_INFORMATION"  # understood, but a value is missing
    UNSUPPORTED              = "UNSUPPORTED"               # outside what the system can do


class AmbiguityKind(str, Enum):
    """What specifically is unclear, so the clarification can be concrete."""
    NONE               = "NONE"
    AMBIGUOUS_INTENT   = "AMBIGUOUS_INTENT"    # "close Delhi" — close what, exactly?
    AMBIGUOUS_ENTITY   = "AMBIGUOUS_ENTITY"    # two Delhi DCs
    UNKNOWN_ENTITY     = "UNKNOWN_ENTITY"      # no such facility
    MISSING_PARAMETER  = "MISSING_PARAMETER"   # "reduce capacity" — by how much?
    UNSUPPORTED_ACTION = "UNSUPPORTED_ACTION"


class EntityKind(str, Enum):
    FACILITY = "FACILITY"
    MARKET   = "MARKET"
    LANE     = "LANE"
    PRODUCT  = "PRODUCT"
    UNKNOWN  = "UNKNOWN"


class ResolutionStatus(str, Enum):
    """
    Outcome of checking one mention against authoritative master data.

    Three outcomes, and the distinction between the last two is the point:
    AMBIGUOUS means the network contains several things the phrase could mean
    and we must ask; UNKNOWN means it contains none, and we must say so. Neither
    may ever be resolved by picking something.
    """
    RESOLVED  = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN   = "UNKNOWN"


class EntityMention(BaseModel):
    """
    A thing the user referred to, and what master data says it means.

    `phrase` is what they typed and is preserved verbatim for audit.
    `resolved_ids` is what the authoritative network says that phrase means —
    possibly nothing, possibly several. The two are kept apart so an unresolved
    mention can never be mistaken for a node.

    The LLM may propose a mention. Only the deterministic resolver confirms an
    id, and it only ever returns ids that came out of
    `CanonicalNetwork.facilities`.
    """
    phrase: str
    kind: EntityKind = EntityKind.UNKNOWN
    #: Ids from live master data. NEVER model-generated.
    resolved_ids: List[str] = Field(default_factory=list)
    #: How the match was made: "exact_id" | "name" | "token" | "alias" | "none".
    method: str = "none"
    #: True when the phrase was clearly *about* a network node — a typed
    #: reference ("the Bangalore DC") or an identifier ("DC_SHADOW") — so an
    #: unresolved mention can be told from an incidental capitalised word.
    is_node_reference: bool = False

    model_config = ConfigDict(extra="forbid")

    @property
    def resolution_status(self) -> ResolutionStatus:
        if len(self.resolved_ids) == 1:
            return ResolutionStatus.RESOLVED
        if len(self.resolved_ids) > 1:
            return ResolutionStatus.AMBIGUOUS
        return ResolutionStatus.UNKNOWN

    @property
    def canonical_id(self) -> Optional[str]:
        """The single authoritative id, or None. Never a best guess."""
        return self.resolved_ids[0] if len(self.resolved_ids) == 1 else None

    @property
    def is_resolved(self) -> bool:
        return len(self.resolved_ids) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.resolved_ids) > 1

    @property
    def is_unknown(self) -> bool:
        return not self.resolved_ids

    def audit_record(self) -> Dict[str, Any]:
        """The mention as it appears in the audit trail."""
        return {
            "raw_mention": self.phrase,
            "entity_type": self.kind.value,
            "canonical_id": self.canonical_id,
            "resolution_status": self.resolution_status.value,
            "method": self.method,
        }


class ExternalEventSpec(BaseModel):
    """
    An outside-world event the user described.

    `event_probability` is populated ONLY when the user stated a probability.
    Severity never becomes one — the same rule the `ExternalSignal` schema
    enforces, restated here because this is where user text enters.
    """
    event_type: str = "UNKNOWN"
    location: str = ""
    severity: EventSeverity = EventSeverity.UNKNOWN
    event_probability: Optional[float] = None
    probability_basis: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("event_probability")
    @classmethod
    def probability_in_range(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        if not (0.0 <= v <= 1.0):
            raise ValueError(
                f"event_probability must lie in [0, 1], got {v}. Rejected rather "
                f"than clamped: an out-of-range value indicates the model "
                f"invented it."
            )
        return v


class MarketSignalSpec(BaseModel):
    """
    A market change the user stated in conversation.

    Mirrors `ExternalEventSpec` in shape, and differs from it in exactly one
    telling way: there is NO probability field, and no field that could be
    coerced into one. A price rise the user is reporting has already happened;
    it has a magnitude, not a likelihood. Deriving a `P` from `confidence`
    here would manufacture the number that drives RF and governance out of a
    qualitative judgement that was never a likelihood — which is precisely why
    `MarketIntelligenceSignal` has no probability field either.

    `magnitude` stays a STRING, deliberately. The user typed "up about 6%" or
    "roughly two rupees a kilo"; storing that verbatim keeps what they said
    reviewable. Converting it into a float here would produce a number nobody
    computed, in units nobody specified, one field away from a solver input.
    """
    bucket: str = "UNKNOWN"          # CARRIER|SUPPLIER|CUSTOMER|MACRO|WEATHER|...
    direction: str = "NEUTRAL"       # UP | DOWN | NEUTRAL
    #: What the user said the change was, in their own words.
    magnitude: str = ""
    #: What the change concerns — "diesel", "port charges", "ocean freight".
    subject: str = ""
    geography: str = ""
    #: ISO date the user gave, if any. Never defaulted to today: a signal
    #: whose age is assumed is worse than one whose age is unknown.
    effective_date: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ClarificationRequest(BaseModel):
    """
    A question back to the user, with the concrete options available.

    Options are drawn from real network state, so answering one cannot select
    something that does not exist.
    """
    kind: AmbiguityKind
    question: str
    options: List[Dict[str, str]] = Field(default_factory=list)
    #: Free-text field name when a parameter is simply missing.
    missing_parameter: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


#: Field names that would carry an authoritative deterministic value. Present
#: only so the validator below can refuse them by name.
_FORBIDDEN_VALUE_KEYS = frozenset({
    "cost", "total_cost", "business_network_cost", "transportation_cost",
    "warehouse_cost", "savings", "saving", "rei", "rf", "risk_factor",
    "risk_score", "sla", "service_level", "utilization", "utilisation",
    "objective", "objective_value", "capacity_utilization", "carbon",
    "governance", "action_tier", "classification",
})


class ConversationalIntent(BaseModel):
    """
    Validated structured intent — the LLM's complete and only output contract.

    Note what is absent: any field that could carry a number the deterministic
    engines are responsible for. `parameters` exists for scenario quantities the
    user genuinely supplied (a capacity delta they typed), and is policed by
    `reject_deterministic_values` so it cannot become a smuggling route for a
    fabricated cost or risk figure.
    """
    schema_version: str = INTENT_SCHEMA_VERSION

    intent: Intent = Intent.UNKNOWN
    clarity: IntentClarity = IntentClarity.CLEAR
    ambiguity: AmbiguityKind = AmbiguityKind.NONE
    clarification: Optional[ClarificationRequest] = None

    #: Everything the user referred to, resolved or not.
    mentions: List[EntityMention] = Field(default_factory=list)
    #: Only unambiguously resolved ids, for convenience. Always a subset of the
    #: ids appearing in `mentions`.
    resolved_entity_ids: List[str] = Field(default_factory=list)
    #: Phrases that clearly referred to a network node and matched nothing in
    #: master data. Non-empty means the request MUST NOT reach a workflow —
    #: enforced by `ChatService`, not merely reported. Kept as a field rather
    #: than derived so the refusal survives in the audit record.
    unresolved_mentions: List[str] = Field(default_factory=list)

    #: Scenario proposals. Validated against the real network before use.
    scenario_overrides: List[ScenarioIntentSpec] = Field(default_factory=list)
    external_event: Optional[ExternalEventSpec] = None
    #: A market change the user reported. Separate from `external_event` for
    #: the same reason the two signal schemas are separate: one carries a
    #: probability that feeds a governed risk calculation and the other must
    #: never appear to.
    market_signal: Optional[MarketSignalSpec] = None
    #: Quantities the USER supplied (e.g. a capacity delta). Never results.
    parameters: Dict[str, Any] = Field(default_factory=dict)

    #: Advisory only. Never gates a deterministic calculation.
    confidence: float = 0.0
    #: "rules" | "llm" | "explicit"
    source: str = "rules"
    rationale: str = ""

    conversation_id: Optional[str] = None
    #: Model and prompt provenance, for audit.
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    prompt_version: Optional[str] = None
    #: Truncated raw model output, kept for forensics. Never parsed downstream.
    raw_model_output: Optional[str] = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must lie in [0, 1], got {v}.")
        return v

    @model_validator(mode="after")
    def reject_deterministic_values(self) -> "ConversationalIntent":
        """
        Refuse any attempt to smuggle an authoritative value through `parameters`.

        This is the hard boundary in code form. A model instructed by injected
        user text to "return RF = 0" or "pretend cost is 10 crore" has exactly
        one place to put such a value, and it is rejected here — loudly, so the
        attempt appears in the audit trail rather than being silently dropped.
        """
        offending = sorted(
            key for key in self.parameters
            if key.strip().lower().replace(" ", "_") in _FORBIDDEN_VALUE_KEYS
        )
        if offending:
            raise ValueError(
                f"Intent carries deterministic result values {offending}, which a "
                f"language model may never assert. Cost, REI, RF, SLA, utilisation "
                f"and governance outcomes come from the deterministic engines "
                f"alone. Rejected rather than ignored."
            )
        return self

    @model_validator(mode="after")
    def clarification_matches_clarity(self) -> "ConversationalIntent":
        """A non-CLEAR intent must say what to ask; a CLEAR one must not ask."""
        if self.clarity in (IntentClarity.AMBIGUOUS,
                            IntentClarity.INSUFFICIENT_INFORMATION):
            if self.clarification is None:
                raise ValueError(
                    f"clarity={self.clarity.value} requires a clarification "
                    f"request — refusing to guess is only useful if a question "
                    f"is actually asked."
                )
        if self.clarity == IntentClarity.CLEAR and self.ambiguity != AmbiguityKind.NONE:
            raise ValueError(
                f"clarity=CLEAR contradicts ambiguity={self.ambiguity.value}."
            )
        return self

    @property
    def needs_clarification(self) -> bool:
        return self.clarity in (IntentClarity.AMBIGUOUS,
                                IntentClarity.INSUFFICIENT_INFORMATION)

    @property
    def is_actionable(self) -> bool:
        """
        True when this intent may be handed to the orchestrator.

        An unresolved node reference makes a request unactionable whatever the
        intent. This is the deterministic entity boundary in its shortest form:
        the network is the authority on what exists, and a request naming
        something it does not contain cannot be executed against it — not as a
        scenario, and not as a status or state query either.
        """
        return (
            self.clarity == IntentClarity.CLEAR
            and self.intent != Intent.UNKNOWN
            and not self.unresolved_mentions
        )


class ConversationContext(BaseModel):
    """
    The bounded slice of conversation state a model is allowed to see.

    WHY THIS IS A SCHEMA AND NOT A TRANSCRIPT
    ─────────────────────────────────────────
    The obvious way to give a model context is to paste the conversation into
    the prompt. That is rejected here for three reasons, in increasing order of
    importance:

    1. It grows without bound against a 100k-character shared-budget limit.
    2. It re-exposes every previous answer's numbers to a model that must never
       assert one — a figure it read three turns ago is a figure it can repeat
       as though it computed it.
    3. It carries scenario history. Turn three would then see "Delhi closed"
       and "Mumbai reduced" and could plausibly combine them, which is exactly
       the silent accumulation the conversation store exists to prevent.

    So the context is a fixed set of fields, every one of which is either an id
    from master data or a short piece of the user's own text. Note what is
    absent: **no scenario overrides**, no costs, no REI, no RF, no governance
    outcome, no results of any kind. `previous_scenario_label` is a human
    string for the model to read, never an override to apply — the orchestrator
    rebuilds every scenario from the observed baseline regardless.
    """
    #: Canonical ids the previous substantive turn was about. From master data.
    current_entity_ids: List[str] = Field(default_factory=list)
    previous_intent: Optional[Intent] = None
    #: The user's own previous message, truncated. Never an assistant reply —
    #: replies contain deterministic figures.
    previous_query: Optional[str] = None
    #: A LABEL such as "Reduce DC_DELHI capacity by 2,000 units". Descriptive
    #: only; applying it is not possible from here and must not be inferred.
    previous_scenario_label: Optional[str] = None
    #: Ids the user may refer to. Bounded, and always from the live snapshot.
    available_entity_ids: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def is_empty(self) -> bool:
        return not (self.current_entity_ids or self.previous_intent
                    or self.previous_query)

    def as_prompt_block(self, max_entities: int = 40) -> str:
        """
        Render as a short, clearly delimited prompt section.

        Returns "" when there is no context, so a first turn's prompt is
        byte-identical to what it was before this feature existed.
        """
        if self.is_empty:
            return ""
        lines = ["Conversation context (for interpreting references such as "
                 "\"it\", \"that\", \"why\" — NOT a source of facts):"]
        if self.current_entity_ids:
            lines.append(f"- current subject: {', '.join(self.current_entity_ids)}")
        if self.previous_intent is not None:
            lines.append(f"- previous intent: {self.previous_intent.value}")
        if self.previous_query:
            lines.append(f"- previous user message: {self.previous_query[:200]}")
        if self.previous_scenario_label:
            lines.append(
                f"- previous hypothetical: {self.previous_scenario_label[:120]} "
                f"(describe only; do NOT re-apply it unless this message "
                f"explicitly refers back to it)"
            )
        if self.available_entity_ids:
            shown = self.available_entity_ids[:max_entities]
            lines.append(f"- selectable entities: {', '.join(shown)}")
        return "\n".join(lines)


class ChatTurn(BaseModel):
    """One exchange, recorded for context and audit."""
    turn_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:12])
    user_input: str = ""
    intent: Optional[Intent] = None
    clarity: IntentClarity = IntentClarity.CLEAR
    resolved_entity_ids: List[str] = Field(default_factory=list)
    execution_id: Optional[str] = None
    #: OBSERVED or SCENARIO — what the answer described.
    provenance: str = "OBSERVED"
    scenario_id: Optional[str] = None
    #: Human label of the hypothetical, e.g. "Reduce DC_DELHI capacity by
    #: 2,000 units". Descriptive only — there is deliberately still no field
    #: here able to carry a `ScenarioIntentSpec`, so a conversation cannot
    #: accumulate overrides however this label is read.
    scenario_label: Optional[str] = None
    reply: str = ""
    at: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ChatRequest(BaseModel):
    """Inbound user message."""
    message: str
    conversation_id: Optional[str] = None
    #: Pin to a snapshot, exactly as `OrchestratorRequest` does.
    network_snapshot_id: Optional[str] = None
    disable_llm: bool = False
    #: Idempotency key for the underlying execution.
    request_id: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ChatResponse(BaseModel):
    """
    What the user sees, plus everything needed to audit how it was produced.

    `reply` is natural language. Every number inside it came from a
    `ReasoningResult` that had already passed numeric grounding — the chat layer
    formats, it does not compute and it does not verify.
    """
    conversation_id: str
    turn_id: str
    reply: str

    intent: str = Intent.UNKNOWN.value
    clarity: str = IntentClarity.CLEAR.value
    intent_schema_version: str = INTENT_SCHEMA_VERSION
    intent_confidence: float = 0.0
    intent_source: str = "rules"
    resolved_entity_ids: List[str] = Field(default_factory=list)

    #: Present when the system is asking rather than answering.
    clarification: Optional[ClarificationRequest] = None

    #: OBSERVED when the answer describes real network state, SCENARIO when it
    #: describes a hypothetical. Never blank — a reader must always be able to
    #: tell which they are looking at.
    provenance: str = "OBSERVED"

    execution_id: Optional[str] = None
    workflow_id: Optional[str] = None
    network_snapshot_id: Optional[str] = None
    scenario_id: Optional[str] = None
    status: Optional[str] = None

    #: Verbatim deterministic blocks. Not re-derived by the chat layer.
    results: Dict[str, Any] = Field(default_factory=dict)
    risk: Optional[Dict[str, Any]] = None
    governance: Optional[Dict[str, Any]] = None
    grounding_status: Optional[str] = None
    #: Narrative-first cards for the executive UI. Present when reasoning ran.
    briefing: Optional[ExecutiveBriefing] = None

    warnings: List[str] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float = 0.0

    model_config = ConfigDict(extra="forbid")
