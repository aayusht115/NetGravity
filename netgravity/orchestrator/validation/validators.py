"""
Orchestrator — Validation layer.

Everything crossing a trust boundary is validated here: inbound requests,
model-proposed scenarios, and engine outputs.

This is where a hallucinated facility identifier dies. The intent agent may
propose "close DC_ATLANTIS"; the scenario validator checks the real network,
finds no such facility, and fails the run with a clear message — long before
the MILP is invoked.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from netgravity.orchestrator.exceptions import (
    InvalidRequestError,
    InvalidScenarioError,
    ValidationFailureError,
)
from netgravity.orchestrator.schemas.requests import (
    NETWORK_WIDE_ACTIONS,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.schemas.network import CanonicalNetwork, NodeRole

logger = logging.getLogger(__name__)

MARKET_ROLES = {NodeRole.MARKET, NodeRole.CUSTOMER}
MAX_INPUT_CHARS = 8_000


class RequestValidator:
    """Validates inbound requests before any work is planned."""

    def validate(self, request: OrchestratorRequest) -> None:
        """
        Raises:
            InvalidRequestError: unusable request.
        """
        has_text = bool((request.input or "").strip())
        has_intent = request.explicit_intent is not None
        has_signal = request.external_signal is not None

        if not (has_text or has_intent or has_signal):
            raise InvalidRequestError(
                "Request must supply at least one of: input text, explicit_intent, "
                "or external_signal.",
                context={"request_id": request.request_id},
            )

        if len(request.input or "") > MAX_INPUT_CHARS:
            raise InvalidRequestError(
                f"Input is {len(request.input):,} characters, above the "
                f"{MAX_INPUT_CHARS:,} limit.",
                context={"request_id": request.request_id},
            )

        if not request.request_id:
            raise InvalidRequestError("request_id must not be empty.")


class ScenarioValidator:
    """
    Validates scenario proposals against the REAL network.

    Every identifier is checked for existence and for being a legal target. A
    proposal that survives this is guaranteed to reference real, disruptable
    facilities.
    """

    def validate(
        self,
        spec: ScenarioIntentSpec,
        network: CanonicalNetwork,
    ) -> None:
        """
        Raises:
            InvalidScenarioError: unknown facility, illegal target, or an
                out-of-range multiplier.
        """
        fac_map = {f.id: f for f in network.facilities}

        # A greenfield site is validated on its own terms: it deliberately
        # names no existing facility, because it is not one yet.
        if spec.action == ScenarioActionType.ADD_FACILITY:
            self._validate_greenfield(spec, network, fac_map)
            return

        # Demand, freight rates and the delivery promise describe the whole
        # network. Naming a facility narrows them; naming none is not an error.
        if not spec.facility_ids and spec.action not in NETWORK_WIDE_ACTIONS:
            raise InvalidScenarioError(
                f"Scenario action '{spec.action.value}' names no facility.",
                context={"action": spec.action.value},
            )

        unknown = [fid for fid in spec.facility_ids if fid not in fac_map]
        if unknown:
            raise InvalidScenarioError(
                f"Scenario references facilities that do not exist in network "
                f"'{network.network_id}': {unknown}. Known facilities: "
                f"{sorted(fac_map)[:25]}",
                context={"unknown": unknown, "network_id": network.network_id},
            )

        # Markets are demand, not capacity — they cannot be closed or disrupted.
        if spec.action in (ScenarioActionType.CLOSE_FACILITY, ScenarioActionType.OPEN_FACILITY):
            markets = [fid for fid in spec.facility_ids if fac_map[fid].role in MARKET_ROLES]
            if markets:
                raise InvalidScenarioError(
                    f"Cannot {spec.action.value} market/customer nodes {markets}: they "
                    f"represent demand, not network capacity.",
                    context={"markets": markets},
                )

        if spec.target_facility_id is not None and spec.target_facility_id not in fac_map:
            raise InvalidScenarioError(
                f"Scenario target facility '{spec.target_facility_id}' does not exist.",
                context={"target": spec.target_facility_id},
            )

        if spec.capacity_multiplier is not None and spec.capacity_multiplier < 0:
            raise InvalidScenarioError(
                f"capacity_multiplier must be >= 0, got {spec.capacity_multiplier}.",
            )

        supplied = [
            name for name, value in (
                ("capacity_multiplier", spec.capacity_multiplier),
                ("capacity_delta_units", spec.capacity_delta_units),
                ("capacity_set_units", spec.capacity_set_units),
            ) if value is not None
        ]
        if len(supplied) > 1:
            raise InvalidScenarioError(
                f"{' and '.join(supplied)} are mutually exclusive; supplying more "
                f"than one leaves the intended capacity ambiguous. 'reduce by "
                f"2,000' and 'set to 2,000' are different instructions.",
                context={"action": spec.action.value, "supplied": supplied},
            )

        if spec.capacity_set_units is not None and spec.capacity_set_units < 0:
            raise InvalidScenarioError(
                f"capacity_set_units must be >= 0, got {spec.capacity_set_units}.",
            )

        if spec.action == ScenarioActionType.CHANGE_CAPACITY:
            if not supplied:
                raise InvalidScenarioError(
                    "CHANGE_CAPACITY requires capacity_multiplier, "
                    "capacity_delta_units or capacity_set_units.",
                    context={"facility_ids": spec.facility_ids},
                )
            # A delta that drives capacity below zero is rejected rather than
            # clamped: clamping to 0 would silently turn "reduce by 5,000" into
            # a full closure, which is a structurally different action.
            if spec.capacity_delta_units is not None:
                for fid in spec.facility_ids:
                    current = fac_map[fid].capacity_units_per_period
                    if current + spec.capacity_delta_units < 0:
                        raise InvalidScenarioError(
                            f"capacity_delta_units {spec.capacity_delta_units:+,.0f} would "
                            f"take '{fid}' from {current:,.0f} to "
                            f"{current + spec.capacity_delta_units:,.0f} units/period. "
                            f"Negative capacity is not meaningful; to remove the facility "
                            f"entirely use CLOSE_FACILITY, which is governed as a "
                            f"structural change.",
                            context={"facility_id": fid, "current_capacity": current,
                                     "delta": spec.capacity_delta_units},
                        )

        if spec.demand_multiplier is not None and spec.demand_multiplier < 0:
            raise InvalidScenarioError(
                f"demand_multiplier must be >= 0, got {spec.demand_multiplier}.",
            )

        # A growth scope that matches no demand row is refused, not run.
        #
        # Scaling zero rows produces a scenario identical to the base case, and
        # a comparison card reading "no change in cost" is the correct answer to
        # a question nobody asked. Misspelling a region is not a finding.
        if spec.action == ScenarioActionType.CHANGE_DEMAND:
            if spec.demand_region:
                want = spec.demand_region.strip().lower()
                known = {(f.region or "").strip() for f in network.facilities if f.region}
                if not any(r.lower() == want for r in known):
                    raise InvalidScenarioError(
                        f"No facility or market in this network is in region "
                        f"'{spec.demand_region}'. Known regions: "
                        f"{sorted(known) or 'none stated in the upload'}.",
                        context={"region": spec.demand_region, "known": sorted(known)},
                    )
            if spec.demand_product_category:
                want = spec.demand_product_category.strip().lower()
                known = {(p.category or "").strip() for p in network.products if p.category}
                if not any(c.lower() == want for c in known):
                    raise InvalidScenarioError(
                        f"No product in this network is in category "
                        f"'{spec.demand_product_category}'. Known categories: "
                        f"{sorted(known) or 'none stated in the upload'}.",
                        context={"category": spec.demand_product_category,
                                 "known": sorted(known)},
                    )

        if spec.action == ScenarioActionType.CHANGE_TRANSPORT_COST:
            if spec.transport_cost_multiplier is None:
                raise InvalidScenarioError(
                    "CHANGE_TRANSPORT_COST requires a transport_cost_multiplier.",
                )
            if spec.transport_cost_multiplier <= 0:
                raise InvalidScenarioError(
                    f"transport_cost_multiplier must be > 0, got "
                    f"{spec.transport_cost_multiplier}. A rate of zero is not a "
                    f"freight-rate change; it removes transport cost from the "
                    f"model entirely.",
                )

        if spec.action == ScenarioActionType.CHANGE_SLA:
            if spec.sla_days_delta is None:
                raise InvalidScenarioError(
                    "CHANGE_SLA requires an sla_days_delta, in days.",
                )
            # A promise can only be tightened as far as same-day. Which rows
            # actually carry an SLA is the builder's business — it refuses when
            # the network states none, rather than silently changing nothing.
            stated = [d.sla_days for d in network.demands if d.sla_days is not None]
            if stated and min(stated) + spec.sla_days_delta < 0:
                raise InvalidScenarioError(
                    f"sla_days_delta {spec.sla_days_delta:+.1f} would take the "
                    f"tightest stated SLA ({min(stated):.1f} days) below zero. "
                    f"A negative delivery promise is not meaningful.",
                    context={"tightest_sla_days": min(stated)},
                )

    # ------------------------------------------------------------------
    def _validate_greenfield(
        self,
        spec: ScenarioIntentSpec,
        network: CanonicalNetwork,
        fac_map: Dict[str, Any],
    ) -> None:
        """
        Check a proposed new site before any network is built from it.

        A greenfield site is the one scenario action whose target does NOT have
        to exist — so the existence check that protects every other action
        against a hallucinated identifier is replaced by checks on the thing
        being proposed instead.
        """
        site = spec.new_facility
        if site is None:
            raise InvalidScenarioError(
                "ADD_FACILITY requires a new_facility: the site to open, with a "
                "name, coordinates and a capacity.",
                context={"action": spec.action.value},
            )
        if not site.name.strip():
            raise InvalidScenarioError("A new facility needs a name.")
        if site.capacity_units_per_period <= 0:
            raise InvalidScenarioError(
                f"A new facility needs a capacity above zero, got "
                f"{site.capacity_units_per_period}. A site with no capacity "
                f"cannot serve anything, so opening it is not a scenario.",
            )
        if site.fixed_cost_per_year < 0 or site.handling_cost_per_unit < 0:
            raise InvalidScenarioError("Facility costs must not be negative.")
        if site.role.upper() not in {"DC", "PLANT", "WAREHOUSE", "HUB"}:
            raise InvalidScenarioError(
                f"A new site must be a DC or a PLANT, got '{site.role}'. Markets "
                f"are demand rather than capacity, and adding one is a change to "
                f"the demand data, not a footprint scenario.",
            )
        # The MILP needs somewhere to send the volume. A network with no market
        # nodes cannot absorb a new distribution centre.
        markets = [f for f in network.facilities if f.role in MARKET_ROLES]
        if not markets:
            raise InvalidScenarioError(
                "This network has no market or customer nodes, so a new "
                "distribution centre has nothing to serve.",
            )
        located = [f for f in markets if f.latitude is not None and f.longitude is not None]
        if not located:
            raise InvalidScenarioError(
                "No market in this network carries coordinates, so the freight "
                "cost from a new site to it cannot be derived from distance.",
            )

    def validate_all(
        self,
        specs: Sequence[ScenarioIntentSpec],
        network: CanonicalNetwork,
    ) -> None:
        for spec in specs:
            self.validate(spec, network)


class ResultValidator:
    """
    Sanity checks on deterministic engine output.

    Cheap invariants that catch integration mistakes early — a reconciliation
    gap or negative cost means something upstream is wrong and must not be
    quietly presented as an answer.
    """

    def validate_optimization(self, output: Dict[str, Any]) -> List[str]:
        """Return warnings; raise only on a genuinely impossible result."""
        warnings: List[str] = []

        if not output:
            raise ValidationFailureError("Optimization produced no output.")

        cost = output.get("business_network_cost")
        if cost is None:
            warnings.append("Optimization result carries no business network cost.")
        elif cost < 0:
            raise ValidationFailureError(
                f"Business network cost is negative ({cost}), which is not physically "
                f"meaningful.",
                context={"business_network_cost": cost},
            )

        if output.get("reconciliation_is_closed") is False:
            warnings.append(
                "Cost reconciliation did not close for this result; the cost breakdown "
                "should be treated as unverified."
            )

        served = output.get("served_demand")
        total = output.get("total_demand")
        if served is not None and total is not None and served > total + 1e-6:
            raise ValidationFailureError(
                f"Served demand ({served}) exceeds total demand ({total}).",
                context={"served": served, "total": total},
            )

        for feature in output.get("service_unsupported_features", []) or []:
            warnings.append(f"Service methodology: {feature}")

        return warnings

    def validate_rei(self, output: Dict[str, Any]) -> List[str]:
        warnings: List[str] = []
        if not output:
            raise ValidationFailureError("REI assessment produced no output.")

        for row in output.get("facilities", []) or []:
            rei = row.get("rei")
            if rei is not None and rei > 1.0 + 1e-6:
                raise ValidationFailureError(
                    f"REI for {row.get('facility_id')} is {rei}, above the maximum of "
                    f"1.0. REI is normalised against the largest impact, so this "
                    f"indicates a defect.",
                    context={"facility_id": row.get("facility_id"), "rei": rei},
                )
            if rei is not None and rei < 0:
                warnings.append(
                    f"{row.get('facility_id')}: negative REI ({rei:.4f}) — a disruption "
                    f"that reduces business cost. Retained for investigation."
                )

        warnings.extend(output.get("warnings", []) or [])
        return warnings


class SnapshotValidator:
    """Guards against operating on stale or mismatched network versions."""

    def validate_freshness(self, snapshot_manager: Any, snapshot_id: Optional[str]) -> None:
        """
        Raises:
            StaleSnapshotError: the pinned snapshot is no longer current.
            MissingDataError: no snapshot is registered.
        """
        from netgravity.orchestrator.exceptions import MissingDataError

        if snapshot_id is None:
            raise MissingDataError(
                "Execution has no pinned network snapshot; refusing to run without a "
                "known data version."
            )
        snapshot_manager.assert_fresh(snapshot_id)

    def validate_consistency(self, results: Sequence[Dict[str, Any]]) -> None:
        """
        Ensure combined results all came from one network version.

        Raises:
            ValidationFailureError: results span different data versions.
        """
        versions = {
            r.get("data_version") for r in results
            if r and r.get("data_version") is not None
        }
        if len(versions) > 1:
            raise ValidationFailureError(
                f"Results span multiple network data versions {sorted(versions)}. "
                f"Combining them would produce an incoherent answer.",
                context={"versions": sorted(v for v in versions if v)},
            )
