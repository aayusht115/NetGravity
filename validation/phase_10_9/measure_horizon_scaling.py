#!/usr/bin/env python
"""
How the multi-period MILP scales with the length of the horizon.

    python validation/phase_10_9/measure_horizon_scaling.py

Why this measurement
--------------------
Modelling every period instead of collapsing them multiplies the flow variables
by the number of periods. Whether that matters is the first question an operator
has about the change, and "it should be fine" is not an answer — the worker
timeout in `docs/operations.md` is set against the longest solve a deployment
will see.

So this solves the same network at 1, 3, 6, 12 and 24 periods and reports the
model size and wall time for each, alongside the collapsed policies for
comparison. A collapse is always a one-period model whatever the data states,
which is exactly why it is cheap and exactly why it cannot answer a seasonality
question.

The absolute times are this machine's. The SHAPE — how time grows with T — is
the transferable finding.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from netgravity.optimization.milp import milp_solve                # noqa: E402
from netgravity.schemas.network import (                            # noqa: E402
    CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus, LaneRecord,
    NodeRole, OptimizationConfig, ProductRecord, TransportMode,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network  # noqa: E402

OUT = pathlib.Path(__file__).with_name("horizon_scaling.json")


def seasonal(base: float, period: int, n: int) -> float:
    """A demand curve with a real peak, so carryover has something to do."""
    import math
    return base * (1.0 + 0.4 * math.sin(2.0 * math.pi * (period - 1) / max(n, 1)))


def widen_to_horizon(network: CanonicalNetwork, periods: int) -> CanonicalNetwork:
    """The same network with its demand restated over `periods` periods."""
    rows = []
    for demand in network.demands:
        for p in range(1, periods + 1):
            rows.append(demand.model_copy(update={
                "period": p,
                "quantity": round(seasonal(demand.quantity, p, periods), 4),
            }))
    return network.model_copy(update={"demands": rows})


def solve_once(network: CanonicalNetwork, policy: str) -> dict:
    config = network.config.model_copy(update={
        "multi_period_policy": policy, "allow_shortage": True, "verbose": False,
    })
    started = time.perf_counter()
    result = milp_solve(network, config)
    seconds = time.perf_counter() - started
    report = result.period_report or {}
    served = sum(f.flow_units for f in result.flow_decisions)
    return {
        "policy": policy,
        "periods_in_data": report.get("n_periods", 1),
        "periods_modelled": report.get("modelled_periods", 1),
        "variables": result.solver.n_variables,
        "constraints": result.solver.n_constraints,
        "seconds": round(seconds, 3),
        "status": result.solver.status.value,
        "objective": round(result.solver.objective_value or 0.0, 2),
        "flow_rows": len(result.flow_decisions),
        "stock_rows": len(result.inventory_decisions),
        "units_moved": round(served, 2),
    }


def main() -> int:
    base = build_case16_network()
    print(f"network: {len(base.facilities)} facilities, {len(base.lanes)} lanes, "
          f"{len(base.demands)} demand rows, {len(base.products)} products")
    print()

    rows = []
    print(f"{'periods':>8} {'policy':<20} {'vars':>7} {'cons':>7} {'seconds':>8} "
          f"{'stock rows':>11} {'status':>10}")
    print("-" * 80)

    for periods in (1, 3, 6, 12, 24):
        network = widen_to_horizon(base, periods)
        policies = ["FULL_HORIZON"]
        # The collapse policies exist to be cheaper; measure that claim too.
        if periods > 1:
            policies += ["PEAK", "REPRESENTATIVE_MEAN"]
        for policy in policies:
            row = solve_once(network, policy)
            row["periods_requested"] = periods
            rows.append(row)
            print(f"{periods:>8} {policy:<20} {row['variables']:>7} "
                  f"{row['constraints']:>7} {row['seconds']:>8.3f} "
                  f"{row['stock_rows']:>11} {row['status']:>10}")

    full = [r for r in rows if r["policy"] == "FULL_HORIZON"]
    one = next((r for r in full if r["periods_requested"] == 1), None)
    longest = full[-1] if full else None

    print()
    print("Findings")
    print("-" * 80)
    if one and longest and one["seconds"] > 0:
        factor = longest["seconds"] / one["seconds"]
        var_factor = longest["variables"] / max(one["variables"], 1)
        print(f"  {longest['periods_requested']} periods vs 1: "
              f"{var_factor:.1f}x the variables, {factor:.1f}x the time")
    for r in full:
        print(f"  {r['periods_requested']:>2} periods -> {r['seconds']:.3f} s")
    collapsed = [r for r in rows if r["policy"] == "PEAK"]
    if collapsed:
        print(f"  a collapse policy stays a one-period model at every horizon: "
              f"{collapsed[-1]['variables']} variables, "
              f"{collapsed[-1]['seconds']:.3f} s at "
              f"{collapsed[-1]['periods_requested']} periods")

    slowest = max((r["seconds"] for r in rows), default=0.0)
    print()
    print(f"  slowest solve measured     : {slowest:.2f} s")
    print(f"  documented worker timeout  : 300 s "
          f"({300 / slowest:.0f}x the slowest solve here)" if slowest else "")
    print("  This is one fixture on one machine. A client network an order of "
          "magnitude larger should be measured before its timeout is trusted.")

    OUT.write_text(json.dumps({
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "network": {
            "facilities": len(base.facilities), "lanes": len(base.lanes),
            "products": len(base.products), "demand_rows": len(base.demands),
        },
        "runs": rows,
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
