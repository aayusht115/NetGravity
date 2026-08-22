#!/usr/bin/env python
"""
Generate the representative client dataset the ingestion tests expect.

    python scripts/generate_mock_dataset.py            # -> data/mock/india
    python scripts/generate_mock_dataset.py --out DIR

WHY THIS SCRIPT EXISTS
──────────────────────
`data/mock/` is deliberately gitignored: it is fabricated demo data and the
team chose not to commit it. The consequence is that 21 ingestion tests — every
test that reads `data/mock/india` — fail on a fresh clone with
`FileNotFoundError`, on the source branch and here alike.

The fix is not to commit the data (that would reverse a deliberate decision)
and not to delete the tests. It is to make the corpus **reproducible**: this
script regenerates it deterministically, so the tests are runnable by anyone
and the repository still carries no client-shaped files.

WHAT IT GENERATES
─────────────────
A small Indian distribution network, fabricated but structurally realistic,
covering the must-have datasets:

    facilities.csv          plants + DCs, capacity, fixed cost, lat/long
    markets.csv             demand zones with SLA days and priority tier
    products.csv            SKU master with weight and unit value
    demand.csv              demand per market/product with variability
    lanes.csv               transport lanes, rate/unit, distance, lead time
    historical_volume.csv   observed volume history per node
    contracts/*.txt         carrier rate cards, for the contract reader
    signals/*.json          seeded news signals, for the guardrail policy

Every value is hard-coded rather than random: the ingestion tests assert on
`data_version`, a content hash of the assembled network, so the same inputs
must produce the same bytes on every machine and every run.

WHAT IT DOES NOT GENERATE, AND WHY
──────────────────────────────────
Running this takes the ingestion suite from 21 failures to 7. The remaining
seven need assets this script deliberately does not fabricate:

* **five** want real PDF binaries under `contracts/pdf_samples/`. Authoring a
  PDF whose text layer happens to satisfy a quality heuristic would be testing
  the fixture, not the reader.
* **one** asserts the original corpus's exact shape (10 facilities, 10 markets,
  1 product, 10 demands). Reshaping this network to match numbers whose intent
  is not recorded would be reverse-engineering a private dataset.
* **one** needs rows that fail column mapping and land in the staging zone.

They are failures of missing demo assets, not of the pipeline, and forcing them
green would say less than leaving them red and explained.

THIS IS NOT CLIENT DATA. Nothing here is derived from a real network.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import List, Sequence

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "mock" / "india"

# ---------------------------------------------------------------------------
# The network
#
# Three plants, five DCs, eight markets. Sized so the MILP solves in well under
# a second while still being large enough that REI's 1+N disruption sweep is a
# real measurement rather than a formality.
# ---------------------------------------------------------------------------

FACILITIES: List[Sequence] = [
    # facility_id, facility_name, role, status, lat, lon, city, state,
    # capacity_units_per_period, fixed_cost_per_year, is_mandatory, is_closable
    ("PLANT_PUNE",   "Pune Plant",        "PLANT", "EXISTING", 18.5204, 73.8567,
     "Pune",      "Maharashtra",   120000, 0,        "TRUE",  "FALSE"),
    ("PLANT_CHENNAI", "Chennai Plant",    "PLANT", "EXISTING", 13.0827, 80.2707,
     "Chennai",   "Tamil Nadu",    100000, 0,        "TRUE",  "FALSE"),
    ("PLANT_BADDI",  "Baddi Plant",       "PLANT", "EXISTING", 30.9578, 76.7914,
     "Baddi",     "Himachal",       80000, 0,        "TRUE",  "FALSE"),
    # DC capacities total 118,000 against 78,000 units of demand. Sized so the
    # network can absorb the loss of any ONE DC but not two — enough headroom
    # for REI to produce real numbers, tight enough that the constraint binds.
    # Deliberately not generous. With ample capacity the optimum closes three
    # DCs and serves everything from two, which makes the REI ranking
    # degenerate — every surviving node scores 1.0 — and exercises none of the
    # capacity constraints. As sized, the sweep yields five real REI values and
    # two INFEASIBLE nodes, which is what a network under real capacity
    # pressure looks like and the more useful test.
    ("DC_BHIWANDI",  "Bhiwandi DC",       "DC",    "EXISTING", 19.2967, 73.0631,
     "Bhiwandi",  "Maharashtra",    32000, 14400000, "FALSE", "TRUE"),
    ("DC_LUHARI",    "Luhari DC",         "DC",    "EXISTING", 28.4595, 76.9536,
     "Luhari",    "Haryana",        30000, 13200000, "FALSE", "TRUE"),
    ("DC_HOSUR",     "Hosur DC",          "DC",    "EXISTING", 12.7409, 77.8253,
     "Hosur",     "Tamil Nadu",     26000, 10800000, "FALSE", "TRUE"),
    ("DC_KOLKATA",   "Kolkata DC",        "DC",    "EXISTING", 22.5726, 88.3639,
     "Kolkata",   "West Bengal",    18000,  9600000, "FALSE", "TRUE"),
    ("DC_GUWAHATI",  "Guwahati DC",       "DC",    "EXISTING", 26.1445, 91.7362,
     "Guwahati",  "Assam",          12000,  6000000, "FALSE", "TRUE"),
]

MARKETS: List[Sequence] = [
    # market_id, market_name, lat, lon, region, state, priority_tier, sla_days
    ("MKT_MUMBAI",    "Mumbai Metro",   19.0760, 72.8777, "WEST",  "Maharashtra", 1, 2),
    ("MKT_DELHI",     "Delhi NCR",      28.7041, 77.1025, "NORTH", "Delhi",       1, 2),
    ("MKT_BANGALORE", "Bangalore",      12.9716, 77.5946, "SOUTH", "Karnataka",   1, 2),
    ("MKT_CHENNAI",   "Chennai",        13.0827, 80.2707, "SOUTH", "Tamil Nadu",  2, 3),
    ("MKT_KOLKATA",   "Kolkata",        22.5726, 88.3639, "EAST",  "West Bengal", 2, 3),
    ("MKT_HYDERABAD", "Hyderabad",      17.3850, 78.4867, "SOUTH", "Telangana",   2, 3),
    ("MKT_AHMEDABAD", "Ahmedabad",      23.0225, 72.5714, "WEST",  "Gujarat",     2, 3),
    ("MKT_GUWAHATI",  "Guwahati",       26.1445, 91.7362, "EAST",  "Assam",       3, 5),
]

PRODUCTS: List[Sequence] = [
    # product_id, product_name, weight_kg, unit_value, family
    ("SKU_AMBIENT", "Ambient Case", 12.5, 850.0, "AMBIENT"),
    ("SKU_CHILLED", "Chilled Case", 15.0, 1400.0, "COLD_CHAIN"),
]

#: market_id, product_id, quantity/period, std_dev
DEMAND: List[Sequence] = [
    ("MKT_MUMBAI",    "SKU_AMBIENT", 14000, 1200),
    ("MKT_MUMBAI",    "SKU_CHILLED",  4200,  520),
    ("MKT_DELHI",     "SKU_AMBIENT", 12500, 1100),
    ("MKT_DELHI",     "SKU_CHILLED",  3800,  470),
    ("MKT_BANGALORE", "SKU_AMBIENT",  9800,  880),
    ("MKT_BANGALORE", "SKU_CHILLED",  2900,  360),
    ("MKT_CHENNAI",   "SKU_AMBIENT",  7400,  660),
    ("MKT_CHENNAI",   "SKU_CHILLED",  2100,  260),
    ("MKT_KOLKATA",   "SKU_AMBIENT",  6200,  540),
    ("MKT_KOLKATA",   "SKU_CHILLED",  1700,  210),
    ("MKT_HYDERABAD", "SKU_AMBIENT",  5900,  520),
    ("MKT_AHMEDABAD", "SKU_AMBIENT",  5100,  450),
    ("MKT_GUWAHATI",  "SKU_AMBIENT",  2400,  300),
]

#: origin_id, destination_id, mode, rate_per_unit, distance_km, lead_time_days
#: Inbound plant→DC first, then DC→market. Every market has a primary and at
#: least one alternative, so a facility disruption has somewhere to reroute —
#: without that, REI would report INFEASIBLE for every node and measure nothing.
LANES: List[Sequence] = [
    # inbound
    ("PLANT_PUNE",    "DC_BHIWANDI",  "ROAD",  4.20,  140, 1),
    ("PLANT_PUNE",    "DC_LUHARI",    "ROAD", 18.40, 1420, 3),
    ("PLANT_PUNE",    "DC_HOSUR",     "ROAD", 11.80,  840, 2),
    ("PLANT_CHENNAI", "DC_HOSUR",     "ROAD",  4.80,  330, 1),
    ("PLANT_CHENNAI", "DC_KOLKATA",   "RAIL", 14.20, 1670, 4),
    ("PLANT_CHENNAI", "DC_BHIWANDI",  "ROAD", 16.10, 1330, 3),
    ("PLANT_BADDI",   "DC_LUHARI",    "ROAD",  5.10,  290, 1),
    ("PLANT_BADDI",   "DC_KOLKATA",   "RAIL", 17.60, 1890, 4),
    ("PLANT_BADDI",   "DC_GUWAHATI",  "RAIL", 21.30, 2170, 5),
    ("PLANT_PUNE",    "DC_GUWAHATI",  "RAIL", 24.90, 2620, 6),
    ("PLANT_CHENNAI", "DC_LUHARI",    "RAIL", 19.80, 2180, 5),
    ("PLANT_BADDI",   "DC_BHIWANDI",  "ROAD", 15.20, 1560, 3),
    ("PLANT_PUNE",    "DC_KOLKATA",   "RAIL", 16.80, 1960, 4),
    ("PLANT_CHENNAI", "DC_GUWAHATI",  "RAIL", 26.40, 2810, 6),
    ("PLANT_BADDI",   "DC_HOSUR",     "RAIL", 22.10, 2340, 5),
    # outbound — primary
    ("DC_BHIWANDI",   "MKT_MUMBAI",    "ROAD",  2.10,   45, 1),
    ("DC_BHIWANDI",   "MKT_AHMEDABAD", "ROAD",  6.40,  520, 1),
    ("DC_LUHARI",     "MKT_DELHI",     "ROAD",  2.40,   65, 1),
    ("DC_HOSUR",      "MKT_BANGALORE", "ROAD",  2.20,   40, 1),
    ("DC_HOSUR",      "MKT_CHENNAI",   "ROAD",  4.60,  330, 1),
    ("DC_HOSUR",      "MKT_HYDERABAD", "ROAD",  7.10,  570, 2),
    ("DC_KOLKATA",    "MKT_KOLKATA",   "ROAD",  2.00,   30, 1),
    ("DC_GUWAHATI",   "MKT_GUWAHATI",  "ROAD",  2.30,   25, 1),
    # outbound — alternates (more expensive, keep the network feasible)
    ("DC_LUHARI",     "MKT_AHMEDABAD", "ROAD",  9.80,  900, 2),
    ("DC_BHIWANDI",   "MKT_DELHI",     "ROAD", 13.50, 1420, 3),
    ("DC_BHIWANDI",   "MKT_HYDERABAD", "ROAD", 10.20,  710, 2),
    ("DC_BHIWANDI",   "MKT_BANGALORE", "ROAD", 11.40,  980, 2),
    ("DC_KOLKATA",    "MKT_GUWAHATI",  "ROAD",  8.90,  990, 2),
    ("DC_KOLKATA",    "MKT_DELHI",     "RAIL", 14.80, 1490, 3),
    ("DC_HOSUR",      "MKT_MUMBAI",    "ROAD", 12.60,  980, 2),
    ("DC_LUHARI",     "MKT_KOLKATA",   "RAIL", 15.20, 1490, 3),
    ("DC_GUWAHATI",   "MKT_KOLKATA",   "ROAD",  9.40,  990, 2),
    ("DC_HOSUR",      "MKT_AHMEDABAD", "ROAD", 15.90, 1490, 3),
    ("DC_BHIWANDI",   "MKT_CHENNAI",   "ROAD", 13.80, 1330, 3),
    ("DC_LUHARI",     "MKT_HYDERABAD", "RAIL", 16.40, 1560, 3),
]

#: node_id, period, volume, order_count
HISTORY: List[Sequence] = [
    (node, period, volume, orders)
    for node, base in (("DC_BHIWANDI", 18000), ("DC_LUHARI", 16000),
                       ("DC_HOSUR", 12500), ("DC_KOLKATA", 8000),
                       ("DC_GUWAHATI", 2400))
    for period, volume, orders in (
        ("2026-04", base, base // 40),
        ("2026-05", int(base * 1.04), int(base * 1.04) // 40),
        ("2026-06", int(base * 0.97), int(base * 0.97) // 40),
    )
]

CONTRACTS = {
    "transcorp_rate_card.txt": """TRANSCORP LOGISTICS PRIVATE LIMITED
