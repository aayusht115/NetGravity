#!/usr/bin/env python3
"""
FLOW 3 — Content classification (what IS this sheet?)
======================================================
USES THE LIVE API — one call per record set (use --limit to cap it).

    python scripts/verify_3_classification.py --limit 3
    python scripts/verify_3_classification.py --limit 1 --trace

WHAT THIS PROVES
    Two independent opinions decide what a sheet contains — the model reads
    the columns and sample rows, and a deterministic alias-overlap scorer
    reads the columns alone. Both are shown, because agreement between two
    methods that cannot see each other's work is the actual evidence; a
    single confident answer is not.

    Disagreement is not a bug here. It is the signal that routes the sheet
    to a human instead of into the network.

WHAT TO LOOK FOR
    - proposed vs rules: do they agree?
    - confidence >= 0.85 -> accepted; below -> review; below 0.50 -> UNKNOWN
    - destination: network / staging / hold. Only 'network' feeds the
      optimizer, and it is deliberately the hardest to reach.
"""

from __future__ import annotations

import argparse

from _verify_common import (MOCK_ROOT, add_common_flags, enable_trace, finish,
                            section, start)
from netgravity.ingestion.ai.classifier import classify, score_by_rules
from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.sources.files import discover


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(MOCK_ROOT))
    parser.add_argument("--limit", type=int, default=3,
                        help="max record sets to classify (each costs one "
                             "API call). Default 3.")
    add_common_flags(parser)
    args = parser.parse_args()

    if args.trace:
        enable_trace()

    config = start("FLOW 3 — CONTENT CLASSIFICATION", needs_ai=True)
    client = get_client(config)

    record_sets = [rs for source in discover(args.source)
                   for rs in source.record_sets()]
    chosen = record_sets[:args.limit]
    print(f"\n  {len(record_sets)} record sets found; classifying "
          f"{len(chosen)} (one API call each)")

    agreements = 0
    for record_set in chosen:
        section(record_set.key)
        print(f"  columns : {', '.join(record_set.columns)}")

        # The rule scorer alone, shown first — it is free, deterministic, and
        # the baseline the model has to beat or confirm.
        rule_type, rule_score, _ = score_by_rules(record_set.columns)
        print(f"  rules alone     : {rule_type.value} ({rule_score:.2f})")

        result = classify(client, record_set)
        print(f"  PROPOSED        : {result.content_type.value} "
              f"({result.confidence:.2f}) by {result.proposed_by}")
        print(f"  rules agree     : {result.rules_agree}")
        print(f"  destination     : {result.destination}")
        print(f"  feeds optimizer : {result.content_type.feeds_optimizer}")
        print(f"  needs review    : {result.needs_review}")
        for reason in result.review_reasons:
            print(f"      - {reason}")
        print(f"  reasoning       : {result.reasoning}")

        if result.rules_agree:
            agreements += 1

    section("summary")
    print(f"  {agreements}/{len(chosen)} classifications had both opinions "
          f"agreeing.")
    print("  Disagreement is not failure — it is what sends a sheet to a "
          "human rather than into the network on one opinion.")

    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
