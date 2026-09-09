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
    "rei", "max_rei",
    "risk_factor", "max_risk_factor", "likelihood", "event_probability",
}

#: Ratios that are a SHARE OF A WHOLE, and read as percentages.
#:
#: "The demand fill rate is 1.000" is the storage format on screen. A fill
#: rate, an unserved share and a share of total units are proportions, and no
#: executive has ever discussed one as a three-decimal ratio.
#:
#: An index is not a proportion, so `rei` and `risk_factor` stay above: they
#: are scores that happen to sit near 1, and rendering an REI of 1.02 as
#: "102%" would assert a percentage of something that has no whole.
#:
#: Only the DISPLAY changes. `EvidenceMetric.value` keeps the ratio — it is
#: what a chart plots — and the unit stays "ratio" so nothing groups these on
#: an axis with figures already expressed out of 100.
_SHARE_FIELDS = {
    "demand_fill_rate", "unserved_demand_rate", "share_of_total_units",
}
#: Counts. "5.00 sites open" is arithmetic notation for a thing you can point
#: at, and the caption beside it already says what is being counted.
_COUNT_FIELDS = {
    "n_facilities_open", "n_facilities_closed", "n_facilities",
    "n_lanes", "n_markets", "n_products", "periods_modelled",
}
_UNIT_FIELDS = {
    "throughput_units", "capacity_units", "flow_units", "total_demand",
    "served_demand", "unserved_demand", "rerouted_volume", "units_delta",
    "baseline_units", "comparison_units",
}


#: What each metric is CALLED, where the storage key does not read as English.
#:
#: These strings are the caption under the headline figure on an Overview
#: tile, the label on every Insights row, the left column of the evidence
#: chain on the deep dive, and the first column of the table in the document
#: that leaves the building. They were the key in Title Case, so a reader was
#: shown "N Facilities Open", "Avg Utilization Pct" and "Pct Demand In Sla" —
#: a variable name, an abbreviation nobody speaks, and an acronym that has
#: been Title-Cased into a word.
#:
#: Anything absent falls back to the Title Case of its key, so this is a list
#: of corrections rather than a registry to keep in step.
_LABELS = {
    # Cost
    "business_network_cost":   "Total network cost",
    "total_cost":              "Total cost",
    "cost_per_period":         "Cost per period",
    "facility_cost":           "Facility cost",
    "transport_cost":          "Transport cost",
    "handling_cost":           "Handling cost",
    "inventory_cost":          "Inventory cost",
    "shortage_cost":           "Shortage cost",
    "carbon_cost":             "Carbon cost",
    "opening_cost":            "Opening cost",
    "closure_cost":            "Closure cost",
    "business_cost_delta":     "Change in network cost",
    "business_cost_delta_pct": "Change in network cost",
    # Service
    "demand_fill_rate":        "Demand met",
    "unserved_demand_rate":    "Demand not met",
    "unserved_demand":         "Demand not served",
    "served_demand":           "Demand served",
    "total_demand":            "Total demand",
    "pct_demand_in_sla":       "Demand within its lead time",
    "demand_within_sla":       "Demand within its lead time",
    # Capacity
    "avg_utilization_pct":     "Average utilisation",
    "max_utilization_pct":     "Busiest site",
    "min_utilization_pct":     "Least used site",
    "utilization_pct":         "Utilisation",
    "peak_utilization_pct":    "Peak utilisation",
    "capacity_units":          "Capacity",
    "throughput_units":        "Throughput",
    "flow_units":              "Volume on this lane",
    # Footprint
    "n_facilities_open":       "Sites open",
    "n_facilities_closed":     "Sites not used",
    "n_facilities":            "Sites in the network",
    # Carbon
    "total_carbon_kg":         "Transport emissions",
    "carbon_kg":               "Emissions",
    # Policy thresholds
    "utilization_over_pct":    "Utilisation threshold",
    "utilization_under_pct":   "Under-use threshold",
    # Resilience
    "rei":                     "Resilience exposure index",
    "max_rei":                 "Highest single-site exposure",
    "share_of_total_units":    "Share of total volume",
    "distance_km":             "Distance",
    "lead_time_days":          "Lead time",
    "rate_per_unit":           "Rate per unit",
}


def metric_label(key: str) -> str:
    """The metric's name for a reader, or its key in Title Case."""
    return _LABELS.get(key) or key.replace("_", " ").title()


#: Symbols for the currencies a client is likely to price a network in. A code
#: that is not here is rendered as the code itself ("SEK 1,200.00"), which is
#: unambiguous — unlike stamping every amount with a rupee sign, which is what
#: this module used to do to networks priced in dollars.
_CURRENCY_SYMBOLS: Dict[str, str] = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CNY": "¥", "AUD": "A$", "CAD": "C$", "SGD": "S$", "NZD": "NZ$",
    "HKD": "HK$", "BRL": "R$", "ZAR": "R", "KRW": "₩", "RUB": "₽",
    "TRY": "₺", "ILS": "₪", "THB": "฿", "PHP": "₱", "VND": "₫",
}


def format_money(value: float, currency: Optional[str]) -> str:
    """
    One amount, in the currency the network states.

    With no currency the amount is rendered bare. That is the honest reading of
    an upload that never named a unit, and it must stay distinguishable from a
    figure we know to be rupees.
    """
    if not currency:
        return f"{value:,.2f}"
    code = str(currency).strip().upper()
    symbol = _CURRENCY_SYMBOLS.get(code)
    return f"{symbol}{value:,.2f}" if symbol else f"{code} {value:,.2f}"


def _find_currency(node: Any, depth: int = 0) -> Optional[str]:
    """
    The currency this payload's money is denominated in.

    Read from the payload rather than passed in, because the payload is built
    from `NetworkStateResult`, which now carries the currency alongside the
    costs it describes — so the unit and the number cannot drift apart.
    """
    if depth > 6:
        return None
    if isinstance(node, dict):
        value = node.get("currency")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
        for child in node.values():
            if isinstance(child, (dict, list)):
                found = _find_currency(child, depth + 1)
                if found:
                    return found
    elif isinstance(node, list):
        for item in node[:50]:
            if isinstance(item, (dict, list)):
                found = _find_currency(item, depth + 1)
                if found:
                    return found
    return None


def _display(value: Any, key: str, currency: Optional[str] = None) -> tuple[str, str]:
    if value is None:
        return "Not available", ""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value), ""
    if key in _CURRENCY_FIELDS or key.endswith("_cost"):
        return format_money(float(value), currency), (currency or "currency")
    if key in _PERCENT_FIELDS or key.endswith("_pct"):
        return f"{value:,.2f}%", "percent"
    if key in _SHARE_FIELDS:
        # The unit stays "ratio" deliberately: the stored quantity is one, and
        # `display_value` is the only thing a reader reads (`value` exists to
        # be plotted). Calling it "percent" here would let a chart group it on
        # an axis with figures that really are out of 100 and plot 1.0 there.
        return f"{value * 100:,.1f}%", "ratio"
    if key in _RATIO_FIELDS:
        return f"{value:.3f}", "ratio"
    if key in _COUNT_FIELDS:
        return f"{value:,.0f}", "count"
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
    currency = _find_currency(payload)
    for path, key, value, row_entity in _iter_values(payload):
        ref = _ref(path)
        display, unit = _display(value, key, currency)
        metric_scope = scope if entity_id else ReasoningScope.NETWORK
        metrics[ref] = EvidenceMetric(
            ref=ref,
            label=metric_label(key),
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
