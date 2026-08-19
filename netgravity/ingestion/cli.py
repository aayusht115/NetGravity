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
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(strict=args.strict)

    print()
    print("NetGravity — Data Ingestion")
    print(RULE)
    print(cfg.describe())
    if cfg.stub_mode:
        print(f"  note            : {STUB_MODE_BANNER}")

    result = run_ingestion(
        Path(args.source),
        config=cfg,
        save=not args.dry_run,
        include_contracts=not args.no_contracts,
        include_signals=not args.no_signals,
        include_distributors=not args.no_distributors,
        label=args.label,
    )

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
