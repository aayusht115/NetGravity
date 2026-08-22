"""
NetGravity — Distributor Column Mapping Prompt & Parser
========================================================
Every distributor sends a differently-shaped spreadsheet: different column
names, different order, different units. Today somebody re-types or re-maps
each one by hand before any analysis can start.

This asks the model to infer what each column means and map it onto our
canonical fields — flagging anything it is not confident about rather than
guessing silently. The confirmed mapping is then CACHED per distributor, so
the second file from the same source costs no model call and needs no review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from netgravity.ingestion.ai.client import LLMClient
from netgravity.ingestion.schemas.mapping import ColumnMapping, DistributorMapping

CANONICAL_FIELDS = {
    "market_id": "Demand zone / market identifier (e.g. MKT_DELHI)",
    "facility_id": "Facility identifier (plant or DC, e.g. DC_MUMBAI)",
    "product_id": "Product / SKU identifier",
    "quantity": "Volume in units per period",
    "weight_kg": "Weight in kilograms",
    "rate_per_unit": "Transport cost per unit",
    "period": "Date or period the record covers",
    "distance_km": "Distance in kilometres",
    "lead_time_days": "Transit time in days",
}

PROMPT_TEMPLATE = """You are mapping an unfamiliar distributor spreadsheet onto \
a fixed canonical schema for a supply-chain optimiser.

CANONICAL FIELDS AVAILABLE:
{canonical_fields}

KNOWN IDENTIFIERS IN THIS NETWORK (use to recognise identifier columns):
{known_ids}

THE FILE'S COLUMNS AND SAMPLE VALUES:
{sample}

Return ONLY a JSON object with this exact shape:

{{
  "target_entity": "demand | facilities | lanes",
  "mappings": [
    {{
      "source_column": "exact column header from the file",
      "target_field": "one of the canonical fields above",
      "confidence": 0.0 to 1.0,
      "reasoning": "why you believe this mapping is correct",
      "source_unit": "unit found in the source, or null",
      "target_unit": "canonical unit, or null",
      "conversion_factor": number to multiply source values by (1.0 if none)
    }}
  ],
  "unmapped_columns": ["columns you could not confidently map"],
  "notes": "anything a human should check before trusting this"
}}

RULES:
- Do NOT force a mapping. A column you are unsure about belongs in
  unmapped_columns, not in mappings with low confidence.
- Watch for unit mismatches: kg vs units vs cartons is the most common and
  most damaging error in this kind of file. Set conversion_factor accordingly
  and explain it in reasoning.
- Confidence below 0.90 will be surfaced to a human for review, so use the
  full range honestly rather than defaulting high.
"""


def propose_mapping(
    client: LLMClient,
    *,
    columns: List[str],
    sample_rows: List[Dict[str, Any]],
    distributor_id: str,
    distributor_name: str = "",
    known_ids: Optional[List[str]] = None,
) -> Tuple[DistributorMapping, str]:
    """Ask the model to map an unfamiliar file's columns. Returns mapping + note."""
    sample_text = _render_sample(columns, sample_rows)
    prompt = PROMPT_TEMPLATE.format(
        canonical_fields="\n".join(f"  - {k}: {v}" for k, v in CANONICAL_FIELDS.items()),
        known_ids=", ".join((known_ids or [])[:40]) or "(none supplied)",
        sample=sample_text,
    )

    response = client.extract_json(
        task=f"column mapping ({distributor_id})",
        prompt=prompt,
        stub_key="distributor_mapping",
        stub_context={"distributor_id": distributor_id},
        max_tokens=2000,
    )

    mapping = _to_mapping(response.data, distributor_id=distributor_id,
                          distributor_name=distributor_name,
                          proposed_by=response.provenance)

    note = response.notes
    if response.data.get("notes"):
        note = f"{note} — {response.data['notes']}"
    return mapping, note


def _render_sample(columns: List[str], rows: List[Dict[str, Any]],
                   limit: int = 5) -> str:
    lines = []
    for col in columns:
        values = [str(r.get(col, "")) for r in rows[:limit]]
        values = [v for v in values if v != ""]
        preview = " | ".join(values[:limit]) if values else "(all blank)"
        lines.append(f'  "{col}"  ->  {preview}')
    return "\n".join(lines)


def _to_mapping(data: Dict[str, Any], *, distributor_id: str,
                distributor_name: str, proposed_by: str) -> DistributorMapping:
    mappings: List[ColumnMapping] = []

    for raw in data.get("mappings") or []:
        target = str(raw.get("target_field") or "").strip()
        if target not in CANONICAL_FIELDS:
            continue   # never accept a field the schema does not define
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        mappings.append(ColumnMapping(
            source_column=str(raw.get("source_column") or ""),
            target_field=target,
            confidence=max(0.0, min(1.0, confidence)),
            reasoning=str(raw.get("reasoning") or ""),
            source_unit=raw.get("source_unit"),
            target_unit=raw.get("target_unit"),
            conversion_factor=float(raw.get("conversion_factor") or 1.0),
        ))

    return DistributorMapping(
        distributor_id=distributor_id,
        distributor_name=distributor_name or distributor_id,
        target_entity=str(data.get("target_entity") or "demand"),
        mappings=mappings,
        unmapped_columns=[str(c) for c in (data.get("unmapped_columns") or [])],
        confirmed_by_human=False,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        proposed_by=proposed_by,
    )
