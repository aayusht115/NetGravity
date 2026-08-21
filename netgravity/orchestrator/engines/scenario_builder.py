"""
Orchestrator — Scenario materialisation.

Turns a validated `ScenarioIntentSpec` into a hypothetical `CanonicalNetwork`,
reusing NetGravity's existing `ScenarioEngine` override semantics rather than
reimplementing them.

Isolation is the whole point of this module:

    observed snapshot  --(deep copy)-->  scenario network  --> stored separately

The parent snapshot is never mutated. Two scenarios built from the same parent
share nothing. There is deliberately no path back from a scenario network into
the snapshot store.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from netgravity.schemas.network import CanonicalNetwork, FacilityStatus, NodeRole
from netgravity.schemas.scenario import FacilityChange, Scenario

from netgravity.orchestrator.exceptions import InvalidScenarioError
from netgravity.orchestrator.schemas.requests import ScenarioActionType, ScenarioIntentSpec

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}


class ScenarioBuilder:
    """Materialises hypothetical networks from validated specs."""

    def build(
        self,
        base_network: CanonicalNetwork,
        spec: ScenarioIntentSpec,
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Apply a scenario spec to a COPY of the base network.

        Args:
            base_network: Observed (or parent) network. Never mutated.
            spec:         Already validated by `ScenarioValidator`.

        Returns:
            (scenario_network, human-readable override descriptions)

        Raises:
            InvalidScenarioError: the action is unsupported or cannot be applied.
        """
        # Defensive copy: the caller's network — and the stored snapshot it may
        # have come from — must be untouchable from here.
        working = base_network.model_copy(deep=True)
        overrides: List[str] = []

        if spec.action == ScenarioActionType.CLOSE_FACILITY:
            working, overrides = self._close(working, spec.facility_ids)
        elif spec.action == ScenarioActionType.OPEN_FACILITY:
            working, overrides = self._open(working, spec.facility_ids)
        elif spec.action == ScenarioActionType.CHANGE_CAPACITY:
            working, overrides = self._change_capacity(
                working, spec.facility_ids, spec.capacity_multiplier,
                spec.capacity_delta_units,
            )
        elif spec.action == ScenarioActionType.CHANGE_DEMAND:
            working, overrides = self._change_demand(working, spec.demand_multiplier)
        elif spec.action == ScenarioActionType.SHIFT_VOLUME:
            working, overrides = self._shift_volume(
                working, spec.facility_ids, spec.target_facility_id,
            )
        else:  # pragma: no cover - enum is exhaustive above
            raise InvalidScenarioError(
                f"Unsupported scenario action '{spec.action.value}'.",
                context={"action": spec.action.value},
            )

        logger.info(
            "orchestrator.scenario.materialised action=%s overrides=%s",
            spec.action.value, overrides,
        )
        return working, overrides

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _close(
        self, network: CanonicalNetwork, facility_ids: List[str],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Close facilities via the existing ScenarioEngine.

        Delegated rather than hand-rolled so closure economics behave
        identically to every other closure in the system — in particular the
        engine preserves `baseline_status`, which is what lets the MILP charge
        closure cost for an EXISTING facility whose `status` it overwrites.
        """
        from netgravity.scenarios.engine import ScenarioEngine

        scenario = Scenario(
            scenario_id="orchestrator_close",
            scenario_name="Close facilities",
            facility_changes=[
                FacilityChange(facility_id=fid, action="CLOSE") for fid in facility_ids
            ],
        )
        engine = ScenarioEngine()
        try:
            modified = engine._apply_overrides(network, scenario)  # noqa: SLF001
        except AttributeError:
            modified = self._close_manually(network, facility_ids)
        except Exception as exc:  # noqa: BLE001
            raise InvalidScenarioError(
                f"Failed to apply closure scenario: {exc}",
                context={"facility_ids": facility_ids}, cause=exc,
            ) from exc

        return modified, [f"CLOSE_FACILITY {fid}" for fid in facility_ids]

    @staticmethod
    def _close_manually(
        network: CanonicalNetwork, facility_ids: List[str],
    ) -> CanonicalNetwork:
        """
        Fallback closure if the engine's private override hook moves.

        Mirrors the engine's semantics exactly, including preserving
        `baseline_status` so closure economics still price the transition.
        """
        targets = set(facility_ids)
        facilities = []
        for fac in network.facilities:
            if fac.id in targets:
                facilities.append(fac.model_copy(update={
                    "baseline_status": fac.baseline_status or fac.status,
                    "status": FacilityStatus.CLOSED,
                    "is_forced_closed": True,
                    "is_mandatory": False,
                    "is_closable": True,
                    "capacity_units_per_period": 0.0,
                    "production_capacity_units_per_period": 0.0,
                    "min_throughput_per_period": 0.0,
                }))
            else:
                facilities.append(fac)
        return network.model_copy(update={"facilities": facilities})

    @staticmethod
    def _open(
        network: CanonicalNetwork, facility_ids: List[str],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        targets = set(facility_ids)
        facilities = [
            fac.model_copy(update={
                "is_forced_closed": False,
                "is_mandatory": True,
                "is_closable": False,
            }) if fac.id in targets else fac
            for fac in network.facilities
        ]
        return (network.model_copy(update={"facilities": facilities}),
                [f"OPEN_FACILITY {fid}" for fid in facility_ids])

    @staticmethod
    def _change_capacity(
        network: CanonicalNetwork,
        facility_ids: List[str],
        multiplier: Optional[float],
        delta_units: Optional[float] = None,
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Scale capacity by a ratio, or shift it by an absolute number of units.

        Exactly one form applies; `ScenarioValidator` has already rejected both
        and neither, and has already refused a delta that would go negative.
        """
        if multiplier is None and delta_units is None:
            raise InvalidScenarioError(
                "CHANGE_CAPACITY requires a capacity_multiplier or capacity_delta_units.",
                context={"facility_ids": facility_ids},
            )
        if multiplier is not None and delta_units is not None:
            raise InvalidScenarioError(
                "CHANGE_CAPACITY accepts capacity_multiplier or capacity_delta_units, "
                "not both.",
                context={"facility_ids": facility_ids},
            )

        targets = set(facility_ids)

        def new_capacity(current: float) -> float:
            if multiplier is not None:
                return current * multiplier
            return current + float(delta_units)  # type: ignore[arg-type]

        facilities = [
            fac.model_copy(update={
                "capacity_units_per_period": new_capacity(fac.capacity_units_per_period),
            }) if fac.id in targets else fac
            for fac in network.facilities
        ]
        overrides = [
            (f"CHANGE_CAPACITY {fid} x{multiplier}" if multiplier is not None
             else f"CHANGE_CAPACITY {fid} {float(delta_units):+,.0f} units/period")
            for fid in facility_ids
        ]
        return network.model_copy(update={"facilities": facilities}), overrides

    @staticmethod
    def _change_demand(
        network: CanonicalNetwork, multiplier: Optional[float],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        if multiplier is None:
            raise InvalidScenarioError("CHANGE_DEMAND requires a demand_multiplier.")
        demands = [
            d.model_copy(update={"quantity": d.quantity * multiplier})
            for d in network.demands
        ]
        return (network.model_copy(update={"demands": demands}),
                [f"CHANGE_DEMAND all x{multiplier}"])

    def _shift_volume(
        self,
        network: CanonicalNetwork,
        source_ids: List[str],
        target_id: Optional[str],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Shift a facility's volume to another by closing the source.

        The MILP then reallocates optimally, which is a more honest model of
        "shift Delhi's volume to Kolkata" than hand-assigning flows: the
        optimizer decides how the network actually absorbs it. The target is
        pinned open so it is genuinely available to receive the volume.
        """
        if not target_id:
            raise InvalidScenarioError(
                "SHIFT_VOLUME requires a target_facility_id.",
                context={"source_ids": source_ids},
            )

        modified, overrides = self._close(network, source_ids)
        facilities = [
            fac.model_copy(update={
                "is_forced_closed": False,
                "is_mandatory": True,
                "is_closable": False,
            }) if fac.id == target_id else fac
            for fac in modified.facilities
        ]
        overrides.append(f"SHIFT_VOLUME {source_ids} -> {target_id}")
        return modified.model_copy(update={"facilities": facilities}), overrides
