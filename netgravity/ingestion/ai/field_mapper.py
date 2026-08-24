"""
NetGravity — Field Mapping Engine
==================================
Decides what every column in a record set means, by combining three
independent opinions.

    MEMORY      what a human already confirmed, scoped by evidence.
                Authoritative when it applies — a settled question is not
                re-asked, and no model call is needed to answer it.

    AI          reads the columns AND real sample rows, so it has context.
                This is the only method that can tell a "Weight" on a
                despatch register from a "Weight" on a product master.

    DICTIONARY  the static alias table. Context-blind and often silent, but
                free, instant and perfectly repeatable.

WHY RUN THE DICTIONARY AT ALL IF THE MODEL IS BETTER
----------------------------------------------------
Not for its answers — for its DISAGREEMENTS. The dangerous failure in column
mapping is not the ambiguous column, which gets flagged either way. It is the
column that maps confidently and wrongly, silently, because the header looked
obvious. A model's own confidence score cannot catch that: a model can be
95% sure and wrong. A second method with different blind spots can, because
when two independent approaches diverge, that divergence is evidence in
itself. Agreement is corroboration; disagreement is a flag. Neither is
available from one method alone.

The dictionary costs nothing meaningful: it is a local lookup, and the model
call was happening regardless.

THE STRICTER BAR FOR OPTIMISER-BOUND DATA
-----------------------------------------
Distributor data lands in the staging zone, where a wrong mapping costs a
bad forecast. Facility, product, demand and lane data becomes the
CanonicalNetwork the MILP solves against, where a wrong mapping costs a
wrong recommendation that looks authoritative.

Those are not the same risk, so they do not get the same bar. For
optimiser-bound content, model-and-dictionary agreement is NOT enough to
auto-apply: a human confirms once, and memory carries that confirmation
forward from then on.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from netgravity.ingestion.ai.client import LLM_FAILURE_MARKER, LLMClient
from netgravity.ingestion.field_aliases import (
    DEMAND_ALIASES,
    DEMAND_LOOKUP,
    FACILITY_ALIASES,
    FACILITY_LOOKUP,
    HISTORY_ALIASES,
    HISTORY_LOOKUP,
    LANE_ALIASES,
    LANE_LOOKUP,
    MARKET_ALIASES,
    MARKET_LOOKUP,
    MARKET_SIGNAL_ALIASES,
    MARKET_SIGNAL_LOOKUP,
    PRODUCT_ALIASES,
    PRODUCT_LOOKUP,
    normalise_name,
)
from netgravity.ingestion.memory.field_memory import (
    SCOPE_CONFLICT,
    SCOPE_EXACT,
    SCOPE_GENERALISED,
    SCOPE_SUGGESTED,
    FieldMemory,
)
from netgravity.ingestion.schemas.content import ContentClassification, ContentType
from netgravity.ingestion.schemas.field_mapping import (
    BY_AI,
    BY_AI_AND_DICTIONARY,
    BY_DICTIONARY,
    BY_MEMORY_EXACT,
    BY_MEMORY_GENERALISED,
    BY_NONE,
    ColumnDecision,
    MappingOption,
    SheetMapping,
)
from netgravity.ingestion.sources.base import RecordSet

#: Confidence a model answer needs before it applies without review, for
#: content that does NOT reach the optimiser.
REVIEW_BELOW = 0.90

#: Optimiser-bound content is never auto-applied on a model's say-so alone,
#: at any confidence. Only a prior human confirmation (carried by memory)
#: clears it. See the module docstring.
NETWORK_REQUIRES_CONFIRMATION = True

#: A column nobody has ever confirmed, which the alias table does not
#: recognise either, rests on ONE opinion however confident that opinion
#: sounds. A model can be 95% sure and wrong, and nothing here would catch
#: it. So a first sighting without corroboration is confirmed once — and
#: because memory then carries that confirmation forward (across senders,
#: once enough of them agree), the cost is one question, once, ever.
#:
#: Corroborated columns are unaffected: when the model and the alias table
#: independently agree, that IS the second opinion, and no question is asked.
CONFIRM_FIRST_SIGHTING = True

_ALIAS_TABLES = {
    ContentType.FACILITY: (FACILITY_ALIASES, FACILITY_LOOKUP),
    ContentType.MARKET: (MARKET_ALIASES, MARKET_LOOKUP),
    ContentType.DEMAND: (DEMAND_ALIASES, DEMAND_LOOKUP),
    ContentType.LANE: (LANE_ALIASES, LANE_LOOKUP),
    ContentType.PRODUCT: (PRODUCT_ALIASES, PRODUCT_LOOKUP),
    ContentType.HISTORICAL_VOLUME: (HISTORY_ALIASES, HISTORY_LOOKUP),
    ContentType.MARKET_SIGNAL: (MARKET_SIGNAL_ALIASES, MARKET_SIGNAL_LOOKUP),
}

#: A shipment log has no alias table — nobody wrote a schema for the shape
#: every distributor invents independently. These are the fields such a file
#: can usefully become.
_SHIPMENT_FIELDS: Dict[str, str] = {
    "market_id": "destination / delivery zone identifier",
    "facility_id": "origin facility identifier",
    "product_id": "product or SKU identifier",
    "quantity": "units moved",
    "weight_kg": "weight in kilograms",
    "rate_per_unit": "freight cost per unit",
    "period": "date or period of the movement",
    "distance_km": "distance in kilometres",
    "lead_time_days": "transit time in days",
    "order_count": "number of orders",
    "returns_volume": "units returned",
}


def _build_shipment_lookup() -> Dict[str, str]:
    """
    A dictionary for the one content type that has no alias table.

    SHIPMENT_LOG is exactly the shape nobody wrote a schema for, so the
    cross-check would be silent on every column of every distributor file —
    the files that need it most. Its columns do map onto fields that DO have
    aliases, just spread across several tables (rate_per_unit lives in the
    lane table, quantity in the demand table), so the lookup is assembled as
    a union of those, restricted to fields a shipment log can legitimately
    become.

    Where two tables disagree about an alias, it is DROPPED rather than
    resolved by precedence. An ambiguous second opinion is worse than none:
    the whole value of this check is that a disagreement means something, and
    that only holds while the dictionary speaks unambiguously or stays quiet.
    """
    # Ambiguity must be detected across the FULL alias space, not just the
    # subset being kept. "Node_ID" resolves to facility_id in the facility
    # table and to node_id in the history table; filtering to shipment fields
    # BEFORE comparing hides that clash and lets one arbitrary answer survive
    # as if it were unambiguous.
    meanings: Dict[str, set] = {}
    for _, lookup in _ALIAS_TABLES.values():
        for normalised, canonical in lookup.items():
            meanings.setdefault(normalised, set()).add(canonical)

    return {
        normalised: next(iter(targets))
        for normalised, targets in meanings.items()
        if len(targets) == 1 and next(iter(targets)) in _SHIPMENT_FIELDS
    }


_SHIPMENT_LOOKUP = _build_shipment_lookup()


def canonical_fields_for(content_type: ContentType) -> Dict[str, str]:
    """
    The target vocabulary offered for one content type.

    Scoped to the content type on purpose: offering every field in the system
    invites cross-entity mistakes (mapping a facility column onto a lane
    field), and a shorter, relevant list measurably improves mapping accuracy.
    """
    if content_type in _ALIAS_TABLES:
        aliases, _ = _ALIAS_TABLES[content_type]
        return {
            canonical: ("also written as " + ", ".join(seen[:4])) if seen else ""
            for canonical, seen in aliases.items()
        }
    if content_type == ContentType.SHIPMENT_LOG:
        return dict(_SHIPMENT_FIELDS)
    return dict(_SHIPMENT_FIELDS)          # safest general-purpose vocabulary


def dictionary_opinion(column: str, content_type: ContentType) -> Optional[str]:
    """The static alias table's context-blind answer, or None if it has none."""
    table = _ALIAS_TABLES.get(content_type)
    if table is not None:
        lookup = table[1]
    elif content_type == ContentType.SHIPMENT_LOG:
        lookup = _SHIPMENT_LOOKUP
    else:
        return None
    return lookup.get(normalise_name(column))