Master Transportation Services Agreement — Rate Card

Contract Reference: TCL-2026-0417
Effective Date: 1 April 2026
Expiry Date: 31 March 2027
Carrier: Transcorp Logistics Pvt Ltd
Client: NetGravity Demo Client

1. SCOPE
This rate card governs full-truckload road movements between the Client's
manufacturing plants and distribution centres listed in Annexure A.

2. BASE RATES
   Pune Plant to Bhiwandi DC ............... INR 4.20 per unit
   Baddi Plant to Luhari DC ................ INR 5.10 per unit
   Chennai Plant to Hosur DC ............... INR 4.80 per unit

3. FUEL SURCHARGE
A fuel surcharge of 8.5% applies to all base rates, reviewed monthly against
the published diesel index.

4. MINIMUM COMMITMENT
The Client commits to a minimum of 2,000 trips per contract year. Shortfall
against this commitment attracts a penalty of INR 1,200 per undelivered trip.

5. SERVICE LEVEL
On-time delivery target: 95% measured monthly.
Credits of 2% of monthly billing apply for each full percentage point below
target, capped at 10% of monthly billing.

6. PAYMENT TERMS
Net 45 days from invoice date.
""",
    "speedfreight_rate_card.txt": """SPEEDFREIGHT INDIA LIMITED
