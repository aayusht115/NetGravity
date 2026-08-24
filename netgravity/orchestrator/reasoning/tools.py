"""Pure read-only tools exposed to the OpenAI Reasoning Agent."""

from __future__ import annotations

import json

from netgravity.orchestrator.schemas.reasoning import ReasoningEvidencePack


def evidence_manifest(pack: ReasoningEvidencePack, limit: int = 120) -> str:
    """Compact catalogue for the initial turn; raw lookup stays tool-bound."""
    rows = [
        {
            "ref": metric.ref,
            "label": metric.label,
            "display": metric.display_value,
            "source": metric.source,
            "entity_id": metric.entity_id,
        }
        for metric in list(pack.metrics.values())[:limit]
    ]
    return json.dumps(rows, separators=(",", ":"), default=str)


def lookup_evidence(pack: ReasoningEvidencePack, metric_ref: str) -> str:
    """Return one cited metric; unknown references fail closed."""
    metric = pack.metrics.get(metric_ref)
    if metric is None:
        return json.dumps({"status": "NOT_FOUND", "metric_ref": metric_ref})
    return metric.model_dump_json()


def list_missing_evidence(pack: ReasoningEvidencePack) -> str:
    """Return explicitly unavailable analyses, never inferred substitutes."""
    return json.dumps(pack.unavailable, separators=(",", ":"), default=str)
