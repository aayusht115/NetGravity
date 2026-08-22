#!/usr/bin/env python
"""
Phase 3.1 — run the NLU evaluation.

    python scripts/run_nlu_eval.py                    # offline, free
    python scripts/run_nlu_eval.py --live --budget 20 # real gateway, batched
    python scripts/run_nlu_eval.py --usage            # what is left, no spend

BUDGET IS THE FIRST-CLASS CONCERN
─────────────────────────────────
Gateway capacity is SHARED across every consumer, capped at 100 requests/day
and a cumulative USD budget that does not reset. A careless sweep spends other
people's allowance permanently. So:

* `--budget N` is a hard ceiling enforced locally, before any request is sent.
* Live mode refuses to start without it.
* Batching is the default in live mode: ten utterances per call turns a 159-case
  sweep into ~16 requests instead of 159.
* A small single-utterance CONTROL set runs alongside, so the report can state
  whether batching distorted the result rather than assuming it did not.

CREDENTIALS
───────────
Read from `TEXT_API_URL` / `TEXT_API_TOKEN` only. Never printed, never written
to a file, never passed on the command line — a token in argv is visible to
every process on the machine and lands in shell history.

A gitignored `.env` beside the repo root is loaded if present, purely so the
value can be set once without being typed into a terminal. Existing environment
variables always win; the file never overrides what is already set.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netgravity.orchestrator.agents.intent_agent import IntentAgent          # noqa: E402
from netgravity.orchestrator.agents.llm_gateway import LLMGateway            # noqa: E402
from netgravity.orchestrator.conversation.nlu import ConversationalNLU       # noqa: E402
from netgravity.tests.integration.conftest import build_delhi_network        # noqa: E402
from netgravity.tests.nlu_eval.dataset import CASES, Category, composition   # noqa: E402
from netgravity.tests.nlu_eval.harness import (                              # noqa: E402
    Mode,
    aggregate,
    failures,
    format_report,
    run_batch,
    run_llm_tier,
    run_system,
    to_json,
)

#: Utterances per gateway call in batched mode. Ten keeps the prompt well under
#: the 100k character limit and the response well under 2,000 output tokens.
BATCH_SIZE = 10

#: Cases run one-per-call to check that batching did not distort the result.
CONTROL_IDS = ("st01", "ns01", "ex01", "sc04", "rs13", "ee04", "fc01", "ad01")


def _chunks(items: Sequence, size: int) -> List[List]:
    return [list(items[i:i + size]) for i in range(0, len(items), size)]


def load_dotenv() -> None:
    """
    Populate TEXT_API_* from a gitignored `.env`, without overriding the shell.

    Only the two gateway variables are read. Anything already exported wins, so
    a deliberate `TEXT_API_TOKEN= python …` still disables the gateway.
    """
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in ("TEXT_API_URL", "TEXT_API_TOKEN", "TEXT_API_MODEL") \
                and not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="Call the real gateway. Spends shared budget.")
    ap.add_argument("--budget", type=int, default=0,
                    help="Hard ceiling on gateway requests. Required with --live.")
    ap.add_argument("--single", action="store_true",
                    help="One request per case instead of batching. Expensive.")
    ap.add_argument("--usage", action="store_true",
                    help="Print remaining shared capacity and exit. No generation.")
    ap.add_argument("--max-controls", type=int, default=len(CONTROL_IDS),
                    help="Cap the single-utterance control set. Exists only for "
                         "days when the SHARED daily quota is nearly spent: the "
                         "batched sweep always covers all cases, so trimming "
                         "controls loses a diagnostic, never a case. Controls "
                         "are taken in fixed list order, not chosen after "
                         "seeing results.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Write per-case JSON results here.")
    args = ap.parse_args()

    load_dotenv()
    network = build_delhi_network()

    if args.usage:
        gateway = LLMGateway()
        if not gateway.available:
            print(f"gateway unavailable: {gateway.unavailable_reason()}")
            return 1
        print("health:", gateway.health())
        print("usage:", gateway.usage())
        return 0

    # ---- offline baseline: always runs, always free ------------------------
    nlu = ConversationalNLU()
    offline = [run_system(c, network, nlu, allow_llm=False) for c in CASES]
    print(format_report(aggregate(offline, Mode.SYSTEM)))
    print()
    print("dataset composition:", composition())

    if not args.live:
        _dump(args.out, offline)
        _print_failures(offline)
        return 0

    # ---- live ---------------------------------------------------------------
    if args.budget <= 0:
        print("\nRefusing to run live without --budget N. Gateway capacity is "
              "shared and its cumulative cost does not reset.", file=sys.stderr)
        return 2

    gateway = LLMGateway()
    if not gateway.available:
        print(f"\ngateway unavailable: {gateway.unavailable_reason()}", file=sys.stderr)
        return 3

    # The gateway's own per-instance guard defaults to 4; raise it to exactly
    # the budget the operator authorised, and no further.
    gateway.config.max_requests_per_execution = args.budget

    planned_batches = _chunks(list(CASES), BATCH_SIZE)
    kept = CONTROL_IDS[:max(0, args.max_controls)]
    control = [c for c in CASES if c.id in kept]
    planned = len(planned_batches) + len(control)
    if args.single:
        planned = len(CASES)

    if planned > args.budget:
        print(f"\nPlanned {planned} gateway requests but the budget is "
              f"{args.budget}. Reduce the dataset or raise --budget "
              f"deliberately.", file=sys.stderr)
        return 4

    print(f"\nLive run: {planned} gateway requests "
          f"({'single' if args.single else f'{len(planned_batches)} batches + '
             f'{len(control)} single controls'}).")

    live: List = []
    if args.single:
        agent = IntentAgent(gateway)
        for case in CASES:
            live.append(run_llm_tier(case, network, agent))
        mode = Mode.LLM_TIER
    else:
        for batch in planned_batches:
            live.extend(run_batch(batch, network, gateway))
        mode = Mode.BATCHED

    print()
    print(format_report(aggregate(live, mode)))

    # ---- batching control --------------------------------------------------
    if not args.single and control:
        agent = IntentAgent(gateway)
        singles = [run_llm_tier(c, network, agent) for c in control]
        by_id = {o.case_id: o for o in live}
        agree = sum(
            1 for s in singles
            if by_id.get(s.case_id) is not None
            and by_id[s.case_id].observed_intent == s.observed_intent
        )
        print()
        print(f"batching control: {agree}/{len(singles)} single-call results "
              f"agree with the batched result.")
        for s in singles:
            b = by_id.get(s.case_id)
            flag = "" if b and b.observed_intent == s.observed_intent else "   <-- differs"
            print(f"  {s.case_id:<6} batched={b.observed_intent if b else None:<22} "
                  f"single={s.observed_intent}{flag}")
        live.extend(singles)

    print()
    print("gateway stats:", gateway.stats())
    _dump(args.out, offline + live)
    _print_failures(live)
    return 0


def _print_failures(observations) -> None:
    bad = failures(observations)
    if not bad:
        print("\nno failing cases.")
        return
    print(f"\n{len(bad)} case(s) did not match the label:")
    by_case = {c.id: c for c in CASES}
    for obs in bad:
        case = by_case.get(obs.case_id)
        detail = ", ".join(obs.failed()) or ", ".join(obs.violations) or obs.error or ""
        print(f"  [{obs.case_id}] {obs.text[:64]!r}")
        print(f"        expected intent={case.intent.value if case and case.intent else '-'} "
              f"entities={list(case.entity_ids) if case else []} "
              f"clarity={case.clarity.value if case else '-'} "
              f"ambiguity={case.ambiguity.value if case else '-'}")
        print(f"        observed intent={obs.observed_intent} "
              f"entities={list(obs.observed_entity_ids)} "
              f"clarity={obs.observed_clarity} ambiguity={obs.observed_ambiguity}")
        print(f"        failed: {detail}")
        if case and case.note:
            print(f"        note: {case.note}")


def _dump(path, observations) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_json(observations), encoding="utf-8")
    print(f"\nwrote {len(observations)} observations to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
