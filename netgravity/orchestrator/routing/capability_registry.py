"""
Orchestrator — Capability registry.

The registry is what makes the orchestrator extensible. Adding a Carbon
Optimization Agent, Supplier Risk Agent or Inventory Agent means registering a
`Capability` — the core planner and executor need no changes.

Nothing in the orchestrator core contains a chain of
`if intent == ...: call_x()`. Routing consults the registry; the registry
answers what exists, what it needs, what it produces, and how it may fail.
"""

from __future__ import annotations

import logging
from typing import Dict, Iterable, List, Optional

from netgravity.orchestrator.exceptions import CapabilityNotFoundError
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
    CapabilitySummary,
)
from netgravity.orchestrator.schemas.plans import ExecutionMode
from netgravity.orchestrator.tools.base import Capability, CapabilityTool

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """
    In-memory registry of everything the orchestrator can execute.

    Deliberately simple: a process-local dict behind a stable interface. A
    distributed registry can replace the internals later without touching
    callers.
    """

    def __init__(self) -> None:
        self._capabilities: Dict[str, Capability] = {}
        self._tools: Dict[str, CapabilityTool] = {}
        # Declarations, kept in a SEPARATE store from handlers on purpose. A
        # contract is metadata; a tool is executable. Because a contract can
        # exist here with no entry in `_tools`, a metadata lookup has no handler
        # to call even by accident — which is what "the registry must not
        # execute capabilities" means in practice rather than as a comment.
        #
        # It also lets the registry describe capabilities that are real but are
        # not plan steps: extraction, the twin projection, forecast signal
        # routing. A planner can see they exist and see it may not schedule them.
        self._contracts: Dict[str, CapabilityContract] = {}
        # Explicit alternative capabilities for rerouting under failure
        self._alternatives: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, capability: Capability, *, replace: bool = False) -> None:
        """
        Add a capability.

        Raises:
            ValueError: name already registered and `replace` is False. Silent
                overwrite is refused because it would change orchestrator
                behaviour invisibly.
        """
        if capability.name in self._capabilities and not replace:
            raise ValueError(
                f"Capability '{capability.name}' is already registered. "
                f"Pass replace=True to override deliberately."
            )
        self._capabilities[capability.name] = capability
        self._tools[capability.name] = CapabilityTool(capability)
        # A capability that carries its own declaration registers it too, so the
        # handler and the metadata describing it cannot be added separately and
        # drift apart.
        if capability.contract is not None:
            self.register_contract(capability.contract, replace=True)
        logger.info(
            "orchestrator.capability.registered name=%s mode=%s deps=%s optional=%s",
            capability.name, capability.execution_mode.value,
            list(capability.dependencies), capability.optional,
        )

    def register_all(self, capabilities: Iterable[Capability], *, replace: bool = False) -> None:
        for cap in capabilities:
            self.register(cap, replace=replace)

    def unregister(self, name: str) -> None:
        self._capabilities.pop(name, None)
        self._tools.pop(name, None)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def has(self, name: str) -> bool:
        return name in self._capabilities

    def get(self, name: str) -> Capability:
        cap = self._capabilities.get(name)
        if cap is None:
            raise CapabilityNotFoundError(
                f"Capability '{name}' is not registered. "
                f"Registered: {sorted(self._capabilities)}",
                context={"requested": name},
            )
        return cap

    def tool(self, name: str) -> CapabilityTool:
        if name not in self._tools:
            self.get(name)  # raises with a helpful message
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._capabilities)

    def all(self) -> List[Capability]:
        return [self._capabilities[n] for n in self.names()]

    def deterministic(self) -> List[Capability]:
        return [c for c in self.all() if c.execution_mode == ExecutionMode.DETERMINISTIC]

    def probabilistic(self) -> List[Capability]:
        return [c for c in self.all() if c.execution_mode == ExecutionMode.PROBABILISTIC]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def describe(self) -> List[Dict[str, object]]:
        """Machine-readable listing — powers the /orchestrator/capabilities endpoint."""
        return [
            {
                "name": c.name,
                "description": c.description,
                "execution_mode": c.execution_mode.value,
                "dependencies": list(c.dependencies),
                "parallel_safe": c.parallel_safe,
                "timeout_seconds": c.timeout_seconds,
                "max_attempts": c.retry_policy.max_attempts,
                "optional": c.optional,
                "required_roles": list(c.required_roles),
            }
            for c in self.all()
        ]

    def validate_plan_capabilities(self, capability_names: Iterable[str]) -> None:
        """
        Verify every capability a plan references exists.

        Raises:
            CapabilityNotFoundError
        """
        missing = [n for n in capability_names if n not in self._capabilities]
        if missing:
            raise CapabilityNotFoundError(
                f"Execution plan references unregistered capabilities: {sorted(set(missing))}. "
                f"Registered: {self.names()}",
                context={"missing": sorted(set(missing))},
            )

    # ------------------------------------------------------------------
    # Contracts — declaration, resolution, input checking
    # ------------------------------------------------------------------
    #
    # Everything below is metadata. Nothing here calls a handler, imports an
    # engine, or touches execution state. `resolve` answers "who can do X?" and
    # `validate_inputs` answers "could X run with these inputs?" — the decision
    # to actually run it belongs to the orchestrator, and the running belongs to
    # `CapabilityTool`.

    def register_contract(
        self, contract: CapabilityContract, *, replace: bool = False,
    ) -> None:
        """
        Declare what a capability consumes, produces and depends on.

        Raises:
            ValueError: already declared and `replace` is False. Same reasoning
                as `register`: a silent overwrite would change what the planner
                believes about the system with nothing in the log.
        """
        existing = self._contracts.get(contract.capability_id)
        if existing is not None and not replace and existing != contract:
            raise ValueError(
                f"Capability contract '{contract.capability_id}' is already "
                f"declared. Pass replace=True to override deliberately."
            )
        self._contracts[contract.capability_id] = contract

    def register_contracts(
        self, contracts: Iterable[CapabilityContract], *, replace: bool = False,
    ) -> None:
        for contract in contracts:
            self.register_contract(contract, replace=replace)

    def has_contract(self, capability_id: str) -> bool:
        return capability_id in self._contracts

    def contract(self, capability_id: str) -> CapabilityContract:
        """
        The declaration for one capability.

        Raises:
            CapabilityNotFoundError
        """
        found = self._contracts.get(capability_id)
        if found is None:
            raise CapabilityNotFoundError(
                f"No contract declared for capability '{capability_id}'. "
                f"Declared: {sorted(self._contracts)}",
                context={"requested": capability_id},
            )
        return found

    def contracts(self) -> List[CapabilityContract]:
        return [self._contracts[k] for k in sorted(self._contracts)]

    def resolve(self, domain: CapabilityDomain) -> List[CapabilityContract]:
        """
        Which capabilities answer questions in `domain`.

        The lookup a planner is meant to use: resolve by DOMAIN, not by name, so
        adding or replacing a provider does not require a planner change. Returns
        a list because a domain legitimately has several providers — observed and
        scenario optimisation both serve OPTIMIZATION.
        """
        return [c for c in self.contracts() if c.domain == domain]

    def resolve_capability(
        self,
        domain: CapabilityDomain,
        *,
        schedulable_only: bool = True,
    ) -> Optional[CapabilityContract]:
        """
        One provider for `domain`, or None.

        `schedulable_only` defaults True so a planner cannot be handed a
        capability it is not allowed to place in a plan — extraction and the twin
        projection are real, but scheduling either would be wrong.

        Returns None rather than raising: "nothing serves this domain" is an
        ordinary answer for a planner deciding what a question needs, and
        exceptions are for genuine faults.
        """
        matches = self.resolve(domain)
        if schedulable_only:
            matches = [c for c in matches if c.is_plan_schedulable]
        return matches[0] if matches else None

    def providers_of(self, domain: CapabilityDomain) -> List[str]:
        """Capability ids serving `domain`."""
        return [c.capability_id for c in self.resolve(domain)]

    def domains(self) -> List[str]:
        """Every declared domain, sorted."""
        return sorted({c.domain.value for c in self.contracts()})

    def schedulable(self) -> List[CapabilityContract]:
        """Contracts a planner may place in an `ExecutionPlan`."""
        return [c for c in self.contracts() if c.is_plan_schedulable]

    def authoritative(self) -> List[CapabilityContract]:
        """
        Contracts whose output may be cited as fact.

        Deterministic only. Reasoning is excluded by construction, which is the
        registry-level expression of the rule that a narrative never becomes a
        number.
        """
        return [c for c in self.contracts() if c.is_authoritative]

    def validate_inputs(
        self, capability_id: str, available: object,
    ) -> List[str]:
        """
        Which declared inputs of `capability_id` are absent from `available`.

        Pure comparison against the contract. Returns the missing keys; raises
        nothing for a merely-incomplete input set, because "not yet runnable" is
        information a planner acts on rather than an error.

        Args:
            capability_id: Declared capability to check.
            available: Anything supporting `in` — normally `ToolRequest.params`
                or a set of satisfied context field names.

        Raises:
            CapabilityNotFoundError: no contract is declared for the capability.
                This one IS an error: it means the caller is planning around
                something that does not exist.
        """
        return list(self.contract(capability_id).missing_inputs(available))

    def dependency_map(self) -> Dict[str, List[str]]:
        """
        capability_id -> declared dependencies.

        The raw material for planning. Deliberately NOT a workflow: it states
        what each capability reads, and leaves which of those a given question
        needs to the planner. Nothing here implies one universal order.
        """
        return {c.capability_id: list(c.dependencies) for c in self.contracts()}

    def describe_contracts(self) -> List[CapabilitySummary]:
        """Flat listing of every declaration, for the API and audit records."""
        return [CapabilitySummary.of(c) for c in self.contracts()]

    def undeclared(self) -> List[str]:
        """
        Registered capabilities with no contract.

        A gap rather than a failure: such a capability still executes, but a
        planner cannot reason about what it needs. Surfaced so the gap is
        visible instead of implicit.
        """
        return sorted(n for n in self._capabilities if n not in self._contracts)

    def unimplemented(self) -> List[str]:
        """
        Declared capabilities with no registered handler.

        Expected to be exactly the SERVICE and EMBEDDED ones. Anything
        ORCHESTRATED appearing here is a real inconsistency — a plan could
        reference it and fail at execution time.
        """
        return sorted(k for k in self._contracts if k not in self._capabilities)

    def register_alternative(self, primary: str, *alternatives: str) -> None:
        """
        Explicitly register one or more valid alternative capabilities for rerouting.

        Used by FailureManager when a primary capability fails and cannot be retried.
        """
        if primary not in self._alternatives:
            self._alternatives[primary] = []
        for alt in alternatives:
            if alt not in self._alternatives[primary]:
                self._alternatives[primary].append(alt)

    def get_alternatives(self, capability_id: str) -> List[str]:
        """
        Get valid registered alternative capabilities for a given capability.

        Combines explicitly registered alternatives with contract-declared alternatives,
        filtering to those actually present in the registry.
        """
        candidates: List[str] = list(self._alternatives.get(capability_id, []))
        if capability_id in self._contracts:
            contract = self._contracts[capability_id]
            for alt in contract.alternative_capabilities:
                if alt not in candidates:
                    candidates.append(alt)
        # Ensure candidate is actually registered and executable
        return [alt for alt in candidates if alt in self._capabilities or alt in self._contracts]

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._capabilities
