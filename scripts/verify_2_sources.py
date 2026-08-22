#!/usr/bin/env python3
"""
FLOW 2 — Source discovery (files and sheets)
=============================================
Costs NOTHING — no AI is involved.

    python scripts/verify_2_sources.py [--source data/mock/india]

WHAT THIS PROVES
    Every CSV and every SHEET of every Excel workbook is found, whatever it
    is called. The old pipeline routed on file NAME; this one reads
    everything and decides later, by content. So the thing to check here is
    coverage: does the count match what is actually on disk, including the
    second and third sheets of a workbook, which are the usual casualties.

    It also shows the derived `source_id` — taken from the containing
    FOLDER, not the file name — which is what makes three files from one
    sender resolve to one identity, so a mapping confirmed on one of them
    applies to the others.
"""

from __future__ import annotations

import argparse

from _verify_common import MOCK_ROOT, add_common_flags, finish, section, start
from netgravity.ingestion.sources.files import discover


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(MOCK_ROOT),
                        help="directory to scan")
    add_common_flags(parser)
    args = parser.parse_args()

    config = start("FLOW 2 — SOURCE DISCOVERY", needs_ai=False)
    print(f"\n  scanning: {args.source}")

    sources = discover(args.source)
    total_sets = 0
    total_rows = 0

    for data_source in sources:
        section(f"source_id: {data_source.source_id}")
        for record_set in data_source.record_sets():
            total_sets += 1
            total_rows += len(record_set.rows)
            origin = record_set.origin
            sheet = f" [sheet: {origin.sheet}]" if origin.sheet else ""
            print(f"  {origin.container}{sheet}")
            print(f"      key      : {record_set.key}")
            print(f"      rows     : {len(record_set.rows)}")
            print(f"      columns  : {', '.join(record_set.columns) or '(none)'}")
            if record_set.warning:
                print(f"      WARNING  : {record_set.warning}")

    section("totals")
    print(f"  sources (senders)  : {len(sources)}")
    print(f"  record sets        : {total_sets}")
    print(f"  rows               : {total_rows}")
    print("\n  Check this against what is actually in the folder — every "
          "sheet of every workbook should appear, not just the first.")

    return finish(config)


if __name__ == "__main__":
    raise SystemExit(main())
