"""
NetGravity — Contract Extraction Prompt & Parser
=================================================
Reads a freight contract / rate card and returns structured cost rules.

THE BUSINESS CASE
-----------------
Vendor A quotes Rs.10/kg. Vendor B quotes Rs.12/kg. A looks cheaper — until a
clause buried in an annexure adds Rs.5/kg for a list of "non-serviceable
locations", which happen to be exactly the remote destinations that matter.
Nobody reads every clause of every contract before a network decision. This
is the step that surfaces the number that would otherwise stay invisible
until it appeared on an invoice.

The model extracts; it does not compute. Effective rates are derived
arithmetically in schemas/contract.py from the extracted values.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from netgravity.ingestion.ai.client import LLMClient
from netgravity.ingestion.schemas.contract import (
    ContractRule,
    ExtractionConfidence,
    SurchargeRule,
    SurchargeType,
)

PROMPT_TEMPLATE = """You are extracting structured cost rules from a freight \
contract or rate card so that a supply-chain optimiser can compute the TRUE \
cost of shipping, not just the headline rate.

Return ONLY a JSON object with this exact shape:

{{
  "vendor_name": "string",
  "contract_id": "string",
  "base_rate": number,
  "rate_unit": "string, e.g. INR/kg",
  "surcharges": [
    {{
      "surcharge_type": "NSL | FUEL | PEAK_SEASON | HANDLING | OTHER",
      "rate": number,
      "rate_unit": "string",
      "applies_to_location_ids": ["only if named location IDs are given"],
      "applies_to_pin_codes": ["only if pin codes are listed"],
      "confidence": "HIGH | MEDIUM | LOW",
      "source_excerpt": "the exact clause text this came from"
    }}
  ],
  "minimum_volume": number or null,
  "minimum_volume_unit": "string or null",
  "penalty_clauses": ["string"],
  "contract_start_date": "YYYY-MM-DD or null",
  "contract_end_date": "YYYY-MM-DD or null",
  "extraction_confidence": "HIGH | MEDIUM | LOW",
  "notes": "anything a human should check"
}}

RULES:
- Extract ONLY what the document states. Never estimate a missing rate.
- A surcharge applying to every shipment must have EMPTY applies_to lists.
- A surcharge applying to specific places MUST list those places — this
  distinction determines whether the headline rate is misleading.
- Always populate source_excerpt with the clause text, for auditability.
- If a value is absent, use null. Do not guess.

Known location IDs in this network (map named places onto these where \
possible): {known_locations}

CONTRACT TEXT:
---
{contract_text}
---
"""


def extract_contract(
    client: LLMClient,
    contract_text: str,
    *,
    source_key: str,
    filename: str,
    known_locations: Optional[List[str]] = None,
    stub_key: str = "contract",
) -> Tuple[ContractRule, str]:
    """Extract one contract. Returns the rule plus a provenance note."""
    prompt = PROMPT_TEMPLATE.format(
        known_locations=", ".join(known_locations or []) or "(none supplied)",
        contract_text=contract_text[:14000],
    )

    response = client.extract_json(
        task=f"contract extraction ({filename})",
        prompt=prompt,
        stub_key=stub_key,
        stub_context={"filename": filename},
        max_tokens=2500,
    )

    rule = _to_contract_rule(response.data, source_key=source_key,
                             extracted_by=response.provenance)
    note = response.notes
    if response.data.get("notes"):
        note = f"{note} — {response.data['notes']}"
    return rule, note


def _to_contract_rule(data: Dict[str, Any], *, source_key: str,
                      extracted_by: str) -> ContractRule:
    surcharges: List[SurchargeRule] = []

    for raw in data.get("surcharges") or []:
        try:
            stype = SurchargeType(str(raw.get("surcharge_type", "OTHER")).upper())
        except ValueError:
            stype = SurchargeType.OTHER
        try:
            conf = ExtractionConfidence(str(raw.get("confidence", "MEDIUM")).upper())
        except ValueError:
            conf = ExtractionConfidence.MEDIUM

        surcharges.append(SurchargeRule(
            surcharge_type=stype,
            rate=float(raw.get("rate") or 0.0),
            rate_unit=str(raw.get("rate_unit") or "INR/kg"),
            applies_to_location_ids=list(raw.get("applies_to_location_ids") or []),
            applies_to_pin_codes=[str(p) for p in (raw.get("applies_to_pin_codes") or [])],
            confidence=conf,
            source_excerpt=str(raw.get("source_excerpt") or ""),
        ))

    try:
        overall = ExtractionConfidence(str(data.get("extraction_confidence", "MEDIUM")).upper())
    except ValueError:
        overall = ExtractionConfidence.MEDIUM

    return ContractRule(
        contract_id=str(data.get("contract_id") or source_key),
        vendor_name=str(data.get("vendor_name") or "Unknown vendor"),
        base_rate=float(data.get("base_rate") or 0.0),
        rate_unit=str(data.get("rate_unit") or "INR/kg"),
        surcharges=surcharges,
        minimum_volume=data.get("minimum_volume"),
        minimum_volume_unit=data.get("minimum_volume_unit"),
        penalty_clauses=[str(p) for p in (data.get("penalty_clauses") or [])],
        contract_start_date=data.get("contract_start_date"),
        contract_end_date=data.get("contract_end_date"),
        source_file_key=source_key,
        extracted_by=extracted_by,
        extraction_confidence=overall,
    )
