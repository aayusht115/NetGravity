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

from netgravity.schemas.network import (
    CanonicalNetwork,
    FacilityRecord,
    FacilityStatus,
    NodeRole,
)
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
                spec.capacity_delta_units, spec.capacity_set_units,
            )
        elif spec.action == ScenarioActionType.CHANGE_DEMAND:
            working, overrides = self._change_demand(working, spec.demand_multiplier)
        elif spec.action == ScenarioActionType.SHIFT_VOLUME:
            working, overrides = self._shift_volume(
                working, spec.facility_ids, spec.target_facility_id,
            )
        elif spec.action == ScenarioActionType.ADD_FACILITY:
            working, overrides = self._add_facility(working, spec)
        elif spec.action == ScenarioActionType.CHANGE_TRANSPORT_COST:
            working, overrides = self._change_transport_cost(
                working, spec.facility_ids, spec.transport_cost_multiplier,
            )
        elif spec.action == ScenarioActionType.CHANGE_SLA:
            working, overrides = self._change_sla(working, spec.sla_days_delta)
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
        set_units: Optional[float] = None,
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Scale capacity by a ratio, shift it by units, or set it outright.

        The three are kept distinct all the way down. "Reduce by 2,000" and
        "set to 2,000" coincide only by accident, and collapsing one into the
        other requires knowing the current capacity — so a wrong guess changes
        the answer rather than degrading it.

        Exactly one form applies; `ScenarioValidator` has already rejected
        several and none, and has already refused a delta that would go
        negative.
        """
        supplied = [v for v in (multiplier, delta_units, set_units) if v is not None]
        if not supplied:
            raise InvalidScenarioError(
                "CHANGE_CAPACITY requires a capacity_multiplier, "
                "capacity_delta_units or capacity_set_units.",
                context={"facility_ids": facility_ids},
            )
        if len(supplied) > 1:
            raise InvalidScenarioError(
                "CHANGE_CAPACITY accepts exactly one of capacity_multiplier, "
                "capacity_delta_units or capacity_set_units.",
                context={"facility_ids": facility_ids},
            )

        targets = set(facility_ids)

        def new_capacity(current: float) -> float:
            if multiplier is not None:
                return current * multiplier
            if set_units is not None:
                return float(set_units)
            return current + float(delta_units)  # type: ignore[arg-type]

        facilities = [
            fac.model_copy(update={
                "capacity_units_per_period": new_capacity(fac.capacity_units_per_period),
            }) if fac.id in targets else fac
            for fac in network.facilities
        ]
        def describe(fid: str) -> str:
            if multiplier is not None:
                return f"CHANGE_CAPACITY {fid} x{multiplier}"
            if set_units is not None:
                return f"CHANGE_CAPACITY {fid} = {float(set_units):,.0f} units/period"
            return f"CHANGE_CAPACITY {fid} {float(delta_units):+,.0f} units/period"

        overrides = [describe(fid) for fid in facility_ids]
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

    def _add_facility(
        self,
        network: CanonicalNetwork,
        spec: ScenarioIntentSpec,
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Introduce a site the client does not operate today.

        Delegated to `ScenarioEngine` for the same reason `_close` is: the
        engine already knows how to connect a new node to a network — inbound
        lanes from every plant, outbound lanes to every market, each priced at
        the NETWORK'S OWN average rate per km over its existing road lanes and
        the haversine distance between the two points. Deriving the rate from
        the client's own freight instead of a constant is what makes the answer
        theirs; re-deriving it here would be a second implementation of a
        transport tariff, which is exactly what must not happen.

        The site is added as a CANDIDATE the MILP may leave shut, not as a
        facility pinned open. If opening it does not pay, the solver says so by
        not opening it — which is the answer the user is asking for.
        """
        from netgravity.scenarios.engine import ScenarioEngine

        site = spec.new_facility
        if site is None:  # pragma: no cover — the validator refuses first
            raise InvalidScenarioError("ADD_FACILITY requires a new_facility.")

        role = NodeRole.PLANT if site.role.upper() == "PLANT" else NodeRole.DC
        facility_id = self._greenfield_id(network, site.name)
        capacity = float(site.capacity_units_per_period)

        new_facility = FacilityRecord(
            id=facility_id,
            name=site.name.strip(),
            role=role,
            # CANDIDATE, not EXISTING: it is not operating, and the opening cost
            # must be charged if the solver decides to use it.
            status=FacilityStatus.CANDIDATE,
            latitude=float(site.latitude),
            longitude=float(site.longitude),
            capacity_units_per_period=capacity,
            # A DC keeps the schema's "not a producer" default (1e12).
            #
            # This said `0.0` for anything that is not a plant, reading
            # "produces nothing" — but the field means "may not SHIP more than
            # this", and the MILP's capacity constraint takes the smaller of the
            # two limits. Every greenfield DC was therefore pinned to zero
            # outbound flow: it appeared in the solve, reported its capacity,
            # and could not carry a single unit or ever be opened. A free
            # 100,000-unit DC placed on top of an unserved market stayed shut
            # while 8,733 units of that market's demand went unserved at a
            # penalty of ₹1,000,000 each.
            production_capacity_units_per_period=(
                capacity if role is NodeRole.PLANT else 1e12),
            fixed_cost_per_year=float(site.fixed_cost_per_year),
            handling_cost_per_unit=float(site.handling_cost_per_unit),
            is_closable=True,
            is_mandatory=False,
            is_forced_closed=False,
        )

        scenario = Scenario(
            scenario_id="orchestrator_add_facility",
            scenario_name=f"Open {new_facility.name}",
            facility_changes=[FacilityChange(
                facility_id=facility_id, action="ADD_FACILITY",
                new_facility=new_facility,
            )],
        )
        engine = ScenarioEngine()
        try:
            modified = engine._apply_overrides(network, scenario)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            raise InvalidScenarioError(
                f"The new site '{site.name}' could not be connected to this "
                f"network: {exc}",
                context={"facility_id": facility_id,
                         "latitude": site.latitude, "longitude": site.longitude},
                cause=exc,
            ) from exc

        new_lanes = len(modified.lanes) - len(network.lanes)
        return modified, [
            f"ADD_FACILITY {facility_id} '{new_facility.name}' "
            f"({site.latitude:.4f}, {site.longitude:.4f}) "
            f"capacity {capacity:,.0f} units/period, {new_lanes} lanes derived"
        ]

    @staticmethod
    def _greenfield_id(network: CanonicalNetwork, name: str) -> str:
        """
        A stable, readable id that cannot collide with an existing facility.

        Derived from the name the user typed rather than a UUID, so the site is
        recognisable everywhere it appears — the map tooltip, the comparison
        table, the audit trail.
        """
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.strip().upper())
        slug = "_".join(part for part in slug.split("_") if part)[:24] or "SITE"
        taken = {f.id for f in network.facilities}
        candidate = f"NEW_{slug}"
        suffix = 2
        while candidate in taken:
            candidate = f"NEW_{slug}_{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _change_transport_cost(
        network: CanonicalNetwork,
        facility_ids: List[str],
        multiplier: Optional[float],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Scale freight rates on the lanes in scope.

        `rate_per_km` moves with `rate_per_unit` where the lane carries one, so
        a later relocation re-derives its distance cost from the scenario's
        rates rather than reverting to the baseline's.

        Naming facilities narrows this to the lanes touching them — "our Pune
        carrier raised rates" is a real question, and applying it network-wide
        would answer a different one.
        """
        if multiplier is None:  # pragma: no cover — the validator refuses first
            raise InvalidScenarioError(
                "CHANGE_TRANSPORT_COST requires a transport_cost_multiplier.")

        targets = set(facility_ids)

        def in_scope(lane) -> bool:
            if not targets:
                return True
            return lane.origin_id in targets or lane.destination_id in targets

        touched = 0
        lanes = []
        for lane in network.lanes:
            if not in_scope(lane):
                lanes.append(lane)
                continue
            touched += 1
            update = {"rate_per_unit": lane.rate_per_unit * multiplier}
            if lane.rate_per_km is not None:
                update["rate_per_km"] = lane.rate_per_km * multiplier
            lanes.append(lane.model_copy(update=update))

        if touched == 0:
            raise InvalidScenarioError(
                f"No lane in this network touches {sorted(targets)}, so there is "
                f"no freight rate to change.",
                context={"facility_ids": sorted(targets)},
            )

        scope = f"lanes touching {sorted(targets)}" if targets else "every lane"
        return network.model_copy(update={"lanes": lanes}), [
            f"CHANGE_TRANSPORT_COST x{multiplier} on {scope} ({touched} lanes)"
        ]

    @staticmethod
    def _change_sla(
        network: CanonicalNetwork,
        days_delta: Optional[float],
    ) -> Tuple[CanonicalNetwork, List[str]]:
        """
        Tighten or relax the delivery promise by a number of days.

        Refuses outright when no demand row states an SLA. The alternative —
        applying the change to nothing and returning a "scenario" identical to
        the baseline — reports a change that did not happen, and the user reads
        the unchanged cost as evidence that tightening service is free.
        """
        if days_delta is None:  # pragma: no cover — the validator refuses first
            raise InvalidScenarioError("CHANGE_SLA requires an sla_days_delta.")

        stated = [d for d in network.demands if d.sla_days is not None]
        if not stated:
            raise InvalidScenarioError(
                "No demand row in this network states an SLA in days, so there "
                "is no delivery promise to tighten or relax. Add an SLA column "
                "to the demand data to run this scenario.",
                context={"demand_rows": len(network.demands)},
            )

        demands = [
            d.model_copy(update={"sla_days": max(0.0, d.sla_days + days_delta)})
            if d.sla_days is not None else d
            for d in network.demands
        ]
        return network.model_copy(update={"demands": demands}), [
            f"CHANGE_SLA {days_delta:+.1f} days on {len(stated)} of "
            f"{len(network.demands)} demand rows"
        ]

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
