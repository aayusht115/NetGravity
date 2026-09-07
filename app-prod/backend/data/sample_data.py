"""
NetGravity — Fabricated Sample Data
====================================
3 Supply Sources  |  5 Candidate DCs (Transshipment)  |  10 Demand Destinations
All numbers are fabricated for demonstration purposes only.
Final solution will be stress-tested against real client data.
"""

# ---------------------------------------------------------------------------
# SUPPLY NODES (3 Plants / Factories)
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "id": "S1",
        "name": "Mumbai Plant",
        "type": "source",
        "supply": 2200,           # units/day
        "unit": "units/day",
        "schematic_pos": (0, 3),  # (col, row) for schematic layout
        "color": "#6366f1",
        "city": "Mumbai",
        "region": "West",
    },
    {
        "id": "S2",
        "name": "Ahmedabad Plant",
        "type": "source",
        "supply": 1800,
        "unit": "units/day",
        "schematic_pos": (0, 6),
        "color": "#6366f1",
        "city": "Ahmedabad",
        "region": "West",
    },
    {
        "id": "S3",
        "name": "Hyderabad Plant",
        "type": "source",
        "supply": 1500,
        "unit": "units/day",
        "schematic_pos": (0, 9),
        "color": "#6366f1",
        "city": "Hyderabad",
        "region": "South",
    },
]

# ---------------------------------------------------------------------------
# TRANSSHIPMENT NODES (5 Candidate Distribution Centers)
# ---------------------------------------------------------------------------
TRANSSHIPMENT_NODES = [
    {
        "id": "T1",
        "name": "Pune DC",
        "type": "dc",
        "capacity": 1200,         # units/day throughput
        "fixed_cost": 85,         # ₹ lakh/year
        "handling_cost": 4.5,     # ₹ per unit
        "schematic_pos": (5, 2),
        "color": "#f59e0b",
        "city": "Pune",
        "region": "West",
    },
    {
        "id": "T2",
        "name": "Nagpur DC",
        "type": "dc",
        "capacity": 1000,
        "fixed_cost": 70,
        "handling_cost": 3.8,
        "schematic_pos": (5, 4),
        "color": "#f59e0b",
        "city": "Nagpur",
        "region": "Central",
    },
    {
        "id": "T3",
        "name": "Indore DC",
        "type": "dc",
        "capacity": 800,
        "fixed_cost": 60,
        "handling_cost": 3.5,
        "schematic_pos": (5, 6),
        "color": "#f59e0b",
        "city": "Indore",
        "region": "Central",
    },
    {
        "id": "T4",
        "name": "Bhopal DC",
        "type": "dc",
        "capacity": 900,
        "fixed_cost": 65,
        "handling_cost": 3.6,
        "schematic_pos": (5, 8),
        "color": "#f59e0b",
        "city": "Bhopal",
        "region": "Central",
    },
    {
        "id": "T5",
        "name": "Raipur DC",
        "type": "dc",
        "capacity": 700,
        "fixed_cost": 55,
        "handling_cost": 3.2,
        "schematic_pos": (5, 10),
        "color": "#f59e0b",
        "city": "Raipur",
        "region": "East",
    },
]

