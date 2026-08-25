"""
Orchestrator — Capability declaration types.

A `Capability` (in `tools/base.py`) says how to EXECUTE something: handler,
timeout, retry policy. That is enough for an executor working from a plan
somebody else wrote. It is not enough for a planner that has to write the plan.

To choose a path, a planner needs to know what a capability CONSUMES, what it
PRODUCES, where the authoritative answer ends up, and whether the answer may be
cited as fact. `CapabilityContract` carries exactly that and nothing more.

Two deliberate exclusions:

  * No workflow logic. A contract never says "run after optimization for a
    scenario query". Dependencies are stated as facts about the capability;
    which of them a given question needs is the planner's decision, and baking
    one universal order in here would defeat the purpose.

  * No handler. Contracts are metadata. The registry that holds them cannot
    execute them — see `CapabilityRegistry`, which keeps contracts and handlers
    in separate stores precisely so a lookup can never become a call.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from netgravity.orchestrator.schemas.plans import ExecutionMode


class CapabilityDomain(str, Enum):
    """
    What kind of question a capability answers.

    The planner resolves by domain ("who can forecast?") rather than by name, so
    a capability can be replaced or added without a planner change.

    SIGNAL_INTERPRETATION and SIGNAL_ROUTING are separate entries, not one
    "signals" domain, and that separation is load-bearing. Interpretation
    derives a LIKELIHOOD that feeds `RF = P + REI - P*REI`. Routing decides
    whether a reported signal is RELEVANT enough to reach a forecast, and its
    confidence score never touches RF. A single domain would invite exactly the
    conflation the risk chain is built to prevent.
    """
    EXTRACTION            = "EXTRACTION"
    SIGNAL_INTERPRETATION = "SIGNAL_INTERPRETATION"
    SIGNAL_ROUTING        = "SIGNAL_ROUTING"
    FORECAST              = "FORECAST"
    RESILIENCE            = "RESILIENCE"
    OPTIMIZATION          = "OPTIMIZATION"
    NETWORK_STATE         = "NETWORK_STATE"
    SCENARIO              = "SCENARIO"
    KPI                   = "KPI"
    RISK                  = "RISK"
    REASONING             = "REASONING"
    GOVERNANCE            = "GOVERNANCE"
    DIGITAL_TWIN          = "DIGITAL_TWIN"


class InvocationMode(str, Enum):
    """
    How a capability is actually reached today.

    Recorded honestly rather than aspirationally. Three of the declared
    capabilities are not plan steps, and pretending otherwise would give a
    future planner a schedule it cannot execute.

    ORCHESTRATED  Registered with a handler; appears as a step in an
                  `ExecutionPlan` and runs through `CapabilityTool`.

    SERVICE       Invoked outside the plan graph — by the orchestrator after the
                  plan settles, or directly by an API route. Extraction runs
                  before any execution exists; the Digital Twin projection runs
                  after the authoritative results are in.

    EMBEDDED      A gated stage inside another capability's handler, with no
                  independent entry point. Declared so its inputs, outputs and
                  authority are documented, and so nothing mistakes it for
                  something the planner may schedule on its own.
    """
    ORCHESTRATED = "ORCHESTRATED"
    SERVICE      = "SERVICE"
    EMBEDDED     = "EMBEDDED"


class CapabilityContract(BaseModel):
    """
    Orchestration metadata for one capability.

    Frozen: a contract describes the system as built. Mutating one at runtime
    would mean the planner and the executor disagreed about what a capability
    does, and the failure would surface far from the change.
    """

    capability_id: str
    domain: CapabilityDomain
    #: Class or module that owns the work, e.g. "ForecastingService". A name,
    #: not an import: this module must stay cheap and must not pull an engine in
    #: merely to describe it.
    provider: str
    description: str = ""

    # --- data contract ---
    #: Typed request the provider consumes, by class name. Empty when the
    #: capability reads only from the execution context.
    input_type: str = ""
    #: Typed, authoritative result the provider produces, by class name.
    output_type: str = ""
    #: Attribute on `ExecutionContext` holding that typed result once recorded.
    #: `engine_results` holds a FLATTENED projection for transport; anything
    #: needing per-facility utilisation, per-series status or per-node
    #: calculation state must read the typed field named here instead. Empty
    #: when the capability produces no context-held domain object.
    authoritative_field: str = ""
    #: True when `authoritative_field` holds an immutable IDENTIFIER rather than
    #: a domain object — a pinned snapshot id, a scenario id. Those references
    #: are deliberately identifiers and not objects: that is what makes
    #: stale-state detection and audit possible.
    #:
    #: Declared because output validation would otherwise compare a `str`
    #: against `output_type` and reject a perfectly correct result. Naming the
    #: distinction is better than teaching the validator to guess it.
    authoritative_is_reference: bool = False

    # --- orchestration contract ---
    #: Capability ids whose results this one reads. A statement of fact about
    #: the capability, NOT an execution order — the planner decides which of
    #: these a particular question actually requires.
    dependencies: Tuple[str, ...] = ()
    #: The subset of `dependencies` whose ABSENCE this provider handles
    #: explicitly. Mirrors `PlanStep.soft_depends_on` at the capability level,
    #: and exists because criticality was previously recorded only inside plans
    #: — so a caller invoking a capability outside a plan had no way to know
    #: which inputs were genuinely required.
    #:
    #: Getting this wrong in either direction is harmful, so it is declared per
    #: capability rather than guessed. Marking a required input optional lets a
    #: capability run without evidence it needs; marking an optional one
    #: required would refuse to run RF when only one of P and REI is present —
    #: when the correct behaviour is to run and report NOT_COMPUTABLE.
    optional_dependencies: Tuple[str, ...] = ()
    #: Keys this capability needs in `ToolRequest.params`, or context fields it
    #: cannot run without. Used by `validate_inputs`, which reports what is
    #: missing and executes nothing.
    required_inputs: Tuple[str, ...] = ()
    #: Validators that must pass before the output may be consumed, by name.
    #: Declared so a caller can tell "unvalidated" from "validated and clean".
    validations: Tuple[str, ...] = ()
    #: Explicitly declared alternative capabilities that can serve as a registered
    #: fallback or reroute if this capability encounters an unrecoverable failure.
    alternative_capabilities: Tuple[str, ...] = ()

    #: Ordering rank for a capability that must run AFTER the analytic work,
    #: whatever that work turned out to be. 0 means "not terminal".
    #:
    #: Needed because "runs last" and "depends on everything" are different
    #: claims, and only the first is true of reasoning and governance. Both
    #: declare NO hard dependencies on purpose — a missing input must not
    #: suppress the narrative, and governance must always return a verdict — so
    #: a dependency graph has no edge to place them at the end. Deriving order
    #: from dependencies alone put them FIRST, which is how this field came to
    #: exist.
    #:
    #: Higher runs later: reasoning explains, then governance rules on the
    #: explanation. Terminal capabilities take SOFT edges to everything before
    #: them, so an absent input degrades them rather than blocking them.
    terminal_rank: int = 0

    execution_mode: ExecutionMode = ExecutionMode.DETERMINISTIC
    invocation: InvocationMode = InvocationMode.ORCHESTRATED
    #: For EMBEDDED capabilities, the capability whose handler contains this
    #: stage. Required for EMBEDDED and forbidden otherwise.
    host_capability: Optional[str] = None
    #: True when the provider makes a model call on any path.
    llm_backed: bool = False

    #: Whether a planner may choose this capability as an independent GOAL.
    #:
    #: Separate from being executable, and the distinction is the one Phase 8.2
    #: had to discover the hard way. Every declared capability is executable —
    #: the executor can invoke all sixteen. Being *plannable* is a different
    #: question: extraction runs before an execution exists, the twin projection
    #: runs after the plan settles, and signal routing is a stage inside the
    #: forecast handler. A planner that scheduled any of them would be wrong.
    #:
    #: Forced False for SERVICE and EMBEDDED capabilities by the validator
    #: below, so the two facts cannot disagree. Kept as its own field rather
    #: than derived so that an ORCHESTRATED capability can be withheld from
    #: independent selection later without changing how it is invoked. No
    #: capability currently needs that, and none is marked so speculatively.
    planner_selectable: bool = True

    notes: str = ""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def declaration_must_be_coherent(self) -> "CapabilityContract":
        """
        Reject declarations that would mislead a planner.

        Each check below corresponds to a way the metadata could quietly diverge
        from the code it describes.
        """
        if self.invocation == InvocationMode.EMBEDDED and not self.host_capability:
            raise ValueError(
                f"Capability '{self.capability_id}' is EMBEDDED but names no "
                f"host_capability. A stage with no independent entry point must "
                f"say which handler contains it, or nothing can find it."
            )
        if self.invocation != InvocationMode.EMBEDDED and self.host_capability:
            raise ValueError(
                f"Capability '{self.capability_id}' names a host_capability but "
                f"is {self.invocation.value}, not EMBEDDED. Only an embedded "
                f"stage has a host."
            )
        if self.llm_backed and self.execution_mode == ExecutionMode.DETERMINISTIC:
            raise ValueError(
                f"Capability '{self.capability_id}' is declared llm_backed but "
                f"DETERMINISTIC. Model output is not reproducible from its "
                f"inputs, and marking it deterministic would let a narrative be "
                f"cited as an authoritative figure."
            )
        if self.capability_id in self.dependencies:
            raise ValueError(
                f"Capability '{self.capability_id}' lists itself as a dependency."
            )
        if (self.invocation != InvocationMode.ORCHESTRATED
                and self.planner_selectable):
            raise ValueError(
                f"Capability '{self.capability_id}' is {self.invocation.value} "
                f"but marked planner_selectable. A capability reached through "
                f"something that owns it cannot also be an independent planner "
                f"goal — declare planner_selectable=False."
            )
        stray = [d for d in self.optional_dependencies if d not in self.dependencies]
        if stray:
            raise ValueError(
                f"Capability '{self.capability_id}' marks {stray} optional, but "
                f"does not declare them as dependencies {self.dependencies}. "
                f"Criticality can only be stated for a dependency that exists."
            )
        return self

    @property
    def required_dependencies(self) -> Tuple[str, ...]:
        """
        Dependencies this capability cannot run without.

        What the executor enforces. Everything in `optional_dependencies` is
        excluded, because those providers report the absence themselves — and
        their report is more informative than a refusal to run.
        """
        optional = set(self.optional_dependencies)
        return tuple(d for d in self.dependencies if d not in optional)

    # ------------------------------------------------------------------

    @property
    def is_authoritative(self) -> bool:
        """
        Whether this capability's output may be cited as fact.

        Only deterministic output qualifies. Reasoning is PROBABILISTIC and so is
        never authoritative — it explains the numbers and may not become one.
        """
        return self.execution_mode == ExecutionMode.DETERMINISTIC

    @property
    def is_plan_schedulable(self) -> bool:
        """
        Whether a planner may place this in an `ExecutionPlan`.

        Two conditions, and both matter. `invocation` says the capability has a
        place in a plan at all; `planner_selectable` says a planner may choose
        it as a goal of its own. A capability failing either is reachable only
        through whatever owns it.
        """
        return (self.invocation == InvocationMode.ORCHESTRATED
                and self.planner_selectable)

    @property
    def is_plannable(self) -> bool:
        """Alias for `is_plan_schedulable`, reading the way callers speak."""
        return self.is_plan_schedulable

    def missing_inputs(self, available: object) -> Tuple[str, ...]:
        """
        Which `required_inputs` are absent from `available`.

        Pure metadata comparison — no execution, no engine import, no side
        effect. `available` is any container supporting `in`, normally the
        `ToolRequest.params` dict or a set of satisfied context fields.
        """
        try:
            return tuple(k for k in self.required_inputs if k not in available)  # type: ignore[operator]
        except TypeError:
            return tuple(self.required_inputs)


class CapabilitySummary(BaseModel):
    """Flat projection of a contract, for API listing and audit records."""
    capability_id: str
    domain: str
    provider: str
    output_type: str
    dependencies: Tuple[str, ...] = ()
    invocation: str = ""
    execution_mode: str = ""
    authoritative: bool = True
    llm_backed: bool = False
    description: str = Field(default="")

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def of(cls, contract: CapabilityContract) -> "CapabilitySummary":
        return cls(
            capability_id=contract.capability_id,
            domain=contract.domain.value,
            provider=contract.provider,
            output_type=contract.output_type,
            dependencies=contract.dependencies,
            invocation=contract.invocation.value,
            execution_mode=contract.execution_mode.value,
            authoritative=contract.is_authoritative,
            llm_backed=contract.llm_backed,
            description=contract.description,
        )


__all__ = [
    "CapabilityContract",
    "CapabilityDomain",
    "CapabilitySummary",
    "InvocationMode",
]
