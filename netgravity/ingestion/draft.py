"""UI-oriented provisional view that is never accepted by the optimiser."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from netgravity.ingestion.sources import discover


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return value


def build_draft(source: Path, result: Any, preview_limit: int = 50) -> Dict[str, Any]:
    """Build a compact first draft from proposed mappings, including pending ones."""
    tabular = getattr(result, "tabular", None)
    mappings = list(getattr(tabular, "mappings", []) or [])
    by_key = {mapping.record_key: mapping for mapping in mappings}
    entities: Dict[str, List[Dict[str, Any]]] = {}
    totals: Dict[str, int] = {}

    for data_source in discover(Path(source)):
        for record_set in data_source.record_sets():
            mapping = by_key.get(record_set.key)
            if mapping is None:
                continue
            proposed = {
                decision.source_column: decision
                for decision in mapping.decisions if decision.target_field
            }
            totals[mapping.content_type.value] = (
                totals.get(mapping.content_type.value, 0) + record_set.row_count)
            bucket = entities.setdefault(mapping.content_type.value, [])
            room = max(0, preview_limit - len(bucket))
            for row in record_set.rows[:room]:
                values = {
                    decision.target_field: row.get(source_column)
                    for source_column, decision in proposed.items()
                }
                confidence = {
                    decision.target_field: {
                        "confidence": round(decision.confidence, 3),
                        "status": ("awaiting_confirmation"
                                   if decision.needs_review else "settled"),
                        "source_column": source_column,
                    }
                    for source_column, decision in proposed.items()
                }
                bucket.append({"values": values, "field_status": confidence})

    report = result.report
    request = result.review_request
    issues = [_dump(issue) for issue in report.all_issues]
    unfamiliar = [
        item.as_dict() for item in request.items
        if item.kind == "unfamiliar_field"
    ]
    field_inventory = [
        {
            "record_key": mapping.record_key,
            "content_type": mapping.content_type.value,
            "source_column": decision.source_column,
            "disposition": decision.disposition.value,
            "definition": decision.user_definition,
            "sample_values": list(decision.sample_values),
            "profile": decision.profile.as_dict(),
        }
        for mapping in mappings
        for decision in mapping.decisions
        if decision.disposition.value != "CANONICAL"
    ]
    confirmed_network = None
    if result.network is not None:
        raw_network = result.network.model_dump(mode="json")
        confirmed_network = {
            "network_id": raw_network.get("network_id"),
            "data_version": raw_network.get("data_version"),
            "facilities": list(raw_network.get("facilities") or [])[:preview_limit],
            "products": list(raw_network.get("products") or [])[:preview_limit],
            "demands": list(raw_network.get("demands") or [])[:preview_limit],
            "lanes": list(raw_network.get("lanes") or [])[:preview_limit],
            "preview_limit_per_entity": preview_limit,
            "truncated": any(
                len(raw_network.get(name) or []) > preview_limit
                for name in ("facilities", "products", "demands", "lanes")
            ),
        }
    return {
        "label": "AI-generated provisional draft",
        "safe_for_optimization": False,
        "confirmed_network": confirmed_network,
        "provisional_entities": entities,
        "entity_row_counts": totals,
        "mappings": [mapping.as_dict() for mapping in mappings],
        "contracts": [_dump(contract) for contract in result.contracts],
        "data_quality": {
            "issues": issues,
            "error_count": len(report.errors),
            "warning_count": len(report.warnings),
            "engine_validation_passed": report.engine_validation_passed,
            "engine_validation_issues": list(report.engine_validation_issues),
        },
        "unfamiliar_fields": unfamiliar,
        "noncanonical_field_inventory": field_inventory,
        "review": request.as_dict(),
    }
