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
    FacilityCommitment,
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
  "facility_commitments": [
    {{
      "facility_id": "an ID from the known list below, if the clause names that site recognisably; else empty",
      "facility_label": "what the document calls the site",
      "is_active": true | false | null,
      "allows_early_closure": true | false | null,
      "early_exit_penalty": number or null,
      "penalty_currency": "string or empty",
      "notice_period_days": number or null,
      "term_start_date": "YYYY-MM-DD or null",
      "term_end_date": "YYYY-MM-DD or null",
      "confidence": "HIGH | MEDIUM | LOW",
      "source_excerpt": "the exact clause text this came from"
    }}
  ],
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

FACILITY COMMITMENTS — read these rules carefully, because they decide whether a site may be CLOSED, not merely what it costs:
- Return facility_commitments ONLY for clauses about keeping, leasing or committing to a SITE (a lease term, a take-or-pay, a minimum-term or lock-in clause). A clause about freight rates is a surcharge, not a commitment.
- allows_early_closure must be false ONLY where the document actually prohibits or forbids early exit. If it is silent, use null. Do not infer a lock-in from the existence of an end date — a term that ends is not a term that cannot be exited.
- is_active is about the MODELLED period. Use null where the document gives dates but no basis for deciding whether the term is currently in force.
- early_exit_penalty is a stated figure only. Never derive it from a monthly rent times a remaining term.
- Return an empty list when the document contains no site commitment. An empty list is the correct and common answer for a rate card.

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


def extract_contract_from_pdf(
    client: LLMClient,
    pdf_bytes: bytes,
    *,
    source_key: str,
    filename: str,
    known_locations: Optional[List[str]] = None,
    stub_key: str = "contract",
) -> Tuple[ContractRule, str]:
    """
    Extract a contract by giving the model the PDF ITSELF.

    The escalation path, used only when pypdf either returned nothing (a
    scan) or returned text that failed the quality checks. It is more
    expensive than sending extracted text and provider support for document
    input is uneven, so it is never the first attempt — see
    LLMClient.extract_json_from_pdf for the support caveats.

    The prompt is deliberately the SAME one used for the text path, minus the
    inlined text. Contract extraction rules ("never estimate a missing rate",
    "a blanket surcharge has empty applies_to lists") must not quietly differ
    between the two routes, or the same document could yield different
    structure depending on how it happened to be read.
    """
    prompt = PROMPT_TEMPLATE.format(
        known_locations=", ".join(known_locations or []) or "(none supplied)",
        contract_text="(the document is attached — read it directly)",
    )

    response = client.extract_json_from_pdf(
        task=f"contract extraction from document ({filename})",
        prompt=prompt,
        pdf_bytes=pdf_bytes,
        filename=filename,
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
        facility_commitments=_to_commitments(data.get("facility_commitments")),
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


def _to_commitments(raw_list: Any) -> List[FacilityCommitment]:
    """
    Parse the site-commitment clauses.

    Every field stays None unless the document said it. In particular
    `allows_early_closure` is only False when the model reports False — a
    missing value must not become a prohibition, because a prohibition pins a
    facility open in the MILP and would block a closure the client is free to
    make.
    """
    out: List[FacilityCommitment] = []
    for raw in raw_list or []:
        if not isinstance(raw, dict):
            continue
        try:
            conf = ExtractionConfidence(str(raw.get("confidence", "MEDIUM")).upper())
        except ValueError:
            conf = ExtractionConfidence.MEDIUM

        commitment = FacilityCommitment(
            facility_id=str(raw.get("facility_id") or "").strip(),
            facility_label=str(raw.get("facility_label") or "").strip(),
            is_active=_tri_state(raw.get("is_active")),
            allows_early_closure=_tri_state(raw.get("allows_early_closure")),
            early_exit_penalty=_positive_or_none(raw.get("early_exit_penalty")),
            penalty_currency=str(raw.get("penalty_currency") or "").strip(),
            notice_period_days=_int_or_none(raw.get("notice_period_days")),
            term_start_date=raw.get("term_start_date") or None,
            term_end_date=raw.get("term_end_date") or None,
            confidence=conf,
            source_excerpt=str(raw.get("source_excerpt") or ""),
        )
        # A commitment naming nothing and stating nothing is not evidence.
        if commitment.facility_id or commitment.facility_label:
            out.append(commitment)
    return out


def _tri_state(value: Any) -> Optional[bool]:
    """
    True / False / unstated, keeping the third case distinct.

    Coercing an unstated term to False would silently assert that a site cannot
    be closed. `bool(None)` is False and that is exactly the wrong answer here.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes"}:
            return True
        if lowered in {"false", "no"}:
            return False
    return None


def _positive_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
