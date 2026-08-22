"""
NetGravity — Guardrail Bucket Policy Loader
============================================
Loads the bucket taxonomy and thresholds from thresholds.yaml.

The policy lives in YAML, not Python, because it is a BUSINESS decision the
team owns — not an engineering one. When the confirmed thresholds arrive,
editing the YAML is the entire change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from netgravity.ingestion.schemas.signal import ScenarioUse, SignalBucket

POLICY_PATH = Path(__file__).parent / "thresholds.yaml"


@dataclass
class BucketPolicy:
    bucket: SignalBucket
    base_relevance: float = 0.5
    threshold: float = 0.6
    scenario_use: ScenarioUse = ScenarioUse.LOGGED_ONLY
    rationale: str = ""
    triggers: List[str] = field(default_factory=list)
    excluded_by_default: bool = False
    materiality_pct: Optional[float] = None
    expiry_days: Optional[int] = None


@dataclass
class GuardrailPolicy:
    version: str = "unknown"
    owner: str = ""
    entity_match_bonus: float = 0.25
    materiality_bonus: float = 0.20
    confidence_bonus: Dict[str, float] = field(
        default_factory=lambda: {"HIGH": 0.15, "MEDIUM": 0.05, "LOW": -0.10}
    )
    buckets: Dict[SignalBucket, BucketPolicy] = field(default_factory=dict)

    def for_bucket(self, bucket: SignalBucket) -> BucketPolicy:
        return self.buckets.get(bucket) or BucketPolicy(bucket=bucket)

    def classify(self, text: str) -> SignalBucket:
        """
        Assign a bucket by trigger-keyword match.

        Deliberately deterministic rather than model-driven: the guardrail is
        the thing protecting the optimizer, so its decisions must be
        reproducible and reviewable, not probabilistic.
        """
        lowered = text.lower()
        best, best_hits = SignalBucket.UNKNOWN, 0
        for bucket, policy in self.buckets.items():
            hits = sum(1 for t in policy.triggers if t.lower() in lowered)
            if hits > best_hits:
                best, best_hits = bucket, hits
        return best


_DEFAULT_TRIGGERS = {
    SignalBucket.CARRIER: ["carrier", "freight", "trucking", "logistics provider", "3pl"],
    SignalBucket.SUPPLIER: ["supplier", "vendor", "plant shutdown", "raw material"],
    SignalBucket.CUSTOMER: ["expansion", "new stores", "customer", "retailer"],
    SignalBucket.MACRO: ["fuel", "diesel", "gdp", "duty", "tariff", "policy", "expressway"],
    SignalBucket.WEATHER: ["cyclone", "flood", "monsoon", "disruption"],
    SignalBucket.COMPETITOR: ["competitor", "rival", "market share"],
}


def load_policy(path: Optional[Path] = None) -> GuardrailPolicy:
    """
    Load the guardrail policy.

    PyYAML is optional: if it is unavailable the built-in defaults below are
    used, so the guardrail never becomes a hard dependency failure.
    """
    path = path or POLICY_PATH

    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return _fallback_policy()

    scoring = raw.get("scoring") or {}
    policy = GuardrailPolicy(
        version=str(raw.get("version", "unknown")),
        owner=str(raw.get("owner", "")),
        entity_match_bonus=float(scoring.get("entity_match_bonus", 0.25)),
        materiality_bonus=float(scoring.get("materiality_bonus", 0.20)),
        confidence_bonus={k: float(v) for k, v in
                          (scoring.get("confidence_bonus") or {}).items()},
    )

    for name, cfg in (raw.get("buckets") or {}).items():
        try:
            bucket = SignalBucket(str(name).upper())
        except ValueError:
            continue
        try:
            use = ScenarioUse(str(cfg.get("scenario_use", "LOGGED_ONLY")).upper())
        except ValueError:
            use = ScenarioUse.LOGGED_ONLY

        policy.buckets[bucket] = BucketPolicy(
            bucket=bucket,
            base_relevance=float(cfg.get("base_relevance", 0.5)),
            threshold=float(cfg.get("threshold", 0.6)),
            scenario_use=use,
            rationale=str(cfg.get("rationale", "")).strip(),
            triggers=[str(t) for t in (cfg.get("triggers") or [])],
            excluded_by_default=bool(cfg.get("excluded_by_default", False)),
            materiality_pct=cfg.get("materiality_pct"),
            expiry_days=cfg.get("expiry_days"),
        )

    if not policy.buckets:
        return _fallback_policy()
    return policy


def _fallback_policy() -> GuardrailPolicy:
    """Built-in defaults mirroring thresholds.yaml, used when PyYAML is absent."""
    policy = GuardrailPolicy(version="0.1-builtin-fallback", owner="TBC")
    spec = {
        SignalBucket.CARRIER: (0.70, 0.60, ScenarioUse.FORECAST_ENRICHMENT, False),
        SignalBucket.SUPPLIER: (0.70, 0.60, ScenarioUse.FORECAST_ENRICHMENT, False),
        SignalBucket.CUSTOMER: (0.70, 0.60, ScenarioUse.FORECAST_ENRICHMENT, False),
        SignalBucket.MACRO: (0.45, 0.60, ScenarioUse.SEPARATE_WHATIF, False),
        SignalBucket.WEATHER: (0.65, 0.60, ScenarioUse.SEPARATE_WHATIF, False),
        SignalBucket.COMPETITOR: (0.10, 0.95, ScenarioUse.LOGGED_ONLY, True),
        SignalBucket.UNKNOWN: (0.20, 0.80, ScenarioUse.LOGGED_ONLY, False),
    }
    for bucket, (base, thresh, use, excluded) in spec.items():
        policy.buckets[bucket] = BucketPolicy(
            bucket=bucket, base_relevance=base, threshold=thresh,
            scenario_use=use, excluded_by_default=excluded,
            triggers=_DEFAULT_TRIGGERS.get(bucket, []),
        )
    return policy
