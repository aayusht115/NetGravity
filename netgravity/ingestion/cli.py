"""
NetGravity — Ingestion CLI
===========================
The command a human actually runs. No UI required.

    # ingest and print a report
    python -m netgravity.ingestion --source data/mock/india

    # ingest, then hand the network to the MILP engine and print the result
    python -m netgravity.ingestion --source data/mock/india --solve

    # show what the AI adapters proposed and why
    python -m netgravity.ingestion --source data/mock/india --explain

    # parse and validate only; write nothing
    python -m netgravity.ingestion --source data/mock/india --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from netgravity.ingestion.config import STUB_MODE_BANNER, load_config
from netgravity.ingestion.ai.client import LLMCallError
from netgravity.ingestion.pipeline import run_ingestion
from netgravity.ingestion.validation.report import RULE, render


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m netgravity.ingestion",
        description="NetGravity data ingestion pipeline.",
    )
    p.add_argument("--source", "-s", default="data/mock/india",
                   help="source directory (default: data/mock/india)")
    p.add_argument("--label", "-l", default="",
                   help="human label recorded with the snapshot")
    p.add_argument("--solve", action="store_true",
                   help="after ingesting, run the MILP engine and print the result")
    p.add_argument("--explain", action="store_true",
                   help="show AI-proposed mappings and extracted contract rules")
    p.add_argument("--dry-run", action="store_true",
                   help="validate only; do not write any snapshot")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="show INFO-level issues too")
    p.add_argument("--strict", action="store_true",
                   help="treat warnings as failures (non-zero exit)")
    p.add_argument("--no-contracts", action="store_true", help="skip contract extraction")
    p.add_argument("--no-signals", action="store_true", help="skip external signals")
    p.add_argument("--no-distributors", action="store_true",
                   help="skip distributor file standardization")
    p.add_argument("--list-mappings", action="store_true",
                   help="list cached distributor column mappings and their status")
    p.add_argument("--confirm-mapping", metavar="DISTRIBUTOR_ID",
                   help="approve a distributor's cached column mapping; "
                        "later files from that distributor then skip the AI call")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(strict=args.strict)

    # Mapping administration — these short-circuit before any ingestion runs.
    if args.list_mappings:
        return _list_mappings(cfg)
    if args.confirm_mapping:
        return _confirm_mapping(cfg, args.confirm_mapping)

    print()
    print("NetGravity — Data Ingestion")
    print(RULE)
    print(cfg.describe())
    if cfg.stub_mode:
        print(f"  note            : {STUB_MODE_BANNER}")
    if cfg.key_warning:
        print(f"  WARNING         : {cfg.key_warning}")

    try:
        result = run_ingestion(
            Path(args.source),
            config=cfg,
            save=not args.dry_run,
            include_contracts=not args.no_contracts,
            include_signals=not args.no_signals,
            include_distributors=not args.no_distributors,
            label=args.label,
        )
    except LLMCallError as exc:
        # Strict mode chose to stop rather than quietly use canned data.
        # That is the intended outcome, so report it as a clean failure
        # rather than an unhandled crash.
        print()
        print("  INGESTION STOPPED — live AI extraction failed")
        print(f"  {exc}")
        print()
        print("  Fix the API key / connectivity, or unset NETGRAVITY_LLM_STRICT")
        print("  to allow the run to continue on clearly-labelled stub data.")
        print()
        return 1

    print(render(result.report, verbose=args.verbose))

    if args.explain:
        _print_explain(result)

    if args.solve:
        if result.network is None:
            print("  Cannot solve — no network was assembled.\n")
            return 1
        _solve(result.network)

    if not result.ok:
        return 1
    if args.strict and result.report.warnings:
        print("  STRICT MODE: warnings present — failing.\n")
        return 1
    return 0


def _list_mappings(cfg) -> int:
    """Show every cached distributor mapping and whether a human has approved it."""
    from netgravity.ingestion.adapters import distributor
    from netgravity.ingestion.storage import get_storage

    storage = get_storage(cfg)
    keys = storage.list("standardized", prefix="distributor_mappings/")

    print()
    print("Cached distributor column mappings")
    print(RULE)
    if not keys:
        print("  (none yet — run an ingestion over a distributors/ folder first)")
        print()
        return 0

    for key in keys:
        did = key.split("/")[-1].removesuffix(".json")
        m = distributor.load_cached_mapping(did, storage)
        if m is None:
            continue
        state = "CONFIRMED" if m.confirmed_by_human else "pending confirmation"
        print(f"\n  {did}   [{state}]   mean confidence {m.mean_confidence:.0%}")
        for cm in m.mappings:
            flag = "  <-- needs review" if cm.needs_review else ""
            print(f"      \"{cm.source_column}\" -> {cm.target_field:<14} "
                  f"{cm.confidence:.0%}{flag}")
            if cm.needs_review and cm.reasoning:
                print(f"          why: {cm.reasoning}")
        if m.unmapped_columns:
            print(f"      unmapped: {', '.join(m.unmapped_columns)}")

    print()
    print("  To approve one:  python -m netgravity.ingestion "
          "--confirm-mapping <id>")
    print()
    return 0


def _confirm_mapping(cfg, distributor_id: str) -> int:
    """
    Record human approval of a mapping.

    This is the human-in-the-loop step for ingestion: the model proposes, a
    person approves, and only then is the mapping trusted without review.
    """
    from netgravity.ingestion.adapters import distributor
    from netgravity.ingestion.storage import get_storage

    storage = get_storage(cfg)
    mapping = distributor.load_cached_mapping(distributor_id, storage)

    if mapping is None:
        print(f"\n  No cached mapping found for '{distributor_id}'.")
        print("  Run --list-mappings to see what is available.\n")
        return 1

    print()
    print(f"Confirming mapping for '{distributor_id}'")
    print(RULE)
    for cm in mapping.mappings:
        conv = (f"   [x{cm.conversion_factor:g} {cm.source_unit}->{cm.target_unit}]"
                if cm.conversion_factor != 1.0 else "")
        print(f"  \"{cm.source_column}\" -> {cm.target_field:<14} "
              f"{cm.confidence:.0%}{conv}")

    distributor.confirm_mapping(distributor_id, storage)
    print()
    print(f"  Confirmed. Future files from '{distributor_id}' will reuse this "
          f"mapping with no AI call.")
    print()
    return 0


def _print_explain(result) -> None:
    print("AI extraction detail")
    print(RULE)

    for m in getattr(result, "distributor_mappings", []):
        mode = "STUBBED" if m.proposed_by == "stub" else f"live via {m.proposed_by}"
        state = "confirmed" if m.confirmed_by_human else "PENDING human confirmation"
        print(f"\n  Distributor '{m.distributor_id}'   ({mode}, {state})")
        print(f"    mean confidence: {m.mean_confidence:.0%}  ->  {m.target_entity}")
        for cm in m.mappings:
            flag = "  ⚠ review" if cm.needs_review else ""
            conv = (f"   [x{cm.conversion_factor:g} {cm.source_unit}->{cm.target_unit}]"
                    if cm.conversion_factor != 1.0 else "")
            print(f"      \"{cm.source_column}\" -> {cm.target_field:<14} "
                  f"{cm.confidence:.0%}{conv}{flag}")
        if m.unmapped_columns:
            print(f"      unmapped: {', '.join(m.unmapped_columns)}")

    if not result.contracts:
        print("  (no contracts ingested)")
    for c in result.contracts:
        mode = "STUBBED" if c.extracted_by == "stub" else f"live via {c.extracted_by}"
        print(f"\n  {c.vendor_name}  [{c.contract_id}]   ({mode})")
        print(f"    headline rate : {c.base_rate:g} {c.rate_unit}")
        for s in c.surcharges:
            scope = "all locations"
            if s.applies_to_location_ids:
                scope = f"{len(s.applies_to_location_ids)} location(s): " \
                        f"{', '.join(s.applies_to_location_ids)}"
            elif s.applies_to_pin_codes:
                scope = f"{len(s.applies_to_pin_codes)} pin code(s)"
            print(f"    + {s.surcharge_type.value:<12} {s.rate:g} {s.rate_unit}  "
                  f"[{s.confidence.value}]  -> {scope}")
            if s.source_excerpt:
                print(f"        source: \"{s.source_excerpt[:88]}\"")
        if c.has_hidden_cost:
            print("    ⚠ conditional surcharge present — headline rate understates true cost")

    if result.signals:
        print("\n  External signals")
        for s in result.signals:
            v = s.verdict
            mark = "✓ passed " if s.passed_guardrail else "✗ filtered"
            score = f"{v.relevance_score:.2f}/{v.threshold:.2f}" if v else "n/a"
            print(f"    {mark} [{s.bucket.value:<10}] {score}  {s.title}")
            if v and v.reason:
                print(f"        {v.reason}")
    print()


def _solve(network) -> None:
    """
    The real acceptance test: hand ingested data to the existing MILP engine.

    Until now the engine had only ever solved its hardcoded synthetic fixture.
    A cost coming out here means the whole chain — CSV to optimum — works,
    with no UI involved.
    """
    print("MILP solve on ingested network")
    print(RULE)
    try:
        from netgravity.optimization.milp import solve
    except ImportError as exc:
        print(f"  Could not import the solver: {exc}")
        print("  (ingestion succeeded — this is a wiring issue only)\n")
        return

    try:
        result = solve(network)
    except Exception as exc:
        print(f"  Solver raised: {type(exc).__name__}: {exc}\n")
        return

    print(f"  status        : {result.solver.status.value}")

    kpis = result.kpis
    if kpis is not None:
        print(f"  total cost    : {kpis.total_cost:,.2f} / period")
        print(f"    transport   : {kpis.transport_cost:,.2f}")
        print(f"    facility    : {kpis.facility_cost:,.2f}")
        print(f"    handling    : {kpis.handling_cost:,.2f}")
        print(f"    inventory   : {kpis.inventory_cost:,.2f}")
        print(f"  demand served : {kpis.total_served:,.0f} of {kpis.total_demand:,.0f}")
        if kpis.unmet_demand > 0:
            print(f"  UNMET demand  : {kpis.unmet_demand:,.0f}")

    open_f = result.get_open_facilities()
    if open_f:
        names = ", ".join(sorted(fd.facility_id for fd in open_f))
        print(f"  open facilities ({len(open_f)}): {names}")

    closed = result.get_closed_facilities()
    if closed:
        names = ", ".join(sorted(fd.facility_id for fd in closed))
        print(f"  not opened ({len(closed)}): {names}")
    print()


if __name__ == "__main__":
    sys.exit(main())
