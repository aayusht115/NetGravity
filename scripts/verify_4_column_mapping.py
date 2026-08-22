#!/usr/bin/env python3
"""
FLOW 4 — Excel/CSV column mapping (the messy-spreadsheet flow)
==============================================================
USES THE LIVE API — roughly two calls per sheet (classify, then map).

    python scripts/verify_4_column_mapping.py
    python scripts/verify_4_column_mapping.py --source data/mock/india/distributors --trace

WHAT THIS PROVES
    This is the flow that earns its keep on real client data. A distributor
    sends `Wt (kgs)`, `Despatch Dt`, `Location Code`, `Vehicle No` — nobody
    is going to hand-map that for forty senders. Three independent opinions
    decide what each column means:

        MEMORY      what a human already confirmed, for this sender or
                    corroborated across several
        AI          what the model infers from the header AND the sample
                    values under it
        DICTIONARY  a deterministic alias table — no model involved

    The three are shown side by side per column, because that is the whole
    argument: agreement between methods that cannot see each other is
    evidence, and a lone confident guess is not.

TWO RULES THAT LOOK STRICT AND ARE DELIBERATE
    - NETWORK_REQUIRES_CONFIRMATION: anything bound for the optimizer is
      confirmed by a human even when all three agree. A wrong number that
      reaches the optimizer produces a confident wrong answer, which is
      worse than a slow one.
    - CONFIRM_FIRST_SIGHTING: the first time a column is seen from one
      sender, with nothing corroborating it, it is confirmed once. After
      that, memory answers for free.

WHAT TO LOOK FOR
    - a column with an obvious meaning mapping cleanly (`Qty`, `Rate`)
    - a column that SHOULD be dropped rather than guessed (`Vehicle No`,
      `Remarks`) — dropped is the correct outcome, not a miss
    - unit conversions PROPOSED by the model (`Wt (kgs)` -> kg), with the
      arithmetic left to plain Python
"""

from __future__ import annotations

import argparse

from _verify_common import (MOCK_ROOT, add_common_flags, enable_trace, finish,
                            section, start)
from netgravity.ingestion.ai.classifier import classify
from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.ai.field_mapper import (build_mapping,
                                                  canonical_fields_for,
                                                  dictionary_opinion)
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.sources.files import discover
from netgravity.ingestion.storage.local import LocalStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",
                        default=str(MOCK_ROOT / "distributors"),
                        help="directory to map (default: the messy "
                             "distributor workbook)")
    parser.add_argument("--limit", type=int, default=2,
                        help="max sheets to map. Each costs ~2 API calls.")
    parser.add_argument("--memory-dir", default="",
                        help="storage dir for field memory; omit to run "
                             "without memory (every column looks new)")
    add_common_flags(parser)
    args = parser.parse_args()

    if args.trace:
        enable_trace()

    config = start("FLOW 4 — EXCEL / CSV COLUMN MAPPING", needs_ai=True)
    client = get_client(config)

    memory = None
    if args.memory_dir:
        memory = FieldMemory(LocalStorage(args.memory_dir))
        print(f"\n  field memory: {args.memory_dir} "
              f"(stats: {memory.stats()})")
    else:
        print("\n  field memory: NOT loaded — every column is a first "
              "sighting, so expect confirmations to be requested")

    record_sets = [rs for source in discover(args.source)
                   for rs in source.record_sets()]
    chosen = record_sets[:args.limit]
    print(f"  {len(record_sets)} sheet(s) found; mapping {len(chosen)}")

    for record_set in chosen:
        section(f"{record_set.key}")
        print(f"  sender (source_id) : {record_set.origin.source_id}")
        print(f"  raw headers        : {', '.join(record_set.columns)}")
        if record_set.rows:
            first = record_set.rows[0]
            print("  first row          : "
                  + ", ".join(f"{k}={first.get(k)!r}"
                              for k in record_set.columns[:4]) + " ...")

        classification = classify(client, record_set)
        print(f"\n  classified as      : {classification.content_type.value} "
              f"({classification.confidence:.2f})")

        fields = canonical_fields_for(classification.content_type)
        print(f"  canonical fields   : {len(fields)} available for this "
              f"content type")

        mapping = build_mapping(client, record_set, classification,
                                memory=memory)

        print("\n  THE THREE OPINIONS, PER COLUMN")
        print(f"  {'column':<16} {'AI':<16} {'DICTIONARY':<16} "
              f"{'MEMORY':<12} {'-> CHOSEN':<16} status")
        print("  " + "-" * 96)
        for decision in mapping.decisions:
            ai = decision.ai_target or "-"
            dictionary = decision.dictionary_target or "-"
            chosen_field = decision.target_field or "(dropped)"
            status = ("CONFIRM" if decision.needs_review
                      else ("applied" if decision.is_mapped else "dropped"))
            print(f"  {decision.source_column:<16} {ai:<16} "
                  f"{dictionary:<16} {decision.memory_scope:<12} "
                  f"{chosen_field:<16} {status}")

        print("\n  DETAIL")
        for decision in mapping.decisions:
            flags = []
            if decision.methods_agree:
                flags.append("AI and dictionary AGREE independently")
            if decision.methods_conflict:
                flags.append("AI and dictionary CONFLICT")
            if decision.conversion_factor != 1.0:
                flags.append(
                    f"unit {decision.source_unit} -> {decision.target_unit}, "
                    f"factor {decision.conversion_factor} "
                    f"(proposed by the model; the multiplication itself is "
                    f"plain Python)")
            if not decision.is_mapped:
                flags.append("DROPPED rather than guessed")

            if flags or decision.review_reasons:
                print(f"    {decision.source_column}:")
                for flag in flags:
                    print(f"        - {flag}")
                for reason in decision.review_reasons:
                    print(f"        - needs review: {reason}")
                if decision.memory_rationale:
                    print(f"        - memory: {decision.memory_rationale}")
                if decision.ai_reasoning:
                    print(f"        - AI said: {decision.ai_reasoning}")

        confirmations = [d for d in mapping.decisions if d.needs_review]
        dropped = [d for d in mapping.decisions if not d.is_mapped]
        applied = [d for d in mapping.decisions
                   if d.is_mapped and not d.needs_review]
        agreed = [d for d in mapping.decisions if d.methods_agree]
        print(f"\n  applied without asking : {len(applied)}")
        print(f"  awaiting confirmation  : {len(confirmations)}")
        print(f"  dropped, not guessed   : {len(dropped)}")
        print(f"  AI + dictionary agreed : {len(agreed)} "
              f"(independent corroboration)")

    section("what this run demonstrated")
    print("  A column is only applied silently when the evidence is strong")
    print("  AND it is not optimizer-bound. Everything else is either")
    print("  confirmed by a human once, or dropped rather than guessed.")

    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
