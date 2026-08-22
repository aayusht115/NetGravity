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

        if not spec.facility_ids:
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
