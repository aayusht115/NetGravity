"""
Orchestrator — Deterministic numeric-claim grounding.

The reasoning agent may write the explanation. It may **never** become the
source of truth for a number.

```
Deterministic Results → Canonical Evidence → Reasoning Agent → Claims
                                                                 ↓
                                                      Numeric Claim Validator
                                                                 ↓
                                                        Validated Response
```

Every numeric claim in generated narrative is checked against authoritative
values. Three verdicts:

    GROUNDED      matches an authoritative value within tolerance
    CONTRADICTED  a value of that kind exists, and the claim disagrees
    UNSUPPORTED   no authoritative value of that kind exists at all

Both failure modes matter, and they are different. CONTRADICTED is the model
misreporting a real figure ("cost rose 50%" when it rose 14.3%). UNSUPPORTED is
the model inventing a figure from nothing ("cost rose 12%" when no cost was
computed). Neither may be returned as fact.

Authoritative sources
─────────────────────
    MILP            cost, flow, capacity, feasibility
    KPI engine      SLA, service level, utilisation, demand fulfilment
    REI engine      REI
    Risk engine     P, RF
    Scenario engine scenario overrides and metadata
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tolerance
# ---------------------------------------------------------------------------

#: Relative tolerance. 0.5% absorbs sensible rounding (14.3 vs 14.30, 14.2997)
#: without absorbing a genuine error (14.3 vs 15.8 is ~10% off and fails).
RELATIVE_TOLERANCE = 0.005
#: Absolute floor so near-zero values do not fail on relative comparison alone.
ABSOLUTE_TOLERANCE = 0.01
#: A claim quoted to fewer decimals than the authority is compared at the
#: claim's precision — "14%" against 14.3 is a legitimate rounding, "13%" is not.
ALLOW_ROUNDING_TO_CLAIM_PRECISION = True


class ClaimVerdict(str, Enum):
    GROUNDED     = "GROUNDED"
    CONTRADICTED = "CONTRADICTED"
    UNSUPPORTED  = "UNSUPPORTED"
    #: Numbers that are not factual claims about results (counts, ordinals,
    #: years). Policed loosely on purpose — see `_is_policeable`.
    IGNORED      = "IGNORED"


class ClaimKind(str, Enum):
    """What kind of quantity a claim asserts. Drives which facts it may match."""
    PERCENTAGE = "PERCENTAGE"
    CURRENCY   = "CURRENCY"
    UNITS      = "UNITS"
    RATIO      = "RATIO"      # bare decimal in [0,1] — REI, RF, fill rate
    COUNT      = "COUNT"
    UNKNOWN    = "UNKNOWN"


@dataclass(frozen=True)
class AuthoritativeFact:
    """One deterministic value the narrative is allowed to cite."""
    key: str
    value: float
    kind: ClaimKind
    source: str          # "optimization_result" | "kpi_engine" | "rei_engine" | ...


@dataclass
class NumericClaim:
    """A number asserted in generated narrative."""
    raw_text: str
    value: float
    kind: ClaimKind
    verdict: ClaimVerdict = ClaimVerdict.UNSUPPORTED
    matched_fact: Optional[str] = None
    matched_value: Optional[float] = None
    source: Optional[str] = None
    detail: str = ""
    # Provenance, attached when the claim is accepted.
    provenance: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.raw_text,
            "value": self.value,
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "matched_fact": self.matched_fact,
            "authoritative_value": self.matched_value,
            "source": self.source,
            "detail": self.detail,
            "provenance": self.provenance,
        }


@dataclass
class GroundingReport:
    """Outcome of grounding every claim in one narrative."""
    claims: List[NumericClaim] = field(default_factory=list)
    status: str = "GROUNDED"     # GROUNDED | GROUNDING_FAILED | NO_CLAIMS

    @property
    def contradicted(self) -> List[NumericClaim]:
        return [c for c in self.claims if c.verdict == ClaimVerdict.CONTRADICTED]

    @property
    def unsupported(self) -> List[NumericClaim]:
        return [c for c in self.claims if c.verdict == ClaimVerdict.UNSUPPORTED]

    @property
    def grounded(self) -> List[NumericClaim]:
        return [c for c in self.claims if c.verdict == ClaimVerdict.GROUNDED]

    @property
    def failed(self) -> bool:
        return bool(self.contradicted or self.unsupported)

    def warnings(self) -> List[str]:
        out: List[str] = []
        for c in self.contradicted:
            out.append(
                f"CONTRADICTED numeric claim '{c.raw_text}': authoritative "
                f"{c.matched_fact} = {c.matched_value} (source: {c.source})."
            )
        for c in self.unsupported:
            out.append(
                f"UNSUPPORTED numeric claim '{c.raw_text}': no authoritative "
                f"{c.kind.value.lower()} value exists in the deterministic results."
            )
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "n_claims": len(self.claims),
            "n_grounded": len(self.grounded),
            "n_contradicted": len(self.contradicted),
            "n_unsupported": len(self.unsupported),
            "claims": [c.to_dict() for c in self.claims],
        }


# ---------------------------------------------------------------------------
# Building the authoritative fact set
# ---------------------------------------------------------------------------

#: Deterministic payload key → (fact kind, authoritative source label).
#: Only fields listed here may be cited numerically.
_FACT_SPEC: Dict[str, Tuple[ClaimKind, str]] = {
    # MILP / optimization
    "business_network_cost":     (ClaimKind.CURRENCY, "optimization_result"),
    "solver_objective":          (ClaimKind.CURRENCY, "optimization_result"),
    "business_cost_delta":       (ClaimKind.CURRENCY, "optimization_result"),
    "business_cost_delta_pct":   (ClaimKind.PERCENTAGE, "optimization_result"),
    "baseline_business_cost":    (ClaimKind.CURRENCY, "optimization_result"),
    "facility_cost":             (ClaimKind.CURRENCY, "optimization_result"),
    "transport_cost":            (ClaimKind.CURRENCY, "optimization_result"),
    "handling_cost":             (ClaimKind.CURRENCY, "optimization_result"),
    "inventory_cost":            (ClaimKind.CURRENCY, "optimization_result"),
    "closure_cost":              (ClaimKind.CURRENCY, "optimization_result"),
    "opening_cost":              (ClaimKind.CURRENCY, "optimization_result"),
    "carbon_cost":               (ClaimKind.CURRENCY, "optimization_result"),
    "shortage_penalty_cost":     (ClaimKind.CURRENCY, "optimization_result"),
    # KPI engine
    "total_demand":              (ClaimKind.UNITS, "kpi_engine"),
    "served_demand":             (ClaimKind.UNITS, "kpi_engine"),
    "unserved_demand":           (ClaimKind.UNITS, "kpi_engine"),
    "rerouted_volume":           (ClaimKind.UNITS, "kpi_engine"),
    "total_carbon_kg":           (ClaimKind.UNITS, "kpi_engine"),
    "demand_fill_rate":          (ClaimKind.RATIO, "kpi_engine"),
    "unserved_demand_rate":      (ClaimKind.RATIO, "kpi_engine"),
    "pct_demand_in_sla":         (ClaimKind.PERCENTAGE, "kpi_engine"),
    "avg_utilization_pct":       (ClaimKind.PERCENTAGE, "kpi_engine"),
    "max_utilization_pct":       (ClaimKind.PERCENTAGE, "kpi_engine"),
    "utilization_pct":           (ClaimKind.PERCENTAGE, "kpi_engine"),
    # REI engine
    "max_rei":                   (ClaimKind.RATIO, "rei_engine"),
    "rei":                       (ClaimKind.RATIO, "rei_engine"),
    "max_performance_impact":    (ClaimKind.CURRENCY, "rei_engine"),
    "performance_impact":        (ClaimKind.CURRENCY, "rei_engine"),
    "cost_impact_pct":           (ClaimKind.PERCENTAGE, "rei_engine"),
    "service_loss":              (ClaimKind.RATIO, "rei_engine"),
    # Risk engine
    "risk_factor":               (ClaimKind.RATIO, "risk_engine"),
    "max_risk_factor":           (ClaimKind.RATIO, "risk_engine"),
    "likelihood":                (ClaimKind.RATIO, "risk_engine"),
    "event_probability":         (ClaimKind.RATIO, "risk_engine"),
    "confidence":                (ClaimKind.RATIO, "risk_engine"),
    # Counts
    "n_open_facilities":         (ClaimKind.COUNT, "optimization_result"),
    "n_facilities_open":         (ClaimKind.COUNT, "optimization_result"),
    "n_facilities_closed":       (ClaimKind.COUNT, "optimization_result"),
    "n_facilities_assessed":     (ClaimKind.COUNT, "rei_engine"),
    # Facility and lane-level Digital Twin values, copied from MILP/KPI output.
    "throughput_units":          (ClaimKind.UNITS, "optimization_result"),
    "capacity_units":            (ClaimKind.UNITS, "optimization_result"),
    "flow_units":                (ClaimKind.UNITS, "optimization_result"),
    "baseline_units":            (ClaimKind.UNITS, "digital_twin_comparison"),
    "comparison_units":          (ClaimKind.UNITS, "digital_twin_comparison"),
    "units_delta":               (ClaimKind.UNITS, "digital_twin_comparison"),
    "distance_km":               (ClaimKind.UNITS, "optimization_result"),
    "carbon_kg":                 (ClaimKind.UNITS, "optimization_result"),
    "share_of_total_units":      (ClaimKind.RATIO, "optimization_result"),
    "closure_cost_charged":      (ClaimKind.CURRENCY, "optimization_result"),
}


def build_authoritative_facts(payload: Dict[str, Any]) -> Dict[str, AuthoritativeFact]:
    """
    Flatten deterministic results into the set of citable values.

    Walks the payload recursively so nested blocks (cost components, REI rows)
    are covered. Only keys in `_FACT_SPEC` become facts — anything else is not
    a quantity the narrative may assert.
    """
    facts: Dict[str, AuthoritativeFact] = {}

    def visit(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                spec = _FACT_SPEC.get(key)
                # Digital Twin KPI comparisons use generic value keys and name
                # the actual metric beside them. Inherit that metric's kind so
                # a cost delta cannot accidentally ground against a unit delta.
                if spec is None and key in {
                    "baseline_value", "comparison_value", "abs_delta"
                }:
                    metric_name = str(node.get("metric", ""))
                    metric_spec = _FACT_SPEC.get(metric_name)
                    if metric_spec is not None:
                        spec = (metric_spec[0], "digital_twin_comparison")
                elif spec is None and key == "pct_delta":
                    spec = (ClaimKind.PERCENTAGE, "digital_twin_comparison")
                if spec is not None and isinstance(value, (int, float)) and not isinstance(value, bool):
                    kind, source = spec
                    fact_key = f"{path}.{key}" if path else key
                    facts[fact_key] = AuthoritativeFact(
                        key=fact_key, value=float(value), kind=kind, source=source,
                    )
                visit(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for idx, item in enumerate(node[:50]):   # bound the walk
                visit(item, f"{path}[{idx}]")

    visit(payload)
    return facts


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS = "₹$€£¥"

#: One number pattern for everything. Anchored so a grouped number such as
#: "1,000.00" is captured whole — an earlier version matched sub-spans and
#: turned 1,000.00 into 0.00, which made the deterministic template fail its own
#: grounding check.
_NUMBER_PATTERN = re.compile(
    r"""
    (?<![\w.])                      # not mid-identifier / mid-number
    (?P<sign>[-+])?
    (?P<number>
        \d{1,3}(?:,\d{3})+(?:\.\d+)?   # 1,000  /  12,037.88
      | \d+\.\d+                        # 14.3
      | \.\d+                           # .94
      | \d+                             # 12
    )
    (?![\d,]*\d\s*(?:st|nd|rd|th)\b)    # ignore ordinals
    """,
    re.VERBOSE,
)

#: Scale words that multiply the preceding number.
_SCALE_PATTERN = re.compile(
    r"^\s*(crore|lakh|million|billion|bn|mn|k)\b", re.IGNORECASE,
)
#: Unit markers that follow a number and fix its kind.
_PERCENT_SUFFIX = re.compile(
    r"^\s*(?:%|percent(?:age)?(?:\s*points?)?)", re.IGNORECASE,
)
_UNIT_SUFFIX = re.compile(
    r"^\s*(?:units?|kg|kgs?|tonnes?|tons?|co2e?)\b", re.IGNORECASE,
)

_MULTIPLIERS = {
    "crore": 1e7, "lakh": 1e5, "million": 1e6, "mn": 1e6,
    "billion": 1e9, "bn": 1e9, "k": 1e3,
}


def _parse_number(raw: str) -> Optional[float]:
    """Parse a numeric token, applying any scale word (crore, million, k…)."""
    cleaned = raw.strip()
    for symbol in _CURRENCY_SYMBOLS:
        cleaned = cleaned.replace(symbol, " ")
    lowered = cleaned.lower()

    multiplier = 1.0
    for word, factor in _MULTIPLIERS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            multiplier = factor
            break

    match = re.search(r"[-+]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+|\.\d+|\d+)", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "")) * multiplier
    except ValueError:
        return None


def _decimals_in(raw: str) -> int:
    match = re.search(r"\.(\d+)", raw)
    return len(match.group(1)) if match else 0


def _is_policeable(claim: NumericClaim) -> bool:
    """
    Whether a number is a factual claim worth validating.

    Bare small integers are excluded: "three facilities", "2 scenarios", "2026"
    are counts, ordinals and years, not assertions about computed results.
    Policing them would produce noise that hides the failures that matter.
    Anything carrying a unit — %, currency, "units" — is always policed.
    """
    if claim.kind in (ClaimKind.PERCENTAGE, ClaimKind.CURRENCY,
                      ClaimKind.UNITS, ClaimKind.RATIO, ClaimKind.UNKNOWN):
        return True
    return False   # bare COUNT


def extract_numeric_claims(text: str) -> List[NumericClaim]:
    """
    Extract numeric claims from free-form narrative.

    Secondary mechanism: the reasoning agent is asked for STRUCTURED claims
    first (see `ReasoningAgent`). This covers the case where it returns prose
    anyway, which a prompt-only gateway cannot prevent.
    """
    if not text:
        return []

    claims: List[NumericClaim] = []

    for match in _NUMBER_PATTERN.finditer(text):
        start, end = match.span()
        token = match.group(0)
        trailing = text[end:end + 24]
        preceding = text[max(0, start - 3):start]

        # Classify by the markers around the number.
        scale = _SCALE_PATTERN.match(trailing)
        kind = ClaimKind.UNKNOWN
        span_end = end

        if _PERCENT_SUFFIX.match(trailing):
            kind = ClaimKind.PERCENTAGE
            span_end = end + _PERCENT_SUFFIX.match(trailing).end()
        elif _UNIT_SUFFIX.match(trailing):
            kind = ClaimKind.UNITS
            span_end = end + _UNIT_SUFFIX.match(trailing).end()
        elif any(sym in preceding for sym in _CURRENCY_SYMBOLS):
            kind = ClaimKind.CURRENCY
            # Keep any scale word in the span so "₹12.4 crore" is read as
            # 124,000,000 rather than 12.4.
            if scale:
                span_end = end + scale.end()
        elif scale:
            kind = ClaimKind.CURRENCY
            span_end = end + scale.end()
        elif "." in token:
            # A bare decimal: could be a ratio (0.94) or a formatted currency
            # amount (1,000.00). Left UNKNOWN so it is compared against every
            # fact kind rather than mis-typed and wrongly reported unsupported.
            kind = ClaimKind.RATIO if abs(float(token.replace(",", ""))) <= 1.0 else ClaimKind.UNKNOWN
        else:
            kind = ClaimKind.COUNT

        raw = text[start:span_end].strip()
        # Include a leading currency symbol in the raw text so replacement of an
        # ungrounded claim removes the symbol too.
        if kind == ClaimKind.CURRENCY:
            for sym in _CURRENCY_SYMBOLS:
                if preceding.endswith(sym) or preceding.endswith(sym + " "):
                    raw = preceding[preceding.index(sym):] + raw
                    break

        value = _parse_number(raw)
        if value is None:
            continue

        claims.append(NumericClaim(raw_text=raw, value=value, kind=kind))

    return claims


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def _matches(claim_value: float, fact_value: float, claim_raw: str) -> bool:
    """Whether a claim matches an authoritative value within tolerance."""
    if math.isclose(claim_value, fact_value,
                    rel_tol=RELATIVE_TOLERANCE, abs_tol=ABSOLUTE_TOLERANCE):
        return True

    if ALLOW_ROUNDING_TO_CLAIM_PRECISION:
        # "14%" legitimately rounds 14.3; "13%" does not.
        decimals = _decimals_in(claim_raw)
        if round(fact_value, decimals) == round(claim_value, decimals):
            return True

    return False


def _comparable_facts(
    claim: NumericClaim, facts: Dict[str, AuthoritativeFact],
) -> List[AuthoritativeFact]:
    """
    Facts a claim could legitimately be referring to.

    A ratio claim may also match a percentage fact (0.94 vs 94%) and vice
    versa, because narratives move between the two freely.
    """
    # An unmarked number could refer to anything, so it is compared against
    # every fact. Being permissive here avoids reporting a legitimate figure as
    # unsupported merely because the narrative omitted a unit.
    if claim.kind == ClaimKind.UNKNOWN:
        return list(facts.values())

    kinds = {claim.kind}
    if claim.kind in (ClaimKind.RATIO, ClaimKind.PERCENTAGE):
        kinds |= {ClaimKind.RATIO, ClaimKind.PERCENTAGE}
    if claim.kind == ClaimKind.UNITS:
        kinds |= {ClaimKind.COUNT, ClaimKind.CURRENCY}
    if claim.kind == ClaimKind.CURRENCY:
        kinds |= {ClaimKind.UNITS}
    return [f for f in facts.values() if f.kind in kinds]


def ground_claims(
    claims: List[NumericClaim],
    facts: Dict[str, AuthoritativeFact],
    *,
    provenance: Optional[Dict[str, str]] = None,
) -> GroundingReport:
    """
    Adjudicate every claim against the authoritative facts.

    Args:
        claims:     Extracted or structured claims.
        facts:      Authoritative values, from `build_authoritative_facts`.
        provenance: execution/snapshot/scenario ids attached to accepted claims.

    Returns:
        GroundingReport. `status` is GROUNDING_FAILED if any claim is
        contradicted or unsupported.
    """
    prov = dict(provenance or {})
    report = GroundingReport()

    for claim in claims:
        if not _is_policeable(claim):
            claim.verdict = ClaimVerdict.IGNORED
            claim.detail = "bare count/ordinal — not a claim about computed results"
            report.claims.append(claim)
            continue

        candidates = _comparable_facts(claim, facts)

        if not candidates:
            claim.verdict = ClaimVerdict.UNSUPPORTED
            claim.detail = (
                f"no authoritative {claim.kind.value.lower()} value exists in the "
                f"deterministic results, so this figure has no basis"
            )
            report.claims.append(claim)
            continue

        # Percentage/ratio claims may be expressed either way.
        match: Optional[AuthoritativeFact] = None
        for fact in candidates:
            for candidate_value in _equivalent_values(claim, fact):
                if _matches(claim.value, candidate_value, claim.raw_text):
                    match = fact
                    break
            if match:
                break

        if match is not None:
            claim.verdict = ClaimVerdict.GROUNDED
            claim.matched_fact = match.key
            claim.matched_value = match.value
            claim.source = match.source
            claim.provenance = {**prov, "source": match.source, "fact": match.key}
            report.claims.append(claim)
            continue

        # Nothing matched. Distinguish the two failure modes:
        #   CONTRADICTED — a value of this KIND exists and the claim disagrees
        #                  (the model misreported a real figure).
        #   UNSUPPORTED  — no value of this kind exists at all
        #                  (the model invented a figure from nothing).
        # Comparison is deliberately cross-kind (0.968 may be written "96.8%"),
        # but the VERDICT keys on same-kind availability, so "cost rose 12%"
        # against a payload holding only an REI is correctly unsupported rather
        # than contradicted by an unrelated number.
        same_kind = [f for f in candidates if f.kind == claim.kind]
        if claim.kind == ClaimKind.UNKNOWN:
            same_kind = candidates

        if not same_kind:
            claim.verdict = ClaimVerdict.UNSUPPORTED
            claim.detail = (
                f"no authoritative {claim.kind.value.lower()} value exists in the "
                f"deterministic results, so this figure has no basis"
            )
        else:
            nearest = min(same_kind, key=lambda f: abs(f.value - claim.value))
            claim.verdict = ClaimVerdict.CONTRADICTED
            claim.matched_fact = nearest.key
            claim.matched_value = nearest.value
            claim.source = nearest.source
            claim.detail = (
                f"claimed {claim.value} but the nearest authoritative value is "
                f"{nearest.value} ({nearest.key})"
            )

        report.claims.append(claim)

    report.status = "GROUNDING_FAILED" if report.failed else (
        "NO_CLAIMS" if not [c for c in report.claims
                            if c.verdict != ClaimVerdict.IGNORED]
        else "GROUNDED"
    )

    if report.failed:
        logger.warning(
            "orchestrator.grounding.failed contradicted=%d unsupported=%d",
            len(report.contradicted), len(report.unsupported),
        )
    return report


def _equivalent_values(claim: NumericClaim, fact: AuthoritativeFact) -> List[float]:
    """
    Representations of a fact a claim might legitimately use.

    A fill rate stored as 0.968 may be written "96.8%"; an REI of 1.0 may be
    written "100%". Both are the same assertion.
    """
    values = [fact.value]
    if fact.kind == ClaimKind.RATIO and claim.kind == ClaimKind.PERCENTAGE:
        values.append(fact.value * 100.0)
    if fact.kind == ClaimKind.PERCENTAGE and claim.kind == ClaimKind.RATIO:
        values.append(fact.value / 100.0)
    return values


def ground_narrative(
    text: str,
    payload: Dict[str, Any],
    *,
    provenance: Optional[Dict[str, str]] = None,
    structured_claims: Optional[List[Dict[str, Any]]] = None,
) -> GroundingReport:
    """
    Ground a narrative against deterministic results.

    Prefers structured claims when the agent supplied them; otherwise falls back
    to extraction from prose.
    """
    facts = build_authoritative_facts(payload)

    claims: List[NumericClaim] = []
    if structured_claims:
        for raw in structured_claims:
            if not isinstance(raw, dict):
                continue
            try:
                value = float(raw.get("value"))
            except (TypeError, ValueError):
                continue
            unit = str(raw.get("unit", "")).lower()
            kind = (
                ClaimKind.PERCENTAGE if "percent" in unit or unit == "%"
                else ClaimKind.CURRENCY if unit in ("currency", "inr", "usd", "eur")
                else ClaimKind.UNITS if unit in ("units", "kg", "tonnes")
                else ClaimKind.RATIO
            )
            claims.append(NumericClaim(
                raw_text=str(raw.get("text") or raw.get("type") or value),
                value=value, kind=kind,
            ))

    claims.extend(extract_numeric_claims(text))
    return ground_claims(claims, facts, provenance=provenance)


def strip_ungrounded_claims(text: str, report: GroundingReport) -> str:
    """
    Neutralise ungrounded figures in narrative.

    Replacement rather than deletion: a reader must be able to see that a number
    was removed and why, instead of silently reading a sentence that has quietly
    lost its quantity.
    """
    result = text
    for claim in report.contradicted:
        result = result.replace(
            claim.raw_text,
            f"[UNGROUNDED CLAIM REMOVED — authoritative {claim.matched_fact} = "
            f"{claim.matched_value}]",
        )
    for claim in report.unsupported:
        result = result.replace(
            claim.raw_text, "[UNSUPPORTED FIGURE REMOVED]",
        )
    return result
