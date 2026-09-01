"""Create bounded, referenceable evidence for the Reasoning Agent."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

from netgravity.orchestrator.schemas.reasoning import (
    EvidenceMetric,
    ReasoningEvidencePack,
    ReasoningScope,
)
from netgravity.orchestrator.schemas.twin import DigitalTwinState, TwinComparison


_CURRENCY_FIELDS = {
    "business_network_cost", "solver_objective", "shortage_penalty_cost",
    "facility_cost", "transport_cost", "handling_cost", "inventory_cost",
    "closure_cost", "closure_cost_charged", "opening_cost", "carbon_cost",
    "business_cost_delta", "abs_delta",
}
_PERCENT_FIELDS = {
    "business_cost_delta_pct", "pct_delta", "utilization_pct",
    "avg_utilization_pct", "max_utilization_pct", "pct_demand_in_sla",
}
_RATIO_FIELDS = {
    "demand_fill_rate", "unserved_demand_rate", "rei", "max_rei",
    "risk_factor", "max_risk_factor", "likelihood", "event_probability",
    "share_of_total_units",
}
_UNIT_FIELDS = {
    "throughput_units", "capacity_units", "flow_units", "total_demand",
    "served_demand", "unserved_demand", "rerouted_volume", "units_delta",
    "baseline_units", "comparison_units",
}


def _display(value: Any, key: str) -> tuple[str, str]:
    if value is None:
        return "Not available", ""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value), ""
    if key in _CURRENCY_FIELDS or key.endswith("_cost"):
        return f"₹{value:,.2f}", "INR"
    if key in _PERCENT_FIELDS or key.endswith("_pct"):
        return f"{value:,.2f}%", "percent"
    if key in _RATIO_FIELDS:
        return f"{value:.3f}", "ratio"
    if key in _UNIT_FIELDS or key.endswith("_units"):
        return f"{value:,.0f} units", "units"
    if "carbon_kg" in key:
        return f"{value:,.2f} kg", "kg"
    if "distance_km" in key:
        return f"{value:,.2f} km", "km"
    return f"{value:,.2f}", ""


def _source(key: str) -> str:
    if key in {"rei", "max_rei"} or "exposure" in key:
        return "rei_engine"
    if "risk_factor" in key or key in {"likelihood", "event_probability"}:
        return "risk_engine"
    if key in _PERCENT_FIELDS or key in _UNIT_FIELDS or "demand" in key:
        return "kpi_engine"
    if key in _CURRENCY_FIELDS or key in {"is_open", "solver_status"}:
        return "milp"
    if key in {"abs_delta", "pct_delta", "units_delta"}:
        return "digital_twin_comparison"
    return "deterministic_result"


def _ref(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.>\-]", "_", path).strip("_")[:180]


def policy_thresholds() -> Dict[str, float]:
    """
    Configured thresholds a narrative may cite, as percentages.

    Imported from the module that owns them rather than restated here. If
    `UTILIZATION_THRESHOLDS` changes, the number a briefing quotes changes with
    it — a second copy in this file would be a second definition that drifts.
    """
    from netgravity.config.defaults import UTILIZATION_THRESHOLDS
    return {
        "utilization_over_pct":  UTILIZATION_THRESHOLDS["over_threshold"] * 100.0,
        "utilization_under_pct": UTILIZATION_THRESHOLDS["under_threshold"] * 100.0,
    }


def with_policy_thresholds(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    The payload plus the policy constants a narrative is allowed to CITE.

    A threshold is a fact about the CONFIGURATION rather than a measurement of
    the network — but the numeric-claim validator checks every number in
    generated prose and cannot tell "90% is where we draw the line" apart from
    "90% is what this network measured". Left out of the evidence, the sentence
    "no site reaches the 90% threshold" was adjudicated CONTRADICTED against
    `pct_demand_in_sla = 100` and the figure was stripped out mid-sentence.

    Applied HERE, over whatever payload a caller supplies, rather than inside
    one payload builder. `twin_reasoning_payload` is not the only source: the
    orchestrator assembles its own payload for `reasoning.synthesise` from the
    execution context, so adding the thresholds to the twin builder alone left
    every scenario comparison quoting a threshold it could not ground — four
    contradicted claims on a real client network, and a grounding failure on
    the one path a planner uses most.

    Never overwrites a `thresholds` block a caller already supplied.
    """
    if payload.get("thresholds"):
        return payload
    return {**payload, "thresholds": policy_thresholds()}


