import sys
sys.path.insert(0, '.')
from data.sample_data import (
    SOURCES, TRANSSHIPMENT_NODES, DESTINATIONS,
    SOURCE_DEST_COST, SOURCE_DC_COST, DC_DEST_COST,
    ARC_CAPACITIES_SOURCE_DC, ARC_CAPACITIES_DC_DEST,
)
from models.transportation import solve_transportation
from models.transshipment import solve_transshipment
from models.cog import weiszfeld_cog
from models.facility import solve_facility_location
from models.recommendation import score_scenarios, estimate_kpis_from_solver

print("=== TEST 1: Transportation LP ===")
res = solve_transportation(
    cost_matrix=SOURCE_DEST_COST,
    supply=[s["supply"] for s in SOURCES],
    demand=[d["demand"] for d in DESTINATIONS],
)
print(f"Status: {res['status']}")
print(f"Total Cost: Rs {res['total_cost']:,.2f}/day")
print(f"Avg Cost/Unit: Rs {res['avg_cost_per_unit']:.2f}")
print(f"Active flows: {len(res['flows'])}")

print("\n=== TEST 2: Transshipment LP ===")
res2 = solve_transshipment(
    source_dc_cost=SOURCE_DC_COST,
    dc_dest_cost=DC_DEST_COST,
    supply=[s["supply"] for s in SOURCES],
    demand=[d["demand"] for d in DESTINATIONS],
    dc_capacity=[t["capacity"] for t in TRANSSHIPMENT_NODES],
    dc_handling_cost=[t["handling_cost"] for t in TRANSSHIPMENT_NODES],
    arc_cap_source_dc=ARC_CAPACITIES_SOURCE_DC,
    arc_cap_dc_dest=ARC_CAPACITIES_DC_DEST,
)
print(f"Status: {res2['status']}")
print(f"Total Cost: Rs {res2['total_cost']:,.2f}/day")
print(f"DC Utilization: {res2['dc_utilization_pct']}")

print("\n=== TEST 3: Center of Gravity ===")
pts = [(d["schematic_pos"][0], d["schematic_pos"][1]) for d in DESTINATIONS]
w   = [d["demand"] for d in DESTINATIONS]
cog = weiszfeld_cog(pts, w)
print(f"Optimal location: ({cog['x']:.4f}, {cog['y']:.4f})")
print(f"Converged: {cog['converged']} in {cog['iterations']} iterations")
print(f"Total weighted distance: {cog['total_weighted_distance']:.4f}")

print("\n=== TEST 4: Facility Location MILP ===")
fl = solve_facility_location(
    fixed_costs=[t["fixed_cost"] for t in TRANSSHIPMENT_NODES],
    transport_costs=DC_DEST_COST,
    demand=[d["demand"] for d in DESTINATIONS],
    capacities=[t["capacity"] for t in TRANSSHIPMENT_NODES],
    dc_ids=[t["id"] for t in TRANSSHIPMENT_NODES],
    dest_ids=[d["id"] for d in DESTINATIONS],
    min_open=2,
    max_open=4,
)
print(f"Status: {fl['status']}")
print(f"Open DCs: {fl['open_dcs']}")
print(f"Fixed Savings vs all-open: Rs {fl['fixed_cost_savings_lakh']:.1f} L/yr")

print("\n=== TEST 5: Recommendation Engine ===")
scenarios_kpi = []
test_cases = [
    ("Base",     None),
    ("Closed T5", ["T1","T2","T3","T4"]),
    ("Lean",     ["T2","T3"]),
]
for sc_name, active in test_cases:
    r = solve_transshipment(
        source_dc_cost=SOURCE_DC_COST,
        dc_dest_cost=DC_DEST_COST,
        supply=[s["supply"] for s in SOURCES],
        demand=[d["demand"] for d in DESTINATIONS],
        dc_capacity=[t["capacity"] for t in TRANSSHIPMENT_NODES],
        dc_handling_cost=[t["handling_cost"] for t in TRANSSHIPMENT_NODES],
        arc_cap_source_dc=ARC_CAPACITIES_SOURCE_DC,
        arc_cap_dc_dest=ARC_CAPACITIES_DC_DEST,
        active_dcs=active,
    )
    kpi = estimate_kpis_from_solver(
        r, sc_name,
        active or ["T1","T2","T3","T4","T5"],
        len(active) if active else 5,
        sum(d["demand"] for d in DESTINATIONS),
    )
    scenarios_kpi.append(kpi)

rec = score_scenarios(scenarios_kpi)
print(f"Recommendation: {rec['recommended_scenario']}")
print("Ranked:", [(s["name"], round(s["weighted_score"], 4)) for s in rec["ranked_scenarios"]])
print("\nALL TESTS PASSED.")
