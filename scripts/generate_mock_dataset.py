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

Running this script makes all 21 pass.

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
    shipment_log.csv        transactional despatch records -> staging zone
    contracts/*.txt         carrier rate cards, for the contract reader
    contracts/pdf_samples/  the same rate cards as real, readable PDFs
    signals/*.json          seeded market-intelligence signals, for guardrails

Every value is hard-coded rather than random: the ingestion tests assert on
`data_version`, a content hash of the assembled network, so the same inputs
must produce the same bytes on every machine and every run.

ON THE PDFs
───────────
`write_text_pdf` builds a genuine PDF with a real text layer, from the same
contract text as the `.txt` fixtures. That matters: the tests read it with
pypdf and then run the extracted text through `pdf_quality`, which scores the
proportion of tokens that look like real words. Real contract prose passes that
check because it IS real prose — the heuristic is doing its job, not being
worked around. A stub file shaped to satisfy the threshold would have tested
the fixture instead of the reader.

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
# Shape is fixed by the ingestion suite's own contract
# (test_pipeline_end_to_end.test_india_network_has_the_expected_shape):
#
#     10 facilities = 4 plants + 5 existing DCs + 1 CANDIDATE DC
#     10 markets, 1 product, 10 demand rows, more than 30 lanes
#
# Those numbers are the specification, not a preference. The candidate DC in
# particular is load-bearing: a network with no candidate site exercises none
# of the open/close decision logic, so the optimum is a routing problem rather
# than a network-design one.
# ---------------------------------------------------------------------------

FACILITIES: List[Sequence] = [
    # facility_id, facility_name, role, status, lat, lon, city, state,
    # capacity_units_per_period, fixed_cost_per_year, is_mandatory, is_closable
    ("PLANT_PUNE",    "Pune Plant",     "PLANT", "EXISTING", 18.5204, 73.8567,
     "Pune",     "Maharashtra",   120000, 0, "TRUE",  "FALSE"),
    ("PLANT_CHENNAI", "Chennai Plant",  "PLANT", "EXISTING", 13.0827, 80.2707,
     "Chennai",  "Tamil Nadu",    100000, 0, "TRUE",  "FALSE"),
    ("PLANT_BADDI",   "Baddi Plant",    "PLANT", "EXISTING", 30.9578, 76.7914,
     "Baddi",    "Himachal",       80000, 0, "TRUE",  "FALSE"),
    ("PLANT_HALDIA",  "Haldia Plant",   "PLANT", "EXISTING", 22.0667, 88.1000,
     "Haldia",   "West Bengal",    70000, 0, "TRUE",  "FALSE"),
    # DC capacity totals 118,000 against 78,000 units of demand. Deliberately
    # not generous: with ample capacity the optimum closes three DCs and serves
    # everything from two, which makes the REI ranking degenerate — every
    # surviving node scores 1.0 — and exercises none of the capacity
    # constraints. As sized, the network can absorb the loss of any single
    # node, so REI yields a real spread of values rather than a wall of
    # INFEASIBLE. (The unservable-disruption path is exercised separately, on a
    # deliberately starved copy, in test_extraction_agent.py.)
    ("DC_BHIWANDI",   "Bhiwandi DC",    "DC", "EXISTING",  19.2967, 73.0631,
     "Bhiwandi", "Maharashtra",    32000, 14400000, "FALSE", "TRUE"),
    ("DC_LUHARI",     "Luhari DC",      "DC", "EXISTING",  28.4595, 76.9536,
     "Luhari",   "Haryana",        30000, 13200000, "FALSE", "TRUE"),
    ("DC_HOSUR",      "Hosur DC",       "DC", "EXISTING",  12.7409, 77.8253,
     "Hosur",    "Tamil Nadu",     26000, 10800000, "FALSE", "TRUE"),
    ("DC_KOLKATA",    "Kolkata DC",     "DC", "EXISTING",  22.5726, 88.3639,
     "Kolkata",  "West Bengal",    18000,  9600000, "FALSE", "TRUE"),
    ("DC_GUWAHATI",   "Guwahati DC",    "DC", "EXISTING",  26.1445, 91.7362,
     "Guwahati", "Assam",          12000,  6000000, "FALSE", "TRUE"),
    # The candidate: not open in the baseline, available to the optimizer.
    ("DC_NAGPUR",     "Nagpur DC",      "DC", "CANDIDATE", 21.1458, 79.0882,
     "Nagpur",   "Maharashtra",    20000, 10200000, "FALSE", "TRUE"),
]

MARKETS: List[Sequence] = [
    # market_id, market_name, lat, lon, region, state, priority_tier, sla_days
    ("MKT_MUMBAI",    "Mumbai Metro", 19.0760, 72.8777, "WEST",    "Maharashtra",   1, 2),
    ("MKT_DELHI",     "Delhi NCR",    28.7041, 77.1025, "NORTH",   "Delhi",         1, 2),
    ("MKT_BANGALORE", "Bangalore",    12.9716, 77.5946, "SOUTH",   "Karnataka",     1, 2),
    ("MKT_CHENNAI",   "Chennai",      13.0827, 80.2707, "SOUTH",   "Tamil Nadu",    2, 3),
    ("MKT_KOLKATA",   "Kolkata",      22.5726, 88.3639, "EAST",    "West Bengal",   2, 3),
    ("MKT_HYDERABAD", "Hyderabad",    17.3850, 78.4867, "SOUTH",   "Telangana",     2, 3),
    ("MKT_AHMEDABAD", "Ahmedabad",    23.0225, 72.5714, "WEST",    "Gujarat",       2, 3),
    ("MKT_PUNE",      "Pune City",    18.5204, 73.8567, "WEST",    "Maharashtra",   2, 3),
    ("MKT_LUCKNOW",   "Lucknow",      26.8467, 80.9462, "NORTH",   "Uttar Pradesh", 3, 4),
    ("MKT_GUWAHATI",  "Guwahati",     26.1445, 91.7362, "EAST",    "Assam",         3, 5),
]

#: One SKU. The suite pins this at 1, and a single product keeps the demand
#: table one row per market, which is what makes the 10/10 shape legible.
PRODUCTS: List[Sequence] = [
    ("SKU_AMBIENT", "Ambient Case", 12.5, 850.0, "AMBIENT"),
]

#: market_id, product_id, quantity/period, std_dev — 78,000 units in total.
DEMAND: List[Sequence] = [
    ("MKT_MUMBAI",    "SKU_AMBIENT", 14000, 1200),
    ("MKT_DELHI",     "SKU_AMBIENT", 12500, 1100),
    ("MKT_BANGALORE", "SKU_AMBIENT",  9800,  880),
    ("MKT_CHENNAI",   "SKU_AMBIENT",  7400,  660),
    ("MKT_KOLKATA",   "SKU_AMBIENT",  6200,  540),
    ("MKT_HYDERABAD", "SKU_AMBIENT",  5900,  520),
    ("MKT_AHMEDABAD", "SKU_AMBIENT",  5100,  450),
    ("MKT_PUNE",      "SKU_AMBIENT",  8600,  760),
    ("MKT_LUCKNOW",   "SKU_AMBIENT",  6100,  530),
    ("MKT_GUWAHATI",  "SKU_AMBIENT",  2400,  300),
]

#: origin_id, destination_id, mode, rate_per_unit, distance_km, lead_time_days
#: Inbound plant→DC first, then DC→market. Every market has a primary and at
#: least one alternative, so a facility disruption has somewhere to reroute —
#: without that, REI would report INFEASIBLE for every node and measure nothing.
LANES: List[Sequence] = [
    # --- inbound: plant -> DC ---
    ("PLANT_PUNE",    "DC_BHIWANDI",  "ROAD",  4.20,  140, 1),
    ("PLANT_PUNE",    "DC_LUHARI",    "ROAD", 18.40, 1420, 3),
    ("PLANT_PUNE",    "DC_HOSUR",     "ROAD", 11.80,  840, 2),
    ("PLANT_PUNE",    "DC_NAGPUR",    "ROAD",  7.30,  710, 2),
    ("PLANT_PUNE",    "DC_KOLKATA",   "RAIL", 16.80, 1960, 4),
    ("PLANT_PUNE",    "DC_GUWAHATI",  "RAIL", 24.90, 2620, 6),
    ("PLANT_CHENNAI", "DC_HOSUR",     "ROAD",  4.80,  330, 1),
    ("PLANT_CHENNAI", "DC_BHIWANDI",  "ROAD", 16.10, 1330, 3),
    ("PLANT_CHENNAI", "DC_KOLKATA",   "RAIL", 14.20, 1670, 4),
    ("PLANT_CHENNAI", "DC_LUHARI",    "RAIL", 19.80, 2180, 5),
    ("PLANT_CHENNAI", "DC_NAGPUR",    "ROAD", 11.20, 1090, 3),
    ("PLANT_BADDI",   "DC_LUHARI",    "ROAD",  5.10,  290, 1),
    ("PLANT_BADDI",   "DC_BHIWANDI",  "ROAD", 15.20, 1560, 3),
    ("PLANT_BADDI",   "DC_KOLKATA",   "RAIL", 17.60, 1890, 4),
    ("PLANT_BADDI",   "DC_GUWAHATI",  "RAIL", 21.30, 2170, 5),
    ("PLANT_BADDI",   "DC_NAGPUR",    "ROAD", 13.40, 1310, 3),
    ("PLANT_HALDIA",  "DC_KOLKATA",   "ROAD",  3.90,  125, 1),
    ("PLANT_HALDIA",  "DC_GUWAHATI",  "RAIL", 11.60,  980, 3),
    ("PLANT_HALDIA",  "DC_NAGPUR",    "RAIL", 14.90, 1180, 3),
    ("PLANT_HALDIA",  "DC_LUHARI",    "RAIL", 18.20, 1620, 4),
    # --- outbound: DC -> market, primary ---
    ("DC_BHIWANDI",   "MKT_MUMBAI",    "ROAD",  2.10,   45, 1),
    ("DC_BHIWANDI",   "MKT_PUNE",      "ROAD",  3.40,  150, 1),
    ("DC_BHIWANDI",   "MKT_AHMEDABAD", "ROAD",  6.40,  520, 1),
    ("DC_LUHARI",     "MKT_DELHI",     "ROAD",  2.40,   65, 1),
    ("DC_LUHARI",     "MKT_LUCKNOW",   "ROAD",  6.10,  510, 2),
    ("DC_HOSUR",      "MKT_BANGALORE", "ROAD",  2.20,   40, 1),
    ("DC_HOSUR",      "MKT_CHENNAI",   "ROAD",  4.60,  330, 1),
    ("DC_HOSUR",      "MKT_HYDERABAD", "ROAD",  7.10,  570, 2),
    ("DC_KOLKATA",    "MKT_KOLKATA",   "ROAD",  2.00,   30, 1),
    ("DC_GUWAHATI",   "MKT_GUWAHATI",  "ROAD",  2.30,   25, 1),
    ("DC_NAGPUR",     "MKT_HYDERABAD", "ROAD",  5.80,  500, 2),
    ("DC_NAGPUR",     "MKT_PUNE",      "ROAD",  7.20,  710, 2),
    # --- outbound: alternates, dearer, keep the network feasible ---
    ("DC_LUHARI",     "MKT_AHMEDABAD", "ROAD",  9.80,  900, 2),
    ("DC_BHIWANDI",   "MKT_DELHI",     "ROAD", 13.50, 1420, 3),
    ("DC_BHIWANDI",   "MKT_HYDERABAD", "ROAD", 10.20,  710, 2),
    ("DC_BHIWANDI",   "MKT_BANGALORE", "ROAD", 11.40,  980, 2),
    ("DC_BHIWANDI",   "MKT_CHENNAI",   "ROAD", 13.80, 1330, 3),
    ("DC_KOLKATA",    "MKT_GUWAHATI",  "ROAD",  8.90,  990, 2),
    ("DC_KOLKATA",    "MKT_LUCKNOW",   "RAIL", 11.70, 1000, 3),
    ("DC_KOLKATA",    "MKT_DELHI",     "RAIL", 14.80, 1490, 3),
    ("DC_HOSUR",      "MKT_MUMBAI",    "ROAD", 12.60,  980, 2),
    ("DC_HOSUR",      "MKT_PUNE",      "ROAD", 11.10,  840, 2),
    ("DC_LUHARI",     "MKT_KOLKATA",   "RAIL", 15.20, 1490, 3),
    ("DC_GUWAHATI",   "MKT_KOLKATA",   "ROAD",  9.40,  990, 2),
    ("DC_NAGPUR",     "MKT_MUMBAI",    "ROAD",  8.60,  830, 2),
    ("DC_NAGPUR",     "MKT_KOLKATA",   "RAIL", 12.30, 1180, 3),
    ("DC_NAGPUR",     "MKT_DELHI",     "ROAD", 11.90, 1070, 3),
    ("DC_NAGPUR",     "MKT_AHMEDABAD", "ROAD", 10.40,  880, 2),
    ("DC_NAGPUR",     "MKT_BANGALORE", "ROAD", 11.80, 1090, 3),
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

#: Transactional despatch records — one row per movement.
#:
#: Exercises the STAGING path. Two groups of columns, both needed:
#:
#:   lr_number / vehicle_number / despatch_date / invoice_number
#:       the classifier's shipment markers — what makes this SHIPMENT_LOG
#:       rather than a volume table, and therefore staging rather than network
#:   market_id / product_id / period / quantity / weight_kg
#:       fields the mapper can actually resolve, so the rows survive mapping
#:
#: A real despatch log carries both, which is the point: the transactional
#: identifiers say what kind of file it is, and the entity columns are what
#: makes it useful. With markers alone every row maps to nothing and is
#: rejected — correctly, since nothing in it could be read.
SHIPMENT_LOG: List[Sequence] = [
    ("LR-2026-00" + f"{i:03d}",
     f"2026-06-{(i % 28) + 1:02d}",
     "2026-06",
     origin, destination, "SKU_AMBIENT",
     f"MH12AB{1000 + i}",
     f"INV-2026-{5000 + i}",
     units, round(units * 12.5, 1))
    for i, (origin, destination, units) in enumerate((
        ("DC_BHIWANDI", "MKT_MUMBAI",    480),
        ("DC_BHIWANDI", "MKT_PUNE",      310),
        ("DC_LUHARI",   "MKT_DELHI",     420),
        ("DC_LUHARI",   "MKT_LUCKNOW",   205),
        ("DC_HOSUR",    "MKT_BANGALORE", 330),
        ("DC_HOSUR",    "MKT_CHENNAI",   250),
        ("DC_KOLKATA",  "MKT_KOLKATA",   210),
        ("DC_GUWAHATI", "MKT_GUWAHATI",   80),
        ("DC_BHIWANDI", "MKT_AHMEDABAD", 170),
        ("DC_HOSUR",    "MKT_HYDERABAD", 195),
        ("DC_LUHARI",   "MKT_DELHI",     390),
        ("DC_BHIWANDI", "MKT_MUMBAI",    455),
    ), start=1)
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
The base freight rate for all primary movements under this agreement is
Rs. 10.00 per kg of chargeable weight, applied to the greater of actual and
volumetric weight.

   Pune Plant to Bhiwandi DC ............... INR 4.20 per unit
   Baddi Plant to Luhari DC ................ INR 5.10 per unit
   Chennai Plant to Hosur DC ............... INR 4.80 per unit

3. SURCHARGES
A fuel surcharge of 8.5% applies to all base rates, reviewed monthly against
the published diesel index.

A national special location (NSL) surcharge of Rs. 5.00 per kg applies to
consignments delivered to the pin codes listed in Annexure B, covering the
north-eastern states and Jammu and Kashmir.

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


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def write_text_pdf(path: Path, body: str, *, lines_per_page: int = 52) -> int:
    """
    Write a genuine, minimal PDF carrying `body` as a real text layer.

    WHY HAND-ROLLED
    ───────────────
    The tests that need this read the PDF with pypdf and assert on clause text,
    then run the extracted text through `pdf_quality`. Both are real checks, so
    the fixture has to be a real PDF — not a stub, and not a file that merely
    satisfies the heuristic. `pypdf` can copy and combine pages but cannot
    author text, and pulling in reportlab for one fixture is a heavier
    dependency than the ~40 lines below.

    PDF is a text-based container: a catalogue, a page tree, one Helvetica
    font, and a content stream per page. The only fiddly part is the xref
    table, which must carry the exact byte offset of every object — hence
    building the file as a byte list and measuring as we go.

    Returns the page count.
    """
    lines = body.splitlines() or [""]
    pages = [lines[i:i + lines_per_page]
             for i in range(0, len(lines), lines_per_page)]

    objects: List[bytes] = []          # 1-indexed on write
    page_count = len(pages)
    # Object numbering: 1 catalogue, 2 page tree, 3 font,
    # then per page: 4+2i content stream, 5+2i page.
    page_obj_ids = [5 + 2 * i for i in range(page_count)]

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_obj_ids)
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("latin-1"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for page_lines in pages:
        drawn = "\n".join(f"({_pdf_escape(line)}) Tj T*" for line in page_lines)
        stream = (f"BT\n/F1 9 Tf\n1 0 0 1 56 780 Tm\n12 TL\n{drawn}\nET\n")
        encoded = stream.encode("latin-1", "replace")
        objects.append(
            b"<< /Length " + str(len(encoded)).encode() + b" >>\nstream\n"
            + encoded + b"\nendstream")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + str(len(objects)).encode() + b" 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, payload in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + payload + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF\n").encode()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return page_count


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

    # Transactional records. Column names are chosen to match the classifier's
    # shipment markers (LR number, vehicle number, despatch date, invoice
    # number) so this file is routed to STAGING rather than the network — it is
    # evidence about what happened, not master data about what exists.
    written["shipment_log.csv"] = _write_csv(out / "shipment_log.csv", (
        "lr_number", "despatch_date", "period", "origin_id", "market_id",
        "product_id", "vehicle_number", "invoice_number", "quantity",
        "weight_kg",
    ), SHIPMENT_LOG)

    contracts_dir = out / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    for name, body in CONTRACTS.items():
        (contracts_dir / name).write_text(body, encoding="utf-8")
        written[f"contracts/{name}"] = body.count("\n")

    # The same contracts as real PDFs. Built from the identical text, so the
    # PDF and text fixtures cannot drift apart and disagree about a rate.
    for name, body in CONTRACTS.items():
        pdf_name = name.replace(".txt", ".pdf")
        pages = write_text_pdf(contracts_dir / "pdf_samples" / pdf_name, body)
        written[f"contracts/pdf_samples/{pdf_name}"] = pages

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