# ---------------------------------------------------------------------------
# DEMAND NODES (10 Customer Zones)
# ---------------------------------------------------------------------------
DESTINATIONS = [
    {
        "id": "D1",
        "name": "New Delhi Zone",
        "type": "destination",
        "demand": 600,            # units/day
        "priority": "High",
        "sla_days": 2,            # max delivery days
        "schematic_pos": (10, 1),
        "color": "#22c55e",
        "city": "New Delhi",
        "region": "North",
    },
    {
        "id": "D2",
        "name": "Kolkata Zone",
        "type": "destination",
        "demand": 480,
        "priority": "High",
        "sla_days": 3,
        "schematic_pos": (10, 2),
        "color": "#22c55e",
        "city": "Kolkata",
        "region": "East",
    },
    {
        "id": "D3",
        "name": "Chennai Zone",
        "type": "destination",
        "demand": 350,
        "priority": "Medium",
        "sla_days": 3,
        "schematic_pos": (10, 3),
        "color": "#22c55e",
        "city": "Chennai",
        "region": "South",
    },
    {
        "id": "D4",
        "name": "Bangalore Zone",
        "type": "destination",
        "demand": 420,
        "priority": "High",
        "sla_days": 2,
        "schematic_pos": (10, 4),
        "color": "#22c55e",
        "city": "Bangalore",
        "region": "South",
    },
    {
        "id": "D5",
        "name": "Jaipur Zone",
        "type": "destination",
        "demand": 250,
        "priority": "Medium",
        "sla_days": 3,
        "schematic_pos": (10, 5),
        "color": "#22c55e",
        "city": "Jaipur",
        "region": "North",
    },
    {
        "id": "D6",
        "name": "Lucknow Zone",
        "type": "destination",
        "demand": 310,
        "priority": "Medium",
        "sla_days": 3,
        "schematic_pos": (10, 6),
        "color": "#22c55e",
        "city": "Lucknow",
        "region": "North",
    },
    {
        "id": "D7",
        "name": "Patna Zone",
        "type": "destination",
        "demand": 220,
        "priority": "Low",
        "sla_days": 4,
        "schematic_pos": (10, 7),
        "color": "#22c55e",
        "city": "Patna",
        "region": "East",
    },
    {
        "id": "D8",
        "name": "Bhubaneswar Zone",
        "type": "destination",
        "demand": 190,
        "priority": "Low",
        "sla_days": 4,
        "schematic_pos": (10, 8),
        "color": "#22c55e",
        "city": "Bhubaneswar",
        "region": "East",
    },
    {
        "id": "D9",
        "name": "Coimbatore Zone",
        "type": "destination",
        "demand": 210,
        "priority": "Medium",
        "sla_days": 3,
        "schematic_pos": (10, 9),
        "color": "#22c55e",
        "city": "Coimbatore",
        "region": "South",
    },
    {
        "id": "D10",
        "name": "Surat Zone",
        "type": "destination",
        "demand": 280,
        "priority": "Medium",
        "sla_days": 2,
        "schematic_pos": (10, 10),
        "color": "#22c55e",
        "city": "Surat",
        "region": "West",
    },
]

# ---------------------------------------------------------------------------
# TRANSPORT COST MATRICES (₹ per unit)
# ---------------------------------------------------------------------------
# Source → Destination DIRECT costs (3 × 10 matrix)
# Rows: S1=Mumbai, S2=Ahmedabad, S3=Hyderabad
# Cols: D1=Delhi, D2=Kolkata, D3=Chennai, D4=Bangalore, D5=Jaipur,
#       D6=Lucknow, D7=Patna, D8=Bhubaneswar, D9=Coimbatore, D10=Surat
SOURCE_DEST_COST = [
    #  D1     D2     D3     D4     D5     D6     D7     D8     D9    D10
    [  95,    82,    65,    62,    78,    88,    90,    84,    68,    42],  # S1 Mumbai
    [  82,    90,    78,    75,    65,    79,    85,    88,    80,    38],  # S2 Ahmedabad
    [  88,    75,    48,    42,    85,    72,    78,    65,    45,    70],  # S3 Hyderabad
]

# Source → DC costs (3 × 5 matrix)
# Rows: S1=Mumbai, S2=Ahmedabad, S3=Hyderabad
# Cols: T1=Pune, T2=Nagpur, T3=Indore, T4=Bhopal, T5=Raipur
SOURCE_DC_COST = [
    #  T1    T2    T3    T4    T5
    [  18,   38,   35,   42,   55],   # S1 Mumbai
    [  32,   45,   22,   28,   58],   # S2 Ahmedabad
    [  40,   32,   48,   42,   38],   # S3 Hyderabad
]

# DC → Destination costs (5 × 10 matrix)
# Rows: T1=Pune, T2=Nagpur, T3=Indore, T4=Bhopal, T5=Raipur
# Cols: D1..D10
DC_DEST_COST = [
    #  D1    D2    D3    D4    D5    D6    D7    D8    D9   D10
    [  78,   72,   48,   45,   65,   75,   78,   70,   50,   28],  # T1 Pune
    [  55,   48,   58,   62,   52,   45,   42,   40,   65,   52],  # T2 Nagpur
    [  48,   62,   70,   72,   32,   42,   55,   68,   75,   42],  # T3 Indore
    [  45,   58,   68,   70,   35,   38,   48,   62,   72,   48],  # T4 Bhopal
    [  58,   38,   65,   68,   48,   32,   28,   32,   72,   62],  # T5 Raipur
]

# Arc capacity constraints (units/day) — for maximal flow model
# None means uncapacitated (very large value)
ARC_CAPACITIES_SOURCE_DC = [
    #  T1    T2    T3    T4    T5
    [  800,  600,  500,  400,  350],  # S1 Mumbai
    [  700,  500,  600,  450,  300],  # S2 Ahmedabad
    [  500,  600,  400,  500,  450],  # S3 Hyderabad
]

