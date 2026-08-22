#!/usr/bin/env python3
"""
FLOW 6 — The human review loop (the future UI's API)
=====================================================
USES THE LIVE API — about two calls (classify + map) to produce something
real to review. Add --offline to exercise the review API alone, free.

    python scripts/verify_6_review.py
    python scripts/verify_6_review.py --offline

WHAT THIS PROVES
    There is no review SCREEN yet, but there is a complete request/response
    API for one, so building the screen later is wiring rather than
    redesign. This script plays both halves: it produces the questions a
    human would see, answers them as that human, and shows what changed.

    The round trip is:
        build_request(mappings)   -> questions, options, evidence
        [a human picks answers]
        apply(request, decisions) -> settled mappings + memory written

    Two properties worth watching, both deliberate:
      - a question carries its EVIDENCE (why it is being asked, what each
        method thought), not just a dropdown. A reviewer cannot answer
        well from a bare column name.
      - apply() REFUSES an answer that was not among the offered options.
        A UI bug, or a hand-written API call, must not be able to inject an
        arbitrary target field into the network.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from _verify_common import (MOCK_ROOT, add_common_flags, enable_trace, finish,
                            section, start)
from netgravity.ingestion import review as review_module
from netgravity.ingestion.ai.classifier import classify
from netgravity.ingestion.ai.client import get_client
from netgravity.ingestion.ai.field_mapper import build_mapping
from netgravity.ingestion.memory.field_memory import FieldMemory
from netgravity.ingestion.sources.files import discover
from netgravity.ingestion.storage.local import LocalStorage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(MOCK_ROOT / "distributors"))
    parser.add_argument("--offline", action="store_true",
                        help="skip the model; exercise the review API alone")
    parser.add_argument("--json", action="store_true",
                        help="print the request as JSON — the exact payload "
                             "a UI would receive")
    add_common_flags(parser)
    args = parser.parse_args()

    if args.trace:
        enable_trace()

    config = start("FLOW 6 — HUMAN REVIEW LOOP", needs_ai=not args.offline)
    client = None if args.offline else get_client(config)

    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalStorage(Path(tmp))
        memory = FieldMemory(storage)

        record_sets = [rs for source in discover(args.source)
                       for rs in source.record_sets()][:1]
        if not record_sets:
            print(f"\n  no record sets found under {args.source}")
            return 1

        mappings = []
        for record_set in record_sets:
            classification = classify(client, record_set)
            mappings.append(build_mapping(client, record_set, classification,
                                          memory=memory))

        # ---- what the UI would be handed ---------------------------------
        request = review_module.build_request(mappings, run_id="verify-run")

        section("THE REQUEST (what a reviewer is asked)")
        print(f"  {request.summary}")
        print(f"  empty: {request.is_empty}")

        for item in request.items:
            print(f"\n  [{item.kind}] {item.item_id}")
            print(f"      question : {item.question}")
            print(f"      column   : {item.source_column or '(whole sheet)'}")
            print(f"      proposed : {item.proposed_value} "
                  f"(confidence {item.confidence:.2f})")
            samples = item.context.get("sample_values") or []
            if samples:
                print(f"      samples  : "
                      f"{', '.join(str(s) for s in samples[:4])}")
            for reason in item.reasons:
                print(f"      why      : {reason}")
            print("      options  :")
            for option in item.options:
                print(f"          - {option.value}: {option.label}")

        if args.json:
            section("THE SAME THING AS JSON (the UI payload)")
            print(json.dumps(request.as_dict(), indent=2)[:2500])

        if request.is_empty:
            print("\n  Nothing needed review — nothing to answer.")
            return finish(config)

        # ---- answer as a human would -------------------------------------
        section("ANSWERING (as the reviewer)")
        decisions = []
        for item in request.items:
            answer = item.options[0].value if item.options else None
            if answer is None:
                continue
            print(f"  {item.item_id}: choosing {answer!r}")
            decisions.append({"item_id": item.item_id, "value": answer,
                              "decided_by": "verify-script"})

        outcome = review_module.apply(request, decisions, mappings,
                                      memory=memory)

        section("AFTER APPLYING")
        print(f"  applied    : {len(outcome.applied)}  {outcome.applied}")
        print(f"  remembered : {len(outcome.remembered)}  "
              f"{outcome.remembered}")
        print(f"  rejected   : {len(outcome.rejected)}")
        for entry in outcome.rejected:
            print(f"      {entry}")
        print(f"\n  memory now holds: {memory.stats()}")
        print("  -> those confirmations are remembered, so the same columns "
              "from this sender are not asked again.")

        # ---- the guard ----------------------------------------------------
        section("A VALUE THAT WAS NEVER OFFERED")
        rogue = [{"item_id": request.items[0].item_id,
                  "value": "totally_made_up_field",
                  "decided_by": "someone-hand-rolling-an-api-call"}]
        guard = review_module.apply(request, rogue, mappings, memory=memory)
        print(f"  applied  : {len(guard.applied)}")
        print(f"  rejected : {len(guard.rejected)}")
        for entry in guard.rejected:
            print(f"      {entry}")
        print("  -> refused. A UI bug or a hand-rolled API call cannot inject "
              "an arbitrary field into the network.")

    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
