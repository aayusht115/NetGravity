#!/usr/bin/env python3
"""
FLOW 8 — Market intelligence: three ways in, one guardrail
===========================================================
OFFLINE BY DEFAULT. Costs nothing and makes no network call. Add --live to
use the configured model (~1 call per document) once you actually want that.

    python scripts/verify_8_market_intelligence.py
    python scripts/verify_8_market_intelligence.py --live --limit 2

WHAT THIS PROVES
    External information arrives because a PERSON supplies it. There is no
    fetching anywhere beneath this script — no HTTP call, no feed reader, no
    scraper. Three doors in, and all three end at the same schema and the
    same guardrail:

        chat        "diesel is up 6%"      -> Intent.MARKET_INTELLIGENCE
        document    a news PDF or article  -> adapters/market_intelligence.py
        spreadsheet one row per signal     -> ContentType.MARKET_SIGNAL

    The boundary worth watching is between a MARKET CHANGE and a HAZARD.
    Both describe the outside world; only one carries a probability, and
    RF = P + REI - P*REI is computed from that probability. This script shows
    the classifier holding that line in both directions.

WHAT TO LOOK FOR
    - "fuel prices are expected to rise 8%" staying MARKET_INTELLIGENCE
      despite containing hazard/forecast vocabulary
    - "flooding expected around DC_DELHI" staying EXTERNAL_EVENT
    - an article with no stated date REJECTED rather than stamped with today
    - analyst commentary yielding NO signals, which is a correct answer
    - every signal carrying a guardrail verdict, passed or filtered
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _verify_common import (MOCK_ROOT, add_common_flags, enable_trace, finish,
                            section, start)
from netgravity.ingestion.adapters import market_intelligence
from netgravity.ingestion.schemas.content import ContentType
from netgravity.orchestrator.conversation.nlu import ConversationalNLU
from netgravity.orchestrator.core.planner import WORKFLOW_TEMPLATES
from netgravity.orchestrator.schemas.requests import Intent

NEWS_DIR = MOCK_ROOT / "market_news"

#: Sentences chosen for where they sit relative to the market/hazard line.
CHAT_CASES = [
    ("Diesel is up 6% this week.", Intent.MARKET_INTELLIGENCE),
    ("Fuel prices are expected to rise 8% next month.", Intent.MARKET_INTELLIGENCE),
    ("Port handling charges at Mumbai went up 5% in January.", Intent.MARKET_INTELLIGENCE),
    ("Our carrier has hiked trucking rates by 12%.", Intent.MARKET_INTELLIGENCE),
    ("Flooding is expected around DC_DELHI.", Intent.EXTERNAL_EVENT),
    ("There is a 70% probability of flooding around DC_DELHI.", Intent.EXTERNAL_EVENT),
    ("What has diesel done this year?", None),          # a question, not a report
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=str(NEWS_DIR))
    parser.add_argument("--limit", type=int, default=4,
                        help="max documents to read")
    parser.add_argument("--live", action="store_true",
                        help="use the configured model. Without this the run "
                             "is fully offline and costs nothing.")
    add_common_flags(parser)
    args = parser.parse_args()

    if args.trace:
        enable_trace()

    config = start("FLOW 8 — MARKET INTELLIGENCE", needs_ai=args.live)
    if not args.live:
        config = config.__class__(**{**config.__dict__, "llm_api_key": None})
        print("\n  running OFFLINE — no network call will be made. "
              "Pass --live to use the model.")

    # ---- route 1: chat -------------------------------------------------
    section("ROUTE 1 — CHAT (the boundary against hazards)")
    network = _delhi_network()
    if network is None:
        print("  skipped: the fixture network could not be imported")
    else:
        nlu = ConversationalNLU()
        for text, expected in CHAT_CASES:
            result = nlu.understand(text, network, allow_llm=False)
            got = result.intent
            if expected is None:
                verdict = "ok " if got != Intent.MARKET_INTELLIGENCE else "!! "
                note = "correctly NOT a signal"
            else:
                verdict = "ok " if got == expected else "!! "
                note = f"expected {expected.value}"
            print(f"  {verdict}{got.value:<22} {text}")
            if verdict == "!! ":
                print(f"        {note}")
            if result.market_signal is not None:
                spec = result.market_signal
                print(f"        -> subject={spec.subject!r} "
                      f"direction={spec.direction} "
                      f"magnitude={spec.magnitude!r} bucket={spec.bucket}")
                print(f"        -> no probability field exists on this spec, "
                      f"by design")

        print(f"\n  workflow for the intent: "
              f"{WORKFLOW_TEMPLATES[Intent.MARKET_INTELLIGENCE].workflow_id}")
        print("  that workflow loads the network, explains and governs. It "
              "does NOT solve and does NOT create a scenario.")

    # ---- route 2: documents --------------------------------------------
    section("ROUTE 2 — DOCUMENTS")
    source = Path(args.source)
    if not source.exists():
        print(f"  {source} not found")
    else:
        documents = [p for p in sorted(source.iterdir())
                     if p.suffix.lower() in market_intelligence.SUPPORTED_SUFFIXES]
        print(f"  {len(documents)} document(s) found; reading "
              f"{min(len(documents), args.limit)}")

        for path in documents[:args.limit]:
            print(f"\n  {path.name}")
            signals, result = market_intelligence.ingest_file(
                path, config, known_entity_ids={"DC_DELHI", "DC_MUMBAI"})

            print(f"      signals    : {len(signals)} "
                  f"(rejected {result.rows_rejected})")
            for signal in signals:
                mark = "PASS" if signal.passed_guardrail else "FILTERED"
                print(f"      [{mark}] {signal.title[:58]}")
                print(f"           {signal.bucket.value} / "
                      f"{signal.direction.value} / {signal.magnitude or '(no figure)'} "
                      f"/ confidence {signal.confidence.value}")
                print(f"           published {signal.published_date}"
                      f"{'  effective ' + signal.effective_date if signal.effective_date else ''}")
                if signal.verdict:
                    print(f"           {signal.verdict.reason}")
                print(f"           use: {signal.scenario_use.value}")
            for issue in result.issues:
                print(f"      [{issue.code}] {issue.message[:88]}")

    # ---- route 3: spreadsheets -----------------------------------------
    section("ROUTE 3 — SPREADSHEET (rides the existing tabular pipeline)")
    print(f"  content type : {ContentType.MARKET_SIGNAL.value}")
    print(f"  destination  : {ContentType.MARKET_SIGNAL.destination}")
    print(f"  reaches MILP : {ContentType.MARKET_SIGNAL.feeds_optimizer}")
    print("\n  A signal sheet is classified, column-mapped, remembered and "
          "reviewed by exactly the machinery flows 2-7 already exercise. No "
          "second Excel reader was written, because none was needed.")

    section("WHY EVERYTHING LANDS IN STAGING")
    print("  A signal is context: it shifts an assumption and explains a")
    print("  result. It never edits a rate. Routed to the network it would")
    print("  become an input the MILP treats as fact — a headline turned into")
    print("  a number nobody computed. If a signal warrants a what-if, a")
    print("  person asks for the scenario, with the quantity they chose.")

    return finish(config)


def _delhi_network():
    """The test fixture network, if it can be reached from here."""
    try:
        import sys
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / "netgravity" / "tests" / "integration"))
        from conftest import build_delhi_network      # noqa: WPS433
        return build_delhi_network()
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