def _sample_values(record_set: RecordSet, column: str, limit: int = 5) -> List[str]:
    values: List[str] = []
    for row in record_set.rows:
        raw = row.get(column)
        if raw is None or str(raw).strip() == "":
            continue
        values.append(str(raw)[:40])
        if len(values) >= limit:
            break
    return values


def _known_id_hits(record_set: RecordSet, column: str,
                   known_ids: Sequence[str]) -> int:
    """
    How many of this column's values are identifiers we already know.

    About the strongest single clue available: a column whose values include
    MKT_DELHI is an identifier column, whatever it happens to be titled.
    """
    if not known_ids:
        return 0
    known = {str(k).strip().lower() for k in known_ids}
    hits = 0
    for row in record_set.rows[:50]:
        value = row.get(column)
        if value is not None and str(value).strip().lower() in known:
            hits += 1
    return hits


PROMPT_TEMPLATE = """You are mapping the columns of a supply-chain data file \
onto a fixed canonical schema.

THIS FILE HAS BEEN CLASSIFIED AS: {content_type}
{classification_note}

CANONICAL FIELDS AVAILABLE (map onto these, or return null):
{canonical_fields}

{precedent_block}
{known_id_block}
FILE: {origin}
COLUMNS AND SAMPLE VALUES:
{sample}

Return ONLY a JSON object with this exact shape:

{{
  "mappings": [
    {{
      "source_column": "exact column header, copied verbatim",
      "target_field": "one canonical field above, or null if none fits",
      "confidence": 0.0,
      "reasoning": "the evidence you used, in one line",
      "source_unit": "unit seen in the data, or null",
      "target_unit": "canonical unit, or null",
      "conversion_factor": 1.0
    }}
  ],
  "unmapped_columns": ["columns that map to nothing canonical"]
}}

RULES
- Return an entry for EVERY column listed, even ones you map to null.
- Judge from the sample VALUES, not the header alone. A column titled
  "Weight" means unit weight on a product master and total consignment
  weight on a despatch register — the values and the file's classification
  tell you which.
- Do not invent a canonical field that is not in the list above.
- Set confidence honestly. A confidently wrong mapping silently corrupts
  every downstream number; an admitted uncertainty costs one question.
"""