def _iter_values(node: Any, path: str = "") -> Iterable[tuple[str, str, Any, Optional[str]]]:
    if isinstance(node, dict):
        entity = node.get("facility_id")
        if not entity and node.get("origin_id") and node.get("destination_id"):
            entity = f"{node['origin_id']}->{node['destination_id']}"
        for key, value in node.items():
            child = f"{path}.{key}" if path else key
            if value is None or isinstance(value, (str, int, float, bool)):
                yield child, key, value, entity
            elif isinstance(value, (dict, list)):
                yield from _iter_values(value, child)
    elif isinstance(node, list):
        for index, item in enumerate(node[:500]):
            yield from _iter_values(item, f"{path}.{index}")


def build_evidence_pack(
    payload: Dict[str, Any],
    *,
    scope: ReasoningScope = ReasoningScope.NETWORK,
    entity_id: Optional[str] = None,
    user_question: str = "",
    unavailable: Optional[Dict[str, Any]] = None,
    provenance: Optional[Dict[str, str]] = None,
) -> ReasoningEvidencePack:
    """Index a deterministic payload without deriving or changing its values."""
    metrics: Dict[str, EvidenceMetric] = {}
    for path, key, value, row_entity in _iter_values(payload):
        ref = _ref(path)
        display, unit = _display(value, key)
        metric_scope = scope if entity_id else ReasoningScope.NETWORK
        metrics[ref] = EvidenceMetric(
            ref=ref,
            label=key.replace("_", " ").title(),
            value=value,
            display_value=display,
            unit=unit,
            source=_source(key),
            scope=metric_scope,
            entity_id=row_entity,
        )
    return ReasoningEvidencePack(
        scope=scope,
        entity_id=entity_id,
        user_question=user_question,
        metrics=metrics,
        payload=payload,
        unavailable=dict(unavailable or {}),
        provenance=dict(provenance or {}),
    )


def twin_reasoning_payload(
    state: DigitalTwinState,
    *,
    scope: ReasoningScope,
    entity_id: Optional[str] = None,
    comparison: Optional[TwinComparison] = None,
) -> Dict[str, Any]:
    """Select the exact twin facts relevant to one requested UI scope."""
    facilities = [item.model_dump(mode="json") for item in state.facilities]
    flows = [item.model_dump(mode="json") for item in state.flows]

    if scope is ReasoningScope.FACILITY:
        facilities = [item for item in facilities if item["facility_id"] == entity_id]
        if not facilities:
            raise ValueError(f"Facility '{entity_id}' is not present in state '{state.state_id}'.")
        flows = [item for item in flows
                 if entity_id in (item["origin_id"], item["destination_id"])]
    elif scope is ReasoningScope.LANE:
        flows = [item for item in flows
                 if f"{item['origin_id']}->{item['destination_id']}" == entity_id]
        if not flows:
            raise ValueError(f"Lane '{entity_id}' is not present in state '{state.state_id}'.")
        facility_ids = {flows[0]["origin_id"], flows[0]["destination_id"]}
        facilities = [item for item in facilities if item["facility_id"] in facility_ids]

    payload: Dict[str, Any] = {
        "network_state": state.kpis.model_dump(mode="json") if state.kpis else {},
        "facilities": facilities,
        "flows": flows,
        "risk": state.risk.model_dump(mode="json") if state.risk else {},
        "state": {
            "state_id": state.state_id,
            "snapshot_id": state.snapshot_id,
            "scenario_id": state.scenario_id,
            "state_type": state.state_type.value,
            "calculation_status": state.calculation_status.value,
            "decisions": list(state.decisions),
        },
    }
    if comparison is not None:
        payload["comparison"] = comparison.model_dump(mode="json")
    return payload
