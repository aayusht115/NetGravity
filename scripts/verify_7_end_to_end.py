#!/usr/bin/env python3
"""
FLOW 7 — The whole unified pipeline, end to end
================================================
USES THE LIVE API — roughly two calls per record set. Cap it with --limit
on a shared budget.

    python scripts/verify_7_end_to_end.py --limit 2
    python scripts/verify_7_end_to_end.py --limit 2 --auto-confirm

WHAT THIS PROVES
    Everything the earlier scripts checked in isolation, running as one
    pass: discover -> classify -> map -> route -> stage. This is what the
    `--unified` CLI flag runs.

    ROUTING IS BY CONTENT, NOT BY FOLDER. A file called `stuff.xlsx` in a
    folder called `misc` still reaches the network if its CONTENT is a lane
    table — and a file sitting in a folder called `lanes` does not, if its
    content says otherwise. Three destinations:

        network   feeds the optimizer. The hardest to reach, deliberately.
        staging   understood, but not optimizer-bound.
        hold      not confidently understood — waiting on a human.

    --auto-confirm is for UNATTENDED runs. It answers the open questions
    itself and records `confirmed_by="auto"`, so an auto-confirmed mapping
    is always distinguishable afterwards from one a person actually looked
    at. Without it, anything uncertain lands in `hold` and waits.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from _verify_common import (MOCK_ROOT, add_common_flags, enable_trace, finish,
                            section, start)
from netgravity.ingestion.sources.files import discover
from netgravity.ingestion.storage.local import LocalStorage
from netgravity.ingestion.tabular import (ingest_tabular, parse_into_records,
                                          save_staging)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(MOCK_ROOT))
    parser.add_argument("--limit", type=int, default=2,
                        help="max record sets (each costs ~2 API calls)")
    parser.add_argument("--auto-confirm", action="store_true",
                        help="answer open questions automatically, as an "
                             "unattended run would")
    add_common_flags(parser)
    args = parser.parse_args()

    if args.trace:
        enable_trace()

    config = start("FLOW 7 — UNIFIED PIPELINE, END TO END", needs_ai=True)

    # Cap the work by handing ingest_tabular an explicit, trimmed source list
    # — the same seam an ERP/WMS connector will plug into later, since it
    # takes any DataSource rather than anything file-specific.
    sources = discover(args.source)
    trimmed = []
    budget = args.limit
    for data_source in sources:
        sets = list(data_source.record_sets())[:budget]
        if not sets:
            continue
        budget -= len(sets)
        trimmed.append(_Fixed(data_source.source_id, sets))
        if budget <= 0:
            break

    print(f"\n  source        : {args.source}")
    print(f"  record sets   : {sum(len(s._sets) for s in trimmed)} "
          f"(capped by --limit {args.limit})")
    print(f"  auto-confirm  : {args.auto_confirm}")

    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalStorage(Path(tmp))

        outcome = ingest_tabular(Path(args.source), config, storage,
                                 auto_confirm=args.auto_confirm,
                                 sources=trimmed)

        section("PER RECORD SET")
        for mapping in outcome.mappings:
            print(f"\n  {mapping.record_key}")
            print(f"      sender       : {mapping.source_id}")
            print(f"      content type : {mapping.content_type.value} "
                  f"({mapping.classification.confidence:.2f})")
            print(f"      destination  : {mapping.destination}")
            mapped = [d for d in mapping.decisions if d.is_mapped]
            pending = mapping.pending
            dropped = [d for d in mapping.decisions if not d.is_mapped]
            print(f"      columns      : {len(mapped)} mapped, "
                  f"{len(pending)} awaiting confirmation, "
                  f"{len(dropped)} dropped")
            # rename_map is the settled subset ONLY — a column still awaiting
            # review is left out entirely rather than applied provisionally.
            print(f"      applied now  : {mapping.rename_map or '(none yet)'}")

        section("FILE RESULTS (row-level validation)")
        for result in outcome.results:
            print(f"  {result.source_file}: {result.rows_accepted} accepted, "
                  f"{result.rows_rejected} rejected")
            for issue in result.issues:
                print(f"      [{issue.code}] {issue.message}")

        section("ROUTING (decided by content, never by folder)")
        buckets = {}
        for mapping in outcome.mappings:
            buckets.setdefault(mapping.destination, []).append(
                mapping.record_key)
        for destination in ("network", "staging", "hold"):
            keys = buckets.get(destination, [])
            print(f"  {destination:<9}: {len(keys)}")
            for key in keys:
                print(f"      - {key}")
        print(f"\n  network_rows by content type: "
              f"{ {k.value: len(v) for k, v in outcome.network_rows.items()} }")
        print(f"  staging_rows                : "
              f"{ {k: len(v) for k, v in outcome.staging_rows.items()} }")
        print(f"  held (not understood)       : {len(outcome.held)}")
        print("\n  Only 'network' reaches the optimizer. Anything not "
              "confidently understood waits in 'hold' rather than being "
              "routed on a guess.")

        section("REVIEW QUEUE")
        request = outcome.review_request
        print(f"  needs review : {outcome.needs_review}")
        print(f"  {request.summary}")

        section("PARSED RECORDS (what the optimizer would receive)")
        try:
            records = parse_into_records(outcome)
            for name, items in records.items():
                print(f"  {name:<20}: {len(items)}")
        except Exception as exc:
            print(f"  could not parse: {type(exc).__name__}: {exc}")

        section("STAGING")
        try:
            written = save_staging(outcome, storage, "verify-run")
            print(f"  wrote {len(written)} staged object(s)")
            for key in written[:8]:
                print(f"      - {key}")
        except Exception as exc:
            print(f"  could not stage: {type(exc).__name__}: {exc}")

    return finish(config)


class _Fixed:
    """A DataSource wrapper that yields a pre-trimmed list of record sets."""

    def __init__(self, source_id, sets):
        self._source_id = source_id
        self._sets = sets

    @property
    def source_id(self):
        return self._source_id

    def record_sets(self):
        return iter(self._sets)


if __name__ == "__main__":
    raise SystemExit(main())
