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

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._capabilities