ARC_CAPACITIES_DC_DEST = [
    #  D1    D2    D3    D4    D5    D6    D7    D8    D9   D10
    [  300,  250,  200,  220,  180,  200,  150,  150,  180, 200],  # T1 Pune
    [  280,  240,  200,  210,  180,  220,  180,  170,  160, 190],  # T2 Nagpur
    [  250,  200,  180,  190,  200,  200,  160,  150,  150, 200],  # T3 Indore
    [  250,  210,  180,  190,  200,  220,  170,  160,  150, 190],  # T4 Bhopal
    [  220,  250,  170,  180,  160,  200,  200,  200,  140, 160],  # T5 Raipur
]

# ---------------------------------------------------------------------------
# SCENARIO PARAMETERS (for scenario builder)
# ---------------------------------------------------------------------------
BASE_SCENARIO = {
    "name": "Base Case",
    "description": "Optimal routing using all 5 candidate DCs",
    "active_dcs": ["T1", "T2", "T3", "T4", "T5"],
    "demand_multiplier": 1.0,
    "transport_cost_multiplier": 1.0,
    "carbon_factor": 1.0,        # kg CO2 per unit-km
}

SCENARIOS = [
    {
        "name": "Low Cost",
        "description": "Minimize total transport and facility cost; may sacrifice service",
        "active_dcs": ["T2", "T3", "T4"],
        "demand_multiplier": 1.0,
        "transport_cost_multiplier": 0.95,
        "carbon_factor": 1.05,
    },
    {
        "name": "High Service",
        "description": "Maximize OTIF / service level; all DCs open, priority routing",
        "active_dcs": ["T1", "T2", "T3", "T4", "T5"],
        "demand_multiplier": 1.0,
        "transport_cost_multiplier": 1.10,
        "carbon_factor": 0.98,
    },
    {
        "name": "Low Carbon",
        "description": "Minimize carbon footprint; consolidated, shorter routes preferred",
        "active_dcs": ["T1", "T2", "T4"],
        "demand_multiplier": 1.0,
        "transport_cost_multiplier": 1.05,
        "carbon_factor": 0.75,
    },
    {
        "name": "Resilient",
        "description": "Dual-source critical lanes; reduce single-point-of-failure risk",
        "active_dcs": ["T1", "T2", "T3", "T4", "T5"],
        "demand_multiplier": 1.0,
        "transport_cost_multiplier": 1.08,
        "carbon_factor": 1.02,
    },
]

# Recommendation weights (user-configurable; sum = 1.0)
DEFAULT_WEIGHTS = {
    "cost":       0.40,
    "service":    0.30,
    "resilience": 0.20,
    "carbon":     0.10,
}

# ---------------------------------------------------------------------------
# HELPER: Build node/edge lists for frontend graph rendering
# ---------------------------------------------------------------------------
def get_graph_data():
    nodes = []
    for s in SOURCES:
        nodes.append({"id": s["id"], "name": s["name"], "type": "source",
                       "supply": s["supply"], "pos": s["schematic_pos"],
                       "color": s["color"], "city": s["city"], "region": s["region"]})
    for t in TRANSSHIPMENT_NODES:
        nodes.append({"id": t["id"], "name": t["name"], "type": "dc",
                       "capacity": t["capacity"], "fixed_cost": t["fixed_cost"],
                       "pos": t["schematic_pos"], "color": t["color"],
                       "city": t["city"], "region": t["region"]})
    for d in DESTINATIONS:
        nodes.append({"id": d["id"], "name": d["name"], "type": "destination",
                       "demand": d["demand"], "priority": d["priority"],
                       "sla_days": d["sla_days"], "pos": d["schematic_pos"],
                       "color": d["color"], "city": d["city"], "region": d["region"]})

    edges = []
    # Source → DC arcs
    for i, s in enumerate(SOURCES):
        for j, t in enumerate(TRANSSHIPMENT_NODES):
            edges.append({
                "source": s["id"], "target": t["id"],
                "cost": SOURCE_DC_COST[i][j],
                "capacity": ARC_CAPACITIES_SOURCE_DC[i][j],
                "type": "source_dc"
            })
    # DC → Destination arcs
    for i, t in enumerate(TRANSSHIPMENT_NODES):
        for j, d in enumerate(DESTINATIONS):
            edges.append({
                "source": t["id"], "target": d["id"],
                "cost": DC_DEST_COST[i][j],
                "capacity": ARC_CAPACITIES_DC_DEST[i][j],
                "type": "dc_dest"
            })

    return {"nodes": nodes, "edges": edges}