def _render_sample(record_set: RecordSet, known_ids: Sequence[str],
                   limit: int = 5) -> str:
    lines: List[str] = []
    for column in record_set.columns:
        values = _sample_values(record_set, column, limit)
        hits = _known_id_hits(record_set, column, known_ids)
        marker = (f"   [{hits} values match known network identifiers]"
                  if hits else "")
        shown = ", ".join(values) if values else "(all empty)"
        lines.append(f"  - {column}: {shown}{marker}")
    return "\n".join(lines)


def _render_precedents(record_set: RecordSet, content_type: ContentType,
                       memory: Optional[FieldMemory]) -> str:
    """
    Confirmed precedent, handed to the model as evidence.

    Prior confirmations were previously used only to SKIP columns. Passing
    them into the prompt as well means the model can reason from how this
    organisation has actually resolved similar columns before, rather than
    from the column in isolation.
    """
    if memory is None:
        return ""
    lines: List[str] = []
    for column in record_set.columns:
        for obs in memory.observations_for(content_type.value, column)[:3]:
            lines.append(
                f"  - \"{obs.source_column}\" was confirmed as "
                f"{obs.target_field} for {obs.source_id}"
            )
    if not lines:
        return ""
    return ("PREVIOUSLY CONFIRMED BY A HUMAN FOR THIS KIND OF FILE "
            "(strong precedent — follow it unless the values clearly "
            "contradict it):\n" + "\n".join(lines[:12]) + "\n")


