"""
INDEPENDENT MANUAL HAND-RUN VERIFICATION
==========================================
This network is designed FROM SCRATCH (not reused from the repo's own test
fixtures) to simulate a mentor building their own small, hand-checkable
example and comparing it against the solver's output.

NETWORK
-------
1 mandatory Plant (P), 2 candidate DCs, 2 markets, 1 product.
No inventory cost, no carbon cost, no handling cost, no SLA filtering
-> isolates the result to pure Fixed Cost + Transport Cost so it can be
   solved by hand via simple enumeration (only 2^2 = 4 open/close states).

    P --(rate 2)--> DC1 --(rate 1)--> M1  (demand 300)
    P --(rate 2)--> DC1 --(rate 4)--> M2  (demand 300)
    P --(rate 2)--> DC2 --(rate 4)--> M1
    P --(rate 2)--> DC2 --(rate 1)--> M2

DC1: fixed cost $12,000/yr -> $1,000/month, capacity 600 units/month
DC2: fixed cost $21,600/yr -> $1,800/month, capacity 600 units/month

HAND CALCULATION (by enumeration of the 4 open/close states)
--------------------------------------------------------------
State (DC1=0, DC2=0): INFEASIBLE - no facility open, demand cannot be met,
                       shortage is disallowed by config -> excluded.

State (DC1=1, DC2=0): both markets forced through DC1 (only open DC).
    P->DC1:   600 units x $2 = $1,200
    DC1->M1:  300 units x $1 = $300
    DC1->M2:  300 units x $4 = $1,200
    Transport subtotal        = $2,700
    Fixed cost (DC1 only)      = $1,000
    TOTAL                      = $3,700

State (DC1=0, DC2=1): both markets forced through DC2 (only open DC).
    P->DC2:   600 units x $2 = $1,200
    DC2->M1:  300 units x $4 = $1,200
    DC2->M2:  300 units x $1 = $300
    Transport subtotal        = $2,700
    Fixed cost (DC2 only)      = $1,800
    TOTAL                      = $4,500

State (DC1=1, DC2=1): cheapest routing is M1 via DC1 ($1/unit),
                       M2 via DC2 ($1/unit) -- each DC only needs to
                       receive enough inbound plant volume for its own
                       onward market leg.
    P->DC1:   300 units x $2 = $600
    DC1->M1:  300 units x $1 = $300
    P->DC2:   300 units x $2 = $600
    DC2->M2:  300 units x $1 = $300
    Transport subtotal        = $1,800
    Fixed cost (DC1 + DC2)     = $1,000 + $1,800 = $2,800
    TOTAL                      = $4,600

HAND-CALCULATED OPTIMUM: open DC1 only, close DC2.
    Total cost = $3,700/month
    Facility utilisation: DC1 = 600/600 = 100%
    Demand fill rate = 100% (600/600 served)

This is the number the solver MUST reproduce, to the cent (subject to
solver numerical tolerance), for the codebase to be considered correct
by an independent hand check.
"""

import sys
sys.path.insert(0, ".")

from netgravity.schemas.network import (
    FacilityRecord, ProductRecord, DemandRecord, LaneRecord,
    CanonicalNetwork, OptimizationConfig, NodeRole, FacilityStatus, TransportMode,
)
from netgravity.optimization.milp import solve
from netgravity.validation.checks import validate_network
from netgravity.costs.reconciliation import reconcile_costs  # audit cross-check


def build_hand_run_network() -> CanonicalNetwork:
    facilities = [
        FacilityRecord(
            id="P", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, capacity_units_per_period=10_000,
            is_mandatory=True, is_closable=False, fixed_cost_per_year=0,
        ),
        FacilityRecord(
            id="DC1", name="Candidate DC 1", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE, capacity_units_per_period=600,
            is_mandatory=False, is_closable=True,
            fixed_cost_per_year=12_000, handling_cost_per_unit=0,
        ),
        FacilityRecord(
            id="DC2", name="Candidate DC 2", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE, capacity_units_per_period=600,
            is_mandatory=False, is_closable=True,
            fixed_cost_per_year=21_600, handling_cost_per_unit=0,
        ),
        FacilityRecord(id="M1", name="Market 1", role=NodeRole.MARKET,
                        status=FacilityStatus.EXISTING, is_mandatory=False, is_closable=False),
        FacilityRecord(id="M2", name="Market 2", role=NodeRole.MARKET,
                        status=FacilityStatus.EXISTING, is_mandatory=False, is_closable=False),
    ]
    products = [ProductRecord(id="SKU", name="Test SKU", weight_kg=1.0, unit_value=0.0, holding_rate=0.0)]
    demands = [
        DemandRecord(market_id="M1", product_id="SKU", quantity=300, std_dev=0),
        DemandRecord(market_id="M2", product_id="SKU", quantity=300, std_dev=0),
    ]
    lanes = [
        LaneRecord(origin_id="P", destination_id="DC1", mode=TransportMode.ROAD, rate_per_unit=2, distance_km=50, lead_time_days=1, lane_capacity=10_000),
        LaneRecord(origin_id="P", destination_id="DC2", mode=TransportMode.ROAD, rate_per_unit=2, distance_km=50, lead_time_days=1, lane_capacity=10_000),
        LaneRecord(origin_id="DC1", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=1, distance_km=20, lead_time_days=1, lane_capacity=10_000),
        LaneRecord(origin_id="DC1", destination_id="M2", mode=TransportMode.ROAD, rate_per_unit=4, distance_km=80, lead_time_days=1, lane_capacity=10_000),
        LaneRecord(origin_id="DC2", destination_id="M1", mode=TransportMode.ROAD, rate_per_unit=4, distance_km=80, lead_time_days=1, lane_capacity=10_000),
        LaneRecord(origin_id="DC2", destination_id="M2", mode=TransportMode.ROAD, rate_per_unit=1, distance_km=20, lead_time_days=1, lane_capacity=10_000),
    ]
    config = OptimizationConfig(
        enable_inventory=False,
        enable_carbon_cost=False,
        enforce_sla=False,
        allow_shortage=False,
        solver_name="HiGHS",
        mip_gap=0.0001,
    )
    return CanonicalNetwork(
        network_id="HAND_RUN_TEST",
        description="Independently hand-calculated test. Expected optimum = 3700 (DC1 only).",
        facilities=facilities, products=products, demands=demands, lanes=lanes, config=config,
    )