Secondary Distribution Rate Schedule

Contract Reference: SFI-2026-1102
Effective Date: 1 April 2026
Expiry Date: 30 September 2027
Carrier: SpeedFreight India Ltd
Client: NetGravity Demo Client

1. SCOPE
Secondary distribution from distribution centres to market delivery points.

2. BASE RATES
   Bhiwandi DC to Mumbai Metro ............. INR 2.10 per unit
   Luhari DC to Delhi NCR .................. INR 2.40 per unit
   Hosur DC to Bangalore ................... INR 2.20 per unit
   Kolkata DC to Kolkata ................... INR 2.00 per unit
   Guwahati DC to Guwahati ................. INR 2.30 per unit

3. FUEL SURCHARGE
Fuel surcharge of 6.0% applies to all base rates.

4. VOLUME DISCOUNT
A discount of 3% applies to monthly billed volume above 25,000 units.

5. SERVICE LEVEL
Delivery within the agreed SLA days for 97% of consignments, measured monthly.

6. PAYMENT TERMS
Net 30 days from invoice date.
""",
}


#: External signals, seeded as JSON for `adapters/signals.py`.
#:
#: NOTE ON WHAT THESE ARE. These are the ingestion pipeline's *news/materiality*
#: signals — bucket, direction, magnitude, qualitative confidence. They are a
#: DIFFERENT concept from the orchestrator's `ExternalSignal`, which carries an
#: event probability and feeds RF. Nothing here has a probability, and nothing
#: converts one into the other: deriving P from "confidence: HIGH" would be
#: exactly the fabrication the risk core exists to prevent. See the Phase 4A
#: report, §6.
SIGNALS = [
    {
        "signal_id": "SIG-2026-001",
        "title": "Diesel price rises 8% after duty revision",
        "source_title": "National Business Daily",
        "source_url": "https://example.invalid/fuel-duty-revision",
        "published_date": "2026-07-14",
        "bucket": "MACRO",
        "direction": "UP",
        "magnitude": "+8% fuel cost",
        "affected_entities": ["DC_BHIWANDI", "DC_LUHARI"],
        "geography": "India",
        "confidence": "HIGH",
        "rationale": "Duty revision applies nationally to bulk diesel purchases.",
    },
    {
        "signal_id": "SIG-2026-002",
        "title": "Monsoon disruption warning for eastern corridor",
        "source_title": "Regional Weather Service",
        "source_url": "https://example.invalid/monsoon-east",
        "published_date": "2026-07-21",
        "bucket": "WEATHER",
        "direction": "DOWN",
        "magnitude": "reduced road availability",
        "affected_entities": ["DC_KOLKATA", "DC_GUWAHATI"],
        "geography": "East India",
        "confidence": "MEDIUM",
        "rationale": "Seasonal flooding historically closes NH-27 for short periods.",
    },
    {
        "signal_id": "SIG-2026-003",
        "title": "Transcorp adds capacity on the western corridor",
        "source_title": "Logistics Weekly",
        "source_url": "https://example.invalid/transcorp-capacity",
        "published_date": "2026-06-30",
        "bucket": "CARRIER",
        "direction": "UP",
        "magnitude": "+15% fleet",
        "affected_entities": ["DC_BHIWANDI"],
        "geography": "West India",
        "confidence": "MEDIUM",
        "rationale": "Incumbent carrier on the Pune-Bhiwandi lane.",
    },
    {
        "signal_id": "SIG-2026-004",
        "title": "Rival distributor opens Nagpur hub",
        "source_title": "Trade Press",
        "source_url": "https://example.invalid/rival-nagpur",
        "published_date": "2026-07-02",
        "bucket": "COMPETITOR",
        "direction": "NEUTRAL",
        "magnitude": "",
        "affected_entities": [],
        "geography": "Central India",
        "confidence": "LOW",
        "rationale": "Competitor activity — excluded by default policy.",
    },
]


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return len(rows)


def generate(out: Path) -> dict:
    """Write the corpus. Returns {filename: row count}."""
    written = {}
    written["facilities.csv"] = _write_csv(out / "facilities.csv", (
        "facility_id", "facility_name", "role", "status", "latitude", "longitude",
        "city", "state", "capacity_units_per_period", "fixed_cost_per_year",
        "is_mandatory", "is_closable",
    ), FACILITIES)

    written["markets.csv"] = _write_csv(out / "markets.csv", (
        "market_id", "market_name", "latitude", "longitude", "region", "state",
        "priority_tier", "sla_days",
    ), MARKETS)

    written["products.csv"] = _write_csv(out / "products.csv", (
        "product_id", "product_name", "weight_kg", "unit_value", "family",
    ), PRODUCTS)

    written["demand.csv"] = _write_csv(out / "demand.csv", (
        "market_id", "product_id", "quantity", "std_dev",
    ), DEMAND)

    written["lanes.csv"] = _write_csv(out / "lanes.csv", (
        "origin_id", "destination_id", "mode", "rate_per_unit", "distance_km",
        "lead_time_days",
    ), LANES)

    written["historical_volume.csv"] = _write_csv(out / "historical_volume.csv", (
        "node_id", "period", "volume", "order_count",
    ), HISTORY)

    contracts_dir = out / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    for name, body in CONTRACTS.items():
        (contracts_dir / name).write_text(body, encoding="utf-8")
        written[f"contracts/{name}"] = body.count("\n")

    signals_dir = out / "signals"
    signals_dir.mkdir(parents=True, exist_ok=True)
    (signals_dir / "seed_signals.json").write_text(
        json.dumps(SIGNALS, indent=2), encoding="utf-8")
    written["signals/seed_signals.json"] = len(SIGNALS)

    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    written = generate(args.out)
    print(f"wrote {len(written)} files to {args.out}")
    for name, count in written.items():
        print(f"  {name:<32} {count:>5} rows")
    print("\nThis is fabricated demo data. It is not client data and is gitignored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