def build_mapping(client: Optional[LLMClient], record_set: RecordSet,
                  classification: ContentClassification,
                  *, memory: Optional[FieldMemory] = None,
                  known_ids: Optional[Sequence[str]] = None,
                  sample_limit: int = 5) -> SheetMapping:
    """Produce a decision for every column in `record_set`."""
    content_type = classification.content_type
    source_id = record_set.origin.source_id
    known_ids = list(known_ids or [])

    mapping = SheetMapping(
        record_key=record_set.key,
        source_id=source_id,
        origin_label=record_set.origin.label,
        classification=classification,
        proposed_by="rules",
    )

    # --- 1. What is already settled? --------------------------------------
    memory_resolutions = {}
    if memory is not None:
        for column in record_set.columns:
            memory_resolutions[column] = memory.resolve(
                source_column=column, content_type=content_type.value,
                source_id=source_id)

    unresolved = [
        c for c in record_set.columns
        if not (memory_resolutions.get(c) and memory_resolutions[c].is_known)
    ]

    # --- 2. Ask the model about what is not ------------------------------
    ai_by_column: Dict[str, Dict[str, Any]] = {}
    ai_unmapped: List[str] = []

    if unresolved and client is not None and not client.stub_mode:
        prompt = PROMPT_TEMPLATE.format(
            content_type=content_type.value,
            classification_note=(
                f"(classification confidence {classification.confidence:.0%}"
                + (", still awaiting human confirmation"
                   if classification.needs_review else "") + ")"),
            canonical_fields="\n".join(
                f"  - {name}: {desc}" if desc else f"  - {name}"
                for name, desc in canonical_fields_for(content_type).items()),
            precedent_block=_render_precedents(record_set, content_type, memory),
            known_id_block=(
                "KNOWN IDENTIFIERS IN THIS NETWORK:\n  "
                + ", ".join(str(k) for k in known_ids[:40]) + "\n"
                if known_ids else ""),
            origin=record_set.origin.label,
            sample=_render_sample(record_set, known_ids, sample_limit),
        )
        response = client.extract_json(
            task=f"column mapping ({record_set.origin.label})",
            prompt=prompt,
            stub_key="distributor_mapping",
            stub_context={"filename": record_set.origin.container},
            max_tokens=2000,
        )
        if response.failed:
            mapping.notes.append(response.notes)
            mapping.proposed_by = f"rules ({LLM_FAILURE_MARKER})"
        else:
            mapping.proposed_by = response.provenance
            mapping.notes.append(response.notes)
            for raw in (response.data or {}).get("mappings") or []:
                column = str(raw.get("source_column") or "").strip()
                if column:
                    ai_by_column[column] = raw
            ai_unmapped = [str(c) for c in
                           ((response.data or {}).get("unmapped_columns") or [])]
    elif client is not None and client.stub_mode:
        mapping.proposed_by = "rules (no AI key)"
        mapping.notes.append(
            "no AI key configured — columns resolved from confirmed memory and "
            "the static alias table only")

    # --- 3. Decide each column --------------------------------------------
    for column in record_set.columns:
        mapping.decisions.append(_decide(
            column=column,
            record_set=record_set,
            content_type=content_type,
            resolution=memory_resolutions.get(column),
            ai_raw=ai_by_column.get(column),
            ai_said_unmapped=column in ai_unmapped,
            known_ids=known_ids,
        ))

    mapping.unmapped_columns = [
        d.source_column for d in mapping.decisions if not d.is_mapped
    ]
    return mapping