def main():
    net = build_hand_run_network()

    # Step 1: validation layer (must pass before the solver ever sees it)
    print("=" * 70)
    print("STEP 1 — VALIDATION LAYER (validation/checks.py)")
    print("=" * 70)
    report = validate_network(net)
    print(f"is_valid       : {report.is_valid}")
    print(f"total issues   : {len(report.issues)}  (errors={len(report.errors)}, warnings={len(report.warnings)})")
    for i in report.issues:
        print(f"  [{i.severity}] {i.code}: {i.description}")
    print("PASS -- no blocking errors" if report.is_valid else "FAIL -- blocking errors present")

    # Step 2: solve
    print()
    print("=" * 70)
    print("STEP 2 — SOLVE (optimization/milp.py, solver = HiGHS)")
    print("=" * 70)
    result = solve(net, net.config)
    print("Solver status         :", result.solver.status)
    print("Solver objective value:", result.solver.objective_value)
    print("MIP gap               :", getattr(result.solver, "mip_gap", None))
    print("Runtime (s)           :", getattr(result.solver, "runtime_seconds", None))

    print()
    print("Facility decisions:")
    for fd in result.facility_decisions:
        print(f"  {fd.facility_id:6s}  open={fd.is_open!s:5s}  throughput={fd.throughput_units:7.1f}  "
              f"capacity_util={fd.utilization_pct:6.2f}%")

    print()
    print("Flow decisions:")
    for fl in result.flow_decisions:
        print(f"  {fl.origin_id:5s} -> {fl.destination_id:5s}  qty={fl.flow_units:7.1f}  "
              f"rate={fl.rate_per_unit:5.2f}  cost=${fl.transport_cost:8.2f}")

    print()
    print("KPIs:")
    k = result.kpis
    print(f"  total_cost      = ${k.total_cost:,.2f}")
    print(f"  facility_cost   = ${k.facility_cost:,.2f}")
    print(f"  transport_cost  = ${k.transport_cost:,.2f}")
    print(f"  handling_cost   = ${k.handling_cost:,.2f}")
    print(f"  inventory_cost  = ${k.inventory_cost:,.2f}")
    print(f"  demand_fill_rate= {k.demand_fill_rate*100:.2f}%")
    print(f"  n_facilities_open = {k.n_facilities_open}")

    # Step 3: independent cost reconciliation (module cross-check, not just solver's own number)
    print()
    print("=" * 70)
    print("STEP 3 — INDEPENDENT COST RECONCILIATION (costs/reconciliation.py)")
    print("=" * 70)
    try:
        recon = reconcile_costs(result, net, net.config)
        print(recon)
    except Exception as e:
        print("Reconciliation module call raised (may need different signature):", repr(e))

    # Step 4: compare against hand calculation
    print()
    print("=" * 70)
    print("STEP 4 — COMPARISON: HAND CALCULATION vs SOLVER OUTPUT")
    print("=" * 70)
    expected_total = 3700.0
    expected_open = {"P": True, "DC1": True, "DC2": False}
    actual_total = result.solver.objective_value
    actual_open = {fd.facility_id: fd.is_open for fd in result.facility_decisions}

    print(f"Expected total cost : ${expected_total:,.2f}")
    print(f"Actual total cost   : ${actual_total:,.2f}")
    diff = abs(actual_total - expected_total)
    print(f"Absolute difference : ${diff:,.4f}")
    print(f"MATCH: {'YES' if diff < 0.01 else 'NO -- DISCREPANCY'}")

    print()
    print(f"Expected open/close : {expected_open}")
    print(f"Actual open/close   : { {k_: v for k_, v in actual_open.items() if k_ in expected_open} }")
    facility_match = all(actual_open.get(fid) == is_open for fid, is_open in expected_open.items())
    print(f"MATCH: {'YES' if facility_match else 'NO -- DISCREPANCY'}")

    print()
    if diff < 0.01 and facility_match:
        print("RESULT: Hand-run verification PASSED. Solver output matches independent manual calculation exactly.")
    else:
        print("RESULT: Hand-run verification FAILED. Investigate before mentor presentation.")


if __name__ == "__main__":
    main()
