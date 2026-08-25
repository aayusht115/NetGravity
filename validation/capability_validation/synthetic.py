"""
One controlled synthetic network, and demand history for its markets.

Everything downstream in this validation run reads from here, so that
ingestion, optimisation, resilience, risk, forecasting and the Digital Twin
are all describing the SAME network rather than several convenient ones.

Design constraints the brief sets, and how they are met:

    3 plants                PLANT_PUNE, PLANT_CHENNAI, PLANT_DELHI
    5 distribution centres  DC_DELHI, DC_MUMBAI, DC_KOLKATA, DC_BANGALORE,
                            DC_HYDERABAD
    7 markets               MKT_{DELHI,MUMBAI,KOLKATA,CHENNAI,BANGALORE,
                                HYDERABAD,PUNE}
    2 products              PROD_STD, PROD_PREMIUM
    multiple lanes          plant→DC and DC→market, deliberately not a full
                            mesh, so sourcing has real choices to make
    capacity constraints    every plant and DC has a finite throughput
    resilience attributes   is_disruption_target, closure/opening costs,
                            contract status, lead times

**No entity outside this master data may be used in a deterministic test.**
`ENTITY_IDS` is exported so tests can assert that, and the out-of-scope signal
case in §6 deliberately names an id that is NOT in it.

── Feasibility is a design property here, not luck ───────────────────────────
Total DC capacity and total plant capacity both exceed peak total demand with
headroom, and every market is reachable from at least two DCs. That matters
because half the validation depends on a feasible baseline existing: REI is
defined against a feasible baseline cost, and a network that only just solves
would make the disruption cases infeasible for reasons that have nothing to do
with the code under test.

── The hidden horizon ────────────────────────────────────────────────────────
`build_demand_history` returns 36 observed periods and 6 held-out ones. Only
`train` is ever handed to the forecaster; `test` exists so accuracy can be
measured against periods the model was never shown. The split happens here,
once, so no downstream section can leak it by accident.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

#: Written out rather than inlined, so an editing pass cannot turn an escaped
#: newline in a generated CSV into a real one.
NEWLINE = chr(10)

from netgravity.schemas.network import (
    CanonicalNetwork,
    ContractStatus,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    ProductRecord,
    TransportMode,
)

# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------

PLANTS = ["PLANT_PUNE", "PLANT_CHENNAI", "PLANT_DELHI"]
DCS = ["DC_DELHI", "DC_MUMBAI", "DC_KOLKATA", "DC_BANGALORE", "DC_HYDERABAD"]
MARKETS = [
    "MKT_DELHI", "MKT_MUMBAI", "MKT_KOLKATA", "MKT_CHENNAI",
    "MKT_BANGALORE", "MKT_HYDERABAD", "MKT_PUNE",
]
PRODUCTS = ["PROD_STD", "PROD_PREMIUM"]

#: Every legitimate entity id. A deterministic test naming anything outside
#: this set is testing a fabrication, not the system.
ENTITY_IDS = frozenset(PLANTS + DCS + MARKETS)

#: Used by the out-of-scope routing case. Deliberately absent from ENTITY_IDS.
OUT_OF_SCOPE_ID = "MKT_SINGAPORE"

#: Demand pattern assigned to each market, per the brief.
DEMAND_PATTERNS: Dict[str, str] = {
    "MKT_DELHI":     "stable_seasonal",
    "MKT_MUMBAI":    "growth",
    "MKT_KOLKATA":   "seasonal",
    "MKT_CHENNAI":   "intermittent",
    "MKT_BANGALORE": "structural_break",
    "MKT_HYDERABAD": "decline",
    "MKT_PUNE":      "noisy",
}

TRAIN_PERIODS = 36
TEST_PERIODS = 6

_COORDS = {
    "PLANT_PUNE":     (18.52, 73.86),
    "PLANT_CHENNAI":  (13.08, 80.27),
    "PLANT_DELHI":    (28.70, 77.10),
    "DC_DELHI":       (28.61, 77.21),
    "DC_MUMBAI":      (19.08, 72.88),
    "DC_KOLKATA":     (22.57, 88.36),
    "DC_BANGALORE":   (12.97, 77.59),
    "DC_HYDERABAD":   (17.39, 78.49),
    "MKT_DELHI":      (28.65, 77.23),
    "MKT_MUMBAI":     (19.20, 72.98),
    "MKT_KOLKATA":    (22.60, 88.40),
    "MKT_CHENNAI":    (13.05, 80.25),
    "MKT_BANGALORE":  (12.93, 77.62),
    "MKT_HYDERABAD":  (17.42, 78.45),
    "MKT_PUNE":       (18.55, 73.90),
}

_REGION = {
    "PLANT_DELHI": "NORTH", "DC_DELHI": "NORTH", "MKT_DELHI": "NORTH",
    "DC_MUMBAI": "WEST", "MKT_MUMBAI": "WEST", "PLANT_PUNE": "WEST", "MKT_PUNE": "WEST",
    "DC_KOLKATA": "EAST", "MKT_KOLKATA": "EAST",
    "PLANT_CHENNAI": "SOUTH", "MKT_CHENNAI": "SOUTH",
    "DC_BANGALORE": "SOUTH", "MKT_BANGALORE": "SOUTH",
    "DC_HYDERABAD": "SOUTH", "MKT_HYDERABAD": "SOUTH",
}


# ---------------------------------------------------------------------------
# Demand history
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketDemand:
    """Observed history and the held-out future for one market."""
    market_id: str
    pattern: str
    train: np.ndarray          # TRAIN_PERIODS observations, the only input a forecast sees
    test: np.ndarray           # TEST_PERIODS held-out actuals
    description: str


def _series(pattern: str, rng: np.random.Generator, n: int) -> Tuple[np.ndarray, str]:
    t = np.arange(n, dtype=float)

    if pattern == "stable_seasonal":
        y = 900 + 120 * np.sin(2 * np.pi * t / 12) + rng.normal(0, 30, n)
        desc = "900 base, ±120 annual cycle, sd 30"
    elif pattern == "growth":
        y = 600 + 14.0 * t + rng.normal(0, 40, n)
        desc = "600 base, +14/period linear growth, sd 40"
    elif pattern == "seasonal":
        y = 750 + 220 * np.sin(2 * np.pi * t / 12 + np.pi / 3) + rng.normal(0, 35, n)
        desc = "750 base, ±220 annual cycle (phase-shifted), sd 35"
    elif pattern == "intermittent":
        occurred = rng.uniform(0, 1, n) < 0.35
        y = np.where(occurred, rng.gamma(4.0, 60.0, n), 0.0)
        desc = "P(demand)=0.35, size ~ Gamma(4, 60) — spare-parts shape"
    elif pattern == "structural_break":
        # Break at 3/4 through the OBSERVED history, so ~9 post-break periods
        # are visible at forecast time: enough for the change-point detector to
        # locate and for the regime comparison to measure.
        brk = int(TRAIN_PERIODS * 0.75)
        y = np.where(t < brk, 500.0, 1050.0) + rng.normal(0, 45, n)
        desc = f"500 -> 1050 step at period {brk + 1}, sd 45"
    elif pattern == "decline":
        y = 1100 - 16.0 * t + rng.normal(0, 40, n)
        desc = "1100 base, -16/period decline, sd 40"
    elif pattern == "noisy":
        y = 700 + rng.normal(0, 260, n)
        desc = "700 base, sd 260 (CV ~0.37)"
    else:
        raise ValueError(f"unknown pattern {pattern!r}")

    return np.clip(y, 0.0, None), desc


def build_demand_history(seed: int = 20260825) -> Dict[str, MarketDemand]:
    """
    Monthly demand for every market, split into observed and held-out.

    One generator seeded once, drawn per market in a fixed order, so the whole
    dataset is reproducible from this single integer.
    """
    rng = np.random.default_rng(seed)
    total = TRAIN_PERIODS + TEST_PERIODS
    out: Dict[str, MarketDemand] = {}

    for market in MARKETS:                       # fixed order — reproducibility
        pattern = DEMAND_PATTERNS[market]
        y, desc = _series(pattern, rng, total)
        out[market] = MarketDemand(
            market_id=market, pattern=pattern,
            train=y[:TRAIN_PERIODS], test=y[TRAIN_PERIODS:], description=desc,
        )
    return out


# ---------------------------------------------------------------------------
# Canonical network
# ---------------------------------------------------------------------------

def _distance_km(a: str, b: str) -> float:
    """Great-circle distance, rounded. Only used to make rates plausible."""
    (la1, lo1), (la2, lo2) = _COORDS[a], _COORDS[b]
    p1, p2 = np.radians(la1), np.radians(la2)
    dp, dl = p2 - p1, np.radians(lo2 - lo1)
    h = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return round(float(2 * 6371 * np.arcsin(np.sqrt(h))), 1)


#: Longest DC→market haul the network will carry at all. Generated from
#: distance rather than hand-listed, because a hand-written serving map is
#: where an accidentally single-sourced market hides.
_MAX_OUTBOUND_KM = 1_700.0

#: SLA per product, in days. Premium is tighter, but not so tight that a
#: market has only ONE service-feasible DC.
#:
#: This was measured, not guessed. Lead time here is `0.5 + km/500`, so a 2-day
#: premium SLA admits only DCs within 750 km — and at that setting MKT_DELHI
#: and MKT_KOLKATA each had exactly one eligible DC. Losing it left premium
#: demand structurally unservable, so EVERY DC disruption came back INFEASIBLE
#: and REI was never computable for any facility. That is a defect in the
#: dataset, not in the resilience engine: a network which cannot survive any
#: single loss cannot exercise the code path that measures cost exposure.
#:
#: At 3.0 days every market has at least two service-feasible DCs, so a
#: disruption degrades cost — which is what REI is defined on. The fragile
#: variant below deliberately keeps the tight setting so the infeasible path
#: is still covered.
_SLA_DAYS = {"PROD_STD": 5.0, "PROD_PREMIUM": 3.0}

def _dc_serves() -> Dict[str, List[str]]:
    """DC → markets it may serve, by distance ceiling."""
    return {
        dc: [m for m in MARKETS if _distance_km(dc, m) <= _MAX_OUTBOUND_KM]
        for dc in DCS
    }

#: Plant → DCs it may supply.
_PLANT_SERVES: Dict[str, List[str]] = {
    "PLANT_DELHI":   ["DC_DELHI", "DC_KOLKATA", "DC_MUMBAI"],
    "PLANT_PUNE":    ["DC_MUMBAI", "DC_HYDERABAD", "DC_DELHI"],
    "PLANT_CHENNAI": ["DC_BANGALORE", "DC_HYDERABAD", "DC_KOLKATA"],
}


def build_network(
    demand: Dict[str, MarketDemand],
    *,
    period: int = TRAIN_PERIODS,
    network_id: str = "synthetic_india_v1",
) -> CanonicalNetwork:
    """
    Assemble the canonical network for ONE planning period.

    Demand for the period is taken from the last OBSERVED month of each
    market's history — never from the held-out future, so the optimisation and
    resilience sections describe the same observed reality the forecaster was
    trained on.
    """
    facilities: List[FacilityRecord] = []

    for pid in PLANTS:
        facilities.append(FacilityRecord(
            id=pid, name=pid.replace("_", " ").title(), role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING,
            latitude=_COORDS[pid][0], longitude=_COORDS[pid][1],
            fixed_cost_per_year=9_000_000.0,
            handling_cost_per_unit=11.0,
            production_capacity_units_per_period=3_200.0,
            capacity_units_per_period=3_200.0,
            eligible_product_ids=list(PRODUCTS),
            is_closable=False, is_mandatory=True,
            is_disruption_target=True,
            contract_status=ContractStatus.ACTIVE,
            replenishment_lead_time_days=5.0,
            region=_REGION[pid], country="IN",
            tags=["plant", "synthetic"],
        ))

    dc_capacity = {
        "DC_DELHI": 2_600.0, "DC_MUMBAI": 2_400.0, "DC_KOLKATA": 1_500.0,
        "DC_BANGALORE": 1_800.0, "DC_HYDERABAD": 2_000.0,
    }
    for did in DCS:
        facilities.append(FacilityRecord(
            id=did, name=did.replace("_", " ").title(), role=NodeRole.DC,
            status=FacilityStatus.EXISTING,
            latitude=_COORDS[did][0], longitude=_COORDS[did][1],
            fixed_cost_per_year=2_400_000.0,
            handling_cost_per_unit=6.5,
            opening_cost=1_200_000.0,
            closure_cost=800_000.0,
            capacity_units_per_period=dc_capacity[did],
            eligible_product_ids=list(PRODUCTS),
            is_closable=True, is_mandatory=False,
            is_disruption_target=True,
            contract_status=ContractStatus.ACTIVE,
            contract_allows_early_closure=(did != "DC_DELHI"),
            replenishment_lead_time_days=3.0,
            region=_REGION[did], country="IN",
            tags=["dc", "synthetic"],
        ))

    for mid in MARKETS:
        facilities.append(FacilityRecord(
            id=mid, name=mid.replace("_", " ").title(), role=NodeRole.MARKET,
            status=FacilityStatus.EXISTING,
            latitude=_COORDS[mid][0], longitude=_COORDS[mid][1],
            is_closable=False, is_mandatory=True,
            is_disruption_target=False,
            region=_REGION[mid], country="IN",
            tags=["market", "synthetic"],
        ))

    products = [
        ProductRecord(id="PROD_STD", name="Standard Pack", unit="cases",
                      weight_kg=12.0, volume_m3=0.03, unit_value=850.0,
                      holding_rate=0.18),
        ProductRecord(id="PROD_PREMIUM", name="Premium Pack", unit="cases",
                      weight_kg=15.0, volume_m3=0.04, unit_value=2_100.0,
                      holding_rate=0.22),
    ]

    lanes: List[LaneRecord] = []
    for plant, dcs in _PLANT_SERVES.items():
        for dc in dcs:
            km = _distance_km(plant, dc)
            lanes.append(LaneRecord(
                origin_id=plant, destination_id=dc, mode=TransportMode.ROAD,
                rate_per_unit=round(2.4 + km * 0.0075, 3),
                distance_km=km,
                lead_time_days=round(1.0 + km / 500.0, 2),
                eligible_product_ids=list(PRODUCTS),
                is_active_baseline=True,
            ))
    for dc, mkts in _dc_serves().items():
        for mkt in mkts:
            km = _distance_km(dc, mkt)
            lanes.append(LaneRecord(
                origin_id=dc, destination_id=mkt, mode=TransportMode.ROAD,
                rate_per_unit=round(3.1 + km * 0.0095, 3),
                distance_km=km,
                lead_time_days=round(0.5 + km / 500.0, 2),
                eligible_product_ids=list(PRODUCTS),
                is_active_baseline=True,
            ))

    # Demand: the last OBSERVED period only. PROD_PREMIUM is a fixed share, so
    # the two products differ in value without changing the network's shape.
    demands: List[DemandRecord] = []
    for mid in MARKETS:
        observed = float(demand[mid].train[-1])
        std = float(np.std(demand[mid].train))
        for prod, share in (("PROD_STD", 0.72), ("PROD_PREMIUM", 0.28)):
            sla = _SLA_DAYS[prod]
            demands.append(DemandRecord(
                market_id=mid, product_id=prod, period=period,
                quantity=round(observed * share, 2),
                std_dev=round(std * share, 2),
                sla_days=sla,
                service_level=0.95,
                priority=1 if prod == "PROD_PREMIUM" else 2,
            ))

    net = CanonicalNetwork(
        facilities=facilities, products=products, demands=demands, lanes=lanes,
        network_id=network_id,
        description=(
            "Phase 8.0 controlled synthetic network — 3 plants, 5 DCs, "
            "7 markets, 2 products. Feasible by design with capacity headroom."
        ),
    )
    # Stamped here so every downstream result can be tied back to this exact
    # dataset; an unversioned network makes provenance untestable.
    return net.model_copy(update={"data_version": net.compute_data_version()})


# ---------------------------------------------------------------------------
# Tabular views, for the ingestion section
# ---------------------------------------------------------------------------

def write_tabular_views(network: CanonicalNetwork, out_dir: Path) -> Dict[str, Path]:
    """
    Two DIFFERENTLY structured tabular files describing the same facilities,
    plus the supporting tables.

    The two shapes exist because §4 asks for column mapping to be exercised:

      * `facilities_standard.csv` uses INGESTION's documented alias vocabulary
        (`netgravity/ingestion/field_aliases.py`) — `Facility_ID`, `Type`,
        `Capacity_Units`, and canonical `NodeRole` values.
      * `facilities_client_style.csv` uses the sort of headers a client actually
        sends — "Site Code", "Monthly Capacity (cases)", "Manufacturing Plant"
        — none of which is an alias, and whose role values are prose.

    Both describe the identical five DCs and three plants, so a mapping failure
    shows up as a difference between two files that should agree.

    ── A correction worth recording ───────────────────────────────────────────
    The first version of this file used the CANONICAL MODEL field names — `id`,
    `name`, `role` — on the assumption that "standard" meant the Pydantic
    schema. It does not. Ingestion's alias table maps `Facility_ID`,
    `facility_id`, `Node_ID` and `Site_ID` onto `facility_id`; plain `id` is not
    among them. Every row was therefore rejected with R-001 ("required field
    'facility_id' is missing or blank"), and the harness came within one
    conclusion of reporting that as an ingestion defect. It was a defect in the
    test data. The model's field names and the ingestion vocabulary are two
    different namespaces, and this file must speak the second one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: Dict[str, Path] = {}

    nodes = [f for f in network.facilities if f.role in (NodeRole.PLANT, NodeRole.DC)]

    # -- A: ingestion's own alias vocabulary ------------------------------
    std = out_dir / "facilities_standard.csv"
    lines = ["Facility_ID,Facility_Name,Type,Capacity_Units,Fixed_Annual_Cost,"
             "Variable_Handling_Cost_Per_Unit,Latitude,Longitude,Region"]
    for f in nodes:
        lines.append(
            f"{f.id},{f.name},{f.role.value},{f.capacity_units_per_period},"
            f"{f.fixed_cost_per_year},{f.handling_cost_per_unit},"
            f"{f.latitude},{f.longitude},{f.region}"
        )
    std.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["facilities_standard"] = std

    # -- B: headers and values a client would actually send ---------------
    client = out_dir / "facilities_client_style.csv"
    lines = ['"Site Code","Site Name","Facility Category",'
             '"Monthly Capacity (cases)","Annual Fixed Cost (INR)",'
             '"Handling Rate","Lat","Long","Zone"']
    for f in nodes:
        role = "Manufacturing Plant" if f.role is NodeRole.PLANT else "Distribution Centre"
        lines.append(
            f'"{f.id}","{f.name}","{role}",{f.capacity_units_per_period},'
            f'{f.fixed_cost_per_year},{f.handling_cost_per_unit},'
            f'{f.latitude},{f.longitude},"{f.region}"'
        )
    client.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["facilities_client_style"] = client

    # -- markets, in MARKET_ALIASES vocabulary ----------------------------
    # Needed for referential integrity, not decoration: without markets in the
    # bundle every DC->market lane and every demand row is rejected R-006
    # ("references unknown ID"). The facilities table carries plants and DCs
    # only, which is how the ingestion schema separates them.
    markets = out_dir / "markets.csv"
    lines = ["Market_ID,Market_Name,Latitude,Longitude,Region,SLA_Days"]
    for f in network.facilities:
        if f.role is not NodeRole.MARKET:
            continue
        sla = min((d.sla_days or 5.0) for d in network.demands
                  if d.market_id == f.id)
        lines.append(f"{f.id},{f.name},{f.latitude},{f.longitude},{f.region},{sla}")
    markets.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["markets"] = markets

    # -- products, in PRODUCT_ALIASES vocabulary --------------------------
    # Also referential, for the same reason: demand rows name a product, and a
    # product the bundle never declares is rejected R-006.
    products = out_dir / "products.csv"
    lines = ["Product_ID,Product_Name,Unit,Weight_KG,Unit_Volume,Unit_Value,Holding_Rate"]
    for pr in network.products:
        lines.append(f"{pr.id},{pr.name},{pr.unit},{pr.weight_kg},"
                     f"{pr.volume_m3},{pr.unit_value},{pr.holding_rate}")
    products.write_text(NEWLINE.join(lines) + NEWLINE, encoding="utf-8")
    written["products"] = products

    lanes = out_dir / "lanes.csv"
    lines = ["Origin_ID,Destination_ID,Mode,Unit_Cost,Distance_KM,Lead_Time_Days"]
    for ln in network.lanes:
        lines.append(f"{ln.origin_id},{ln.destination_id},{ln.mode.value},"
                     f"{ln.rate_per_unit},{ln.distance_km},{ln.lead_time_days}")
    lanes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["lanes"] = lanes

    dem = out_dir / "demand.csv"
    lines = ["Market_ID,Product_ID,Period,Demand_Units,Std_Dev,SLA_Days,Service_Level"]
    for d in network.demands:
        lines.append(f"{d.market_id},{d.product_id},{d.period},{d.quantity},"
                     f"{d.std_dev},{d.sla_days},{d.service_level}")
    dem.write_text("\n".join(lines) + "\n", encoding="utf-8")
    written["demand"] = dem

    # -- D: deliberate defects, in the alias vocabulary so the rules run ---
    bad = out_dir / "facilities_with_errors.csv"
    bad.write_text(
        "Facility_ID,Facility_Name,Type,Capacity_Units,Fixed_Annual_Cost\n"
        "DC_DELHI,Dc Delhi,DC,2600.0,2400000.0\n"
        "DC_BROKEN,Dc Broken,DC,-500.0,2400000.0\n"          # negative capacity
        "DC_NOCAP,Dc Nocap,DC,,2400000.0\n"                   # missing capacity
        "DC_BADROLE,Dc Badrole,TELEPORTER,900.0,2400000.0\n"   # unknown role
        ",Nameless,DC,900.0,2400000.0\n",                      # missing id
        encoding="utf-8",
    )
    written["facilities_with_errors"] = bad
    return written


def write_dataset_manifest(
    network: CanonicalNetwork,
    demand: Dict[str, MarketDemand],
    out_dir: Path,
) -> Path:
    """Record the dataset so any number in the report can be traced back."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "network_id": network.network_id,
        "data_version": network.compute_data_version(),
        "counts": {
            "plants": len(PLANTS), "dcs": len(DCS), "markets": len(MARKETS),
            "products": len(PRODUCTS), "lanes": len(network.lanes),
            "demand_records": len(network.demands),
            "facilities_total": len(network.facilities),
        },
        "entity_ids": sorted(ENTITY_IDS),
        "out_of_scope_id_used_in_routing_test": OUT_OF_SCOPE_ID,
        "capacity": {
            "total_plant_capacity": sum(
                f.production_capacity_units_per_period
                for f in network.facilities if f.role is NodeRole.PLANT),
            "total_dc_capacity": sum(
                f.capacity_units_per_period
                for f in network.facilities if f.role is NodeRole.DC),
            "total_period_demand": sum(d.quantity for d in network.demands),
        },
        "demand_history": {
            m: {
                "pattern": d.pattern,
                "description": d.description,
                "train_periods": len(d.train),
                "test_periods_hidden": len(d.test),
                "train": [round(float(v), 2) for v in d.train],
                "test_HELD_OUT": [round(float(v), 2) for v in d.test],
            }
            for m, d in demand.items()
        },
    }
    path = out_dir / "dataset_manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def build_fragile_network(
    demand: Dict[str, MarketDemand],
    *,
    period: int = TRAIN_PERIODS,
) -> CanonicalNetwork:
    """
    The same network with a 2-day premium SLA.

    Exists so §10 can exercise the INFEASIBLE branch honestly. At this setting
    MKT_DELHI and MKT_KOLKATA have exactly one service-feasible DC each, so
    losing it leaves premium demand structurally unservable — which is what a
    genuinely fragile network looks like, and what REI must report as
    unavailable rather than as a REI of zero.

    Only the SLA is tightened. An earlier version also halved DC capacity,
    which made the UNDISRUPTED baseline infeasible — and REI needs a feasible
    baseline to measure against, so the assessment refused to start and the
    infeasible-disruption branch was never reached. The point is a network that
    works until something breaks, not one that never worked.
    """
    base = build_network(demand, period=period, network_id="synthetic_india_fragile")
    demands = [
        d.model_copy(update={"sla_days": 2.0}) if d.product_id == "PROD_PREMIUM" else d
        for d in base.demands
    ]
    fragile = base.model_copy(update={"demands": demands, "data_version": None})
    return fragile.model_copy(update={"data_version": fragile.compute_data_version()})


__all__ = [
    "DCS", "DEMAND_PATTERNS", "ENTITY_IDS", "MARKETS", "OUT_OF_SCOPE_ID",
    "PLANTS", "PRODUCTS", "TEST_PERIODS", "TRAIN_PERIODS",
    "MarketDemand", "build_demand_history", "build_fragile_network",
    "build_network", "write_dataset_manifest", "write_tabular_views",
]