def _decide(*, column: str, record_set: RecordSet, content_type: ContentType,
            resolution, ai_raw: Optional[Dict[str, Any]],
            ai_said_unmapped: bool,
            known_ids: Sequence[str]) -> ColumnDecision:
    """Combine the three opinions for one column into a single decision."""
    decision = ColumnDecision(
        source_column=column,
        sample_values=_sample_values(record_set, column),
    )

    dictionary_target = dictionary_opinion(column, content_type)
    decision.dictionary_target = dictionary_target

    if ai_raw:
        target = ai_raw.get("target_field")
        decision.ai_target = str(target) if target else None
        try:
            decision.ai_confidence = max(0.0, min(1.0, float(
                ai_raw.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            decision.ai_confidence = 0.0
        decision.ai_reasoning = str(ai_raw.get("reasoning") or "")
        decision.source_unit = ai_raw.get("source_unit") or None
        decision.target_unit = ai_raw.get("target_unit") or None
        try:
            decision.conversion_factor = float(ai_raw.get("conversion_factor") or 1.0)
        except (TypeError, ValueError):
            decision.conversion_factor = 1.0

    # Candidate list, most authoritative first — this is what a reviewer sees.
    options: List[MappingOption] = []
    if resolution is not None:
        decision.memory_scope = resolution.scope
        decision.memory_rationale = resolution.rationale
        for alternative in resolution.alternatives:
            options.append(MappingOption(
                target_field=alternative.target_field,
                suggested_by="memory",
                rationale=f"confirmed by {', '.join(alternative.source_ids)}",
                support=alternative.support,
            ))
    if decision.ai_target:
        options.append(MappingOption(
            target_field=decision.ai_target, suggested_by="ai",
            rationale=decision.ai_reasoning or "read from the file's context"))
    if dictionary_target:
        options.append(MappingOption(
            target_field=dictionary_target, suggested_by="dictionary",
            rationale="matches a known column name in the alias table"))
    decision.options = options

    # --- settled by memory -------------------------------------------------
    if resolution is not None and resolution.is_known:
        decision.target_field = resolution.target_field
        decision.confidence = resolution.confidence
        decision.decided_by = (BY_MEMORY_EXACT if resolution.scope == SCOPE_EXACT
                               else BY_MEMORY_GENERALISED)
        decision.needs_review = False
        return decision

    reasons: List[str] = []

    # --- memory knows of a genuine disagreement ---------------------------
    if resolution is not None and resolution.scope == SCOPE_CONFLICT:
        reasons.append(resolution.rationale)
    elif resolution is not None and resolution.scope == SCOPE_SUGGESTED:
        reasons.append(resolution.rationale)

    # --- combine model and dictionary -------------------------------------
    if decision.ai_target and dictionary_target:
        if decision.ai_target == dictionary_target:
            decision.target_field = decision.ai_target
            decision.decided_by = BY_AI_AND_DICTIONARY
            decision.confidence = min(1.0, max(decision.ai_confidence, 0.95))
        else:
            decision.target_field = decision.ai_target
            decision.decided_by = BY_AI
            decision.confidence = min(decision.ai_confidence, 0.60)
            reasons.append(
                f"the model read this as '{decision.ai_target}' but the alias "
                f"table says '{dictionary_target}' — two independent methods "
                f"disagreed, so neither is applied without a check")
    elif decision.ai_target:
        decision.target_field = decision.ai_target
        decision.decided_by = BY_AI
        decision.confidence = decision.ai_confidence
    elif dictionary_target and not ai_said_unmapped:
        decision.target_field = dictionary_target
        decision.decided_by = BY_DICTIONARY
        decision.confidence = 0.70
        reasons.append(
            "matched only by the alias table, which cannot see the file's "
            "context — worth a glance")
    else:
        decision.decided_by = BY_NONE
        decision.confidence = 0.0
        if not ai_said_unmapped:
            reasons.append("no canonical field could be identified for this column")

    # --- the bar ----------------------------------------------------------
    if decision.target_field:
        if content_type.feeds_optimizer and NETWORK_REQUIRES_CONFIRMATION:
            reasons.append(
                f"{content_type.value} data feeds the optimiser directly, so a "
                f"mapping is confirmed once by a human before it is trusted; "
                f"after that it is remembered")
        elif (CONFIRM_FIRST_SIGHTING
                and decision.decided_by == BY_AI
                and not dictionary_target
                and decision.memory_scope == "none"):
            reasons.append(
                "first time this column has been seen, and the alias table does "
                "not recognise it either — so this rests on a single opinion. "
                "Confirmed once, then remembered")
        elif decision.confidence < REVIEW_BELOW:
            reasons.append(
                f"confidence {decision.confidence:.0%} is below the "
                f"{REVIEW_BELOW:.0%} bar")

    # An unmapped column is not a problem to escalate — it is simply data we
    # do not need. Only say something when a mapping was actually proposed.
    decision.review_reasons = reasons
    decision.needs_review = bool(reasons) and decision.is_mapped
    if not decision.is_mapped:
        decision.needs_review = False
    return decision
