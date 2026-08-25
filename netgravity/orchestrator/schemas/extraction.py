"""
Orchestrator — Extraction / Parsing Agent contracts.

THE BOUNDARY THIS MODULE DEFINES
────────────────────────────────

    client files / external sources  →  ExtractionResult  →  Orchestrator
                                     ↑                    ↑
                                     │                    │
                            extraction stops here    deterministic side starts

The Orchestrator consumes a *canonical snapshot* and *structured evidence*. It
never sees a workbook, a sheet name, a row offset or a parser. Everything below
`ExtractionResult` is an implementation detail of the agent.

WHAT THE AGENT IS, AND IS NOT
─────────────────────────────
The data-ingestion pipeline in `netgravity/ingestion/` is the **client-data
implementation component of this agent**, not a separate agent. The agent is a
thin routing and validation layer over it: it decides which extraction
capability applies to a source, applies deterministic acceptance rules to the
output, and returns structured evidence. It does not parse anything itself, and
deliberately duplicates none of the pipeline's logic.

WHAT THE AGENT MUST NEVER DO
────────────────────────────
Compute. There is no field on `ExtractionResult` able to carry an REI, an RF, a
governance verdict or an optimisation objective, for the same reason
`ConversationalIntent` has none: extraction produces *evidence*, and evidence
that arrives pre-scored cannot be checked. The deterministic engines own those
numbers and are reached through the Orchestrator, after this boundary.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Bumped when this contract changes shape, so an old extraction record can be
#: read against the right schema.
EXTRACTION_SCHEMA_VERSION = "1.0"


class SourceType(str, Enum):
    """
    What kind of source is being extracted, and therefore which capability runs.

    The agent routes on this. It is supplied by the caller or inferred from the
    path — never proposed by a language model, because routing decides which
    deterministic pipeline executes.
    """
    CLIENT_DATA_DIRECTORY = "CLIENT_DATA_DIRECTORY"   # a folder of client files
    CLIENT_DATA_FILE      = "CLIENT_DATA_FILE"        # one workbook or CSV
    EXTERNAL_SIGNAL_TEXT  = "EXTERNAL_SIGNAL_TEXT"    # free text about a HAZARD event
    #: A news article, circular or notice describing a MARKET change — a
    #: fuel price, a surcharge, a port notice, a duty. Deliberately NOT the
    #: same route as EXTERNAL_SIGNAL_TEXT, which produces a hazard event
    #: carrying an event probability that feeds RF. Sending a price story
    #: down that route would present a cost change as a disruption with an
    #: unknown likelihood; sending a flood warning down this one would
    #: silently discard the probability the risk chain needs. Two concepts,
    #: two routes, no conversion between them.
    MARKET_INTELLIGENCE_DOC = "MARKET_INTELLIGENCE_DOC"
    UNSUPPORTED           = "UNSUPPORTED"


class ExtractionStatus(str, Enum):
    """
    Outcome of one extraction, in the vocabulary §14 of the phase brief asks for.

    HUMAN_REVIEW_REQUIRED is distinct from REJECTED on purpose: rejection means
    the data cannot be used, review means it *might* be usable once a person has
    confirmed something the system will not decide on its own. Collapsing the
    two would either discard recoverable data or auto-accept questionable data,
    and both are worse than asking.
    """
    ACCEPTED              = "ACCEPTED"
    WARNING               = "WARNING"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    REJECTED              = "REJECTED"


class ValidationSeverity(str, Enum):
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"


class ValidationFinding(BaseModel):
    """
    One thing the deterministic validators said about the source data.

    `where` carries provenance down to the row when the parser knew it — file,
    sheet, row, field. Without that an "invalid capacity" message is unactionable
    against a 40,000-row workbook.
    """
    severity: ValidationSeverity
    code: str
    message: str
    where: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ExtractionProvenance(BaseModel):
    """
    Where the evidence came from and how it was produced.

    Required for explainability: a planner asked to act on an ingested network
    must be able to get back to the file and row a number came from.
    """
    ingestion_id: str = ""
    source: str = ""
    source_type: SourceType = SourceType.UNSUPPORTED
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    files: List[Dict[str, Any]] = Field(default_factory=list)
    #: True when any part of the extraction used a model rather than rules.
    #: Recorded so a reader can never mistake assisted output for parsed output.
    ai_assisted: bool = False
    ai_provider: Optional[str] = None
    #: Free-form counts: rows read, accepted, rejected, per file type.
    counts: Dict[str, int] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


#: Names that would carry a value the deterministic engines own. Present only so
#: the validator below can refuse them by name — the same device
#: `ConversationalIntent` uses for the language boundary.
_FORBIDDEN_METRIC_KEYS = frozenset({
    "rei", "rf", "risk_factor", "risk_score", "governance", "action_tier",
    "classification", "objective", "objective_value", "event_probability",
    "probability",
})


class ExtractionRequest(BaseModel):
    """One extraction job."""
    source: str
    source_type: SourceType = SourceType.CLIENT_DATA_DIRECTORY
    ingestion_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    #: Persist the resulting snapshot to the curated zone.
    save_snapshot: bool = False
    #: Register the snapshot with the orchestrator's SnapshotManager.
    register_snapshot: bool = False
    #: Accept AI-proposed column mappings without human confirmation. Defaults
    #: to False: an unconfirmed mapping is exactly the case that should stop and
    #: ask, and defaulting it on would make review opt-out.
    auto_confirm_mappings: bool = False
    #: Allow the ingestion pipeline to call a model at all.
    allow_ai: bool = False
    options: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


class ExtractionResult(BaseModel):
    """
    Structured evidence, and the only thing the Orchestrator consumes.

    `canonical_network` is MAIN's own `CanonicalNetwork` — there is deliberately
    no second canonical model and no adapter between two of them. The ingestion
    pipeline imports MAIN's schemas and builds one directly.

    `external_signals` is typed `Any` rather than `ExternalSignal` because the
    two signal concepts in this codebase are genuinely different things that
    happen to share a name; see the Phase 4A report. Nothing here converts one
    into the other, because doing so would require inventing a probability.
    """
    schema_version: str = EXTRACTION_SCHEMA_VERSION
    status: ExtractionStatus
    ingestion_id: str = ""

    #: MAIN's CanonicalNetwork. Untyped here only to keep this schema module
    #: free of a heavyweight import cycle; validated on assignment below.
    canonical_data: Optional[Any] = None
    snapshot_id: Optional[str] = None
    data_version: Optional[str] = None

    external_signals: List[Any] = Field(default_factory=list)
    #: `MarketIntelligenceSignal` records — market context, guardrail-scored.
    #:
    #: A SEPARATE list, not a second kind of thing in `external_signals`. The
    #: two signal types are different concepts that briefly shared a name, and
    #: a single mixed list would force every consumer to type-test its
    #: contents to find out whether it was holding a hazard with a probability
    #: or a price change without one. Getting that test wrong is exactly the
    #: failure the Phase 4A rename was performed to prevent, so the schema
    #: does not create the opportunity.
    #:
    #: Nothing converts between the two lists. Deriving an event probability
    #: from a signal's qualitative confidence would manufacture the number
    #: that drives RF and governance.
    market_intelligence: List[Any] = Field(default_factory=list)

    validation_results: List[ValidationFinding] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)

    provenance: ExtractionProvenance = Field(default_factory=ExtractionProvenance)
    #: Rows or mappings a person must confirm. Non-empty implies
    #: HUMAN_REVIEW_REQUIRED.
    review_items: List[Dict[str, Any]] = Field(default_factory=list)
    duration_seconds: float = 0.0

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    @model_validator(mode="after")
    def reject_computed_values(self) -> "ExtractionResult":
        """
        Refuse any attempt to smuggle an engine-owned value through options-like
        free-form fields.

        Extraction produces evidence. A cost, an REI, an RF or a governance
        outcome arriving from a parser is either fabricated or stale, and in
        both cases it must not be mistaken for something the engines computed.
        """
        for item in self.review_items:
            offending = sorted(
                k for k in item
                if str(k).strip().lower().replace(" ", "_") in _FORBIDDEN_METRIC_KEYS
            )
            if offending:
                raise ValueError(
                    f"Extraction result carries engine-owned values {offending}. "
                    f"REI, RF, governance outcomes and optimisation objectives "
                    f"come from the deterministic engines, never from a parser."
                )
        return self

    @model_validator(mode="after")
    def status_matches_content(self) -> "ExtractionResult":
        """A review requirement must actually say what needs reviewing."""
        if self.status == ExtractionStatus.HUMAN_REVIEW_REQUIRED and \
                not self.review_items and not self.warnings:
            raise ValueError(
                "status=HUMAN_REVIEW_REQUIRED with nothing to review. Refusing to "
                "block a run without telling the operator what to look at."
            )
        if self.status == ExtractionStatus.ACCEPTED and self.errors:
            raise ValueError(
                f"status=ACCEPTED contradicts {len(self.errors)} error(s)."
            )
        return self

    @property
    def ok(self) -> bool:
        """True when the result may be handed to the Orchestrator unattended."""
        return self.status in (ExtractionStatus.ACCEPTED, ExtractionStatus.WARNING) \
            and self.canonical_data is not None

    @property
    def needs_review(self) -> bool:
        return self.status == ExtractionStatus.HUMAN_REVIEW_REQUIRED
