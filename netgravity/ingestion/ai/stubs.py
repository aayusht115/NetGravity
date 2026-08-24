"""
NetGravity — Canned AI Responses (stub mode)
=============================================
Used when NETGRAVITY_LLM_API_KEY is not set.

PURPOSE
-------
1. The pipeline runs end to end with no credentials.
2. Tests are fast and deterministic — no network, no spend, no flakiness.
3. A demo can be given before the LLM provider decision is finalised.

Every stubbed result is flagged as stubbed in the ingestion report, so canned
output can never be mistaken for a live extraction.

These payloads mirror the shape a real model returns, and their content
matches the Case 16 India storyline already used by the frontend:
TransCorp quotes a lower headline rate than SpeedFreight but hides a
non-serviceable-location surcharge that flips the comparison.
"""

from __future__ import annotations

from typing import Any, Dict

# ---------------------------------------------------------------------------
# Distributor column mapping
# ---------------------------------------------------------------------------

_DISTRIBUTOR_MAPPING: Dict[str, Any] = {
    "target_entity": "demand",
    "mappings": [
        {
            "source_column": "Location Code",
            "target_field": "market_id",
            "confidence": 0.93,
            "reasoning": "Values match the MKT_* identifier pattern used in the market master.",
        },
        {
            "source_column": "Qty",
            "target_field": "quantity",
            "confidence": 0.87,
            "reasoning": "Numeric shipment quantity. Ambiguous header — 'Qty' could be "
                         "orders or units; magnitudes align with unit volumes.",
            "source_unit": "units",
            "target_unit": "units",
            "conversion_factor": 1.0,
        },
        {
            "source_column": "Wt (kgs)",
            "target_field": "weight_kg",
            "confidence": 0.95,
            "reasoning": "Header states kilograms explicitly.",
            "source_unit": "kg",
            "target_unit": "kg",
            "conversion_factor": 1.0,
        },
        {
            "source_column": "Rate",
            "target_field": "rate_per_unit",
            "confidence": 0.78,
            "reasoning": "Cost column, but unclear whether it is tax-inclusive. "
                         "Flagged for human review before being trusted.",
        },
        {
            "source_column": "Despatch Dt",
            "target_field": "period",
            "confidence": 0.91,
            "reasoning": "Dispatch date in DD-MM-YYYY; normalised to ISO period.",
            "source_unit": "DD-MM-YYYY",
            "target_unit": "ISO-8601",
            "conversion_factor": 1.0,
        },
    ],
    "unmapped_columns": ["Remarks", "Vehicle No"],
    "notes": "The 'Remarks' column contains free text that occasionally records "
             "surcharges; recommend routing it to contract review rather than "
             "discarding it.",
}

# ---------------------------------------------------------------------------
# Contract extraction — the two-vendor comparison
# ---------------------------------------------------------------------------

_CONTRACT_TRANSCORP: Dict[str, Any] = {
    "vendor_name": "TransCorp Logistics",
    "contract_id": "TC-2026-0472",
    "base_rate": 10.0,
    "rate_unit": "INR/kg",
    "surcharges": [
        {
            "surcharge_type": "FUEL",
            "rate": 2.0,
            "rate_unit": "INR/kg",
            "applies_to_location_ids": [],
            "confidence": "HIGH",
            "source_excerpt": "A fuel surcharge of Rs. 2.00 per kg shall apply to all "
                              "consignments irrespective of destination.",
        },
        {
            "surcharge_type": "NSL",
            "rate": 5.0,
            "rate_unit": "INR/kg",
            "applies_to_location_ids": ["MKT_GUWAHATI"],
            "applies_to_pin_codes": ["781001", "781005", "781006", "783301",
                                     "784001", "785001", "786001", "787001",
                                     "788001", "790001", "791001", "792001"],
            "confidence": "MEDIUM",
            "source_excerpt": "Deliveries to Non-Serviceable Locations (Annexure C, "
                              "12 pin codes in the North East region) attract an "
                              "additional handling charge of Rs. 5.00 per kg.",
        },
    ],
    "minimum_volume": 500.0,
    "minimum_volume_unit": "kg/shipment",
    "penalty_clauses": ["Rs. 500 per incident for late pickup beyond the agreed window."],
    "contract_start_date": "2026-04-01",
    "contract_end_date": "2027-03-31",
    "extraction_confidence": "MEDIUM",
    "notes": "Headline rate of Rs.10/kg understates true cost for North East "
             "destinations, where the effective rate reaches Rs.17/kg.",
}

_CONTRACT_SPEEDFREIGHT: Dict[str, Any] = {
    "vendor_name": "SpeedFreight India",
    "contract_id": "SF-2026-1180",
    "base_rate": 12.0,
    "rate_unit": "INR/kg",
    "surcharges": [],
    "minimum_volume": 200.0,
    "minimum_volume_unit": "kg/shipment",
    "penalty_clauses": [],
    "contract_start_date": "2026-04-01",
    "contract_end_date": "2027-03-31",
    "extraction_confidence": "HIGH",
    "notes": "All-inclusive flat rate with pan-India coverage and no conditional "
             "surcharges.",
}

# ---------------------------------------------------------------------------
# External signal structuring
# ---------------------------------------------------------------------------

_SIGNAL_STRUCTURE: Dict[str, Any] = {
    "bucket": "MACRO",
    "direction": "UP",
    "magnitude": "unspecified",
    "confidence": "MEDIUM",
    "affected_entities": [],
    "rationale": "Structured from the supplied signal record without a live model.",
}


#: Document-shaped market-intelligence replies, for offline runs.
#:
#: There are several, keyed by filename hint, because ONE canned answer would
#: make every document look identical and hide the behaviours most worth
#: seeing offline: an article that yields nothing, and one that is rejected
#: for stating no date. A stub that only ever demonstrates the happy path
#: teaches the wrong thing about the pipeline.
#:
#: Same mechanism the contract stubs already use — see `get()` below.
_MARKET_SIGNAL_DIESEL: Dict[str, Any] = {
    "signals": [
        {
            "title": "Bulk diesel price revised upward by 8%",
            "source_title": "Petroleum Planning and Analysis Cell",
            "source_url": None,
            "published_date": "2026-07-14",
            "effective_date": "2026-07-16",
            "bucket": "MACRO",
            "direction": "UP",
            "magnitude": "+8%",
            "affected_entities": [],
            "geography": "India",
            "confidence": "HIGH",
            "rationale": "Structured offline without a live model.",
            "states_probability": False,
        }
    ]
}

_MARKET_SIGNAL_PORT: Dict[str, Any] = {
    "signals": [
        {
            "title": "Congestion surcharge of INR 2.00 per kg on containerised cargo",
            "source_title": "Jawaharlal Nehru Port Authority",
            "source_url": None,
            "published_date": "2026-07-19",
            "effective_date": "2026-08-01",
            "bucket": "CARRIER",
            "direction": "UP",
            "magnitude": "INR 2.00 per kg",
            "affected_entities": [],
            "geography": "Nhava Sheva",
            "confidence": "HIGH",
            "rationale": "Structured offline without a live model.",
            "states_probability": False,
        },
        {
            "title": "Average vessel dwell time up from 1.8 to 3.1 days",
            "source_title": "Jawaharlal Nehru Port Authority",
            "source_url": None,
            "published_date": "2026-07-19",
            "effective_date": None,
            "bucket": "CARRIER",
            "direction": "UP",
            "magnitude": "+1.3 days dwell",
            "affected_entities": [],
            "geography": "Nhava Sheva",
            "confidence": "MEDIUM",
            "rationale": "Structured offline without a live model.",
            "states_probability": False,
        },
    ]
}

#: An article about nothing relevant. An EMPTY list is a correct answer, and
#: the pipeline must be able to say so rather than inventing a signal to have
#: something to return.
_MARKET_SIGNAL_NONE: Dict[str, Any] = {"signals": []}

#: Stated change, no stated date. Exercises the rejection path — the date is
#: not defaulted to today, because a signal whose age is assumed is worse than
#: one whose age is unknown.
_MARKET_SIGNAL_UNDATED: Dict[str, Any] = {
    "signals": [
        {
            "title": "Western corridor freight rates up around 5%",
            "source_title": "",
            "published_date": None,
            "bucket": "CARRIER",
            "direction": "UP",
            "magnitude": "+5%",
            "confidence": "LOW",
            "rationale": "Structured offline without a live model.",
            "states_probability": False,
        }
    ]
}

#: Default for any market-intelligence call with no filename hint.
_MARKET_SIGNAL_DOC = _MARKET_SIGNAL_DIESEL

_REGISTRY: Dict[str, Dict[str, Any]] = {
    "distributor_mapping": _DISTRIBUTOR_MAPPING,
    "contract_transcorp": _CONTRACT_TRANSCORP,
    "contract_speedfreight": _CONTRACT_SPEEDFREIGHT,
    # NOTE: no "market_signal" entry. The exact-key lookup below runs
    # before the filename hints, so registering it would make every
    # document return the same canned answer and hide the cases most
    # worth seeing offline — the article that yields nothing, and the one
    # rejected for stating no date. Same reason "contract" is absent.
    "signal_structure": _SIGNAL_STRUCTURE,
}


def get(stub_key: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Return the canned payload for a call site.

    Falls back to matching on filename hints in `context` so that adding a new
    sample contract does not require a new registry entry.
    """
    context = context or {}

    if stub_key == "field_clarification":
        proposed = context.get("proposed_value") or "__unresolved__"
        return {
            "recommendation": proposed,
            "reason": "The suggestion uses the mapped schema and visible column evidence.",
            "question": "Can you confirm its meaning?",
            "missing_information": [],
        }

    if stub_key in _REGISTRY:
        return dict(_REGISTRY[stub_key])

    hint = str(context.get("filename", "")).lower()
    if "transcorp" in hint or "vendor_x" in hint:
        return dict(_CONTRACT_TRANSCORP)
    if "speedfreight" in hint or "vendor_y" in hint:
        return dict(_CONTRACT_SPEEDFREIGHT)

    if stub_key.startswith("contract"):
        return dict(_CONTRACT_SPEEDFREIGHT)
    if stub_key.startswith("market_signal"):
        if "commentary" in hint or "analyst" in hint:
            return dict(_MARKET_SIGNAL_NONE)
        if "undated" in hint or "snippet" in hint:
            return dict(_MARKET_SIGNAL_UNDATED)
        if "port" in hint or "jnpa" in hint or "congestion" in hint:
            return dict(_MARKET_SIGNAL_PORT)
        return dict(_MARKET_SIGNAL_DOC)
    if stub_key.startswith("signal"):
        return dict(_SIGNAL_STRUCTURE)

    return {}
